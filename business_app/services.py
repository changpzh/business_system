from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4

from .algorithm_client import algorithm_client
from .config import settings
from .database import audit, db, now_text


ENTITY_CONFIG = {
    "calendar": ("machine_calendar", "calendar_id"),
    "machine": ("machine_profiles", "machine_id"),
    "worker": ("worker_profiles", "worker_id"),
    "resource_group": ("resource_group_profiles", "resource_group_id"),
    "order": ("order_processes", "order_id"),
}

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


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def parse_json_columns(record: dict[str, Any] | None, *columns: str) -> dict[str, Any] | None:
    if not record:
        return record
    for column in columns:
        if record.get(column):
            record[column.removesuffix("_json")] = json.loads(record[column])
        record.pop(column, None)
    return record


def build_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "machine_calendar": {},
        "machine_profiles": [],
        "worker_profiles": [],
        "resource_group_profiles": [],
        "order_processes": [],
    }
    for entity_type, (snapshot_key, _) in ENTITY_CONFIG.items():
        rows = connection.execute(
            "SELECT payload_json FROM master_records WHERE entity_type=? ORDER BY entity_id", (entity_type,)
        ).fetchall()
        values = [json.loads(row["payload_json"]) for row in rows]
        snapshot[snapshot_key] = (values[0] if values else {}) if entity_type == "calendar" else values
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
    group: dict[str, Any],
    machine_index: dict[str, dict[str, Any]],
    worker_index: dict[str, dict[str, Any]],
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

    allowed_machines = {str(item.get("machine_id")) for item in group.get("machines", [])}
    allowed_workers = {str(item.get("worker_id")) for item in group.get("workers", [])}
    if machine_id and machine_id not in allowed_machines:
        raise ValueError(f"设备 {machine_id} 不属于工序资源组")
    if worker_id and worker_id not in allowed_workers:
        raise ValueError(f"人员 {worker_id} 不属于工序资源组")
    if machine_id and machine_index.get(machine_id, {}).get("status", "ACTIVE") != "ACTIVE":
        raise ValueError(f"设备 {machine_id} 当前不可用")
    if worker_id and worker_index.get(worker_id, {}).get("status", "ACTIVE") != "ACTIVE":
        raise ValueError(f"人员 {worker_id} 当前不可用")

    if machine_id:
        machine = machine_index.get(machine_id, {})
        requirements = process.get("resource_requirements", {}) or {}
        cooling_method = process.get("cooling_method") or requirements.get("cooling_method")
        capabilities = machine.get("capabilities", {}) or {}
        cooling_methods = machine.get("cooling_methods", capabilities.get("cooling_methods", [])) or []
        if cooling_method and cooling_method not in cooling_methods:
            raise ValueError(f"设备 {machine_id} 不支持工序要求的冷却方式 {cooling_method}")

    mappings = group.get("machine_worker_mapping", []) or []
    if machine_id and worker_id and mappings:
        valid_mapping = any(
            str(item.get("machine_id")) == machine_id
            and worker_id in {str(value) for value in item.get("allowed_workers", [])}
            for item in mappings
        )
        if not valid_mapping:
            raise ValueError(f"设备 {machine_id} 与人员 {worker_id} 不满足资源组映射约束")

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


def lock_process(process_id: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """人工锁定订单工序，并记录操作人、时间和原因。"""
    with db() as connection:
        snapshot = build_snapshot(connection)
        machine_index = {
            str(item.get("machine_id")): item for item in snapshot.get("machine_profiles", [])
        }
        worker_index = {
            str(item.get("worker_id")): item for item in snapshot.get("worker_profiles", [])
        }
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
            if str(process.get("status") or "").upper() in {"COMPLETED", "CANCELLED"}:
                raise ValueError("已完成或已取消工序不能人工锁定")
            expected_version = str(payload.get("schedule_version_id") or "")
            current_version = str(process.get("schedule_version_id") or "")
            if expected_version and expected_version != current_version:
                raise ValueError("计划版本已变化，请刷新甘特图后重新操作")
            current_lock_time = str((process.get("locks") or {}).get("lock_time") or "")
            if "expected_lock_time" in payload and str(payload.get("expected_lock_time") or "") != current_lock_time:
                raise ValueError("人工锁已变化，请刷新甘特图后重新操作")
            group = group_index.get(str(process.get("resource_group_id") or ""), {})
            normalized = _validate_lock_payload(process, payload, group, machine_index, worker_index)
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
                "PROCESS_MANUALLY_LOCKED",
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
                "PROCESS_MANUALLY_UNLOCKED",
                "process",
                process_id,
                {"before": previous, "unlock_reason": reason, "order_id": order.get("order_id")},
            )
            return {"process_id": process_id, "order_id": order.get("order_id"), "locks": {}}
    raise KeyError("工序不存在")


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


