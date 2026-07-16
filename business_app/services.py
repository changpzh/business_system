from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import uuid4

from .algorithm_client import AlgorithmClientError, algorithm_client
from .config import settings
from .constants import (
    ADJUSTMENT_CODE_BASE_CHANGEOVER,
    ADJUSTMENT_CODE_DOWNSTREAM_LOCK_CONFLICT,
    ADJUSTMENT_CODE_DUE_DATE_DELAY,
    ADJUSTMENT_CODE_DURATION_VALID,
    ADJUSTMENT_CODE_MACHINE_CONFLICT,
    ADJUSTMENT_CODE_MACHINE_LOCK_CONFLICT,
    ADJUSTMENT_CODE_MANUAL_LOCK,
    ADJUSTMENT_CODE_PAST_TIME,
    ADJUSTMENT_CODE_PREDECESSOR_LOCKED,
    ADJUSTMENT_CODE_PREDECESSOR_UNFINISHED,
    ADJUSTMENT_CODE_PREDECESSOR_VALID,
    ADJUSTMENT_CODE_PROCESS_CONSTRAINT,
    ADJUSTMENT_CODE_TOOLING_CHANGE,
    ADJUSTMENT_CODE_WORKER_CONFLICT,
    ADJUSTMENT_CODE_WORKER_LOCK_CONFLICT,
    CHANGE_TYPE_ADDED,
    CHANGE_TYPE_MODIFIED,
    CHANGE_TYPE_REMOVED,
    ADJUSTMENT_STRATEGIES,
    ADJUSTMENT_STRATEGY_LOCAL_RESCHEDULE,
    ADJUSTMENT_STRATEGY_MOVE_ONLY,
    ADJUSTMENT_STRATEGY_SYNC_DOWNSTREAM,
    AUDIT_ACTION_PROCESS_ADJUSTMENT_CONFIRMED,
    AUDIT_ACTION_PROCESS_MANUALLY_LOCKED,
    AUDIT_ACTION_PROCESS_MANUALLY_UNLOCKED,
    AUDIT_ACTION_TASK_CREATED,
    AUDIT_ACTION_TASK_FAILED,
    AUDIT_ACTION_TASK_SUCCEEDED,
    AUDIT_ACTION_VERSION_PUBLISHED,
    CONSTRAINT_CODE_PROCESS_DURATION_SHORTAGE,
    CONSTRAINT_CODE_RESOURCE_CALENDAR,
    CONSTRAINT_CODE_RESOURCE_CALENDAR_NO_WINDOW,
    CONSTRAINT_CODE_TIME_RANGE_INVALID,
    DISPATCH_RULE_DELIVERY,
    ORDER_BUSINESS_TYPES,
    PROCESS_MANUAL_ADJUSTMENT_BLOCKED_STATUSES,
    PROCESS_PUBLISHED_STATUSES,
    PROCESS_RUNTIME_LOCK_STATUSES,
    PROCESS_TERMINAL_STATUSES,
    PROCESS_STATUS_CANCELLED,
    PROCESS_STATUS_COMPLETED,
    PROCESS_STATUS_CONFIRMED,
    PROCESS_STATUS_PENDING,
    PROCESS_STATUS_SCHEDULED,
    RECORD_STATUS_ACTIVE,
    RESOURCE_GROUP_SCHEDULE_TYPES,
    RESOURCE_GROUP_TYPE_INSPECTION,
    RESOURCE_GROUP_TYPE_MANUAL,
    SCHEDULE_RECORD_STATUS_EFFECTIVE,
    SCHEDULE_RECORD_STATUS_HISTORICAL,
    SCHEDULE_RECORD_STATUS_UNKNOWN,
    SCHEDULE_RECORD_STATUS_UNSCHEDULED,
    SCHEDULE_MODE_LOCAL,
    SCHEDULE_MODE_STATIC,
    SCHEDULE_TASK_ID_PREFIX,
    SCHEDULE_TYPE_CHOICES,
    SCHEDULE_TYPE_MACHINING,
    SCHEDULE_VERSION_ID_PREFIX,
    TASK_RUNNABLE_STATUSES,
    TASK_STATUS_FAILED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCEEDED,
    VERSION_STATUS_APPROVED,
    VERSION_STATUS_DRAFT,
    VERSION_STATUS_PUBLISHED,
    VERSION_STATUS_SUPERSEDED,
    VERSION_REVIEW_DECISIONS,
    VERSION_AUDIT_ACTION_PREFIX,
)
from .database import audit, db, now_text
from .domain import (
    is_virtual_resource,
    normalize_order_business_type,
    order_business_types_for_schedule_type,
    resolve_task_schedule_type,
    schedule_type_for_order_business_type,
)
from .system_configuration import get_system_configuration


ENTITY_CONFIG = {
    "calendar": ("machine_calendar", "calendar_id"),
    "machine": ("machine_profiles", "machine_id"),
    "worker": ("worker_profiles", "worker_id"),
    "resource_group": ("resource_group_profiles", "resource_group_id"),
    "order": ("order_processes", "order_id"),
}

SCHEDULE_TYPES = SCHEDULE_TYPE_CHOICES

BATCH_METADATA_FIELDS = (
    "batch_id",
    "batch_merged",
    "batch_member_count",
    "batch_process_ids",
    "batch_order_ids",
    "batch_effective_volume",
    "batch_capacity_volume",
    "batch_merge_allowed",
)


def parse_json_columns(record: dict[str, Any] | None, *columns: str) -> dict[str, Any] | None:
    if not record:
        return record
    for column in columns:
        if record.get(column):
            record[column.removesuffix("_json")] = json.loads(record[column])
        record.pop(column, None)
    return record


def _calendar_schedule_type(calendar: dict[str, Any]) -> str | None:
    explicit = str(calendar.get("schedule_type") or "").lower()
    return explicit if explicit in SCHEDULE_TYPES else None


def select_calendar(
    connection: sqlite3.Connection,
    schedule_type: str = SCHEDULE_TYPE_MACHINING,
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT payload_json FROM master_records WHERE entity_type='calendar' ORDER BY entity_id"
    ).fetchall()
    calendars = [json.loads(row["payload_json"]) for row in rows]
    exact = next((item for item in calendars if _calendar_schedule_type(item) == schedule_type), None)
    return exact or {}


def build_snapshot(
    connection: sqlite3.Connection,
    schedule_type: str = SCHEDULE_TYPE_MACHINING,
    *,
    include_all_calendars: bool = False,
    filter_orders: bool = False,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "machine_calendar": {},
        "machine_profiles": [],
        "worker_profiles": [],
        "resource_group_profiles": [],
        "order_processes": [],
    }
    all_calendars: list[dict[str, Any]] = []
    for entity_type, (snapshot_key, _) in ENTITY_CONFIG.items():
        rows = connection.execute(
            "SELECT payload_json FROM master_records WHERE entity_type=? ORDER BY entity_id", (entity_type,)
        ).fetchall()
        values = [json.loads(row["payload_json"]) for row in rows]
        if entity_type == "calendar":
            all_calendars = values
            snapshot[snapshot_key] = select_calendar(connection, schedule_type)
        else:
            snapshot[snapshot_key] = values
    if filter_orders:
        allowed_order_types = order_business_types_for_schedule_type(schedule_type)
        snapshot["order_processes"] = [
            order
            for order in snapshot["order_processes"]
            if normalize_order_business_type(order.get("order_business_type")) in allowed_order_types
        ]
    if include_all_calendars:
        snapshot["machine_calendars"] = all_calendars
    return snapshot


