from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from business_app.database import SCHEMA
from business_app.services import build_effective_schedule, execute_process_adjustment


class CriticalPathBackendTests(unittest.TestCase):
    def test_critical_local_adjustment_requires_explicit_override_authorization(self) -> None:
        preview = {
            "can_execute": True,
            "warnings": [],
            "critical_override_required": True,
            "recommended_strategy": "move_only",
        }
        with patch("business_app.services.preview_process_adjustment", return_value=preview):
            with self.assertRaisesRegex(ValueError, "必须明确授权"):
                execute_process_adjustment("P10", {}, "tester")

    def test_effective_schedule_keeps_process_metrics_and_paths(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        task_payload = json.dumps({}, ensure_ascii=False)
        connection.execute(
            """INSERT INTO schedule_tasks(
                   task_id,schedule_type,mode,dispatching_rule,status,request_json,snapshot_json,
                   created_by,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "TASK-1",
                "machining",
                "static",
                "DELIVERY",
                "SUCCEEDED",
                task_payload,
                task_payload,
                "tester",
                "2026-07-17T08:00:00",
            ),
        )
        result = {
            "schedule": [
                {
                    "process_id": "P10",
                    "process_name": "关键工序",
                    "order_id": "ORDER-1",
                    "status": "CONFIRMED",
                    "plan_start_time": "2026-07-17T08:00:00",
                    "plan_end_time": "2026-07-17T09:00:00",
                    "machine_id": "M1",
                    "worker_id": "W1",
                    "structural_critical": True,
                    "structural_path_ids": ["SP_ORDER-1_01"],
                    "structural_path_count": 1,
                    "delivery_critical": True,
                    "delivery_critical_level": "CORE",
                    "delivery_path_ids": ["DP_ORDER-1_01"],
                    "delivery_path_count": 1,
                    "delivery_total_slack_minutes": 0,
                    "resource_critical": True,
                    "resource_path_ids": ["RP_GLOBAL_01"],
                    "resource_path_count": 1,
                    "resource_total_slack_minutes": 0,
                    "theoretical_earliest_start_time": "2026-07-17T08:00:00",
                    "theoretical_earliest_finish_time": "2026-07-17T09:00:00",
                }
            ],
            "critical_path_analysis": {
                "enabled": True,
                "structural": {"enabled": True},
                "delivery": {"enabled": True},
                "resource": {"enabled": True},
                "all_paths": [
                    {
                        "path_id": "SP_ORDER-1_01",
                        "critical_type": "structural",
                        "order_id": "ORDER-1",
                        "rank": 1,
                        "process_ids": ["P10"],
                        "duration_minutes": 60,
                        "total_slack_minutes": 0,
                    },
                    {
                        "path_id": "DP_ORDER-1_01",
                        "critical_type": "delivery",
                        "order_id": "ORDER-1",
                        "rank": 1,
                        "process_ids": ["P10"],
                        "duration_minutes": 60,
                        "total_slack_minutes": 0,
                    },
                    {
                        "path_id": "RP_GLOBAL_01",
                        "critical_type": "resource",
                        "order_id": None,
                        "rank": 1,
                        "process_ids": ["P10"],
                        "duration_minutes": 60,
                        "total_slack_minutes": 0,
                    },
                ],
            },
        }
        connection.execute(
            """INSERT INTO schedule_versions(
                   version_id,version_no,task_id,schedule_type,status,result_json,created_by,
                   created_at,published_by,published_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "PLAN-1",
                1,
                "TASK-1",
                "machining",
                "PUBLISHED",
                json.dumps(result, ensure_ascii=False),
                "tester",
                "2026-07-17T08:00:00",
                "tester",
                "2026-07-17T08:10:00",
            ),
        )
        master_records = {
            ("resource_group", "RG1"): {
                "resource_group_id": "RG1",
                "resource_group_type": "MACHINING",
                "machines": [{"machine_id": "M1"}],
                "workers": [{"worker_id": "W1"}],
            },
            ("order", "ORDER-1"): {
                "order_id": "ORDER-1",
                "order_business_type": "MACHINING",
                "due_date": "2026-07-18",
                "processes": [
                    {
                        "process_id": "P10",
                        "process_name": "关键工序",
                        "status": "CONFIRMED",
                        "resource_group_id": "RG1",
                        "assigned_machine_id": "M1",
                        "assigned_worker_id": "W1",
                        "plan_start_time": "2026-07-17T08:00:00",
                        "plan_end_time": "2026-07-17T09:00:00",
                        "schedule_version_id": "PLAN-1",
                        "locks": {},
                    }
                ],
            },
        }
        for (entity_type, entity_id), payload in master_records.items():
            connection.execute(
                """INSERT INTO master_records(
                       entity_type,entity_id,payload_json,revision,updated_by,updated_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    entity_type,
                    entity_id,
                    json.dumps(payload, ensure_ascii=False),
                    1,
                    "tester",
                    "2026-07-17T08:00:00",
                ),
            )

        effective = build_effective_schedule(connection, schedule_type="machining")

        self.assertEqual(len(effective["schedule"]), 1)
        self.assertTrue(effective["schedule"][0]["structural_critical"])
        self.assertTrue(effective["schedule"][0]["delivery_critical"])
        self.assertTrue(effective["schedule"][0]["resource_critical"])
        self.assertEqual(
            effective["schedule"][0]["resource_path_ids"],
            ["RP_GLOBAL_01"],
        )
        self.assertTrue(effective["critical_path_analysis"]["enabled"])
        self.assertEqual(
            effective["critical_path_analysis"]["structural"]["representative_path_count"],
            1,
        )
        self.assertEqual(
            effective["critical_path_analysis"]["delivery"]["representative_path_count"],
            1,
        )
        self.assertEqual(
            effective["critical_path_analysis"]["resource"]["representative_path_count"],
            1,
        )
        self.assertEqual(
            effective["critical_path_analysis"]["all_paths"][0]["schedule_version_id"],
            "PLAN-1",
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