def _schedule_type_from_group(group_type: str) -> str:
    value = str(group_type or "").upper()
    if "MACHIN" in value:
        return "machining"
    if "HEAT" in value or "SURFACE" in value:
        return "heat_treatment"
    if "ASSEMB" in value or "INSPECT" in value:
        return "assembly"
    return value.lower()


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
    published_versions = [dict(row) for row in version_rows if row["status"] == "PUBLISHED"]
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
        for process in order.get("processes", []):
            process_id = str(process.get("process_id") or "")
            version_id = str(process.get("schedule_version_id") or "")
            version = version_index.get(version_id, {})
            algorithm = algorithm_processes.get((version_id, process_id), {})
            group = group_index.get(str(process.get("resource_group_id") or ""), {})
            current_schedule_type = str(
                version.get("schedule_type")
                or process.get("schedule_type")
                or order.get("schedule_type")
                or _schedule_type_from_group(group.get("resource_group_type", ""))
            )
            if version_id in current_version_ids:
                schedule_state = "EFFECTIVE"
            elif version_id:
                schedule_state = "HISTORICAL"
            else:
                schedule_state = "UNSCHEDULED"
            machine = str(process.get("assigned_machine_id") or algorithm.get("machine_id") or "")
            worker = str(process.get("assigned_worker_id") or algorithm.get("worker_id") or "")
            locks = process.get("locks") or {}
            rows.append(
                {
                    "process_id": process_id,
                    "process_name": process.get("process_name") or algorithm.get("process_name") or "",
                    "sequence": process.get("sequence") or algorithm.get("sequence") or 0,
                    "order_id": current_order_id,
                    "order_status": order.get("status") or "",
                    "product_id": order.get("product_id") or "",
                    "product_name": order.get("product_name") or "",
                    "priority": order.get("priority"),
                    "due_date": order.get("due_date") or algorithm.get("due_date") or "",
                    "schedule_type": current_schedule_type,
                    "schedule_state": schedule_state,
                    "status": process.get("status") or "PENDING",
                    "resource_group_id": process.get("resource_group_id") or "",
                    "machine_id": machine,
                    "machine_name": machine_index.get(machine, {}).get("machine_name") or "",
                    "worker_id": worker,
                    "worker_name": worker_index.get(worker, {}).get("worker_name") or "",
                    "plan_start_time": process.get("plan_start_time") or algorithm.get("plan_start_time") or "",
                    "plan_end_time": process.get("plan_end_time") or algorithm.get("plan_end_time") or "",
                    "material_ready": process.get("material_ready"),
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
                    "allow_manual_lock": schedule_state == "EFFECTIVE"
                    and str(process.get("status") or "").upper() not in {"COMPLETED", "CANCELLED"},
                    "lock_options": {
                        "machines": [
                            {
                                "machine_id": str(ref.get("machine_id") or ""),
                                "machine_name": machine_index.get(str(ref.get("machine_id") or ""), {}).get(
                                    "machine_name"
                                )
                                or "",
                            }
                            for ref in group.get("machines", [])
                            if machine_index.get(str(ref.get("machine_id") or ""), {}).get("status", "ACTIVE")
                            == "ACTIVE"
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
                            if worker_index.get(str(ref.get("worker_id") or ""), {}).get("status", "ACTIVE")
                            == "ACTIVE"
                        ],
                    },
                    "source_process_status": algorithm.get("source_status") or "",
                    "has_conflict": False,
                    "conflict_types": [],
                }
            )

    effective_rows = [row for row in rows if row["schedule_state"] == "EFFECTIVE"]
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
            row["schedule_state"] != "EFFECTIVE",
            row.get("plan_start_time") or "9999",
            row.get("order_id") or "",
            row.get("sequence") or 0,
        )
    )
    visible_ids = {str(row["process_id"]) for row in filtered_rows}
    visible_conflicts = [
        conflict for conflict in conflicts if any(process_id_value in visible_ids for process_id_value in conflict["process_ids"])
    ]
    visible_effective = [row for row in filtered_rows if row["schedule_state"] == "EFFECTIVE"]
    visible_unscheduled = [row for row in filtered_rows if row["schedule_state"] != "EFFECTIVE"]
    status_counts: dict[str, int] = {}
    for row in filtered_rows:
        status_value = str(row.get("status") or "UNKNOWN")
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
            "unscheduled_processes": sum(row["schedule_state"] == "UNSCHEDULED" for row in filtered_rows),
            "historical_processes": sum(row["schedule_state"] == "HISTORICAL" for row in filtered_rows),
            "locked_processes": sum(bool(row["manually_locked"]) for row in visible_effective),
            "conflict_count": len(visible_conflicts),
            "order_count": len({row["order_id"] for row in filtered_rows if row["order_id"]}),
            "machine_count": len({row["machine_id"] for row in visible_effective if row["machine_id"]}),
            "worker_count": len({row["worker_id"] for row in visible_effective if row["worker_id"]}),
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
    machines = {str(item.get("machine_id")) for item in snapshot.get("machine_profiles", [])}
    workers = {str(item.get("worker_id")) for item in snapshot.get("worker_profiles", [])}
    groups = {str(item.get("resource_group_id")): item for item in snapshot.get("resource_group_profiles", [])}
    for group_id, group in groups.items():
        for item in group.get("machines", []):
            if str(item.get("machine_id")) not in machines:
                errors.append(f"资源组 {group_id} 引用了不存在的设备 {item.get('machine_id')}")
        for item in group.get("workers", []):
            if str(item.get("worker_id")) not in workers:
                errors.append(f"资源组 {group_id} 引用了不存在的人员 {item.get('worker_id')}")
    process_ids: set[str] = set()
    for order in snapshot.get("order_processes", []):
        order_id = str(order.get("order_id", ""))
        if not order_id:
            errors.append("存在缺少 order_id 的订单")
        for process in order.get("processes", []):
            process_id = str(process.get("process_id", ""))
            if not process_id:
                errors.append(f"订单 {order_id} 存在缺少 process_id 的工序")
            elif process_id in process_ids:
                errors.append(f"工序编号重复: {process_id}")
            process_ids.add(process_id)
            group_id = str(process.get("resource_group_id", ""))
            if group_id not in groups:
                errors.append(f"工序 {process_id} 引用了不存在的资源组 {group_id}")
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
                    except ValueError:
                        errors.append(f"工序 {process_id} 的人工锁时间格式无效")
                for field in ("lock_time", "operator", "lock_reason"):
                    if not str(locks.get(field) or "").strip():
                        errors.append(f"工序 {process_id} 的人工锁缺少 {field}")
    return errors


