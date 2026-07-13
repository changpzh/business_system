from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(TEMP_DIR.name) / "test.db")
os.environ["SESSION_SECRET"] = "test-secret"

from fastapi.testclient import TestClient

from business_app.algorithm_client import algorithm_client
from business_app.main import app


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
                json={"schedule_type": "machining", "mode": "static", "dispatching_rule": "DELIVERY"},
                headers=self.headers,
            )
        finally:
            algorithm_client.execute = original_execute
        self.assertEqual(created.status_code, 202)
        task_id = created.json()["task_id"]
        task = self.client.get(f"/api/tasks/{task_id}", headers=self.headers).json()
        self.assertEqual(task["status"], "SUCCEEDED")

        versions = self.client.get("/api/versions", headers=self.headers).json()
        version_id = next(item["version_id"] for item in versions if item["task_id"] == task_id)
        version_detail = self.client.get(f"/api/versions/{version_id}", headers=self.headers).json()
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


if __name__ == "__main__":
    unittest.main()
