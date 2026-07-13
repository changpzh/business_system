from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import BASE_DIR, settings
from .security import hash_password


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','planner','approver','viewer')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS master_records (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS schedule_tasks (
    task_id TEXT PRIMARY KEY,
    schedule_type TEXT NOT NULL,
    mode TEXT NOT NULL,
    dispatching_rule TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    response_json TEXT,
    error_message TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS schedule_versions (
    version_id TEXT PRIMARY KEY,
    version_no INTEGER NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    schedule_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('DRAFT','APPROVED','PUBLISHED','SUPERSEDED','REJECTED')),
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_comment TEXT,
    published_by TEXT,
    published_at TEXT,
    FOREIGN KEY(task_id) REFERENCES schedule_tasks(task_id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_created ON schedule_tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_versions_created ON schedule_versions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);
"""


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.database_path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def audit(connection: sqlite3.Connection, actor: str, action: str, target_type: str, target_id: str, detail: Any = None) -> None:
    connection.execute(
        "INSERT INTO audit_logs(actor,action,target_type,target_id,detail_json,created_at) VALUES(?,?,?,?,?,?)",
        (actor, action, target_type, target_id, json.dumps(detail or {}, ensure_ascii=False), now_text()),
    )


def initialize_database() -> None:
    with db() as connection:
        connection.executescript(SCHEMA)
        stamp = now_text()
        connection.execute(
            "INSERT OR IGNORE INTO users(username,display_name,password_hash,role,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("admin", "系统管理员", hash_password("admin123"), "admin", stamp, stamp),
        )
        running = connection.execute("SELECT task_id FROM schedule_tasks WHERE status IN ('QUEUED','RUNNING')").fetchall()
        for row in running:
            connection.execute(
                "UPDATE schedule_tasks SET status='FAILED', error_message=?, completed_at=? WHERE task_id=?",
                ("业务服务重启，后台任务执行状态已失效，请重试", stamp, row["task_id"]),
            )

        count = connection.execute("SELECT COUNT(*) AS count FROM master_records").fetchone()["count"]
        demo_path = BASE_DIR / "seed" / "demo_snapshot.json"
        if count == 0 and demo_path.exists():
            snapshot = json.loads(demo_path.read_text(encoding="utf-8"))
            mapping = {
                "machine_calendar": "calendar",
                "machine_profiles": "machine",
                "worker_profiles": "worker",
                "resource_group_profiles": "resource_group",
                "order_processes": "order",
            }
            for source, entity_type in mapping.items():
                records = snapshot[source] if isinstance(snapshot[source], list) else [snapshot[source]]
                id_field = {
                    "calendar": "calendar_id",
                    "machine": "machine_id",
                    "worker": "worker_id",
                    "resource_group": "resource_group_id",
                    "order": "order_id",
                }[entity_type]
                for record in records:
                    connection.execute(
                        "INSERT INTO master_records(entity_type,entity_id,payload_json,revision,updated_by,updated_at) VALUES(?,?,?,?,?,?)",
                        (entity_type, record[id_field], json.dumps(record, ensure_ascii=False), 1, "system", stamp),
                    )
            audit(connection, "system", "DEMO_DATA_INITIALIZED", "system", "master_data")