def generate_task_id(schedule_type: str, mode: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"SCH-{settings.factory_code}-{schedule_type.upper()}-{mode.upper()}-{stamp}-{uuid4().hex[:4].upper()}"


def create_task(data: dict[str, Any], actor: str) -> str:
    schedule_type = str(data.get("schedule_type", "machining"))
    mode = str(data.get("mode", "static"))
    task_id = str(data.get("task_id") or generate_task_id(schedule_type, mode))
    with db() as connection:
        snapshot = build_snapshot(connection)
        _restore_batch_metadata_from_history(connection, snapshot)
        validation_errors = validate_snapshot(snapshot)
        if validation_errors:
            raise ValueError("主数据快照校验失败：" + "；".join(validation_errors[:10]))
        payload = {
            "task_id": task_id,
            "schedule_type": schedule_type,
            "mode": mode,
            "dispatching_rule": str(data.get("dispatching_rule", "DELIVERY")),
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
                "QUEUED",
                json.dumps(payload, ensure_ascii=False),
                json.dumps(snapshot, ensure_ascii=False),
                actor,
                now_text(),
            ),
        )
        audit(connection, actor, "TASK_CREATED", "schedule_task", task_id, {"schedule_type": schedule_type, "mode": mode})
    return task_id


def execute_task(task_id: str) -> None:
    with db() as connection:
        task = connection.execute("SELECT * FROM schedule_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not task or task["status"] not in {"QUEUED", "FAILED"}:
            return
        connection.execute(
            "UPDATE schedule_tasks SET status='RUNNING', started_at=?, completed_at=NULL, error_message=NULL WHERE task_id=?",
            (now_text(), task_id),
        )
        request_payload = json.loads(task["request_json"])
        actor = task["created_by"]

    try:
        response = algorithm_client.execute(request_payload)
        if response.get("status") != "SUCCEEDED":
            error = response.get("error") or {}
            message = error.get("message") or "算法任务执行失败"
            with db() as connection:
                connection.execute(
                    "UPDATE schedule_tasks SET status='FAILED',response_json=?,error_message=?,completed_at=? WHERE task_id=?",
                    (json.dumps(response, ensure_ascii=False), message, now_text(), task_id),
                )
                audit(connection, "algorithm", "TASK_FAILED", "schedule_task", task_id, error)
            return
        save_task_result(task_id, response, actor)
    except Exception as exc:
        with db() as connection:
            connection.execute(
                "UPDATE schedule_tasks SET status='FAILED',error_message=?,completed_at=? WHERE task_id=?",
                (str(exc), now_text(), task_id),
            )
            audit(connection, "system", "TASK_FAILED", "schedule_task", task_id, {"message": str(exc)})


def save_task_result(task_id: str, response: dict[str, Any], actor: str = "algorithm") -> str:
    with db() as connection:
        task = connection.execute("SELECT * FROM schedule_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not task:
            raise KeyError(f"任务不存在: {task_id}")
        existing = connection.execute("SELECT version_id FROM schedule_versions WHERE task_id=?", (task_id,)).fetchone()
        connection.execute(
            "UPDATE schedule_tasks SET status='SUCCEEDED',response_json=?,error_message=NULL,completed_at=? WHERE task_id=?",
            (json.dumps(response, ensure_ascii=False), response.get("completed_at") or now_text(), task_id),
        )
        if existing:
            return existing["version_id"]
        next_no = connection.execute("SELECT COALESCE(MAX(version_no),0)+1 AS no FROM schedule_versions").fetchone()["no"]
        version_id = f"PLAN-{datetime.now().strftime('%Y%m%d')}-{int(next_no):04d}"
        connection.execute(
            """INSERT INTO schedule_versions(version_id,version_no,task_id,schedule_type,status,result_json,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                version_id,
                next_no,
                task_id,
                task["schedule_type"],
                "DRAFT",
                json.dumps(response.get("result") or {}, ensure_ascii=False),
                task["created_by"],
                now_text(),
            ),
        )
        audit(connection, actor, "TASK_SUCCEEDED", "schedule_task", task_id, {"version_id": version_id})
        return version_id


def publish_version(version_id: str, actor: str) -> int:
    with db() as connection:
        version = connection.execute("SELECT * FROM schedule_versions WHERE version_id=?", (version_id,)).fetchone()
        if not version:
            raise KeyError("排程版本不存在")
        if version["status"] != "APPROVED":
            raise ValueError("只有已审批版本才能发布")
        connection.execute(
            "UPDATE schedule_versions SET status='SUPERSEDED' WHERE schedule_type=? AND status='PUBLISHED'",
            (version["schedule_type"],),
        )
        stamp = now_text()
        connection.execute(
            "UPDATE schedule_versions SET status='PUBLISHED',published_by=?,published_at=? WHERE version_id=?",
            (actor, stamp, version_id),
        )
        result = json.loads(version["result_json"])
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
                process["status"] = "CONFIRMED"
                _sync_batch_metadata(process, scheduled)
                process["locks"] = process.get("locks") or {}
                changed = True
                updated += 1
            if changed:
                connection.execute(
                    "UPDATE master_records SET payload_json=?,revision=revision+1,updated_by=?,updated_at=? WHERE entity_type='order' AND entity_id=?",
                    (json.dumps(order, ensure_ascii=False), actor, stamp, row["entity_id"]),
                )
        audit(connection, actor, "VERSION_PUBLISHED", "schedule_version", version_id, {"updated_processes": updated})
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
            changes.append({"process_id": process_id, "change_type": "ADDED" if before is None else "REMOVED" if after is None else "MODIFIED", "fields": fields})
    return {
        "left_version_id": left["version_id"],
        "right_version_id": right["version_id"],
        "changed_process_count": len(changes),
        "changes": changes,
        "kpis": {"before": left_result.get("kpis", {}), "after": right_result.get("kpis", {})},
    }
