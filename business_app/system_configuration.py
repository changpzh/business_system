"""业务系统部署工艺配置及切换保护。"""

from __future__ import annotations

import sqlite3
from typing import Any

from .constants import (
    AUDIT_ACTION_SYSTEM_CONFIGURATION_UPDATED,
    DEFAULT_SYSTEM_NAMES,
    DEPLOYMENT_PROCESS_TYPE_DEBUG,
    DEPLOYMENT_PROCESS_TYPES,
    SYSTEM_CONFIGURATION_ID,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    VERSION_STATUS_PUBLISHED,
)
from .database import audit, now_text
from .domain import allowed_schedule_types


CONFIG_ID = SYSTEM_CONFIGURATION_ID


def get_system_configuration(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM system_configuration WHERE config_id=?",
        (CONFIG_ID,),
    ).fetchone()
    if not row:
        stamp = now_text()
        connection.execute(
            """INSERT INTO system_configuration(
                   config_id,deployment_process_type,system_display_name,deployment_locked,updated_by,updated_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                CONFIG_ID,
                DEPLOYMENT_PROCESS_TYPE_DEBUG,
                DEFAULT_SYSTEM_NAMES[DEPLOYMENT_PROCESS_TYPE_DEBUG],
                0,
                "system",
                stamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM system_configuration WHERE config_id=?",
            (CONFIG_ID,),
        ).fetchone()
    result = dict(row)
    result["deployment_locked"] = bool(result.get("deployment_locked"))
    result["allowed_schedule_types"] = sorted(allowed_schedule_types(result["deployment_process_type"]))
    return result


def update_system_configuration(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    current = get_system_configuration(connection)
    deployment_type = str(
        payload.get("deployment_process_type", current["deployment_process_type"])
    ).strip().upper()
    if deployment_type not in DEPLOYMENT_PROCESS_TYPES:
        raise ValueError("部署工艺类型必须为 MACHINING、HEAT_TREATMENT、ASSEMBLY 或 DEBUG")

    display_name = str(
        payload.get("system_display_name") or DEFAULT_SYSTEM_NAMES[deployment_type]
    ).strip()
    if not display_name:
        raise ValueError("系统显示名称不能为空")
    if len(display_name) > 40:
        raise ValueError("系统显示名称不能超过 40 个字符")

    target_locked = bool(payload.get("deployment_locked", current["deployment_locked"]))
    reason = str(payload.get("change_reason") or "").strip()
    if not reason:
        raise ValueError("修改部署配置必须填写原因")

    changing = (
        deployment_type != current["deployment_process_type"]
        or display_name != current["system_display_name"]
        or target_locked != current["deployment_locked"]
    )
    if not changing:
        return current
    if current["deployment_locked"] and not bool(payload.get("confirm_unlock")):
        raise ValueError("部署配置已锁定，必须明确确认解锁后才能修改")

    deployment_changed = deployment_type != current["deployment_process_type"]
    if deployment_changed:
        running = connection.execute(
            "SELECT COUNT(*) AS count FROM schedule_tasks WHERE status IN (?,?)",
            (TASK_STATUS_QUEUED, TASK_STATUS_RUNNING),
        ).fetchone()["count"]
        if running:
            raise ValueError("存在排队中或运行中的排程任务，不能切换部署工艺")
        published = connection.execute(
            "SELECT COUNT(*) AS count FROM schedule_versions WHERE status=?",
            (VERSION_STATUS_PUBLISHED,),
        ).fetchone()["count"]
        if published and not bool(payload.get("confirm_published_versions")):
            raise ValueError("存在已发布计划，必须明确确认后才能切换部署工艺")

    stamp = now_text()
    connection.execute(
        """UPDATE system_configuration
              SET deployment_process_type=?,system_display_name=?,deployment_locked=?,updated_by=?,updated_at=?
            WHERE config_id=?""",
        (deployment_type, display_name, int(target_locked), actor, stamp, CONFIG_ID),
    )
    audit(
        connection,
        actor,
        AUDIT_ACTION_SYSTEM_CONFIGURATION_UPDATED,
        "system_configuration",
        CONFIG_ID,
        {
            "before": {
                "deployment_process_type": current["deployment_process_type"],
                "system_display_name": current["system_display_name"],
                "deployment_locked": current["deployment_locked"],
            },
            "after": {
                "deployment_process_type": deployment_type,
                "system_display_name": display_name,
                "deployment_locked": target_locked,
            },
            "change_reason": reason,
        },
    )
    return get_system_configuration(connection)
