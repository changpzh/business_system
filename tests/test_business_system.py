from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(TEMP_DIR.name) / "test.db")
os.environ["SESSION_SECRET"] = "test-secret"

from fastapi.testclient import TestClient

from business_app.algorithm_client import algorithm_client
from business_app.main import app
from business_app.services import (
    _detect_schedule_conflicts,
    _restore_batch_metadata_from_history,
    _sync_batch_metadata,
    compare_versions,
    ga_parameters_for_process_count,
    is_manually_locked,
    next_working_day_shift_start,
)


class BusinessSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        response = cls.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert response.status_code == 200, response.text
        cls.headers = {"Authorization": f"Bearer {response.json()['token']}"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)
        TEMP_DIR.cleanup()

    def test_health_dashboard_and_seed_data(self) -> None:
        self.assertEqual(self.client.get("/health").json()["status"], "UP")
        dashboard = self.client.get("/api/dashboard", headers=self.headers)
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.json()["master_counts"]["order"], 3)
        snapshot = self.client.get("/api/master-data/snapshot", headers=self.headers).json()
        self.assertEqual(len(snapshot["machine_profiles"]), 3)
        validation = self.client.get("/api/master-data/validate", headers=self.headers).json()
        self.assertTrue(validation["valid"], validation["errors"])

    def test_task_defaults_use_next_working_day_day_shift(self) -> None:
        response = self.client.get("/api/tasks/defaults", headers=self.headers)
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
        for label in (
            "交期优先(EDD)",
            "优先级优先(PRIORITY)",
            "最小松弛时间(SLACK)",
            "效率优先(EFFICIENCY)",
            "先到先服务(FCFS)",
        ):
            self.assertIn(label, app_js)

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
        self.assertIn("/assets/premium.css?v=20260715-1", index)
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
        task = self.client.get(f"/api/tasks/{task_id}", headers=self.headers).json()
        self.assertEqual(task["status"], "SUCCEEDED")
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
        self.assertFalse(version_detail["result"]["schedule"][0]["manually_locked"])
        self.assertEqual(version_detail["result"]["schedule"][0]["lock_details"], {})
        reviewed = self.client.post(
            f"/api/versions/{version_id}/review",
            json={"decision": "APPROVED", "comment": "测试审批"},
            headers=self.headers,
        )
        self.assertEqual(reviewed.json()["status"], "APPROVED")
        published = self.client.post(f"/api/versions/{version_id}/publish", headers=self.headers)
        self.assertEqual(published.json()["updated_processes"], 1)

        snapshot = self.client.get("/api/master-data/snapshot", headers=self.headers).json()
        order = next(item for item in snapshot["order_processes"] if item["order_id"] == "DEMO_MC_ORDER")
        self.assertEqual(order["processes"][0]["status"], "CONFIRMED")
        self.assertEqual(order["processes"][0]["schedule_version_id"], version_id)
        self.assertEqual(order["processes"][0]["locks"], {})

        published_detail = self.client.get(f"/api/versions/{version_id}", headers=self.headers).json()
        published_process = published_detail["result"]["schedule"][0]
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

    def test_z_single_operation_adjustment_preview_and_execute(self) -> None:
        preview_payload = {
            "plan_start_time": "2026-07-16T08:00:00",
            "plan_end_time": "2026-07-16T14:00:00",
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
            json={**preview_payload, "plan_start_time": "2026-07-13T08:00:00", "plan_end_time": "2026-07-13T14:00:00"},
            headers=self.headers,
        )
        self.assertEqual(past_preview.status_code, 200, past_preview.text)
        self.assertFalse(past_preview.json()["can_execute"])
        self.assertIn("F-07", {item["code"] for item in past_preview.json()["hard_errors"]})

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
        self.assertEqual(adjusted["plan_start_time"], "2026-07-16T08:00:00")
        self.assertEqual(adjusted["plan_end_time"], "2026-07-16T14:00:00")
        self.assertEqual(adjusted["schedule_version_id"], result["version_id"])


if __name__ == "__main__":
    unittest.main()
