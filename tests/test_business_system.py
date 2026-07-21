from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from lunardate import LunarDate


TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(TEMP_DIR.name) / "test.db")
os.environ["SESSION_SECRET"] = "test-secret"

from fastapi.testclient import TestClient

from business_app.algorithm_client import algorithm_client
from business_app.calendar_rules import available_minutes_for_profile, calendar_day_shifts
from business_app.constants import ADJUSTMENT_CODE_PAST_TIME
from business_app.database import db, initialize_database, now_text
from business_app.main import app
from business_app.services import (
    _detect_schedule_conflicts,
    _restore_batch_metadata_from_history,
    _sync_batch_metadata,
    build_effective_schedule,
    build_machine_load_context,
    compare_version_core_metrics,
    compare_versions,
    ga_parameters_for_process_count,
    is_manually_locked,
    next_working_day_shift_start,
    select_calendar,
    validate_snapshot,
)


class BusinessSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.original_constraint_validator = algorithm_client.validate_process_constraints

        def fake_constraint_validator(payload):
            process = next(
                (
                    item
                    for order in payload["data_snapshot"].get("order_processes", [])
                    for item in order.get("processes", [])
                    if str(item.get("process_id")) == str(payload.get("process_id"))
                ),
                {},
            )
            required_minutes = float(
                payload.get("duration_minutes")
                or process.get("unit_duration_minutes")
                or 0
            )
            issues = []
            start_value = payload.get("plan_start_time")
            end_value = payload.get("plan_end_time")
            if start_value and end_value:
                start = datetime.fromisoformat(str(start_value))
                end = datetime.fromisoformat(str(end_value))
                if end <= start or (end - start).total_seconds() / 60 + 1e-6 < required_minutes:
                    issues.append(
                        {
                            "code": "PROCESS_DURATION_SHORTAGE",
                            "title": "工时不足",
                            "message": "目标时段不足",
                            "severity": "hard",
                        }
                    )
            suggested_window = None
            if payload.get("find_window_from") and not issues:
                suggested_start = datetime.fromisoformat(str(payload["find_window_from"]))
                suggested_end = suggested_start + timedelta(minutes=required_minutes)
                suggested_window = {
                    "plan_start_time": suggested_start.isoformat(timespec="seconds"),
                    "plan_end_time": suggested_end.isoformat(timespec="seconds"),
                }
            return {
                "valid": not issues,
                "issues": issues,
                "required_minutes": required_minutes,
                "requires_continuous": payload.get("schedule_type") == "heat_treatment",
                "suggested_window": suggested_window,
            }

        algorithm_client.validate_process_constraints = fake_constraint_validator
        response = cls.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert response.status_code == 200, response.text
        cls.headers = {"Authorization": f"Bearer {response.json()['token']}"}

    @classmethod
    def tearDownClass(cls) -> None:
        algorithm_client.validate_process_constraints = cls.original_constraint_validator
        cls.client_context.__exit__(None, None, None)
        TEMP_DIR.cleanup()

    def test_health_dashboard_and_seed_data(self) -> None:
        self.assertEqual(self.client.get("/health").json()["status"], "UP")
        dashboard = self.client.get("/api/dashboard", headers=self.headers)
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.json()["master_counts"]["order"], 3)
        snapshot = self.client.get("/api/master-data/snapshot", headers=self.headers).json()
        self.assertEqual(len(snapshot["machine_profiles"]), 4)
        self.assertEqual(
            {item["schedule_type"] for item in snapshot["machine_calendars"]},
            {"machining", "heat_treatment", "assembly"},
        )
        calendar_id = snapshot["machine_calendar"]["calendar_id"]
        self.assertEqual(
            self.client.delete(f"/api/master-data/calendar/{calendar_id}", headers=self.headers).status_code,
            409,
        )
        validation = self.client.get("/api/master-data/validate", headers=self.headers).json()
        self.assertTrue(validation["valid"], validation["errors"])

    def test_restart_initialization_preserves_existing_business_data(self) -> None:
        """服务重启只能补建表和恢复任务状态，不能清除已有业务数据。"""
        entity_id = "RESTART_PRESERVE_SENTINEL"
        stamp = now_text()
        with db() as connection:
            connection.execute(
                """INSERT INTO master_records(
                       entity_type,entity_id,payload_json,revision,updated_by,updated_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    "machine",
                    entity_id,
                    json.dumps(
                        {
                            "machine_id": entity_id,
                            "machine_name": "重启数据保留测试设备",
                            "machine_type": "TEST",
                            "workshop_type": "MACHINING",
                            "status": "ACTIVE",
                        },
                        ensure_ascii=False,
                    ),
                    1,
                    "test",
                    stamp,
                ),
            )
        try:
            initialize_database()
            with db() as connection:
                preserved = connection.execute(
                    "SELECT COUNT(*) AS count FROM master_records WHERE entity_type='machine' AND entity_id=?",
                    (entity_id,),
                ).fetchone()["count"]
            self.assertEqual(preserved, 1)
        finally:
            with db() as connection:
                connection.execute(
                    "DELETE FROM master_records WHERE entity_type='machine' AND entity_id=?",
                    (entity_id,),
                )

    def test_task_defaults_use_next_working_day_day_shift(self) -> None:
        response = self.client.get("/api/tasks/defaults?schedule_type=machining", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        schedule_start = datetime.fromisoformat(response.json()["schedule_start"])
        self.assertGreater(schedule_start.date(), datetime.now().date())
        self.assertLess(schedule_start.weekday(), 5)
        self.assertEqual((schedule_start.hour, schedule_start.minute), (8, 0))

    def test_next_working_day_uses_named_day_shift(self) -> None:
        calendar = {
            "weekly_shifts": {
                "0": [],
                "1": [
                    {"name": "夜班", "segments": [{"start": "00:00", "end": "06:00"}]},
                    {"name": "白班", "segments": [{"start": "08:30", "end": "17:00"}]},
                ],
                "2": [],
                "3": [],
                "4": [],
                "5": [],
                "6": [],
            },
            "special_shifts": {},
        }
        result = next_working_day_shift_start(calendar, datetime(2026, 7, 19, 10, 0))
        self.assertEqual(result, datetime(2026, 7, 20, 8, 30))

    def test_next_working_day_skips_special_rule_holidays(self) -> None:
        shifts = [{"name": "白班", "segments": [{"start": "08:00", "end": "17:00"}]}]
        calendar = {
            "day_shift_start": "08:00",
            "weekly_shifts": {str(day): shifts for day in range(7)},
            "special_shifts": {},
            "special_rules": [
                {
                    "rule_type": "fixed_date_range",
                    "month": 5,
                    "day_start": 1,
                    "day_end": 5,
                    "shifts": [],
                    "priority": 90,
                }
            ],
        }

        result = next_working_day_shift_start(calendar, datetime(2026, 4, 30, 10, 0))
        self.assertEqual(result, datetime(2026, 5, 6, 8, 0))

    def test_special_shift_overrides_holiday_rule_for_default_start(self) -> None:
        shifts = [{"name": "白班", "segments": [{"start": "08:00", "end": "17:00"}]}]
        calendar = {
            "day_shift_start": "08:00",
            "weekly_shifts": {str(day): shifts for day in range(7)},
            "special_shifts": {
                "2026-05-02": [
                    {"name": "调休白班", "segments": [{"start": "10:00", "end": "14:00"}]}
                ]
            },
            "special_rules": [
                {
                    "rule_type": "fixed_date_range",
                    "month": 5,
                    "day_start": 1,
                    "day_end": 5,
                    "shifts": [],
                    "priority": 90,
                }
            ],
        }

        result = next_working_day_shift_start(calendar, datetime(2026, 4, 30, 10, 0))
        self.assertEqual(result, datetime(2026, 5, 2, 10, 0))

    def test_business_calendar_supports_lunar_special_rule(self) -> None:
        holiday_start = LunarDate(2026, 1, 1).to_solar_date()
        calendar = {
            "weekly_shifts": {
                str(day): [{"segments": [{"start": "08:00", "end": "17:00"}]}]
                for day in range(7)
            },
            "special_rules": [
                {
                    "rule_type": "lunar",
                    "lunar_month": 1,
                    "lunar_day": 1,
                    "duration_days": 7,
                    "shifts": [],
                }
            ],
        }

        self.assertEqual(calendar_day_shifts(calendar, holiday_start), [])

    def test_machine_calendar_available_minutes_include_overrides_and_downtime(self) -> None:
        calendar = {
            "weekly_shifts": {
                "1": [
                    {
                        "segments": [
                            {"start": "08:00", "end": "12:00"},
                            {"start": "13:00", "end": "17:00"},
                        ]
                    }
                ]
            }
        }
        profile = {
            "machine_id": "MC_TEST",
            "status": "ACTIVE",
            "availability_overrides": [
                {
                    "date": "2026-07-20",
                    "status": "CONFIRMED",
                    "segments": [{"start": "17:00", "end": "19:00"}],
                }
            ],
            "unavailability": [
                {
                    "date": "2026-07-20",
                    "status": "CONFIRMED",
                    "segments": [{"start": "10:00", "end": "11:00"}],
                }
            ],
        }
        minutes = available_minutes_for_profile(
            calendar,
            profile,
            datetime(2026, 7, 20, 8, 0),
            datetime(2026, 7, 20, 19, 0),
        )
        self.assertEqual(minutes, 540.0)

    def test_machine_load_context_uses_schedule_start_and_latest_finish(self) -> None:
        snapshot = {
            "machine_calendar": {
                "weekly_shifts": {
                    "1": [{"segments": [{"start": "08:00", "end": "17:00"}]}]
                }
            },
            "machine_profiles": [
                {
                    "machine_id": "MC_TEST",
                    "machine_name": "测试设备",
                    "status": "ACTIVE",
                    "unavailability": [],
                    "availability_overrides": [],
                }
            ],
        }
        result = {
            "schedule": [
                {
                    "machine_id": "MC_TEST",
                    "plan_start_time": "2026-07-20T09:00:00",
                    "plan_end_time": "2026-07-20T16:00:00",
                }
            ]
        }
        context = build_machine_load_context(snapshot, result, "2026-07-20T08:00:00")
        self.assertEqual(context["period_start"], "2026-07-20T08:00:00")
        self.assertEqual(context["period_end"], "2026-07-20T16:00:00")
        self.assertEqual(context["machines"][0]["machine_name"], "测试设备")
        self.assertEqual(context["machines"][0]["available_minutes"], 480.0)

    def test_calendar_selection_uses_schedule_type(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE master_records(entity_type TEXT,entity_id TEXT,payload_json TEXT)"
        )
        for schedule_type, start in (
            ("machining", "08:00"),
            ("heat_treatment", "00:00"),
            ("assembly", "09:00"),
        ):
            payload = {
                "calendar_id": f"CAL_{schedule_type}",
                "schedule_type": schedule_type,
                "weekly_shifts": {"1": [{"segments": [{"start": start, "end": "17:00"}]}]},
            }
            connection.execute(
                "INSERT INTO master_records VALUES('calendar',?,?)",
                (payload["calendar_id"], json.dumps(payload)),
            )
        self.assertEqual(select_calendar(connection, "heat_treatment")["calendar_id"], "CAL_heat_treatment")
        self.assertEqual(select_calendar(connection, "assembly")["calendar_id"], "CAL_assembly")
        connection.close()

    def test_calendar_selection_does_not_guess_legacy_calendar_type(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE master_records(entity_type TEXT,entity_id TEXT,payload_json TEXT)"
        )
        connection.execute(
            "INSERT INTO master_records VALUES('calendar','LEGACY',?)",
            (json.dumps({"calendar_id": "LEGACY", "calendar_name": "热表日历", "weekly_shifts": {}}),),
        )
        self.assertEqual(select_calendar(connection, "heat_treatment"), {})
        connection.close()

    def test_enabled_cooling_constraint_requires_method_in_business_snapshot(self) -> None:
        snapshot = self.client.get("/api/master-data/snapshot", headers=self.headers).json()
        order = next(item for item in snapshot["order_processes"] if item.get("order_id") == "DEMO_HEAT_ORDER")
        process = order["processes"][0]
        process["cooling_constraint_enabled"] = True
        process["cooling_method"] = ""
        self.assertTrue(
            any("已启用冷却约束但缺少 cooling_method" in item for item in validate_snapshot(snapshot))
        )
        process["cooling_method"] = "N2_GAS_QUENCH"
        process["override_batch_rules"] = {
            "allow_batch_merge": True,
            "heat_treat_recipe": "HT-A",
            "unit_size_category": "L",
            "unit_volume_coefficient": 2.0,
        }
        order["material_grade"] = "   "
        self.assertTrue(any("允许合批但缺少 material_grade" in item for item in validate_snapshot(snapshot)))

    def test_temperature_range_requires_full_machine_capability_coverage(self) -> None:
        snapshot = self.client.get("/api/master-data/snapshot", headers=self.headers).json()
        order = next(item for item in snapshot["order_processes"] if item.get("order_id") == "DEMO_HEAT_ORDER")
        process = order["processes"][0]
        group = next(
            item
            for item in snapshot["resource_group_profiles"]
            if item.get("resource_group_id") == process.get("resource_group_id")
        )
        machine_ids = {str(item["machine_id"]) for item in group.get("machines", [])}
        for machine in snapshot["machine_profiles"]:
            if str(machine.get("machine_id")) in machine_ids:
                machine.setdefault("capabilities", {})["temp_range"] = [400, 900]

        process["resource_requirements"] = {"temp_range": [900, 900]}
        errors = validate_snapshot(snapshot)
        self.assertFalse(any("温度要求" in item for item in errors), errors)

        process["resource_requirements"] = {"temp_range": [500, 1200]}
        errors = validate_snapshot(snapshot)
        self.assertTrue(any("没有温度能力完整覆盖的设备" in item for item in errors), errors)

        process["resource_requirements"] = {"temp_range_min": 500, "temp_range_max": 1200}
        errors = validate_snapshot(snapshot)
        self.assertTrue(any("不支持旧温度字段" in item for item in errors), errors)

    def test_master_data_save_rejects_invalid_temperature_shapes(self) -> None:
        machine = self.client.get("/api/master-data/machine", headers=self.headers).json()[0]["payload"]
        machine.setdefault("capabilities", {})["temp_range"] = [900]
        response = self.client.put(
            f"/api/master-data/machine/{machine['machine_id']}",
            json=machine,
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("两个数字", response.json()["detail"])

        order = self.client.get("/api/master-data/order", headers=self.headers).json()[0]["payload"]
        order["processes"][0]["resource_requirements"] = {
            "temp_range_min": 500,
            "temp_range_max": 1200,
        }
        response = self.client.put(
            f"/api/master-data/order/{order['order_id']}",
            json=order,
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("不支持旧温度字段", response.json()["detail"])

    def test_worker_skills_schema_allows_missing_workshop_type(self) -> None:
        worker_id = "WORKER_SKILLS_SCHEMA_TEST"
        payload = {
            "worker_id": worker_id,
            "worker_name": "技能模型测试人员",
            "status": "ACTIVE",
            "skills": [],
            "unavailability": [],
            "availability_overrides": [],
        }
        try:
            response = self.client.put(
                f"/api/master-data/worker/{worker_id}",
                json=payload,
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
            saved = response.json()["payload"]
            self.assertEqual(saved["skills"], [])
            self.assertNotIn("skill_level", saved)
            self.assertNotIn("workshop_type", saved)

            payload["skills"] = "five_axis"
            response = self.client.put(
                f"/api/master-data/worker/{worker_id}",
                json=payload,
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 422)
            self.assertIn("字符串数组", response.json()["detail"])
        finally:
            self.client.delete(
                f"/api/master-data/worker/{worker_id}",
                headers=self.headers,
            )

    def test_snapshot_checks_required_skills_against_worker_skills(self) -> None:
        snapshot = self.client.get("/api/master-data/snapshot", headers=self.headers).json()
        order = next(item for item in snapshot["order_processes"] if item.get("order_id") == "DEMO_MC_ORDER")
        process = order["processes"][0]
        group = next(
            item
            for item in snapshot["resource_group_profiles"]
            if item.get("resource_group_id") == process.get("resource_group_id")
        )
        worker_ids = {str(item["worker_id"]) for item in group.get("workers", [])}
        process["required_skills"] = ["five_axis"]
        for worker in snapshot["worker_profiles"]:
            if str(worker.get("worker_id")) in worker_ids:
                worker["skills"] = []
        errors = validate_snapshot(snapshot)
        self.assertTrue(any("没有具备全部技能的人员" in item for item in errors), errors)

        matching_worker = next(
            worker for worker in snapshot["worker_profiles"] if str(worker.get("worker_id")) in worker_ids
        )
        matching_worker["skills"] = ["five_axis"]
        errors = validate_snapshot(snapshot)
        self.assertFalse(any("没有具备全部技能的人员" in item for item in errors), errors)

    def test_resource_role_and_priority_are_validated_in_business_snapshot(self) -> None:
        snapshot = self.client.get("/api/master-data/snapshot", headers=self.headers).json()
        group = next(item for item in snapshot["resource_group_profiles"] if not item.get("virtual_resource"))
        group["machines"][0]["role"] = "standby"
        group["machines"][0]["priority"] = "high"
        errors = validate_snapshot(snapshot)
        self.assertTrue(any("role 非法" in item for item in errors), errors)
        self.assertTrue(any("priority 必须是有限数字" in item for item in errors), errors)
        response = self.client.put(
            f"/api/master-data/resource_group/{group['resource_group_id']}",
            json=group,
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("role 非法", response.json()["detail"])

    def test_route_cycle_and_external_plan_source_are_rejected(self) -> None:
        snapshot = self.client.get("/api/master-data/snapshot", headers=self.headers).json()
        machining_order = next(
            item for item in snapshot["order_processes"] if item.get("order_id") == "DEMO_MC_ORDER"
        )
        first, second = machining_order["processes"][:2]
        first["previous_process_ids"] = [second["process_id"]]
        second["previous_process_ids"] = [first["process_id"]]
        errors = validate_snapshot(snapshot)
        self.assertTrue(any("循环前置关系" in item for item in errors), errors)

        first["previous_process_ids"] = []
        second["external_plan"] = {
            "status": "CONFIRMED",
            "source_business_type": "SURFACE_TREAT",
            "reference_id": "OUTSIDE-001",
            "plan_start_time": "2026-07-20T08:00:00",
            "plan_end_time": "2026-07-20T12:00:00",
            "confirmed_by": "外部计划员",
            "confirmed_at": "2026-07-16T10:00:00",
        }
        errors = validate_snapshot(snapshot)
        self.assertTrue(any("必须与资源组类型 HEAT_TREAT 一致" in item for item in errors), errors)

    def test_effective_schedule_uses_order_scope_for_cross_business_processes(self) -> None:
        with db() as connection:
            result = build_effective_schedule(
                connection,
                schedule_type="machining",
                order_id="DEMO_MC_ORDER",
            )
        processes = {item["process_id"]: item for item in result["processes"]}
        self.assertIn("DEMO_MC_P20", processes)
        self.assertEqual(processes["DEMO_MC_P20"]["schedule_type"], "machining")
        self.assertEqual(processes["DEMO_MC_P20"]["resource_group_type"], "HEAT_TREAT")
        self.assertTrue(processes["DEMO_MC_P20"]["virtual_resource"])

    def test_ga_parameters_follow_process_scale_boundaries(self) -> None:
        expectations = {
            0: (56, 30),
            100: (56, 30),
            101: (48, 20),
            500: (48, 20),
            501: (40, 15),
            1000: (40, 15),
            1001: (32, 10),
            4999: (32, 10),
            5000: (24, 4),
        }
        for process_count, expected in expectations.items():
            with self.subTest(process_count=process_count):
                result = ga_parameters_for_process_count(process_count)
                self.assertEqual((result["population_size"], result["generations"]), expected)

    def test_task_modal_allows_population_one_and_zero_generations(self) -> None:
        app_js = (Path(__file__).parents[1] / "business_app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="taskPopulationSize" type="number" min="1"', app_js)
        self.assertIn('id="taskGenerations" type="number" min="0"', app_js)
        self.assertIn("population<1", app_js)
        self.assertIn("generations<0", app_js)
        self.assertIn("仅生成初始排程方案，不执行遗传寻优", app_js)
        self.assertIn("/summary`", app_js)
        self.assertIn("function downloadTaskPayload", app_js)
        self.assertIn("完整大数据通过下载获取", app_js)
        self.assertNotIn("JSON.stringify(t.request??{},null,2)", app_js)
        for label in (
            "交期优先(EDD)",
            "优先级优先(PRIORITY)",
            "最小松弛时间(SLACK)",
            "效率优先(EFFICIENCY)",
            "先到先服务(FCFS)",
        ):
            self.assertIn(label, app_js)

    def test_task_modal_supports_common_weight_presets(self) -> None:
        app_js = (Path(__file__).parents[1] / "business_app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        premium = (
            Path(__file__).parents[1] / "business_app" / "static" / "premium.css"
        ).read_text(encoding="utf-8")
        self.assertIn("const taskWeightPresets", app_js)
        for label in ("综合均衡", "交付优先", "设备效率", "负荷均衡"):
            self.assertIn(f"label:'{label}'", app_js)
        self.assertIn("values:[0.10,0.30,0.25,0.10,0.10,0.05,0.10]", app_js)
        self.assertIn("values:[0.20,0.10,0.10,0.30,0.15,0.05,0.10]", app_js)
        self.assertIn("values:[0.10,0.10,0.10,0.15,0.30,0.20,0.05]", app_js)
        self.assertIn("title.textContent='方案选择权重'", app_js)
        self.assertIn("'当前：自定义'", app_js)
        self.assertIn("function applyTaskWeightPreset", app_js)
        self.assertIn("replaceAll('TOPSIS 权重','方案选择权重')", app_js)
        self.assertIn(".weight-preset-button.active", premium)

    def test_task_modal_uses_compact_layout_with_visible_submit_actions(self) -> None:
        static_dir = Path(__file__).parents[1] / "business_app" / "static"
        app_js = (static_dir / "app.js").read_text(encoding="utf-8")
        premium = (static_dir / "premium.css").read_text(encoding="utf-8")
        self.assertIn("function enableCompactTaskModal", app_js)
        self.assertIn("classList.add('schedule-task-modal')", app_js)
        self.assertIn("classList.add('task-json-field')", app_js)
        self.assertIn("form.classList.toggle('local-mode'", app_js)
        self.assertIn(".modal-card.schedule-task-modal", premium)
        self.assertIn("width: min(1320px, 98vw)", premium)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", premium)
        self.assertIn(".schedule-task-modal #taskForm > .task-config-section", premium)
        self.assertIn(".schedule-task-modal #taskForm > .form-actions", premium)
        self.assertIn("position: sticky", premium)

    def test_version_comparison_defaults_to_previous_and_latest_versions(self) -> None:
        app_js = (Path(__file__).parents[1] / "business_app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("form.elements.left.value=versions[1].version_id", app_js)
        self.assertIn("form.elements.right.value=versions[0].version_id", app_js)
        self.assertIn("Number(b.version_no)", app_js)
        self.assertIn("排程模式", app_js)
        self.assertIn("派工规则", app_js)
        self.assertIn("工艺类型", app_js)
        self.assertIn("comparisonModeLabel(run?.mode)", app_js)
        self.assertIn("scheduleTypeLabel(run?.schedule_type)", app_js)
        self.assertIn("function versionParameterPanel", app_js)
        self.assertIn("function renderVersionsWithSearch", app_js)
        self.assertIn("搜索版本号、任务、创建人、模式、派工规则", app_js)
        self.assertIn('<th>排程模式 / 派工规则</th>', app_js)
        self.assertNotIn('id="versionModeFilter"', app_js)
        self.assertNotIn('id="versionDispatchingFilter"', app_js)
        self.assertIn("function renderTasksWithSearch", app_js)
        self.assertIn("function taskTableDetailed", app_js)
        self.assertIn("搜索任务号、创建人、模式、派工规则", app_js)
        self.assertIn("selectedVersionIds:new Set()", app_js)
        self.assertIn("function openSelectedVersionComparison", app_js)
        self.assertIn("function renderMultiVersionMetricComparison", app_js)
        self.assertIn("/api/versions/compare-metrics", app_js)
        self.assertIn("已选版本指标对比", app_js)

    def test_task_detail_retry_is_an_interactive_button(self) -> None:
        app_js = (Path(__file__).parents[1] / "business_app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="retryTaskButton" type="button"', app_js)
        self.assertIn("retryButton.onclick=()=>retryTask(id,retryButton)", app_js)
        self.assertIn("button.textContent='正在重试…'", app_js)

    def test_plan_version_kpis_include_tardiness_count(self) -> None:
        app_js = (Path(__file__).parents[1] / "business_app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function countMetric", app_js)
        self.assertIn("['延期订单数',countMetric(tardyCount)]", app_js)

    def test_version_types_are_colored_and_master_data_navigation_is_reordered(self) -> None:
        static_dir = Path(__file__).parents[1] / "business_app" / "static"
        app_js = (static_dir / "app.js").read_text(encoding="utf-8")
        styles = (static_dir / "styles.css").read_text(encoding="utf-8")
        premium = (static_dir / "premium.css").read_text(encoding="utf-8")
        index = (static_dir / "index.html").read_text(encoding="utf-8")

        self.assertIn("scheduleTypeBadge(v.schedule_type)", app_js)
        self.assertIn("scheduleModeBadge(v.mode)", app_js)
        self.assertIn("scheduleModeBadge(task.mode)", app_js)
        for schedule_type in ("machining", "heat_treatment", "assembly"):
            self.assertIn(f".schedule-type-badge.{schedule_type}", styles)
        for mode in ("static", "dynamic", "local"):
            self.assertIn(f".schedule-mode-badge.{mode}", styles)
        self.assertLess(index.index('data-page="dashboard"'), index.index('data-page="master"'))
        self.assertLess(index.index('data-page="master"'), index.index('data-page="versions"'))
        self.assertLess(index.index('data-page="effective"'), index.index('data-page="tasks"'))
        self.assertLess(index.index('data-page="tasks"'), index.index('data-page="users"'))
        self.assertIn("高级计划与排程", index)
        self.assertIn("智能制造计划平台", index)
        self.assertIn("APS ENGINE ONLINE", index)
        self.assertIn(".login-brand-signature", styles)
        self.assertIn(".login-card .eyebrow.dark{font-size:13px", styles)
        self.assertIn(".login-card h2{font-size:36px", styles)
        self.assertIn(".feature-strip{gap:30px", styles)
        self.assertIn("SECURE PLANNING CONSOLE", styles)
        self.assertIn("@keyframes login-grid-drift", styles)
        self.assertIn("/assets/comparison.css?v=20260721-4", index)
        self.assertIn("/assets/premium.css?v=20260721-10", index)
        self.assertIn("/assets/app.js?v=20260721-10", index)
        self.assertIn("grid-template-columns: repeat(6, minmax(0, 1fr))", premium)
        self.assertIn(".sidebar", premium)
        self.assertIn(".topbar h2", premium)
        self.assertIn(".data-table", premium)
        self.assertIn(".modal-card", premium)
        self.assertIn("<th>创建人</th>", app_js)
        self.assertIn("v.created_by||'-'", app_js)
        self.assertIn(".version-list-table th:last-child", styles)
        self.assertIn("position:sticky;right:0", styles)
        self.assertIn(".version-table-wrap{overflow-x:hidden}", styles)
        self.assertIn(".version-list-table{min-width:0;table-layout:fixed", styles)
        self.assertNotIn(".version-list-table{min-width:1800px}", styles)

    def test_order_master_data_has_three_scoped_order_tabs(self) -> None:
        static_dir = Path(__file__).parents[1] / "business_app" / "static"
        app_js = (static_dir / "app.js").read_text(encoding="utf-8")
        styles = (static_dir / "styles.css").read_text(encoding="utf-8")

        for label in ("机加订单", "热表订单", "装配订单"):
            self.assertIn(label, app_js)
        self.assertIn("function ensureOrderScope", app_js)
        self.assertIn("function switchOrderType", app_js)
        self.assertIn("data-order-type", app_js)
        self.assertIn(".order-type-tabs", styles)

    def test_business_unit_configuration_and_virtual_frontend_rules_are_wired(self) -> None:
        static_dir = Path(__file__).parents[1] / "business_app" / "static"
        app_js = (static_dir / "app.js").read_text(encoding="utf-8")
        index = (static_dir / "index.html").read_text(encoding="utf-8")
        premium = (static_dir / "premium.css").read_text(encoding="utf-8")
        self.assertIn('data-page="settings"', index)
        self.assertIn('id="systemBrandTitle"', index)
        self.assertIn("state.systemConfig=await api('/api/system-configuration')", app_js)
        self.assertIn("function renderSystemConfiguration", app_js)
        self.assertIn("deploymentScheduleTypes", app_js)
        self.assertIn("order_business_type", app_js)
        self.assertIn("const resourceFields=item.virtual_resource?'':", app_js)
        self.assertIn("if(!item.virtual_resource){const machine=", app_js)
        self.assertIn(".system-config-panel", premium)
        self.assertIn(".badge.VIRTUAL", premium)

    def test_calendar_master_data_has_three_scoped_calendar_tabs(self) -> None:
        app_js = (Path(__file__).parents[1] / "business_app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        for label in ("机加日历", "热表日历", "装配日历"):
            self.assertIn(label, app_js)
        self.assertIn("function calendarScheduleType", app_js)
        self.assertIn("function switchCalendarType", app_js)
        self.assertIn("data-calendar-type", app_js)
        self.assertIn("defaultCalendarWeeklyShifts", app_js)
        self.assertIn("function deleteAllMaster", app_js)
        self.assertIn("一键删除", app_js)
        self.assertIn("currentMasterDeleteUrl", app_js)

    def test_master_data_batch_delete_respects_current_module_scope(self) -> None:
        with db() as connection:
            backup = [dict(row) for row in connection.execute("SELECT * FROM master_records").fetchall()]
        try:
            machining_orders = self.client.get(
                "/api/master-data/order", headers=self.headers
            ).json()
            expected_machining = sum(
                1
                for row in machining_orders
                if row["payload"].get("order_business_type") == "MACHINING"
            )
            response = self.client.delete(
                "/api/master-data/order?schedule_type=machining", headers=self.headers
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["deleted"], expected_machining)
            remaining_orders = self.client.get(
                "/api/master-data/order", headers=self.headers
            ).json()
            self.assertFalse(
                any(
                    row["payload"].get("order_business_type") == "MACHINING"
                    for row in remaining_orders
                )
            )
            self.assertTrue(remaining_orders)
            self.assertEqual(
                self.client.delete("/api/master-data/order", headers=self.headers).status_code,
                422,
            )

            response = self.client.delete(
                "/api/master-data/calendar?schedule_type=heat_treatment", headers=self.headers
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["deleted"], 1)
            remaining_calendars = self.client.get(
                "/api/master-data/calendar", headers=self.headers
            ).json()
            self.assertNotIn(
                "heat_treatment",
                {row["payload"].get("schedule_type") for row in remaining_calendars},
            )

            response = self.client.delete("/api/master-data/machine", headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertGreater(response.json()["deleted"], 0)
            self.assertEqual(
                self.client.get("/api/master-data/machine", headers=self.headers).json(), []
            )

            with db() as connection:
                log = connection.execute(
                    "SELECT detail_json FROM audit_logs WHERE action=? AND target_type='machine' ORDER BY id DESC LIMIT 1",
                    ("MASTER_DATA_DELETED",),
                ).fetchone()
            self.assertIsNotNone(log)
            self.assertTrue(json.loads(log["detail_json"])["batch"])
        finally:
            with db() as connection:
                connection.execute("DELETE FROM master_records")
                connection.executemany(
                    """INSERT INTO master_records(
                           entity_type,entity_id,payload_json,revision,updated_by,updated_at
                       ) VALUES(?,?,?,?,?,?)""",
                    [
                        (
                            row["entity_type"],
                            row["entity_id"],
                            row["payload_json"],
                            row["revision"],
                            row["updated_by"],
                            row["updated_at"],
                        )
                        for row in backup
                    ],
                )

    def test_version_comparison_returns_core_metric_trends(self) -> None:
        left = {
            "version_id": "PLAN-LEFT",
            "schedule_type": "heat_treatment",
            "mode": "static",
            "dispatching_rule": "DELIVERY",
            "request_json": json.dumps(
                {"config_overrides": {"nsga3": {"population_size": 56, "generations": 30}}}
            ),
            "started_at": "2026-07-20T07:59:50",
            "completed_at": "2026-07-20T08:00:02",
            "result_json": json.dumps(
                {
                    "metadata": {"order_count": 10},
                    "kpis": {
                        "makespan": 600,
                        "total_tardiness": 120,
                        "machine_idle_rate": 0.4,
                        "tardiness_count": 2,
                        "wip_waiting": 300,
                    },
                    "topsis_ranking": [{"rank": 1, "topsis_score": 0.612483}],
                    "schedule": [
                        {
                            "process_id": "P1",
                            "plan_start_time": "2026-07-20T08:00:00",
                            "plan_end_time": "2026-07-20T10:00:00",
                            "machine_id": "M1",
                            "worker_id": "W1",
                        }
                    ],
                }
            ),
        }
        right = {
            "version_id": "PLAN-RIGHT",
            "schedule_type": "assembly",
            "mode": "dynamic",
            "dispatching_rule": "PRIORITY",
            "request_json": json.dumps(
                {"config_overrides": {"nsga3": {"population_size": 48, "generations": 20}}}
            ),
            "started_at": "2026-07-20T08:00:00",
            "completed_at": "2026-07-20T08:00:08",
            "result_json": json.dumps(
                {
                    "metadata": {"order_count": 10},
                    "kpis": {
                        "makespan": 540,
                        "total_tardiness": 60,
                        "machine_idle_rate": 0.3,
                        "tardiness_count": 1,
                        "wip_waiting": 360,
                    },
                    "topsis_ranking": [{"rank": 1, "topsis_score": 0.70126}],
                    "schedule": [
                        {
                            "process_id": "P1",
                            "plan_start_time": "2026-07-20T08:30:00",
                            "plan_end_time": "2026-07-20T10:30:00",
                            "machine_id": "M2",
                            "worker_id": "W1",
                        }
                    ],
                }
            ),
        }
        result = compare_versions(left, right)
        metrics = {item["key"]: item for item in result["metric_comparison"]}
        self.assertEqual(metrics["makespan"]["trend"], "down")
        self.assertEqual(metrics["makespan"]["outcome"], "improved")
        self.assertEqual(metrics["machine_utilization"]["trend"], "up")
        self.assertEqual(metrics["tardiness_count"]["before"], 2)
        self.assertEqual(metrics["tardiness_count"]["after"], 1)
        self.assertEqual(metrics["tardiness_count"]["delta"], -1)
        self.assertEqual(metrics["tardiness_count"]["outcome"], "improved")
        self.assertEqual(metrics["on_time_delivery_rate"]["after"], 0.9)
        self.assertEqual(metrics["wip_waiting"]["outcome"], "worsened")
        self.assertEqual(result["score_comparison"]["before"], 0.612483)
        self.assertEqual(result["score_comparison"]["after"], 0.70126)
        self.assertEqual(result["score_comparison"]["outcome"], "improved")
        self.assertEqual(result["score_comparison"]["change_rate_percent"], 14.49)
        self.assertEqual(result["changes"][0]["fields"]["machine_id"]["after"], "M2")
        self.assertEqual(result["run_comparison"]["before"]["population_size"], 56)
        self.assertEqual(result["run_comparison"]["after"]["generations"], 20)
        self.assertEqual(result["run_comparison"]["before"]["duration_seconds"], 12.0)
        self.assertEqual(result["run_comparison"]["before"]["mode"], "static")
        self.assertEqual(result["run_comparison"]["after"]["dispatching_rule"], "PRIORITY")
        self.assertEqual(result["run_comparison"]["before"]["schedule_type"], "heat_treatment")
        self.assertEqual(result["run_comparison"]["after"]["schedule_type"], "assembly")

    def test_multi_version_core_metrics_preserve_order_and_exclude_details(self) -> None:
        records = [
            {
                "version_id": "PLAN-B",
                "version_no": 2,
                "schedule_type": "machining",
                "status": "DRAFT",
                "created_at": "2026-07-21T09:00:00",
                "result_json": json.dumps(
                    {
                        "metadata": {"order_count": 10},
                        "kpis": {
                            "makespan": 540,
                            "total_tardiness": 60,
                            "tardiness_count": 1,
                            "machine_idle_rate": 0.3,
                            "wip_waiting": 360,
                        },
                        "topsis_score": 0.70126,
                        "schedule": [{"process_id": "P-B"}],
                    }
                ),
            },
            {
                "version_id": "PLAN-A",
                "version_no": 1,
                "schedule_type": "machining",
                "status": "DRAFT",
                "created_at": "2026-07-21T08:00:00",
                "result_json": json.dumps(
                    {
                        "metadata": {"order_count": 10},
                        "kpis": {
                            "makespan": 600,
                            "total_tardiness": 120,
                            "tardiness_count": 2,
                            "machine_idle_rate": 0.4,
                            "wip_waiting": 300,
                        },
                        "topsis_score": 0.612483,
                        "schedule": [{"process_id": "P-A"}],
                    }
                ),
            },
        ]
        result = compare_version_core_metrics(records)
        self.assertEqual([item["version_id"] for item in result["versions"]], ["PLAN-B", "PLAN-A"])
        self.assertEqual(result["versions"][0]["metrics"]["tardiness_count"], 1)
        definitions = {item["key"]: item for item in result["metric_definitions"]}
        self.assertEqual(definitions["comprehensive_score"]["best_version_ids"], ["PLAN-B"])
        self.assertEqual(definitions["makespan"]["best_version_ids"], ["PLAN-B"])
        self.assertNotIn("schedule", result["versions"][0])
        self.assertNotIn("changes", result)

    def test_multi_version_core_metric_endpoint_requires_two_versions(self) -> None:
        task_ids = ["TEST-MULTI-COMPARE-A", "TEST-MULTI-COMPARE-B"]
        version_ids = ["PLAN-TEST-MULTI-A", "PLAN-TEST-MULTI-B"]
        stamp = now_text()
        try:
            with db() as connection:
                for index, (task_id, version_id) in enumerate(zip(task_ids, version_ids), start=1):
                    connection.execute(
                        "INSERT INTO schedule_tasks(task_id,schedule_type,mode,dispatching_rule,status,request_json,"
                        "snapshot_json,response_json,created_by,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            task_id,
                            "machining",
                            "static",
                            "DELIVERY",
                            "SUCCEEDED",
                            "{}",
                            "{}",
                            "{}",
                            "admin",
                            stamp,
                            stamp,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO schedule_versions(version_id,version_no,task_id,schedule_type,status,result_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            version_id,
                            9200 + index,
                            task_id,
                            "machining",
                            "DRAFT",
                            json.dumps(
                                {
                                    "metadata": {"order_count": 4},
                                    "kpis": {
                                        "makespan": 600 - index * 60,
                                        "tardiness_count": 3 - index,
                                    },
                                    "topsis_score": 0.5 + index / 10,
                                    "schedule": [{"process_id": f"P-{index}"}],
                                }
                            ),
                            "admin",
                            stamp,
                        ),
                    )

            invalid = self.client.post(
                "/api/versions/compare-metrics",
                json={"version_ids": [version_ids[0]]},
                headers=self.headers,
            )
            self.assertEqual(invalid.status_code, 422, invalid.text)

            response = self.client.post(
                "/api/versions/compare-metrics",
                json={"version_ids": [version_ids[1], version_ids[0]]},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(
                [item["version_id"] for item in payload["versions"]],
                [version_ids[1], version_ids[0]],
            )
            self.assertTrue(all("schedule" not in item for item in payload["versions"]))
            self.assertNotIn("changes", payload)
        finally:
            with db() as connection:
                connection.executemany(
                    "DELETE FROM schedule_versions WHERE version_id=?",
                    [(version_id,) for version_id in version_ids],
                )
                connection.executemany(
                    "DELETE FROM schedule_tasks WHERE task_id=?",
                    [(task_id,) for task_id in task_ids],
                )

    def test_admin_can_create_and_update_planner(self) -> None:
        created = self.client.post(
            "/api/users",
            json={"username": "planner_test", "display_name": "测试计划员", "password": "planner123", "role": "planner"},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201)
        updated = self.client.put(
            "/api/users/planner_test",
            json={"display_name": "测试计划员", "role": "viewer", "active": True},
            headers=self.headers,
        )
        self.assertEqual(updated.json()["role"], "viewer")

    def test_master_data_update_increments_revision(self) -> None:
        record = self.client.get("/api/master-data/machine", headers=self.headers).json()[0]
        payload = record["payload"]
        payload["machine_name"] = "更新后的设备名称"
        response = self.client.put(
            f"/api/master-data/machine/{payload['machine_id']}", json=payload, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], record["revision"] + 1)

    def test_master_data_batch_import_and_export_use_array_format(self) -> None:
        payload = [
            {
                "machine_id": "BATCH_MC_01",
                "machine_name": "批量导入设备",
                "machine_type": "加工中心",
                "workshop_type": "MACHINING",
                "status": "ACTIVE",
                "capabilities": {},
                "unavailability": [],
                "availability_overrides": [],
            }
        ]
        imported = self.client.post("/api/master-data/machine/batch", json=payload, headers=self.headers)
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["imported"], 1)

        exported = self.client.get("/api/master-data/machine/batch", headers=self.headers)
        self.assertEqual(exported.status_code, 200)
        self.assertIsInstance(exported.json(), list)
        self.assertTrue(any(item["machine_id"] == "BATCH_MC_01" for item in exported.json()))

    def test_master_data_rejects_invalid_resource_calendar_fields(self) -> None:
        record = self.client.get("/api/master-data/machine", headers=self.headers).json()[0]["payload"]
        record["unavailability"] = [
            {
                "date_start": "2026-07-12",
                "segments": [{"start": "8:00", "end": "09:00"}],
                "status": "UNKNOWN",
            }
        ]

        response = self.client.put(
            f"/api/master-data/machine/{record['machine_id']}",
            json=record,
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("date_start", response.json()["detail"])

        invalid = self.client.post("/api/master-data/machine/batch", json={}, headers=self.headers)
        self.assertEqual(invalid.status_code, 422)

    def test_historical_batch_metadata_is_restored_for_rolling_snapshot(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE schedule_versions(version_no INTEGER, result_json TEXT)")
        batch_id = "BATCH_P1_P2"
        batch_tasks = [
            {
                "process_id": process_id,
                "machine_id": "FURNACE_01",
                "worker_id": "WORKER_01",
                "plan_start_time": "2026-07-14T08:00:00",
                "plan_end_time": "2026-07-14T12:00:00",
                "batch_id": batch_id,
                "batch_merged": True,
                "batch_member_count": 2,
                "batch_process_ids": ["P1", "P2"],
            }
            for process_id in ("P1", "P2")
        ]
        connection.execute(
            "INSERT INTO schedule_versions(version_no,result_json) VALUES(?,?)",
            (1, json.dumps({"schedule": batch_tasks})),
        )
        snapshot = {
            "order_processes": [
                {
                    "order_id": "O1",
                    "processes": [
                        {
                            "process_id": process_id,
                            "assigned_machine_id": "FURNACE_01",
                            "assigned_worker_id": "WORKER_01",
                            "plan_start_time": "2026-07-14T08:00:00",
                            "plan_end_time": "2026-07-14T12:00:00",
                        }
                        for process_id in ("P1", "P2")
                    ],
                }
            ]
        }

        _restore_batch_metadata_from_history(connection, snapshot)

        processes = snapshot["order_processes"][0]["processes"]
        self.assertEqual({process["batch_id"] for process in processes}, {batch_id})
        conflict_rows = [
            {
                **process,
                "machine_id": process["assigned_machine_id"],
                "worker_id": process["assigned_worker_id"],
            }
            for process in processes
        ]
        self.assertEqual(_detect_schedule_conflicts(conflict_rows), [])
        connection.close()

    def test_publishing_schedule_replaces_or_clears_batch_metadata(self) -> None:
        process = {"process_id": "P1", "batch_id": "OLD_BATCH", "batch_merged": True}
        _sync_batch_metadata(
            process,
            {
                "process_id": "P1",
                "batch_id": "NEW_BATCH",
                "batch_merged": True,
                "batch_member_count": 2,
            },
        )
        self.assertEqual(process["batch_id"], "NEW_BATCH")
        self.assertEqual(process["batch_member_count"], 2)

        _sync_batch_metadata(process, {"process_id": "P1"})
        self.assertNotIn("batch_id", process)
        self.assertNotIn("batch_merged", process)

    def test_only_nonempty_locks_are_reported_as_manual_lock(self) -> None:
        self.assertFalse(is_manually_locked({"locks": {}}))
        self.assertTrue(
            is_manually_locked(
                {
                    "locks": {
                        "machine_id": "FURNACE_01",
                        "lock_time": "2026-07-14T07:00:00",
                        "operator": "planner",
                        "lock_reason": "人工指定设备",
                    }
                }
            )
        )

    def test_task_version_review_and_publish_workflow(self) -> None:
        original_execute = algorithm_client.execute

        def fake_execute(payload):
            return {
                "task_id": payload["task_id"],
                "status": "SUCCEEDED",
                "schedule_type": "machining",
                "mode": "static",
                "completed_at": "2026-07-13T12:00:00",
                "result": {
                    "metadata": {"task_count": 1},
                    "schedule": [
                        {
                            "order_id": "DEMO_MC_ORDER",
                            "process_id": "DEMO_MC_P10",
                            "process_name": "精加工",
                            "machine_id": "DEMO_MC_01",
                            "worker_id": "DEMO_W_MC",
                            "plan_start_time": "2026-07-14T08:00:00",
                            "plan_end_time": "2026-07-14T13:00:00",
                            "status": "PENDING",
                        }
                    ],
                    "kpis": {"makespan_hours": 5},
                },
            }

        algorithm_client.execute = fake_execute
        try:
            created = self.client.post(
                "/api/tasks",
                json={
                    "schedule_type": "machining",
                    "mode": "static",
                    "dispatching_rule": "DELIVERY",
                    "schedule_start": "2026-07-14T08:00:00",
                    "config_overrides": {"nsga3": {"population_size": 56, "generations": 30}},
                },
                headers=self.headers,
            )
        finally:
            algorithm_client.execute = original_execute
        self.assertEqual(created.status_code, 202)
        task_id = created.json()["task_id"]
        task_response = self.client.get(f"/api/tasks/{task_id}", headers=self.headers)
        task = task_response.json()
        self.assertEqual(task["status"], "SUCCEEDED")
        summary_response = self.client.get(f"/api/tasks/{task_id}/summary", headers=self.headers)
        self.assertEqual(summary_response.status_code, 200, summary_response.text)
        self.assertLess(len(summary_response.content), len(task_response.content))
        summary = summary_response.json()
        self.assertNotIn("data_snapshot", summary["request"])
        self.assertGreater(summary["snapshot_summary"]["process_count"], 0)
        self.assertEqual(summary["response"]["result"]["schedule_count"], 1)
        self.assertNotIn("schedule", summary["response"]["result"])
        self.assertTrue(summary["response_available"])

        request_payload = self.client.get(
            f"/api/tasks/{task_id}/payload/request", headers=self.headers
        )
        self.assertEqual(request_payload.status_code, 200, request_payload.text)
        self.assertIn("attachment", request_payload.headers["content-disposition"])
        self.assertIn("data_snapshot", request_payload.json())
        response_payload = self.client.get(
            f"/api/tasks/{task_id}/payload/response", headers=self.headers
        )
        self.assertEqual(response_payload.status_code, 200, response_payload.text)
        self.assertEqual(response_payload.json()["result"]["schedule"][0]["process_id"], "DEMO_MC_P10")
        task_rows = self.client.get("/api/tasks", headers=self.headers).json()
        task_row = next(item for item in task_rows if item["task_id"] == task_id)
        self.assertEqual(task_row["schedule_type"], "machining")
        self.assertEqual(task_row["population_size"], 56)
        self.assertEqual(task_row["generations"], 30)
        self.assertIsNotNone(task_row["duration_seconds"])

        versions = self.client.get("/api/versions", headers=self.headers).json()
        version_row = next(item for item in versions if item["task_id"] == task_id)
        version_id = version_row["version_id"]
        self.assertEqual(version_row["population_size"], 56)
        self.assertEqual(version_row["generations"], 30)
        self.assertIsNotNone(version_row["duration_seconds"])
        version_detail = self.client.get(f"/api/versions/{version_id}", headers=self.headers).json()
        parameters = version_detail["schedule_parameters"]
        self.assertEqual(parameters["schedule_type"], "machining")
        self.assertEqual(parameters["mode"], "static")
        self.assertEqual(parameters["dispatching_rule"], "DELIVERY")
        self.assertEqual(parameters["schedule_start"], "2026-07-14T08:00:00")
        self.assertEqual(parameters["population_size"], 56)
        self.assertEqual(parameters["generations"], 30)
        self.assertEqual(parameters["config_overrides"]["nsga3"]["population_size"], 56)
        self.assertEqual(
            version_detail["result"]["schedule"][0]["material_ready_time"],
            "2026-07-13T08:00:00",
        )
        expected_machine_name = next(
            machine["machine_name"]
            for machine in request_payload.json()["data_snapshot"]["machine_profiles"]
            if machine["machine_id"] == "DEMO_MC_01"
        )
        self.assertEqual(
            version_detail["result"]["schedule"][0]["machine_name"],
            expected_machine_name,
        )
        load_context = version_detail["machine_load_context"]
        self.assertEqual(load_context["period_start"], "2026-07-14T08:00:00")
        self.assertEqual(load_context["period_end"], "2026-07-14T13:00:00")
        self.assertGreater(load_context["machines"][0]["available_minutes"], 0)
        self.assertFalse(version_detail["result"]["schedule"][0]["manually_locked"])
        self.assertEqual(version_detail["result"]["schedule"][0]["lock_details"], {})
        self.assertEqual(version_detail["result"]["schedule"][0]["status"], "PENDING")
        reviewed = self.client.post(
            f"/api/versions/{version_id}/review",
            json={"decision": "APPROVED", "comment": "测试审批"},
            headers=self.headers,
        )
        self.assertEqual(reviewed.json()["status"], "APPROVED")
        self.assertEqual(reviewed.json()["process_status"], "SCHEDULED")
        approved_detail = self.client.get(f"/api/versions/{version_id}", headers=self.headers).json()
        self.assertEqual(approved_detail["result"]["schedule"][0]["status"], "SCHEDULED")
        published = self.client.post(f"/api/versions/{version_id}/publish", headers=self.headers)
        self.assertEqual(published.json()["updated_processes"], 1)

        snapshot = self.client.get("/api/master-data/snapshot", headers=self.headers).json()
        order = next(item for item in snapshot["order_processes"] if item["order_id"] == "DEMO_MC_ORDER")
        self.assertEqual(order["processes"][0]["status"], "CONFIRMED")
        self.assertEqual(order["processes"][0]["schedule_version_id"], version_id)
        self.assertEqual(order["processes"][0]["locks"], {})

        published_detail = self.client.get(f"/api/versions/{version_id}", headers=self.headers).json()
        published_process = published_detail["result"]["schedule"][0]
        self.assertEqual(published_process["status"], "CONFIRMED")
        self.assertEqual(published_process["effective_status"], "CONFIRMED")
        self.assertEqual(published_process["effective_schedule_version_id"], version_id)
        self.assertTrue(published_process["is_effective_version"])

        effective = self.client.get(
            "/api/effective-schedule?schedule_type=machining&order_id=DEMO_MC_ORDER",
            headers=self.headers,
        )
        self.assertEqual(effective.status_code, 200)
        effective_data = effective.json()
        self.assertEqual(effective_data["summary"]["effective_processes"], 1)
        self.assertEqual(effective_data["summary"]["conflict_count"], 0)
        self.assertEqual(effective_data["summary"]["locked_processes"], 0)
        effective_process = effective_data["schedule"][0]
        self.assertEqual(effective_process["process_id"], "DEMO_MC_P10")
        self.assertEqual(effective_process["status"], "CONFIRMED")
        self.assertEqual(effective_process["schedule_state"], "EFFECTIVE")
        self.assertEqual(effective_process["schedule_version_id"], version_id)
        self.assertEqual(effective_process["machine_id"], "DEMO_MC_01")
        self.assertEqual(effective_process["worker_id"], "DEMO_W_MC")
        self.assertFalse(effective_process["manually_locked"])

        locked = self.client.post(
            "/api/processes/DEMO_MC_P10/lock",
            json={
                "machine_id": "DEMO_MC_01",
                "worker_id": "DEMO_W_MC",
                "start_time": "2026-07-14T08:00:00",
                "end_time": "2026-07-14T13:00:00",
                "lock_reason": "测试人工锁定",
                "schedule_version_id": version_id,
            },
            headers=self.headers,
        )
        self.assertEqual(locked.status_code, 200, locked.text)
        self.assertEqual(locked.json()["locks"]["operator"], "admin")
        self.assertEqual(locked.json()["locks"]["lock_reason"], "测试人工锁定")
        lock_time = locked.json()["locks"]["lock_time"]

        stale_update = self.client.post(
            "/api/processes/DEMO_MC_P10/lock",
            json={
                "machine_id": "DEMO_MC_01",
                "lock_reason": "过期页面提交",
                "schedule_version_id": version_id,
                "expected_lock_time": "",
            },
            headers=self.headers,
        )
        self.assertEqual(stale_update.status_code, 409)

        effective_locked = self.client.get(
            "/api/effective-schedule?schedule_type=machining&order_id=DEMO_MC_ORDER",
            headers=self.headers,
        ).json()
        self.assertEqual(effective_locked["summary"]["locked_processes"], 1)
        self.assertTrue(effective_locked["schedule"][0]["manually_locked"])

        missing_reason = self.client.request(
            "DELETE",
            "/api/processes/DEMO_MC_P10/lock",
            json={"schedule_version_id": version_id},
            headers=self.headers,
        )
        self.assertEqual(missing_reason.status_code, 422)
        unlocked = self.client.request(
            "DELETE",
            "/api/processes/DEMO_MC_P10/lock",
            json={
                "unlock_reason": "测试解除",
                "schedule_version_id": version_id,
                "expected_lock_time": lock_time,
            },
            headers=self.headers,
        )
        self.assertEqual(unlocked.status_code, 200, unlocked.text)
        self.assertEqual(unlocked.json()["locks"], {})

    def test_rejected_draft_keeps_processes_pending(self) -> None:
        task_id = "TEST-REJECTED-STATE-TASK"
        version_id = "PLAN-TEST-REJECTED-STATE"
        stamp = now_text()
        result = {
            "schedule": [
                {
                    "order_id": "DEMO_MC_ORDER",
                    "process_id": "DEMO_MC_P10",
                    "status": "PENDING",
                    "machine_id": "DEMO_MC_01",
                    "worker_id": "DEMO_W_MC",
                    "plan_start_time": "2026-07-20T08:00:00",
                    "plan_end_time": "2026-07-20T12:00:00",
                }
            ]
        }
        with db() as connection:
            connection.execute(
                "INSERT INTO schedule_tasks(task_id,schedule_type,mode,dispatching_rule,status,request_json,"
                "snapshot_json,response_json,created_by,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    "machining",
                    "static",
                    "DELIVERY",
                    "SUCCEEDED",
                    "{}",
                    "{}",
                    "{}",
                    "admin",
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                "INSERT INTO schedule_versions(version_id,version_no,task_id,schedule_type,status,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (version_id, 9999, task_id, "machining", "DRAFT", json.dumps(result), "admin", stamp),
            )

        rejected = self.client.post(
            f"/api/versions/{version_id}/review",
            json={"decision": "REJECTED", "comment": "参数需要调整"},
            headers=self.headers,
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["status"], "REJECTED")
        self.assertEqual(rejected.json()["process_status"], "PENDING")
        self.assertEqual(rejected.json()["updated_processes"], 0)
        detail = self.client.get(f"/api/versions/{version_id}", headers=self.headers).json()
        self.assertEqual(detail["result"]["schedule"][0]["status"], "PENDING")

    def test_z_single_operation_adjustment_preview_and_execute(self) -> None:
        future_start = (datetime.now() + timedelta(days=3)).replace(hour=8, minute=0, second=0, microsecond=0)
        future_end = future_start + timedelta(hours=6)
        preview_payload = {
            "plan_start_time": future_start.isoformat(timespec="seconds"),
            "plan_end_time": future_end.isoformat(timespec="seconds"),
            "assigned_machine_id": "DEMO_MC_01",
            "assigned_worker_id": "DEMO_W_MC",
        }
        preview = self.client.post(
            "/api/processes/DEMO_MC_P10/adjustments/preview",
            json=preview_payload,
            headers=self.headers,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        preview_data = preview.json()
        self.assertTrue(preview_data["can_execute"], preview_data["hard_errors"])
        self.assertEqual(preview_data["operation"], "move_backward")
        self.assertEqual(preview_data["recommended_strategy"], "move_only")

        past_preview = self.client.post(
            "/api/processes/DEMO_MC_P10/adjustments/preview",
            json={
                **preview_payload,
                "plan_start_time": (datetime.now() - timedelta(days=3)).replace(hour=8, minute=0).isoformat(timespec="seconds"),
                "plan_end_time": (datetime.now() - timedelta(days=3)).replace(hour=14, minute=0).isoformat(timespec="seconds"),
            },
            headers=self.headers,
        )
        self.assertEqual(past_preview.status_code, 200, past_preview.text)
        self.assertFalse(past_preview.json()["can_execute"])
        self.assertIn(
            ADJUSTMENT_CODE_PAST_TIME,
            {item["code"] for item in past_preview.json()["hard_errors"]},
        )

        original_execute = algorithm_client.execute
        captured_payload = {}

        def fake_local_execute(payload):
            captured_payload.update(payload)
            adjustment = payload["local_adjustments"][0]
            return {
                "task_id": payload["task_id"],
                "status": "SUCCEEDED",
                "schedule_type": "machining",
                "mode": "local",
                "completed_at": "2026-07-14T22:00:00",
                "result": {
                    "metadata": {"task_count": 1, "mode": "local"},
                    "schedule": [
                        {
                            "order_id": "DEMO_MC_ORDER",
                            "process_id": "DEMO_MC_P10",
                            "process_name": "精加工",
                            "machine_id": adjustment["assigned_machine_id"],
                            "worker_id": adjustment["assigned_worker_id"],
                            "plan_start_time": adjustment["plan_start_time"],
                            "plan_end_time": adjustment["plan_end_time"],
                        }
                    ],
                    "kpis": {},
                },
            }

        algorithm_client.execute = fake_local_execute
        try:
            executed = self.client.post(
                "/api/processes/DEMO_MC_P10/adjustments/execute",
                json={**preview_payload, "strategy": "move_only", "confirm_warnings": True},
                headers=self.headers,
            )
        finally:
            algorithm_client.execute = original_execute
        self.assertEqual(executed.status_code, 200, executed.text)
        result = executed.json()
        self.assertEqual(result["strategy"], "move_only")
        self.assertTrue(result["version_id"].startswith("PLAN-"))
        self.assertFalse(captured_payload["config_overrides"]["scheduling"]["local_include_downstream"])

        effective = self.client.get(
            "/api/effective-schedule?schedule_type=machining&order_id=DEMO_MC_ORDER",
            headers=self.headers,
        ).json()
        adjusted = effective["schedule"][0]
        self.assertEqual(adjusted["plan_start_time"], future_start.isoformat(timespec="seconds"))
        self.assertEqual(adjusted["plan_end_time"], future_end.isoformat(timespec="seconds"))
        self.assertEqual(adjusted["schedule_version_id"], result["version_id"])

    def test_zz_deployment_configuration_limits_orders_and_task_type(self) -> None:
        current = self.client.get("/api/system-configuration", headers=self.headers)
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json()["deployment_process_type"], "DEBUG")

        changed = self.client.put(
            "/api/system-configuration",
            json={
                "deployment_process_type": "MACHINING",
                "system_display_name": "机加智能排程",
                "deployment_locked": False,
                "change_reason": "自动化测试切换机加单元",
                "confirm_published_versions": True,
            },
            headers=self.headers,
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        orders = self.client.get("/api/master-data/order", headers=self.headers).json()
        self.assertEqual({row["payload"]["order_business_type"] for row in orders}, {"MACHINING"})
        defaults = self.client.get(
            "/api/tasks/defaults?schedule_type=heat_treatment", headers=self.headers
        )
        self.assertEqual(defaults.status_code, 200, defaults.text)
        self.assertEqual(defaults.json()["schedule_type"], "machining")

        restored = self.client.put(
            "/api/system-configuration",
            json={
                "deployment_process_type": "DEBUG",
                "system_display_name": "调试智能排程",
                "deployment_locked": False,
                "change_reason": "自动化测试恢复调试单元",
                "confirm_published_versions": True,
            },
            headers=self.headers,
        )
        self.assertEqual(restored.status_code, 200, restored.text)

    def test_virtual_resources_are_excluded_from_conflict_detection(self) -> None:
        rows = [
            {
                "process_id": "V1",
                "machine_id": "VIRTUAL_MACHINE",
                "worker_id": "VIRTUAL_WORKER",
                "plan_start_time": "2026-07-20T08:00:00",
                "plan_end_time": "2026-07-20T12:00:00",
                "virtual_resource": True,
            },
            {
                "process_id": "V2",
                "machine_id": "VIRTUAL_MACHINE",
                "worker_id": "VIRTUAL_WORKER",
                "plan_start_time": "2026-07-20T09:00:00",
                "plan_end_time": "2026-07-20T11:00:00",
                "virtual_resource": True,
            },
        ]
        self.assertEqual(_detect_schedule_conflicts(rows), [])


if __name__ == "__main__":
    unittest.main()