def _schedule_allocation_key(record: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
    process_id = str(record.get("process_id") or "")
    machine_id = str(record.get("assigned_machine_id") or record.get("machine_id") or "")
    worker_id = str(record.get("assigned_worker_id") or record.get("worker_id") or "")
    plan_start_time = str(record.get("plan_start_time") or "")
    plan_end_time = str(record.get("plan_end_time") or "")
    if not process_id or not machine_id or not plan_start_time or not plan_end_time:
        return None
    return process_id, machine_id, worker_id, plan_start_time, plan_end_time


def _batch_metadata(record: dict[str, Any]) -> dict[str, Any]:
    if not record.get("batch_id"):
        return {}
    return {field: deepcopy(record[field]) for field in BATCH_METADATA_FIELDS if field in record}


def _sync_batch_metadata(process: dict[str, Any], scheduled: dict[str, Any]) -> None:
    """把计划版本中的合批身份写回工序；新计划已拆批时清除旧身份。"""
    metadata = _batch_metadata(scheduled)
    for field in BATCH_METADATA_FIELDS:
        if field in metadata:
            process[field] = metadata[field]
        else:
            process.pop(field, None)


def is_manually_locked(record: dict[str, Any] | None) -> bool:
    """locks 只保存人工锁，因此非空即表示人工锁定。"""
    return bool((record or {}).get("locks") or {})


def _validate_lock_payload(
    process: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    reason = str(payload.get("lock_reason") or "").strip()
    if not reason:
        raise ValueError("人工锁定必须填写锁定原因")
    if len(reason) > 500:
        raise ValueError("锁定原因不能超过 500 个字符")

    machine_id = str(payload.get("machine_id") or "").strip()
    worker_id = str(payload.get("worker_id") or "").strip()
    start_text = str(payload.get("start_time") or "").strip()
    end_text = str(payload.get("end_time") or "").strip()
    if bool(start_text) != bool(end_text):
        raise ValueError("锁定开始时间和结束时间必须同时填写")
    if not any((machine_id, worker_id, start_text, end_text)):
        raise ValueError("至少需要锁定设备、人员或计划时间")

    normalized: dict[str, Any] = {"lock_reason": reason}
    if machine_id:
        normalized["machine_id"] = machine_id
    if worker_id:
        normalized["worker_id"] = worker_id
    if start_text:
        try:
            start = datetime.fromisoformat(start_text)
            end = datetime.fromisoformat(end_text)
        except ValueError as exc:
            raise ValueError("锁定开始时间或结束时间格式无效") from exc
        if end <= start:
            raise ValueError("锁定结束时间必须晚于开始时间")
        normalized["start_time"] = start.isoformat(timespec="seconds")
        normalized["end_time"] = end.isoformat(timespec="seconds")
    return normalized


def _schedule_type_for_group(group: dict[str, Any]) -> str:
    return RESOURCE_GROUP_SCHEDULE_TYPES.get(
        str(group.get("resource_group_type") or "").upper(),
        SCHEDULE_TYPE_MACHINING,
    )


def _process_constraint_result(
    snapshot: dict[str, Any],
    schedule_type: str,
    process_id: str,
    machine_id: Any,
    worker_id: Any,
    start_time: Any = None,
    end_time: Any = None,
    *,
    require_complete_resources: bool = True,
    find_window_from: Any = None,
    duration_minutes: float | None = None,
) -> dict[str, Any]:
    request = {
        "schedule_type": schedule_type,
        "data_snapshot": snapshot,
        "process_id": process_id,
        "machine_id": machine_id or None,
        "worker_id": worker_id or None,
        "plan_start_time": start_time or None,
        "plan_end_time": end_time or None,
        "find_window_from": find_window_from or None,
        "duration_minutes": duration_minutes,
        "require_complete_resources": require_complete_resources,
    }
    try:
        result = algorithm_client.validate_process_constraints(request)
    except AlgorithmClientError as exc:
        raise ValueError(f"统一工艺约束服务不可用：{exc}") from exc
    if not isinstance(result, dict) or "valid" not in result:
        raise ValueError("统一工艺约束服务返回无效结果")
    return result


def _raise_constraint_errors(result: dict[str, Any], prefix: str = "") -> None:
    if result.get("valid"):
        return
    messages = [
        str(item.get("message") or item.get("code"))
        for item in result.get("issues", []) or []
        if item.get("severity", "hard") == "hard"
    ]
    raise ValueError(prefix + ("；".join(messages) or "工艺约束不满足"))


def lock_process(process_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """人工锁定订单工序，并记录操作人、时间和原因。"""
    with db() as connection:
        snapshot = build_snapshot(connection, include_all_calendars=True)
        group_index = {
            str(item.get("resource_group_id")): item
            for item in snapshot.get("resource_group_profiles", [])
        }
        order_rows = connection.execute(
            "SELECT * FROM master_records WHERE entity_type='order' ORDER BY entity_id"
        ).fetchall()
        for row in order_rows:
            order = json.loads(row["payload_json"])
            process = next(
                (item for item in order.get("processes", []) if str(item.get("process_id")) == process_id),
                None,
            )
            if not process:
                continue
            if str(process.get("status") or "").upper() in PROCESS_TERMINAL_STATUSES:
                raise ValueError("已完成或已取消工序不能人工锁定")
            expected_version = str(payload.get("schedule_version_id") or "")
            current_version = str(process.get("schedule_version_id") or "")
            if expected_version and expected_version != current_version:
                raise ValueError("计划版本已变化，请刷新甘特图后重新操作")
            current_lock_time = str((process.get("locks") or {}).get("lock_time") or "")
            if "expected_lock_time" in payload and str(payload.get("expected_lock_time") or "") != current_lock_time:
                raise ValueError("人工锁已变化，请刷新甘特图后重新操作")
            group = group_index.get(str(process.get("resource_group_id") or ""), {})
            normalized = _validate_lock_payload(process, payload)
            if is_virtual_resource(group) and (normalized.get("machine_id") or normalized.get("worker_id")):
                raise ValueError("虚拟工序人工锁只能锁定开始和结束时间，不能指定设备或人员")
            schedule_type = _schedule_type_for_group(group)
            snapshot["machine_calendar"] = select_calendar(connection, schedule_type)
            snapshot.pop("machine_calendars", None)
            resolved_machine_id = normalized.get("machine_id") or process.get("assigned_machine_id")
            resolved_worker_id = normalized.get("worker_id") or process.get("assigned_worker_id")
            constraint_result = _process_constraint_result(
                snapshot,
                schedule_type,
                process_id,
                resolved_machine_id,
                resolved_worker_id,
                normalized.get("start_time"),
                normalized.get("end_time"),
                require_complete_resources=False,
            )
            _raise_constraint_errors(constraint_result, "人工锁校验未通过：")
            stamp = now_text()
            locks = {
                **{key: value for key, value in normalized.items() if key != "lock_reason"},
                "lock_time": stamp,
                "operator": actor,
                "lock_reason": normalized["lock_reason"],
            }

            for other_order in snapshot.get("order_processes", []):
                for other in other_order.get("processes", []):
                    if str(other.get("process_id")) == process_id:
                        continue
                    other_locks = other.get("locks") or {}
                    if not other_locks or not locks.get("start_time") or not other_locks.get("start_time"):
                        continue
                    shares_resource = (
                        locks.get("machine_id")
                        and locks.get("machine_id") == other_locks.get("machine_id")
                    ) or (
                        locks.get("worker_id")
                        and locks.get("worker_id") == other_locks.get("worker_id")
                    )
                    if not shares_resource:
                        continue
                    start = datetime.fromisoformat(locks["start_time"])
                    end = datetime.fromisoformat(locks["end_time"])
                    other_start = datetime.fromisoformat(str(other_locks["start_time"]))
                    other_end = datetime.fromisoformat(str(other_locks["end_time"]))
                    same_batch = process.get("batch_id") and process.get("batch_id") == other.get("batch_id")
                    if not same_batch and start < other_end and other_start < end:
                        raise ValueError(f"人工锁定与工序 {other.get('process_id')} 的人工资源锁冲突")

            previous = deepcopy(process.get("locks") or {})
            process["locks"] = locks
            connection.execute(
                "UPDATE master_records SET payload_json=?,revision=revision+1,updated_by=?,updated_at=? "
                "WHERE entity_type='order' AND entity_id=?",
                (json.dumps(order, ensure_ascii=False), actor, stamp, row["entity_id"]),
            )
            audit(
                connection,
                actor,
                AUDIT_ACTION_PROCESS_MANUALLY_LOCKED,
                "process",
                process_id,
                {"before": previous, "after": locks, "order_id": order.get("order_id")},
            )
            return {"process_id": process_id, "order_id": order.get("order_id"), "locks": locks}
    raise KeyError("工序不存在")


def unlock_process(process_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """人工解锁订单工序；解锁原因必填并进入审计日志。"""
    reason = str(payload.get("unlock_reason") or "").strip()
    if not reason:
        raise ValueError("人工解锁必须填写解锁原因")
    if len(reason) > 500:
        raise ValueError("解锁原因不能超过 500 个字符")
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM master_records WHERE entity_type='order' ORDER BY entity_id"
        ).fetchall()
        for row in rows:
            order = json.loads(row["payload_json"])
            process = next(
                (item for item in order.get("processes", []) if str(item.get("process_id")) == process_id),
                None,
            )
            if not process:
                continue
            expected_version = str(payload.get("schedule_version_id") or "")
            current_version = str(process.get("schedule_version_id") or "")
            if expected_version and expected_version != current_version:
                raise ValueError("计划版本已变化，请刷新甘特图后重新操作")
            previous = deepcopy(process.get("locks") or {})
            if not previous:
                raise ValueError("当前工序没有人工锁")
            if "expected_lock_time" in payload and str(payload.get("expected_lock_time") or "") != str(
                previous.get("lock_time") or ""
            ):
                raise ValueError("人工锁已变化，请刷新甘特图后重新操作")
            stamp = now_text()
            process["locks"] = {}
            connection.execute(
                "UPDATE master_records SET payload_json=?,revision=revision+1,updated_by=?,updated_at=? "
                "WHERE entity_type='order' AND entity_id=?",
                (json.dumps(order, ensure_ascii=False), actor, stamp, row["entity_id"]),
            )
            audit(
                connection,
                actor,
                AUDIT_ACTION_PROCESS_MANUALLY_UNLOCKED,
                "process",
                process_id,
                {"before": previous, "unlock_reason": reason, "order_id": order.get("order_id")},
            )
            return {"process_id": process_id, "order_id": order.get("order_id"), "locks": {}}
    raise KeyError("工序不存在")


def _parse_adjustment_time(value: Any, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是有效的 ISO 日期时间") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _intervals_overlap(left_start: datetime, left_end: datetime, right_start: datetime, right_end: datetime) -> bool:
    return left_start < right_end and right_start < left_end


def _calendar_day_shifts(calendar: dict[str, Any], target_day: date) -> list[dict[str, Any]]:
    """返回指定日期的配置班次，并优先应用特殊日期覆盖规则。"""
    special = (calendar.get("special_shifts") or {}).get(target_day.isoformat())
    if special is not None:
        shifts = special if isinstance(special, list) else special.get("shifts", [special])
    else:
        shifts = (calendar.get("weekly_shifts") or {}).get(str((target_day.weekday() + 1) % 7), [])
    return [shift for shift in shifts or [] if isinstance(shift, dict)]


def next_working_day_shift_start(
    calendar: dict[str, Any],
    current_time: datetime | None = None,
    horizon_days: int = 366,
) -> datetime:
    """根据工厂日历查找下一个工作日的白班开始时间。"""
    now = current_time or datetime.now()
    target_day = now.date() + timedelta(days=1)
    for _ in range(max(horizon_days, 1)):
        shifts = _calendar_day_shifts(calendar, target_day)
        preferred_start = calendar.get("day_shift_start")
        if shifts and preferred_start:
            return datetime.combine(
                target_day,
                datetime.strptime(str(preferred_start), "%H:%M").time(),
            )
        day_shifts = [
            shift
            for shift in shifts
            if "白班" in str(shift.get("name") or "")
            or "day" in str(shift.get("name") or "").lower()
        ]
        candidates = day_shifts or shifts
        starts = [
            datetime.combine(target_day, datetime.strptime(str(segment["start"]), "%H:%M").time())
            for shift in candidates
            for segment in shift.get("segments", []) or []
            if segment.get("start")
        ]
        if starts:
            return min(starts)
        target_day += timedelta(days=1)
    raise ValueError("工厂日历未来一年内没有可用白班")


def count_schedule_processes(connection: sqlite3.Connection, schedule_type: str) -> int:
    """按订单业务归属统计全部有效工序，用于动态遗传参数。"""
    order_business_types = sorted(order_business_types_for_schedule_type(schedule_type))
    if not order_business_types:
        return 0
    placeholders = ",".join("?" for _ in order_business_types)
    row = connection.execute(
        f"""SELECT COUNT(*) AS process_count
            FROM master_records AS orders
            CROSS JOIN json_each(orders.payload_json, '$.processes') AS process
           WHERE orders.entity_type='order'
             AND UPPER(COALESCE(json_extract(orders.payload_json, '$.order_business_type'), ''))
                 IN ({placeholders})
             AND UPPER(COALESCE(json_extract(process.value, '$.status'), ?))
                 NOT IN (?, ?)""",
        [
            *order_business_types,
            PROCESS_STATUS_PENDING,
            PROCESS_STATUS_COMPLETED,
            PROCESS_STATUS_CANCELLED,
        ],
    ).fetchone()
    return int(row["process_count"] or 0) if row else 0


def ga_parameters_for_process_count(process_count: int) -> dict[str, int]:
    """根据工序规模返回对应的生产环境遗传算法参数。"""
    count = max(int(process_count or 0), 0)
    if count <= 100:
        population_size, generations = 56, 30
    elif count <= 500:
        population_size, generations = 48, 20
    elif count <= 1000:
        population_size, generations = 40, 15
    elif count < 5000:
        population_size, generations = 32, 10
    else:
        population_size, generations = 24, 4
    return {
        "process_count": count,
        "population_size": population_size,
        "generations": generations,
    }


def _process_adjustment_indexes(snapshot: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    process_index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for order in snapshot.get("order_processes", []):
        for process in order.get("processes", []):
            if process.get("process_id"):
                process_index[str(process["process_id"])] = (order, process)
    machine_index = {
        str(item.get("machine_id")): item for item in snapshot.get("machine_profiles", []) if item.get("machine_id")
    }
    worker_index = {
        str(item.get("worker_id")): item for item in snapshot.get("worker_profiles", []) if item.get("worker_id")
    }
    group_index = {
        str(item.get("resource_group_id")): item
        for item in snapshot.get("resource_group_profiles", [])
        if item.get("resource_group_id")
    }
    return process_index, machine_index, worker_index, group_index


def _adjustment_issue(code: str, title: str, message: str, issue_type: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "title": title, "message": message, "type": issue_type, **details}


def preview_process_adjustment(process_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """预校验一次单工序时间/设备/人员调整，并返回确认弹窗所需的影响分析。"""
    with db() as connection:
        effective = build_effective_schedule(connection)
        current = next((item for item in effective.get("schedule", []) if str(item.get("process_id")) == process_id), None)
        snapshot = build_snapshot(
            connection,
            str((current or {}).get("schedule_type") or SCHEDULE_TYPE_MACHINING),
        )
    process_index, machine_index, worker_index, group_index = _process_adjustment_indexes(snapshot)
    record = process_index.get(process_id)
    if not record or not current:
        raise KeyError("工序不存在或尚未进入生效排程")
    order, process = record

    expected_version = str(payload.get("schedule_version_id") or "")
    if expected_version and expected_version != str(current.get("schedule_version_id") or ""):
        raise ValueError("计划版本已变化，请刷新甘特图后重新操作")

    current_start = _parse_adjustment_time(current.get("plan_start_time"), "当前计划开始时间")
    current_end = _parse_adjustment_time(current.get("plan_end_time"), "当前计划完工时间")
    new_start = _parse_adjustment_time(payload.get("plan_start_time"), "新计划开始时间")
    new_end = _parse_adjustment_time(payload.get("plan_end_time"), "新计划完工时间")
    machine_id = str(payload.get("assigned_machine_id") or current.get("machine_id") or "")
    worker_id = str(payload.get("assigned_worker_id") or current.get("worker_id") or "")
    group = group_index.get(str(process.get("resource_group_id") or ""), {})
    virtual_resource = is_virtual_resource(group)
    machine = machine_index.get(machine_id, {})
    worker = worker_index.get(worker_id, {})
    now = datetime.now()

    hard_errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    constraint_result = _process_constraint_result(
        snapshot,
        str(current.get("schedule_type") or _schedule_type_for_group(group)),
        process_id,
        machine_id,
        worker_id,
        new_start.isoformat(timespec="seconds"),
        new_end.isoformat(timespec="seconds"),
        require_complete_resources=True,
    )
    required_minutes = float(constraint_result.get("required_minutes") or 0)
    for issue in constraint_result.get("issues", []) or []:
        target = warnings if issue.get("severity") == "warning" else hard_errors
        target.append(
            _adjustment_issue(
                str(issue.get("code") or ADJUSTMENT_CODE_PROCESS_CONSTRAINT),
                str(issue.get("title") or "工艺约束"),
                str(issue.get("message") or "工艺约束不满足"),
                "warning" if issue.get("severity") == "warning" else "hard",
            )
        )
    new_minutes = max((new_end - new_start).total_seconds() / 60.0, 0)

    if current.get("manually_locked"):
        hard_errors.append(
            _adjustment_issue(
                ADJUSTMENT_CODE_MANUAL_LOCK,
                "任务已锁定",
                "当前工序已人工锁定，请先解锁后再调整",
                "hard",
            )
        )
    if not any(
        item["code"]
        in {
            CONSTRAINT_CODE_TIME_RANGE_INVALID,
            CONSTRAINT_CODE_PROCESS_DURATION_SHORTAGE,
            CONSTRAINT_CODE_RESOURCE_CALENDAR,
        }
        for item in hard_errors
    ):
        checks.append(
            _adjustment_issue(
                ADJUSTMENT_CODE_DURATION_VALID,
                "工时充足",
                "目标时段满足标准工时",
                "pass",
            )
        )

    earliest_start = now
    for boundary in (process.get("material_ready_time"), order.get("release_date")):
        if boundary:
            earliest_start = max(earliest_start, _parse_adjustment_time(boundary, "工序最早可开工时间"))
    if new_start <= now:
        hard_errors.append(
            _adjustment_issue(
                ADJUSTMENT_CODE_PAST_TIME,
                "时间已过",
                "不能将任务提前到已过去的时间",
                "hard",
            )
        )

    explicit_predecessors = {str(value) for value in process.get("previous_process_ids", []) or []}
    if not explicit_predecessors:
        sequence = float(process.get("sequence") or 0)
        previous = [
            item
            for item in order.get("processes", [])
            if float(item.get("sequence") or 0) < sequence
        ]
        if previous:
            explicit_predecessors = {str(max(previous, key=lambda item: float(item.get("sequence") or 0))["process_id"])}
    predecessor_details: list[dict[str, Any]] = []
    for predecessor_id in sorted(explicit_predecessors):
        predecessor_row = next(
            (item for item in effective.get("processes", []) if str(item.get("process_id")) == predecessor_id),
            None,
        )
        predecessor_record = process_index.get(predecessor_id)
        if not predecessor_row or not predecessor_row.get("plan_end_time"):
            continue
        predecessor_end = _parse_adjustment_time(predecessor_row["plan_end_time"], "前序完工时间")
        earliest_start = max(earliest_start, predecessor_end)
        locked = bool(predecessor_row.get("manually_locked"))
        predecessor_details.append(
            {
                "process_id": predecessor_id,
                "process_name": predecessor_row.get("process_name") or "",
                "plan_end_time": predecessor_end.isoformat(timespec="seconds"),
                "locked": locked,
            }
        )
        if new_start < predecessor_end:
            code = (
                ADJUSTMENT_CODE_PREDECESSOR_LOCKED
                if locked or bool((predecessor_record or ({}, {}))[1].get("locks") or {})
                else ADJUSTMENT_CODE_PREDECESSOR_UNFINISHED
            )
            hard_errors.append(
                _adjustment_issue(
                    code,
                    "前序工序未完工"
                    if code == ADJUSTMENT_CODE_PREDECESSOR_UNFINISHED
                    else "前序工序已锁定",
                    f"前序工序 {predecessor_id} 预计 {predecessor_end.isoformat(timespec='minutes')} 完工，最早可开工时间为该时刻",
                    "hard",
                    process_id=predecessor_id,
                    earliest_start=predecessor_end.isoformat(timespec="seconds"),
                )
            )
    if not any(
        item["code"]
        in {ADJUSTMENT_CODE_PREDECESSOR_UNFINISHED, ADJUSTMENT_CODE_PREDECESSOR_LOCKED}
        for item in hard_errors
    ):
        checks.append(
            _adjustment_issue(
                ADJUSTMENT_CODE_PREDECESSOR_VALID,
                "前序约束",
                "新开工时间满足前序完工约束",
                "pass",
            )
        )

    requirements = process.get("resource_requirements", {}) or {}

    displaced: list[dict[str, Any]] = []
    for other in effective.get("schedule", []):
        if str(other.get("process_id")) == process_id or not other.get("plan_start_time") or not other.get("plan_end_time"):
            continue
        if virtual_resource or bool(other.get("virtual_resource")):
            continue
        same_batch = current.get("batch_id") and current.get("batch_id") == other.get("batch_id")
        if same_batch:
            continue
        shares_machine = machine_id and machine_id == str(other.get("machine_id") or "")
        shares_worker = worker_id and worker_id == str(other.get("worker_id") or "")
        if not (shares_machine or shares_worker):
            continue
        other_start = _parse_adjustment_time(other["plan_start_time"], "占用任务开始时间")
        other_end = _parse_adjustment_time(other["plan_end_time"], "占用任务完工时间")
        if not _intervals_overlap(new_start, new_end, other_start, other_end):
            continue
        is_locked = (
            bool(other.get("manually_locked"))
            or str(other.get("status") or "").upper() in PROCESS_RUNTIME_LOCK_STATUSES
        )
        resource_types = [name for name, hit in (("machine", shares_machine), ("worker", shares_worker)) if hit]
        detail = {
            "process_id": other.get("process_id"),
            "process_name": other.get("process_name") or "",
            "order_id": other.get("order_id") or "",
            "plan_start_time": other.get("plan_start_time"),
            "plan_end_time": other.get("plan_end_time"),
            "locked": is_locked,
            "resource_types": resource_types,
        }
        displaced.append(detail)
        if is_locked:
            code = (
                ADJUSTMENT_CODE_MACHINE_LOCK_CONFLICT
                if shares_machine
                else ADJUSTMENT_CODE_WORKER_LOCK_CONFLICT
            )
            hard_errors.append(
                _adjustment_issue(
                    code,
                    "目标时段被锁定任务占用",
                    f"时段已被锁定任务 {other.get('process_id')} 占用，不可覆盖",
                    "hard",
                    process_id=other.get("process_id"),
                )
            )
        else:
            code = (
                ADJUSTMENT_CODE_MACHINE_CONFLICT
                if shares_machine
                else ADJUSTMENT_CODE_WORKER_CONFLICT
            )
            warnings.append(
                _adjustment_issue(
                    code,
                    "目标时段已被占用",
                    f"时段被 {other.get('process_id')} 占用（未锁定），确认后将重新排产",
                    "warning",
                    process_id=other.get("process_id"),
                )
            )

    downstream: list[dict[str, Any]] = []
    process_sequence = float(process.get("sequence") or 0)
    for downstream_process in sorted(order.get("processes", []), key=lambda item: float(item.get("sequence") or 0)):
        downstream_id = str(downstream_process.get("process_id") or "")
        predecessors = {str(value) for value in downstream_process.get("previous_process_ids", []) or []}
        if downstream_id == process_id or not (
            process_id in predecessors or float(downstream_process.get("sequence") or 0) > process_sequence
        ):
            continue
        downstream_row = next(
            (item for item in effective.get("processes", []) if str(item.get("process_id")) == downstream_id),
            None,
        )
        if not downstream_row or not downstream_row.get("plan_start_time"):
            continue
        locked = bool(downstream_row.get("manually_locked"))
        downstream_start = _parse_adjustment_time(downstream_row["plan_start_time"], "后续开工时间")
        downstream.append(
            {
                "process_id": downstream_id,
                "process_name": downstream_row.get("process_name") or "",
                "plan_start_time": downstream_row.get("plan_start_time"),
                "plan_end_time": downstream_row.get("plan_end_time"),
                "locked": locked,
            }
        )
        if locked and new_end > downstream_start:
            hard_errors.append(
                _adjustment_issue(
                    ADJUSTMENT_CODE_DOWNSTREAM_LOCK_CONFLICT,
                    "冲击后续锁定任务",
                    f"新完工时间晚于后续锁定任务 {downstream_id} 的开工时间，后续无法顺延",
                    "hard",
                    process_id=downstream_id,
                )
            )

    machine_changed = machine_id != str(current.get("machine_id") or "")
    worker_changed = worker_id != str(current.get("worker_id") or "")
    changeover: dict[str, Any] = {
        "machine_changed": machine_changed,
        "tooling_change_minutes": 0.0,
        "base_changeover_minutes": 0.0,
        "total_minutes": 0.0,
        "details": [],
    }
    if machine_changed and not virtual_resource:
        required_tooling = (
            requirements.get("tooling_id")
            or process.get("required_tooling_id")
            or process.get("tooling_id")
        )
        current_tooling = machine_index.get(str(current.get("machine_id") or ""), {}).get("current_tooling_id")
        target_tooling = machine.get("current_tooling_id")
        if required_tooling and target_tooling != required_tooling and current_tooling != target_tooling:
            tooling_minutes = float(
                machine.get("tooling_change_minutes")
                or group.get("tooling_change_minutes")
                or process.get("tooling_change_minutes")
                or 30
            )
            changeover["tooling_change_minutes"] = tooling_minutes
            changeover["details"].append(f"更换工装 {target_tooling or '-'} → {required_tooling}")
            warnings.append(
                _adjustment_issue(
                    ADJUSTMENT_CODE_TOOLING_CHANGE,
                    "工装需要更换",
                    f"需要更换工装，预计额外换产 {tooling_minutes:g} 分钟",
                    "warning",
                )
            )
        base_minutes = float(
            machine.get("base_changeover_minutes")
            or machine.get("changeover_minutes")
            or group.get("base_changeover_minutes")
            or 25
        )
        changeover["base_changeover_minutes"] = base_minutes
        changeover["details"].append("程序加载与首件检验")
        warnings.append(
            _adjustment_issue(
                ADJUSTMENT_CODE_BASE_CHANGEOVER,
                "基础换产",
                f"切换设备需要程序加载和首件检验，预计 {base_minutes:g} 分钟",
                "warning",
            )
        )
    changeover["total_minutes"] = changeover["tooling_change_minutes"] + changeover["base_changeover_minutes"]

    delta_minutes = (new_start - current_start).total_seconds() / 60.0
    current_order_end = max(
        (
            _parse_adjustment_time(item["plan_end_time"], "订单完工时间")
            for item in effective.get("schedule", [])
            if str(item.get("order_id") or "") == str(order.get("order_id") or "") and item.get("plan_end_time")
        ),
        default=current_end,
    )
    projected_order_end = current_order_end + timedelta(minutes=delta_minutes)
    due_date_value = order.get("due_date")
    due_warning = None
    if due_date_value:
        due_date = datetime.combine(date.fromisoformat(str(due_date_value)[:10]), time.max)
        if projected_order_end > due_date:
            delay_days = max((projected_order_end - due_date).total_seconds() / 86400.0, 0)
            due_warning = {
                "due_date": due_date_value,
                "projected_order_end": projected_order_end.isoformat(timespec="seconds"),
                "delay_days": round(delay_days, 2),
            }
            warnings.append(
                _adjustment_issue(
                    ADJUSTMENT_CODE_DUE_DATE_DELAY,
                    "订单交付延期",
                    f"订单预计延期 {delay_days:.1f} 天交付",
                    "warning",
                    delay_days=round(delay_days, 2),
                )
            )

    if machine_changed and not virtual_resource:
        operation = "machine_change"
    elif worker_changed and not virtual_resource:
        operation = "worker_change"
    elif delta_minutes < 0:
        operation = "move_forward"
    elif delta_minutes > 0:
        operation = "move_backward"
    else:
        operation = "time_adjustment"

    if displaced:
        recommended_strategy = ADJUSTMENT_STRATEGY_LOCAL_RESCHEDULE
    elif downstream and delta_minutes != 0:
        recommended_strategy = ADJUSTMENT_STRATEGY_SYNC_DOWNSTREAM
    else:
        recommended_strategy = ADJUSTMENT_STRATEGY_MOVE_ONLY
    options = [
        {
            "value": ADJUSTMENT_STRATEGY_MOVE_ONLY,
            "label": f"仅移动 {process_id}",
            "description": "保留后续工序原计划；发生占用时仍会重排被挤任务",
            "recommended": recommended_strategy == ADJUSTMENT_STRATEGY_MOVE_ONLY,
        },
        {
            "value": ADJUSTMENT_STRATEGY_SYNC_DOWNSTREAM,
            "label": "同步调整后续工序",
            "description": "后续工序跟随新的前序边界重新排产",
            "recommended": recommended_strategy == ADJUSTMENT_STRATEGY_SYNC_DOWNSTREAM,
        },
        {
            "value": ADJUSTMENT_STRATEGY_LOCAL_RESCHEDULE,
            "label": "局部重排受影响区域",
            "description": "重排目标、被挤任务和后续工序，其他任务保持固定",
            "recommended": recommended_strategy == ADJUSTMENT_STRATEGY_LOCAL_RESCHEDULE,
        },
    ]

    snap_boundary = earliest_start.replace(second=0, microsecond=0)
    if snap_boundary < earliest_start:
        snap_boundary += timedelta(minutes=1)
    suggestion_result = _process_constraint_result(
        snapshot,
        str(current.get("schedule_type") or _schedule_type_for_group(group)),
        process_id,
        machine_id,
        worker_id,
        require_complete_resources=True,
        find_window_from=snap_boundary.isoformat(timespec="seconds"),
        duration_minutes=required_minutes,
    )
    if not suggestion_result.get("valid"):
        for issue in suggestion_result.get("issues", []) or []:
            if issue.get("severity", "hard") != "hard":
                continue
            if any(item.get("code") == issue.get("code") for item in hard_errors):
                continue
            hard_errors.append(
                _adjustment_issue(
                    str(issue.get("code") or CONSTRAINT_CODE_RESOURCE_CALENDAR_NO_WINDOW),
                    str(issue.get("title") or "无可用日历窗口"),
                    str(issue.get("message") or "找不到可用日历窗口"),
                    "hard",
                )
            )
    return {
        "process_id": process_id,
        "order_id": order.get("order_id") or "",
        "process_name": current.get("process_name") or process.get("process_name") or "",
        "schedule_type": current.get("schedule_type") or SCHEDULE_TYPE_MACHINING,
        "virtual_resource": virtual_resource,
        "operation": operation,
        "can_execute": not hard_errors,
        "requires_confirmation": bool(warnings or hard_errors or machine_changed or worker_changed or delta_minutes),
        "current": {
            "plan_start_time": current_start.isoformat(timespec="seconds"),
            "plan_end_time": current_end.isoformat(timespec="seconds"),
            "machine_id": current.get("machine_id") or "",
            "worker_id": current.get("worker_id") or "",
            "schedule_version_id": current.get("schedule_version_id") or "",
        },
        "target": {
            "plan_start_time": new_start.isoformat(timespec="seconds"),
            "plan_end_time": new_end.isoformat(timespec="seconds"),
            "machine_id": machine_id,
            "machine_name": machine.get("machine_name") or "",
            "worker_id": worker_id,
            "worker_name": worker.get("worker_name") or "",
            "duration_minutes": new_minutes,
            "delta_minutes": delta_minutes,
        },
        "checks": checks,
        "hard_errors": hard_errors,
        "warnings": warnings,
        "predecessors": predecessor_details,
        "downstream": downstream,
        "displaced_tasks": displaced,
        "changeover": changeover,
        "delivery_impact": due_warning,
        "recommended_strategy": recommended_strategy,
        "options": options,
        "snap_suggestion": suggestion_result.get("suggested_window"),
        "authorized_replan_process_ids": sorted(
            {
                str(item.get("process_id"))
                for item in displaced + downstream
                if item.get("process_id") and not item.get("locked")
            }
        ),
    }


def execute_process_adjustment(process_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """同步执行已确认的单工序调整，并将局部排程版本直接发布为生效计划。"""
    preview = preview_process_adjustment(process_id, payload)
    if not preview["can_execute"]:
        messages = "；".join(item["message"] for item in preview["hard_errors"])
        raise ValueError(messages or "单工序调整未通过校验")
    if preview["warnings"] and not payload.get("confirm_warnings"):
        raise ValueError("存在可覆盖警告，请确认影响范围后再执行")

    strategy = str(payload.get("strategy") or preview["recommended_strategy"])
    if strategy not in ADJUSTMENT_STRATEGIES:
        raise ValueError("未知的局部调整方案")
    include_downstream = strategy != ADJUSTMENT_STRATEGY_MOVE_ONLY
    authorized_ids = {
        str(item["process_id"])
        for item in preview.get("displaced_tasks", [])
        if item.get("process_id") and not item.get("locked")
    }
    if include_downstream:
        authorized_ids.update(
            str(item["process_id"])
            for item in preview.get("downstream", [])
            if item.get("process_id") and not item.get("locked")
        )

    local_adjustment = {
        "process_id": process_id,
        "plan_start_time": preview["target"]["plan_start_time"],
        "plan_end_time": preview["target"]["plan_end_time"],
        "local_adjustment_authorized": True,
    }
    if not preview.get("virtual_resource"):
        local_adjustment["assigned_machine_id"] = preview["target"]["machine_id"]
        local_adjustment["assigned_worker_id"] = preview["target"]["worker_id"]

    task_data = {
        "schedule_type": preview.get("schedule_type") or SCHEDULE_TYPE_MACHINING,
        "mode": SCHEDULE_MODE_LOCAL,
        "dispatching_rule": str(payload.get("dispatching_rule") or DISPATCH_RULE_DELIVERY),
        "schedule_start": datetime.now().isoformat(timespec="seconds"),
        "config_overrides": {
            "scheduling": {
                "local_include_conflicts": True,
                "local_include_downstream": include_downstream,
                "local_authorized_process_ids": sorted(authorized_ids),
            }
        },
        "local_adjustments": [local_adjustment],
    }
    task_id = create_task(task_data, actor)
    with db() as connection:
        task = connection.execute("SELECT request_json FROM schedule_tasks WHERE task_id=?", (task_id,)).fetchone()
        connection.execute(
            "UPDATE schedule_tasks SET status=?,started_at=?,error_message=NULL WHERE task_id=?",
            (TASK_STATUS_RUNNING, now_text(), task_id),
        )
        request_payload = json.loads(task["request_json"])
    response = algorithm_client.execute(request_payload)
    if response.get("status") != TASK_STATUS_SUCCEEDED:
        error = response.get("error") or {}
        message = error.get("message") or "局部排程执行失败"
        with db() as connection:
            connection.execute(
                "UPDATE schedule_tasks SET status=?,response_json=?,error_message=?,completed_at=? WHERE task_id=?",
                (
                    TASK_STATUS_FAILED,
                    json.dumps(response, ensure_ascii=False),
                    message,
                    now_text(),
                    task_id,
                ),
            )
            audit(connection, "algorithm", AUDIT_ACTION_TASK_FAILED, "schedule_task", task_id, error)
        raise ValueError(message)

    version_id = save_task_result(task_id, response, actor)
    stamp = now_text()
    review_schedule_version(
        version_id,
        VERSION_STATUS_APPROVED,
        actor,
        f"单工序调整确认：{process_id} / {strategy}",
    )
    with db() as connection:
        audit(
            connection,
            actor,
            AUDIT_ACTION_PROCESS_ADJUSTMENT_CONFIRMED,
            "process",
            process_id,
            {
                "task_id": task_id,
                "version_id": version_id,
                "strategy": strategy,
                "current": preview["current"],
                "target": preview["target"],
                "affected_process_ids": sorted(authorized_ids),
            },
        )
    updated = publish_version(version_id, actor)

    lock_warning = ""
    if payload.get("lock_after_adjustment"):
        try:
            lock_process(
                process_id,
                {
                    "machine_id": preview["target"]["machine_id"],
                    "worker_id": preview["target"]["worker_id"],
                    "start_time": preview["target"]["plan_start_time"],
                    "end_time": preview["target"]["plan_end_time"],
                    "lock_reason": str(payload.get("lock_reason") or "单工序调整确认后锁定"),
                    "schedule_version_id": version_id,
                    "expected_lock_time": "",
                },
                actor,
            )
        except ValueError as exc:
            lock_warning = str(exc)
    return {
        "process_id": process_id,
        "task_id": task_id,
        "version_id": version_id,
        "updated_processes": updated,
        "strategy": strategy,
        "lock_warning": lock_warning,
        "preview": preview,
    }


def _restore_batch_metadata_from_history(
    connection: sqlite3.Connection,
    snapshot: dict[str, Any],
    version_rows: list[sqlite3.Row] | None = None,
) -> None:
    """兼容修复旧数据：按完全相同的工序、资源和时间从历史版本恢复合批身份。"""
    rows = version_rows or connection.execute(
        "SELECT version_no,result_json FROM schedule_versions ORDER BY version_no DESC"
    ).fetchall()
    historical_batches: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        try:
            result = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        for task in result.get("schedule", []):
            key = _schedule_allocation_key(task)
            metadata = _batch_metadata(task)
            if key and metadata:
                historical_batches.setdefault(key, metadata)

    for order in snapshot.get("order_processes", []):
        for process in order.get("processes", []):
            if process.get("batch_id"):
                continue
            key = _schedule_allocation_key(process)
            metadata = historical_batches.get(key) if key else None
            if metadata:
                process.update(deepcopy(metadata))


def _time_value(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError):
        return None


def _detect_schedule_conflicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for resource_type, field in (("machine", "machine_id"), ("worker", "worker_id")):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if bool(row.get("virtual_resource")):
                continue
            resource_id = str(row.get(field) or "")
            start = _time_value(row.get("plan_start_time"))
            end = _time_value(row.get("plan_end_time"))
            if resource_id and start is not None and end is not None and end > start:
                grouped.setdefault(resource_id, []).append({**row, "_start": start, "_end": end})
        for resource_id, items in grouped.items():
            items.sort(key=lambda item: (item["_start"], item["_end"], str(item.get("process_id"))))
            for index, left in enumerate(items):
                for right in items[index + 1 :]:
                    if right["_start"] >= left["_end"]:
                        break
                    same_batch = left.get("batch_id") and left.get("batch_id") == right.get("batch_id")
                    if same_batch:
                        continue
                    process_ids = sorted([str(left.get("process_id")), str(right.get("process_id"))])
                    conflicts.append(
                        {
                            "conflict_id": f"{resource_type}:{resource_id}:{':'.join(process_ids)}",
                            "resource_type": resource_type,
                            "resource_id": resource_id,
                            "process_ids": process_ids,
                            "start_time": max(str(left.get("plan_start_time")), str(right.get("plan_start_time"))),
                            "end_time": min(str(left.get("plan_end_time")), str(right.get("plan_end_time"))),
                        }
                    )
    return conflicts


def build_effective_schedule(
    connection: sqlite3.Connection,
    *,
    schedule_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    process_status: str | None = None,
    order_id: str | None = None,
    machine_id: str | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    snapshot = build_snapshot(connection)
    version_rows = connection.execute(
        """SELECT version_id,version_no,task_id,schedule_type,status,published_by,published_at,result_json
           FROM schedule_versions ORDER BY version_no DESC"""
    ).fetchall()
    _restore_batch_metadata_from_history(connection, snapshot, version_rows)
    version_index = {str(row["version_id"]): dict(row) for row in version_rows}
    published_versions = [
        dict(row) for row in version_rows if row["status"] == VERSION_STATUS_PUBLISHED
    ]
    current_version_ids = {str(row["version_id"]) for row in published_versions}

    algorithm_processes: dict[tuple[str, str], dict[str, Any]] = {}
    for version in version_rows:
        version_id = str(version["version_id"])
        try:
            result = json.loads(version["result_json"])
        except (TypeError, json.JSONDecodeError):
            result = {}
        for process in result.get("schedule", []):
            if process.get("process_id"):
                algorithm_processes[(version_id, str(process["process_id"]))] = process

    machine_index = {
        str(item.get("machine_id")): item for item in snapshot.get("machine_profiles", []) if item.get("machine_id")
    }
    worker_index = {
        str(item.get("worker_id")): item for item in snapshot.get("worker_profiles", []) if item.get("worker_id")
    }
    group_index = {
        str(item.get("resource_group_id")): item
        for item in snapshot.get("resource_group_profiles", [])
        if item.get("resource_group_id")
    }

    rows: list[dict[str, Any]] = []
    for order in snapshot.get("order_processes", []):
        current_order_id = str(order.get("order_id") or "")
        order_schedule_type = schedule_type_for_order_business_type(order.get("order_business_type")) or ""
        for process in order.get("processes", []):
            process_id = str(process.get("process_id") or "")
            version_id = str(process.get("schedule_version_id") or "")
            version = version_index.get(version_id, {})
            algorithm = algorithm_processes.get((version_id, process_id), {})
            group = group_index.get(str(process.get("resource_group_id") or ""), {})
            current_schedule_type = str(
                version.get("schedule_type")
                or order_schedule_type
            )
            if version_id in current_version_ids:
                schedule_state = SCHEDULE_RECORD_STATUS_EFFECTIVE
            elif version_id:
                schedule_state = SCHEDULE_RECORD_STATUS_HISTORICAL
            else:
                schedule_state = SCHEDULE_RECORD_STATUS_UNSCHEDULED
            machine = str(process.get("assigned_machine_id") or algorithm.get("machine_id") or "")
            worker = str(process.get("assigned_worker_id") or algorithm.get("worker_id") or "")
            locks = process.get("locks") or {}
            rows.append(
                {
                    "process_id": process_id,
                    "process_name": process.get("process_name") or algorithm.get("process_name") or "",
                    "sequence": process.get("sequence") or algorithm.get("sequence") or 0,
                    "order_id": current_order_id,
                    "order_business_type": order.get("order_business_type") or algorithm.get("order_business_type") or "",
                    "order_status": order.get("status") or "",
                    "product_id": order.get("product_id") or "",
                    "product_name": order.get("product_name") or "",
                    "priority": order.get("priority"),
                    "due_date": order.get("due_date") or algorithm.get("due_date") or "",
                    "schedule_type": current_schedule_type,
                    "schedule_state": schedule_state,
                    "status": process.get("status") or PROCESS_STATUS_PENDING,
                    "resource_group_id": process.get("resource_group_id") or "",
                    "resource_group_type": group.get("resource_group_type") or algorithm.get("resource_group_type") or "",
                    "virtual_resource": bool(
                        algorithm.get("virtual_resource")
                        if "virtual_resource" in algorithm
                        else group.get("virtual_resource", False)
                    ),
                    "machine_id": machine,
                    "machine_name": machine_index.get(machine, {}).get("machine_name") or "",
                    "worker_id": worker,
                    "worker_name": worker_index.get(worker, {}).get("worker_name") or "",
                    "plan_start_time": process.get("plan_start_time") or algorithm.get("plan_start_time") or "",
                    "plan_end_time": process.get("plan_end_time") or algorithm.get("plan_end_time") or "",
                    "material_ready_time": process.get("material_ready_time") or "",
                    "batch_id": algorithm.get("batch_id") or process.get("batch_id") or "",
                    "batch_merged": bool(algorithm.get("batch_merged") or process.get("batch_merged")),
                    "schedule_version_id": version_id,
                    "version_status": version.get("status") or "",
                    "published_by": version.get("published_by") or "",
                    "published_at": version.get("published_at") or "",
                    "preserved_in_run": bool(algorithm.get("preserved_in_run")),
                    "manually_locked": is_manually_locked(process),
                    "lock_details": locks,
                    "allow_manual_lock": schedule_state == SCHEDULE_RECORD_STATUS_EFFECTIVE
                    and str(process.get("status") or "").upper() not in PROCESS_TERMINAL_STATUSES,
                    "allow_manual_adjustment": schedule_state == SCHEDULE_RECORD_STATUS_EFFECTIVE
                    and not is_manually_locked(process)
                    and str(process.get("status") or "").upper()
                    not in PROCESS_MANUAL_ADJUSTMENT_BLOCKED_STATUSES,
                    "lock_options": {
                        "machines": [
                            {
                                "machine_id": str(ref.get("machine_id") or ""),
                                "machine_name": machine_index.get(str(ref.get("machine_id") or ""), {}).get(
                                    "machine_name"
                                )
                                or "",
                                "allowed_worker_ids": next(
                                    (
                                        [str(value) for value in mapping.get("allowed_workers", []) or []]
                                        for mapping in group.get("machine_worker_mapping", []) or []
                                        if str(mapping.get("machine_id") or "")
                                        == str(ref.get("machine_id") or "")
                                    ),
                                    [str(item.get("worker_id") or "") for item in group.get("workers", [])],
                                ),
                            }
                            for ref in group.get("machines", [])
                            if machine_index.get(str(ref.get("machine_id") or ""), {}).get(
                                "status",
                                RECORD_STATUS_ACTIVE,
                            )
                            == RECORD_STATUS_ACTIVE
                        ],
                        "workers": [
                            {
                                "worker_id": str(ref.get("worker_id") or ""),
                                "worker_name": worker_index.get(str(ref.get("worker_id") or ""), {}).get(
                                    "worker_name"
                                )
                                or "",
                            }
                            for ref in group.get("workers", [])
                            if worker_index.get(str(ref.get("worker_id") or ""), {}).get(
                                "status",
                                RECORD_STATUS_ACTIVE,
                            )
                            == RECORD_STATUS_ACTIVE
                        ],
                    },
                    "source_process_status": algorithm.get("source_status") or "",
                    "has_conflict": False,
                    "conflict_types": [],
                }
            )

    effective_rows = [
        row for row in rows if row["schedule_state"] == SCHEDULE_RECORD_STATUS_EFFECTIVE
    ]
    conflicts = _detect_schedule_conflicts(effective_rows)
    process_conflicts: dict[str, set[str]] = {}
    for conflict in conflicts:
        for process_id_value in conflict["process_ids"]:
            process_conflicts.setdefault(process_id_value, set()).add(conflict["resource_type"])
    for row in rows:
        conflict_types = sorted(process_conflicts.get(str(row["process_id"]), set()))
        row["conflict_types"] = conflict_types
        row["has_conflict"] = bool(conflict_types)

    start_value = _time_value(start_time)
    end_value = _time_value(end_time)
    statuses = {value.strip().upper() for value in str(process_status or "").split(",") if value.strip()}

    def matches(row: dict[str, Any]) -> bool:
        if schedule_type and row["schedule_type"] != schedule_type:
            return False
        if statuses and str(row["status"]).upper() not in statuses:
            return False
        if order_id and order_id.lower() not in str(row["order_id"]).lower():
            return False
        if machine_id and machine_id.lower() not in str(row["machine_id"]).lower():
            return False
        if worker_id and worker_id.lower() not in str(row["worker_id"]).lower():
            return False
        row_start = _time_value(row.get("plan_start_time"))
        row_end = _time_value(row.get("plan_end_time"))
        if row_start is not None and row_end is not None:
            if start_value is not None and row_end < start_value:
                return False
            if end_value is not None and row_start > end_value:
                return False
        return True

    filtered_rows = [row for row in rows if matches(row)]
    filtered_rows.sort(
        key=lambda row: (
            row["schedule_state"] != SCHEDULE_RECORD_STATUS_EFFECTIVE,
            row.get("plan_start_time") or "9999",
            row.get("order_id") or "",
            row.get("sequence") or 0,
        )
    )
    visible_ids = {str(row["process_id"]) for row in filtered_rows}
    visible_conflicts = [
        conflict for conflict in conflicts if any(process_id_value in visible_ids for process_id_value in conflict["process_ids"])
    ]
    visible_effective = [
        row
        for row in filtered_rows
        if row["schedule_state"] == SCHEDULE_RECORD_STATUS_EFFECTIVE
    ]
    visible_unscheduled = [
        row
        for row in filtered_rows
        if row["schedule_state"] != SCHEDULE_RECORD_STATUS_EFFECTIVE
    ]
    status_counts: dict[str, int] = {}
    for row in filtered_rows:
        status_value = str(row.get("status") or SCHEDULE_RECORD_STATUS_UNKNOWN)
        status_counts[status_value] = status_counts.get(status_value, 0) + 1

    version_payload = []
    for version in published_versions:
        item = {key: version[key] for key in version if key != "result_json"}
        version_payload.append(item)
    return {
        "generated_at": now_text(),
        "published_versions": version_payload,
        "summary": {
            "total_processes": len(filtered_rows),
            "effective_processes": len(visible_effective),
            "unscheduled_processes": sum(
                row["schedule_state"] == SCHEDULE_RECORD_STATUS_UNSCHEDULED
                for row in filtered_rows
            ),
            "historical_processes": sum(
                row["schedule_state"] == SCHEDULE_RECORD_STATUS_HISTORICAL
                for row in filtered_rows
            ),
            "locked_processes": sum(bool(row["manually_locked"]) for row in visible_effective),
            "conflict_count": len(visible_conflicts),
            "order_count": len({row["order_id"] for row in filtered_rows if row["order_id"]}),
            "machine_count": len({
                row["machine_id"]
                for row in visible_effective
                if row["machine_id"] and not row.get("virtual_resource")
            }),
            "worker_count": len({
                row["worker_id"]
                for row in visible_effective
                if row["worker_id"] and not row.get("virtual_resource")
            }),
            "status_counts": status_counts,
        },
        "schedule": visible_effective,
        "processes": filtered_rows,
        "unscheduled_processes": visible_unscheduled,
        "conflicts": visible_conflicts,
        "filter_options": {
            "schedule_types": sorted({row["schedule_type"] for row in rows if row["schedule_type"]}),
            "statuses": sorted({str(row["status"]) for row in rows if row["status"]}),
            "orders": sorted({row["order_id"] for row in rows if row["order_id"]}),
            "machines": sorted({row["machine_id"] for row in rows if row["machine_id"]}),
            "workers": sorted({row["worker_id"] for row in rows if row["worker_id"]}),
        },
    }


def validate_snapshot(snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not (snapshot.get("machine_calendar") or {}).get("weekly_shifts"):
        errors.append("工厂日历缺少 weekly_shifts")
    machines = {
        str(item.get("machine_id")): item
        for item in snapshot.get("machine_profiles", [])
        if item.get("machine_id")
    }
    workers = {
        str(item.get("worker_id")): item
        for item in snapshot.get("worker_profiles", [])
        if item.get("worker_id")
    }
    groups = {str(item.get("resource_group_id")): item for item in snapshot.get("resource_group_profiles", [])}
    for label, records in (("设备", machines), ("人员", workers)):
        for record_id, record in records.items():
            if "virtual_resource" in record and not isinstance(record.get("virtual_resource"), bool):
                errors.append(f"{label} {record_id} 的 virtual_resource 必须是布尔值")
    for group_id, group in groups.items():
        if "virtual_resource" in group and not isinstance(group.get("virtual_resource"), bool):
            errors.append(f"资源组 {group_id} 的 virtual_resource 必须是布尔值")
        group_virtual = is_virtual_resource(group)
        for item in group.get("machines", []):
            machine_id = str(item.get("machine_id"))
            if machine_id not in machines:
                errors.append(f"资源组 {group_id} 引用了不存在的设备 {item.get('machine_id')}")
            elif is_virtual_resource(machines[machine_id]) != group_virtual:
                errors.append(f"资源组 {group_id} 与设备 {machine_id} 的 virtual_resource 必须一致")
        for item in group.get("workers", []):
            worker_id = str(item.get("worker_id"))
            if worker_id not in workers:
                errors.append(f"资源组 {group_id} 引用了不存在的人员 {item.get('worker_id')}")
            elif is_virtual_resource(workers[worker_id]) != group_virtual:
                errors.append(f"资源组 {group_id} 与人员 {worker_id} 的 virtual_resource 必须一致")
    process_ids: set[str] = set()
    for order in snapshot.get("order_processes", []):
        order_id = str(order.get("order_id", ""))
        if not order_id:
            errors.append("存在缺少 order_id 的订单")
        order_business_type = normalize_order_business_type(order.get("order_business_type"))
        if order_business_type not in ORDER_BUSINESS_TYPES:
            errors.append(f"订单 {order_id} 缺少或使用了非法 order_business_type")
        order_schedule_type = schedule_type_for_order_business_type(order_business_type)
        if "material_grade" in order and not isinstance(order.get("material_grade"), str):
            errors.append(f"订单 {order_id} 的 material_grade 必须是字符串")
        allows_batch_merge = any(
            bool((process.get("override_batch_rules", {}) or {}).get("allow_batch_merge"))
            for process in order.get("processes", [])
        )
        if allows_batch_merge and not str(order.get("material_grade") or "").strip():
            errors.append(f"订单 {order_id} 允许合批但缺少 material_grade")
        order_process_ids = {
            str(process.get("process_id"))
            for process in order.get("processes", [])
            if process.get("process_id")
        }
        route_indegree = {process_id: 0 for process_id in order_process_ids}
        route_dependents = {process_id: [] for process_id in order_process_ids}
        for process in order.get("processes", []):
            process_id = str(process.get("process_id", ""))
            if not process_id:
                errors.append(f"订单 {order_id} 存在缺少 process_id 的工序")
            elif process_id in process_ids:
                errors.append(f"工序编号重复: {process_id}")
            process_ids.add(process_id)
            try:
                if float(process.get("unit_duration_minutes", 0) or 0) <= 0:
                    errors.append(f"工序 {process_id} 的 unit_duration_minutes 必须大于 0")
                if int(process.get("process_quantity", 0) or 0) <= 0:
                    errors.append(f"工序 {process_id} 的 process_quantity 必须大于 0")
            except (TypeError, ValueError):
                errors.append(f"工序 {process_id} 的工时或数量格式无效")
            group_id = str(process.get("resource_group_id", ""))
            group = groups.get(group_id)
            if not group:
                errors.append(f"工序 {process_id} 引用了不存在的资源组 {group_id}")
            else:
                group_type = str(group.get("resource_group_type") or "").upper()
                group_schedule_type = RESOURCE_GROUP_SCHEDULE_TYPES.get(group_type)
                if (
                    group_type not in {RESOURCE_GROUP_TYPE_INSPECTION, RESOURCE_GROUP_TYPE_MANUAL}
                    and group_schedule_type
                    and order_schedule_type
                    and group_schedule_type != order_schedule_type
                    and not is_virtual_resource(group)
                ):
                    errors.append(f"订单 {order_id} 的跨业务工序 {process_id} 必须绑定虚拟资源组")
                if is_virtual_resource(group):
                    if bool((process.get("override_batch_rules", {}) or {}).get("allow_batch_merge")):
                        errors.append(f"虚拟工序 {process_id} 不允许合炉或合槽")
            cooling_enabled = process.get("cooling_constraint_enabled", False)
            if not isinstance(cooling_enabled, bool):
                errors.append(f"工序 {process_id} 的 cooling_constraint_enabled 必须是布尔值")
            cooling_method = process.get("cooling_method")
            if cooling_enabled is True and not str(cooling_method or "").strip():
                errors.append(f"工序 {process_id} 已启用冷却约束但缺少 cooling_method")
            locks = process.get("locks", {})
            if not isinstance(locks, dict):
                errors.append(f"工序 {process_id} 的 locks 必须是对象")
                continue
            allowed_lock_fields = {
                "machine_id",
                "worker_id",
                "start_time",
                "end_time",
                "lock_time",
                "operator",
                "lock_reason",
            }
            unknown_fields = set(locks) - allowed_lock_fields
            if unknown_fields:
                errors.append(f"工序 {process_id} 的 locks 包含旧字段或未知字段: {sorted(unknown_fields)}")
            if locks:
                if group and is_virtual_resource(group) and (locks.get("machine_id") or locks.get("worker_id")):
                    errors.append(f"虚拟工序 {process_id} 的人工锁不能指定设备或人员")
                if not any(locks.get(field) for field in ("machine_id", "worker_id", "start_time", "end_time")):
                    errors.append(f"工序 {process_id} 的人工锁至少需要锁定设备、人员或计划时间")
                if bool(locks.get("start_time")) != bool(locks.get("end_time")):
                    errors.append(f"工序 {process_id} 的人工锁开始和结束时间必须同时提供")
                elif locks.get("start_time"):
                    try:
                        start = datetime.fromisoformat(str(locks["start_time"]))
                        end = datetime.fromisoformat(str(locks["end_time"]))
                        if end <= start:
                            errors.append(f"工序 {process_id} 的人工锁结束时间必须晚于开始时间")
                        elif group and is_virtual_resource(group) and (
                            (end - start).total_seconds() / 60 + 1e-6
                            < float(process.get("unit_duration_minutes", 0) or 0)
                        ):
                            errors.append(f"虚拟工序 {process_id} 的人工锁时间不足 unit_duration_minutes")
                    except ValueError:
                        errors.append(f"工序 {process_id} 的人工锁时间格式无效")
                for field in ("lock_time", "operator", "lock_reason"):
                    if not str(locks.get(field) or "").strip():
                        errors.append(f"工序 {process_id} 的人工锁缺少 {field}")
            external_plan = process.get("external_plan", {}) or {}
            if not isinstance(external_plan, dict):
                errors.append(f"工序 {process_id} 的 external_plan 必须是对象")
            elif external_plan:
                if group and not is_virtual_resource(group):
                    errors.append(f"真实资源工序 {process_id} 不能配置 external_plan")
                if str(external_plan.get("status") or "").upper() != PROCESS_STATUS_CONFIRMED:
                    errors.append(
                        f"工序 {process_id} 的 external_plan.status 目前只允许 {PROCESS_STATUS_CONFIRMED}"
                    )
                required_fields = {
                    "source_business_type",
                    "reference_id",
                    "plan_start_time",
                    "plan_end_time",
                    "confirmed_by",
                    "confirmed_at",
                }
                missing_fields = [field for field in required_fields if not str(external_plan.get(field) or "").strip()]
                if missing_fields:
                    errors.append(f"工序 {process_id} 的 external_plan 缺少字段: {sorted(missing_fields)}")
                else:
                    try:
                        external_start = datetime.fromisoformat(str(external_plan["plan_start_time"]))
                        external_end = datetime.fromisoformat(str(external_plan["plan_end_time"]))
                        datetime.fromisoformat(str(external_plan["confirmed_at"]))
                        required_minutes = float(process.get("unit_duration_minutes", 0) or 0)
                        if external_end <= external_start:
                            errors.append(f"工序 {process_id} 的外部确认结束时间必须晚于开始时间")
                        elif (external_end - external_start).total_seconds() / 60 + 1e-6 < required_minutes:
                            errors.append(f"工序 {process_id} 的外部确认时间不足 unit_duration_minutes")
                    except ValueError:
                        errors.append(f"工序 {process_id} 的 external_plan 时间格式无效")
                source_business_type = normalize_order_business_type(
                    external_plan.get("source_business_type")
                )
                if source_business_type not in ORDER_BUSINESS_TYPES:
                    errors.append(
                        f"工序 {process_id} 的 external_plan.source_business_type 非法: "
                        f"{source_business_type}"
                    )
                elif group:
                    group_type = str(group.get("resource_group_type") or "").upper()
                    if (
                        group_type not in {RESOURCE_GROUP_TYPE_INSPECTION, RESOURCE_GROUP_TYPE_MANUAL}
                        and source_business_type != group_type
                    ):
                        errors.append(
                            f"工序 {process_id} 的外部确认来源 {source_business_type} "
                            f"必须与资源组类型 {group_type} 一致"
                        )
                if locks and (locks.get("start_time") or locks.get("end_time")):
                    errors.append(f"工序 {process_id} 已有外部确认时间，不能同时配置人工时间锁")
            predecessors = {str(value) for value in process.get("previous_process_ids", []) or []}
            missing_predecessors = predecessors - order_process_ids
            if missing_predecessors:
                errors.append(f"工序 {process_id} 引用了订单内不存在的前置工序: {sorted(missing_predecessors)}")
            elif process_id in predecessors:
                errors.append(f"工序 {process_id} 不能把自身配置为前置工序")
            elif process_id in route_indegree:
                route_indegree[process_id] = len(predecessors)
                for predecessor_id in predecessors:
                    route_dependents[predecessor_id].append(process_id)

        ready_process_ids = [
            process_id for process_id, count in route_indegree.items() if count == 0
        ]
        visited_process_count = 0
        while ready_process_ids:
            ready_process_id = ready_process_ids.pop()
            visited_process_count += 1
            for dependent_id in route_dependents[ready_process_id]:
                route_indegree[dependent_id] -= 1
                if route_indegree[dependent_id] == 0:
                    ready_process_ids.append(dependent_id)
        if visited_process_count != len(route_indegree):
            cycle_ids = sorted(
                process_id for process_id, count in route_indegree.items() if count > 0
            )
            errors.append(f"订单 {order_id} 的工艺路线存在循环前置关系: {cycle_ids}")
    return errors


def generate_task_id(schedule_type: str, mode: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return (
        f"{SCHEDULE_TASK_ID_PREFIX}{settings.factory_code}-{schedule_type.upper()}-"
        f"{mode.upper()}-{stamp}-{uuid4().hex[:4].upper()}"
    )


def create_task(data: dict[str, Any], actor: str) -> str:
    mode = str(data.get("mode", SCHEDULE_MODE_STATIC))
    with db() as connection:
        configuration = get_system_configuration(connection)
        schedule_type = resolve_task_schedule_type(
            configuration["deployment_process_type"], data.get("schedule_type")
        )
        task_id = str(data.get("task_id") or generate_task_id(schedule_type, mode))
        snapshot = build_snapshot(connection, schedule_type, filter_orders=True)
        _restore_batch_metadata_from_history(connection, snapshot)
        validation_errors = validate_snapshot(snapshot)
        if validation_errors:
            raise ValueError("主数据快照校验失败：" + "；".join(validation_errors[:10]))
        payload = {
            "task_id": task_id,
            "schedule_type": schedule_type,
            "mode": mode,
            "dispatching_rule": str(data.get("dispatching_rule", DISPATCH_RULE_DELIVERY)),
            "schedule_start": data["schedule_start"],
            "config_overrides": data.get("config_overrides") or {},
            "local_adjustments": data.get("local_adjustments") or [],
            "data_snapshot": snapshot,
        }
        connection.execute(
            """INSERT INTO schedule_tasks(task_id,schedule_type,mode,dispatching_rule,status,request_json,snapshot_json,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                schedule_type,
                mode,
                payload["dispatching_rule"],
                TASK_STATUS_QUEUED,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(snapshot, ensure_ascii=False),
                actor,
                now_text(),
            ),
        )
        audit(
            connection,
            actor,
            AUDIT_ACTION_TASK_CREATED,
            "schedule_task",
            task_id,
            {"schedule_type": schedule_type, "mode": mode},
        )
    return task_id


def execute_task(task_id: str) -> None:
    with db() as connection:
        task = connection.execute("SELECT * FROM schedule_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not task or task["status"] not in TASK_RUNNABLE_STATUSES:
            return
        connection.execute(
            "UPDATE schedule_tasks SET status=?, started_at=?, completed_at=NULL, error_message=NULL WHERE task_id=?",
            (TASK_STATUS_RUNNING, now_text(), task_id),
        )
        request_payload = json.loads(task["request_json"])
        actor = task["created_by"]

    try:
        response = algorithm_client.execute(request_payload)
        if response.get("status") != TASK_STATUS_SUCCEEDED:
            error = response.get("error") or {}
            message = error.get("message") or "算法任务执行失败"
            with db() as connection:
                connection.execute(
                    "UPDATE schedule_tasks SET status=?,response_json=?,error_message=?,completed_at=? WHERE task_id=?",
                    (
                        TASK_STATUS_FAILED,
                        json.dumps(response, ensure_ascii=False),
                        message,
                        now_text(),
                        task_id,
                    ),
                )
                audit(
                    connection,
                    "algorithm",
                    AUDIT_ACTION_TASK_FAILED,
                    "schedule_task",
                    task_id,
                    error,
                )
            return
        save_task_result(task_id, response, actor)
    except Exception as exc:
        with db() as connection:
            connection.execute(
                "UPDATE schedule_tasks SET status=?,error_message=?,completed_at=? WHERE task_id=?",
                (TASK_STATUS_FAILED, str(exc), now_text(), task_id),
            )
            audit(
                connection,
                "system",
                AUDIT_ACTION_TASK_FAILED,
                "schedule_task",
                task_id,
                {"message": str(exc)},
            )


def save_task_result(task_id: str, response: dict[str, Any], actor: str = "algorithm") -> str:
    with db() as connection:
        task = connection.execute("SELECT * FROM schedule_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not task:
            raise KeyError(f"任务不存在: {task_id}")
        existing = connection.execute("SELECT version_id FROM schedule_versions WHERE task_id=?", (task_id,)).fetchone()
        connection.execute(
            "UPDATE schedule_tasks SET status=?,response_json=?,error_message=NULL,completed_at=? WHERE task_id=?",
            (
                TASK_STATUS_SUCCEEDED,
                json.dumps(response, ensure_ascii=False),
                response.get("completed_at") or now_text(),
                task_id,
            ),
        )
        if existing:
            return existing["version_id"]
        next_no = connection.execute("SELECT COALESCE(MAX(version_no),0)+1 AS no FROM schedule_versions").fetchone()["no"]
        version_id = (
            f"{SCHEDULE_VERSION_ID_PREFIX}{datetime.now().strftime('%Y%m%d')}-"
            f"{int(next_no):04d}"
        )
        connection.execute(
            """INSERT INTO schedule_versions(version_id,version_no,task_id,schedule_type,status,result_json,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                version_id,
                next_no,
                task_id,
                task["schedule_type"],
                VERSION_STATUS_DRAFT,
                json.dumps(response.get("result") or {}, ensure_ascii=False),
                task["created_by"],
                now_text(),
            ),
        )
        audit(
            connection,
            actor,
            AUDIT_ACTION_TASK_SUCCEEDED,
            "schedule_task",
            task_id,
            {"version_id": version_id},
        )
        return version_id


def _transition_result_schedule_status(
    result: dict[str, Any],
    source_status: str,
    target_status: str,
) -> int:
    """按新版本流程执行单向状态转换，不处理历史版本状态。"""
    updated = 0
    for item in result.get("schedule", []) or []:
        current = str(item.get("status") or PROCESS_STATUS_PENDING).upper()
        if current != source_status:
            continue
        item["status"] = target_status
        updated += 1
    return updated


def review_schedule_version(
    version_id: str,
    decision: str,
    actor: str,
    comment: str = "",
) -> dict[str, Any]:
    normalized_decision = str(decision or "").upper()
    if normalized_decision not in VERSION_REVIEW_DECISIONS:
        raise ValueError("decision 必须为 APPROVED 或 REJECTED")
    with db() as connection:
        version = connection.execute(
            "SELECT status,result_json FROM schedule_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if not version:
            raise KeyError("排程版本不存在")
        if version["status"] != VERSION_STATUS_DRAFT:
            raise ValueError("只有草稿版本可以审批")
        result = json.loads(version["result_json"] or "{}")
        process_status = (
            PROCESS_STATUS_SCHEDULED
            if normalized_decision == VERSION_STATUS_APPROVED
            else PROCESS_STATUS_PENDING
        )
        updated_processes = (
            _transition_result_schedule_status(
                result,
                PROCESS_STATUS_PENDING,
                PROCESS_STATUS_SCHEDULED,
            )
            if normalized_decision == VERSION_STATUS_APPROVED
            else 0
        )
        connection.execute(
            "UPDATE schedule_versions SET status=?,result_json=?,reviewed_by=?,reviewed_at=?,review_comment=? "
            "WHERE version_id=?",
            (
                normalized_decision,
                json.dumps(result, ensure_ascii=False),
                actor,
                now_text(),
                comment,
                version_id,
            ),
        )
        audit(
            connection,
            actor,
            f"{VERSION_AUDIT_ACTION_PREFIX}{normalized_decision}",
            "schedule_version",
            version_id,
            {"comment": comment, "process_status": process_status, "updated_processes": updated_processes},
        )
        return {
            "version_id": version_id,
            "status": normalized_decision,
            "process_status": process_status,
            "updated_processes": updated_processes,
        }


def publish_version(version_id: str, actor: str) -> int:
    with db() as connection:
        version = connection.execute("SELECT * FROM schedule_versions WHERE version_id=?", (version_id,)).fetchone()
        if not version:
            raise KeyError("排程版本不存在")
        if version["status"] != VERSION_STATUS_APPROVED:
            raise ValueError("只有已审批版本才能发布")
        connection.execute(
            "UPDATE schedule_versions SET status=? WHERE schedule_type=? AND status=?",
            (
                VERSION_STATUS_SUPERSEDED,
                version["schedule_type"],
                VERSION_STATUS_PUBLISHED,
            ),
        )
        stamp = now_text()
        result = json.loads(version["result_json"])
        _transition_result_schedule_status(
            result,
            PROCESS_STATUS_SCHEDULED,
            PROCESS_STATUS_CONFIRMED,
        )
        connection.execute(
            "UPDATE schedule_versions SET status=?,result_json=?,published_by=?,published_at=? WHERE version_id=?",
            (
                VERSION_STATUS_PUBLISHED,
                json.dumps(result, ensure_ascii=False),
                actor,
                stamp,
                version_id,
            ),
        )
        tasks = result.get("schedule") or []
        task_index = {str(item.get("process_id")): item for item in tasks if item.get("process_id")}
        updated = 0
        order_rows = connection.execute("SELECT * FROM master_records WHERE entity_type='order'").fetchall()
        for row in order_rows:
            order = json.loads(row["payload_json"])
            changed = False
            for process in order.get("processes", []):
                scheduled = task_index.get(str(process.get("process_id")))
                if not scheduled:
                    continue
                process["plan_start_time"] = scheduled.get("plan_start_time")
                process["plan_end_time"] = scheduled.get("plan_end_time")
                process["assigned_machine_id"] = scheduled.get("machine_id")
                process["assigned_worker_id"] = scheduled.get("worker_id")
                process["schedule_version_id"] = version_id
                scheduled_status = str(
                    scheduled.get("status") or PROCESS_STATUS_CONFIRMED
                ).upper()
                process["status"] = (
                    scheduled_status
                    if scheduled_status in PROCESS_PUBLISHED_STATUSES
                    else PROCESS_STATUS_CONFIRMED
                )
                _sync_batch_metadata(process, scheduled)
                process["locks"] = process.get("locks") or {}
                changed = True
                updated += 1
            if changed:
                connection.execute(
                    "UPDATE master_records SET payload_json=?,revision=revision+1,updated_by=?,updated_at=? WHERE entity_type='order' AND entity_id=?",
                    (json.dumps(order, ensure_ascii=False), actor, stamp, row["entity_id"]),
                )
        audit(
            connection,
            actor,
            AUDIT_ACTION_VERSION_PUBLISHED,
            "schedule_version",
            version_id,
            {"updated_processes": updated},
        )
        return updated


def compare_versions(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_result = json.loads(left["result_json"])
    right_result = json.loads(right["result_json"])
    left_tasks = {item.get("process_id"): item for item in left_result.get("schedule", [])}
    right_tasks = {item.get("process_id"): item for item in right_result.get("schedule", [])}
    changes = []
    for process_id in sorted(set(left_tasks) | set(right_tasks)):
        before, after = left_tasks.get(process_id), right_tasks.get(process_id)
        fields = {}
        for field in ("plan_start_time", "plan_end_time", "machine_id", "worker_id", "batch_id"):
            old = before.get(field) if before else None
            new = after.get(field) if after else None
            if old != new:
                fields[field] = {"before": old, "after": new}
        if fields or before is None or after is None:
            changes.append(
                {
                    "process_id": process_id,
                    "change_type": (
                        CHANGE_TYPE_ADDED
                        if before is None
                        else CHANGE_TYPE_REMOVED
                        if after is None
                        else CHANGE_TYPE_MODIFIED
                    ),
                    "fields": fields,
                }
            )
    return {
        "left_version_id": left["version_id"],
        "right_version_id": right["version_id"],
        "changed_process_count": len(changes),
        "changes": changes,
        "kpis": {"before": left_result.get("kpis", {}), "after": right_result.get("kpis", {})},
        "score_comparison": _version_score_comparison(left_result, right_result),
        "metric_comparison": _version_metric_comparison(left_result, right_result),
        "run_comparison": {
            "before": task_run_summary(left),
            "after": task_run_summary(right),
        },
    }


def task_run_summary(record: dict[str, Any]) -> dict[str, Any]:
    """从任务或版本记录中提取遗传算法参数和实际运行时长。"""
    request_value = record.get("request_json") or {}
    if isinstance(request_value, str):
        try:
            request_value = json.loads(request_value)
        except json.JSONDecodeError:
            request_value = {}
    nsga3 = ((request_value.get("config_overrides") or {}).get("nsga3") or {})
    population_size = record.get("configured_population_size")
    generations = record.get("configured_generations")
    if population_size is None:
        population_size = nsga3.get("population_size")
    if generations is None:
        generations = nsga3.get("generations")
    mode = record.get("mode") or request_value.get("mode")
    dispatching_rule = record.get("dispatching_rule") or request_value.get("dispatching_rule")
    schedule_type = record.get("schedule_type") or request_value.get("schedule_type")
    schedule_start = request_value.get("schedule_start")
    started_at = record.get("started_at")
    completed_at = record.get("completed_at")
    duration_seconds: float | None = None
    if started_at and completed_at:
        try:
            duration_seconds = max(
                (datetime.fromisoformat(str(completed_at)) - datetime.fromisoformat(str(started_at))).total_seconds(),
                0.0,
            )
        except ValueError:
            duration_seconds = None
    return {
        "population_size": int(population_size) if isinstance(population_size, (int, float)) else None,
        "generations": int(generations) if isinstance(generations, (int, float)) else None,
        "duration_seconds": round(duration_seconds, 3) if duration_seconds is not None else None,
        "mode": str(mode) if mode else None,
        "dispatching_rule": str(dispatching_rule) if dispatching_rule else None,
        "schedule_type": str(schedule_type) if schedule_type else None,
        "schedule_start": str(schedule_start) if schedule_start else None,
    }


def _finite_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            number = float(value)
            if number == number and number not in {float("inf"), float("-inf")}:
                return number
    return None


def _duration_hours(result: dict[str, Any], minute_key: str, hour_key: str) -> float | None:
    kpis = result.get("kpis", {}) or {}
    objectives = result.get("best_objectives", {}) or {}
    hours = _finite_number(kpis.get(hour_key), objectives.get(hour_key))
    if hours is not None:
        return hours
    minutes = _finite_number(kpis.get(minute_key), objectives.get(minute_key))
    return minutes / 60.0 if minutes is not None else None


def _machine_utilization(result: dict[str, Any]) -> float | None:
    kpis = result.get("kpis", {}) or {}
    objectives = result.get("best_objectives", {}) or {}
    direct = _finite_number(
        kpis.get("machine_utilization"),
        kpis.get("average_machine_utilization"),
        objectives.get("machine_utilization"),
    )
    if direct is not None:
        return direct / 100.0 if abs(direct) > 1 else direct
    idle_rate = _finite_number(kpis.get("machine_idle_rate"), objectives.get("machine_idle_rate"))
    if idle_rate is None:
        return None
    idle_rate = idle_rate / 100.0 if abs(idle_rate) > 1 else idle_rate
    return 1.0 - idle_rate


def _on_time_delivery_rate(result: dict[str, Any]) -> float | None:
    kpis = result.get("kpis", {}) or {}
    objectives = result.get("best_objectives", {}) or {}
    direct = _finite_number(kpis.get("on_time_delivery_rate"), objectives.get("on_time_delivery_rate"))
    if direct is not None:
        return direct / 100.0 if abs(direct) > 1 else direct
    tardy_count = _finite_number(kpis.get("tardiness_count"), objectives.get("tardiness_count"))
    metadata = result.get("metadata", {}) or {}
    order_count = _finite_number(metadata.get("order_count"))
    if not order_count:
        order_count = float(
            len({task.get("order_id") for task in result.get("schedule", []) if task.get("order_id")})
        )
    if tardy_count is None or not order_count:
        return None
    return max(order_count - tardy_count, 0.0) / order_count


def _topsis_score(result: dict[str, Any]) -> float | None:
    direct = _finite_number(result.get("topsis_score"), (result.get("metadata", {}) or {}).get("topsis_score"))
    if direct is not None:
        return direct
    ranking = result.get("topsis_ranking", []) or []
    candidates: list[tuple[float, float]] = []
    for index, item in enumerate(ranking, start=1):
        if not isinstance(item, dict):
            continue
        score = _finite_number(item.get("topsis_score"))
        if score is None:
            continue
        rank = _finite_number(item.get("rank"))
        candidates.append((rank if rank is not None else float(index), score))
    if not candidates:
        return None
    candidates.sort(key=lambda value: (value[0], -value[1]))
    return candidates[0][1]


def _metric_row(
    key: str,
    label: str,
    before: float | None,
    after: float | None,
    unit: str,
    better_when: str,
) -> dict[str, Any]:
    if before is None or after is None:
        trend = "unavailable"
        outcome = "unavailable"
        delta = None
    else:
        delta = after - before
        if abs(delta) < 1e-9:
            trend = "same"
            outcome = "same"
        else:
            trend = "up" if delta > 0 else "down"
            improved = (delta > 0 and better_when == "higher") or (delta < 0 and better_when == "lower")
            outcome = "improved" if improved else "worsened"
    return {
        "key": key,
        "label": label,
        "before": round(before, 4) if before is not None else None,
        "after": round(after, 4) if after is not None else None,
        "delta": round(delta, 4) if delta is not None else None,
        "unit": unit,
        "trend": trend,
        "outcome": outcome,
        "better_when": better_when,
    }


def _version_metric_comparison(
    left_result: dict[str, Any], right_result: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        _metric_row(
            "makespan",
            "最大完工时间",
            _duration_hours(left_result, "makespan", "makespan_hours"),
            _duration_hours(right_result, "makespan", "makespan_hours"),
            "hours",
            "lower",
        ),
        _metric_row(
            "total_tardiness",
            "总延期",
            _duration_hours(left_result, "total_tardiness", "total_tardiness_hours"),
            _duration_hours(right_result, "total_tardiness", "total_tardiness_hours"),
            "hours",
            "lower",
        ),
        _metric_row(
            "machine_utilization",
            "设备利用率",
            _machine_utilization(left_result),
            _machine_utilization(right_result),
            "percent",
            "higher",
        ),
        _metric_row(
            "on_time_delivery_rate",
            "按期交付率",
            _on_time_delivery_rate(left_result),
            _on_time_delivery_rate(right_result),
            "percent",
            "higher",
        ),
        _metric_row(
            "wip_waiting",
            "等待时间",
            _duration_hours(left_result, "wip_waiting", "total_waiting_hours"),
            _duration_hours(right_result, "wip_waiting", "total_waiting_hours"),
            "hours",
            "lower",
        ),
    ]


def _version_score_comparison(
    left_result: dict[str, Any], right_result: dict[str, Any]
) -> dict[str, Any]:
    before_score = _topsis_score(left_result)
    after_score = _topsis_score(right_result)
    comparison = _metric_row(
        "comprehensive_score",
        "综合得分",
        before_score,
        after_score,
        "score",
        "higher",
    )
    delta = after_score - before_score if before_score is not None and after_score is not None else None
    comparison["before"] = round(before_score, 6) if before_score is not None else None
    comparison["after"] = round(after_score, 6) if after_score is not None else None
    comparison["delta"] = round(delta, 6) if delta is not None else None
    comparison["change_rate_percent"] = (
        round(delta / abs(before_score) * 100.0, 2)
        if before_score is not None and delta is not None and abs(before_score) > 1e-12
        else None
    )
    return comparison
