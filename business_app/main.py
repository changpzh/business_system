from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Callable

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .algorithm_client import algorithm_client
from .config import BASE_DIR, settings
from .constants import (
    AUDIT_ACTION_MASTER_DATA_BATCH_IMPORTED,
    AUDIT_ACTION_MASTER_DATA_DELETED,
    AUDIT_ACTION_MASTER_DATA_IMPORTED,
    AUDIT_ACTION_MASTER_DATA_SAVED,
    AUDIT_ACTION_PASSWORD_CHANGED,
    AUDIT_ACTION_TASK_FAILED,
    AUDIT_ACTION_TASK_RETRIED,
    AUDIT_ACTION_USER_CREATED,
    AUDIT_ACTION_USER_LOGIN,
    AUDIT_ACTION_USER_UPDATED,
    DEPLOYMENT_PROCESS_TYPE_DEBUG,
    HEALTH_STATUS_DOWN,
    HEALTH_STATUS_UP,
    SCHEDULE_TYPES,
    TASK_ACTIVE_STATUSES,
    TASK_STATUS_FAILED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_SUCCEEDED,
    USER_ROLE_ADMIN,
    USER_ROLE_APPROVER,
    USER_ROLE_CHOICES,
    USER_ROLE_PLANNER,
    USER_ROLE_VIEWER,
    VERSION_REVIEW_DECISIONS,
    VERSION_STATUS_PUBLISHED,
)
from .database import audit, db, initialize_database, now_text
from .domain import (
    allowed_schedule_types,
    order_visible_in_deployment,
    resolve_task_schedule_type,
    schedule_type_for_order_business_type,
)
from .security import create_token, decode_token, verify_password
from .resource_calendar_validation import raise_resource_calendar_errors
from .temperature_validation import (
    machine_temperature_range,
    raise_order_temperature_errors,
)
from .worker_skills import raise_order_skill_errors, raise_worker_profile_errors
from .services import ENTITY_CONFIG, build_effective_schedule, build_machine_load_context, build_snapshot, compare_version_core_metrics, compare_versions, count_schedule_processes, create_task, execute_process_adjustment, execute_task, ga_parameters_for_process_count, is_manually_locked, lock_process, next_working_day_shift_start, parse_json_columns, preview_process_adjustment, publish_version, review_schedule_version, save_task_result, select_calendar, task_run_summary, unlock_process, validate_resource_group_preference_fields, validate_snapshot
from .system_configuration import get_system_configuration, update_system_configuration


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    payload = decode_token(authorization[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="登录已过期")
    with db() as connection:
        user = connection.execute("SELECT username,display_name,role,active FROM users WHERE username=?", (payload["sub"],)).fetchone()
    if not user or not user["active"]:
        raise HTTPException(status_code=401, detail="用户不可用")
    return dict(user)


def require_roles(*roles: str) -> Callable:
    def dependency(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="当前角色没有此操作权限")
        return user
    return dependency


def ensure_schedule_type_access(connection, schedule_type: str) -> None:
    configuration = get_system_configuration(connection)
    if str(schedule_type) not in allowed_schedule_types(configuration["deployment_process_type"]):
        raise HTTPException(status_code=404, detail="记录不属于当前部署业务单元")


def raise_master_temperature_errors(entity_type: str, record: dict[str, Any], label: str) -> None:
    if entity_type == "machine":
        machine_temperature_range(record, label)
    elif entity_type == "worker":
        raise_worker_profile_errors(record, label)
    elif entity_type == "order":
        raise_order_temperature_errors(record, label)
        raise_order_skill_errors(record, label)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": HEALTH_STATUS_UP, "component": "business-system", "version": "1.0.0"}


@app.post("/api/auth/login")
def login(payload: dict[str, Any]) -> dict[str, Any]:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    with db() as connection:
        row = connection.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        user = dict(row)
        audit(connection, username, AUDIT_ACTION_USER_LOGIN, "user", username)
    return {"token": create_token(user), "user": {key: user[key] for key in ("username", "display_name", "role")}}


@app.get("/api/auth/me")
def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return user


@app.get("/api/system-configuration")
def read_system_configuration(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        return get_system_configuration(connection)


@app.put("/api/system-configuration")
def save_system_configuration(
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN)),
) -> dict[str, Any]:
    try:
        with db() as connection:
            return update_system_configuration(connection, payload, user["username"])
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if any(marker in message for marker in ("锁定", "运行中", "已发布")) else 422
        raise HTTPException(status_code=status_code, detail=message) from exc


@app.put("/api/auth/password")
def change_password(payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, str]:
    from .security import hash_password
    if len(str(payload.get("new_password", ""))) < 8:
        raise HTTPException(status_code=422, detail="新密码至少 8 位")
    with db() as connection:
        row = connection.execute("SELECT password_hash FROM users WHERE username=?", (user["username"],)).fetchone()
        if not verify_password(str(payload.get("old_password", "")), row["password_hash"]):
            raise HTTPException(status_code=422, detail="原密码错误")
        connection.execute("UPDATE users SET password_hash=?,updated_at=? WHERE username=?", (hash_password(str(payload["new_password"])), now_text(), user["username"]))
        audit(
            connection,
            user["username"],
            AUDIT_ACTION_PASSWORD_CHANGED,
            "user",
            user["username"],
        )
    return {"message": "密码修改成功"}


@app.get("/api/users")
def list_users(_: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN))) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute("SELECT username,display_name,role,active,created_at,updated_at FROM users ORDER BY username").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/users", status_code=201)
def create_user(
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN)),
) -> dict[str, Any]:
    from .security import hash_password
    username = str(payload.get("username", "")).strip()
    role = str(payload.get("role", USER_ROLE_VIEWER))
    password = str(payload.get("password", ""))
    if not username or len(password) < 8 or role not in USER_ROLE_CHOICES:
        raise HTTPException(status_code=422, detail="用户名、至少 8 位密码和合法角色为必填项")
    stamp = now_text()
    try:
        with db() as connection:
            connection.execute(
                "INSERT INTO users(username,display_name,password_hash,role,active,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                (username, str(payload.get("display_name") or username), hash_password(password), role, stamp, stamp),
            )
            audit(
                connection,
                user["username"],
                AUDIT_ACTION_USER_CREATED,
                "user",
                username,
                {"role": role},
            )
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(status_code=409, detail="用户名已存在") from exc
        raise
    return {"username": username, "display_name": str(payload.get("display_name") or username), "role": role, "active": 1}


@app.put("/api/users/{username}")
def update_user(
    username: str,
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN)),
) -> dict[str, Any]:
    from .security import hash_password
    role = str(payload.get("role", USER_ROLE_VIEWER))
    active = 1 if payload.get("active", True) else 0
    if role not in USER_ROLE_CHOICES:
        raise HTTPException(status_code=422, detail="用户角色不合法")
    if username == user["username"] and not active:
        raise HTTPException(status_code=409, detail="不能停用当前登录用户")
    with db() as connection:
        row = connection.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        fields: list[Any] = [str(payload.get("display_name") or username), role, active, now_text()]
        sql = "UPDATE users SET display_name=?,role=?,active=?,updated_at=?"
        if payload.get("password"):
            if len(str(payload["password"])) < 8:
                raise HTTPException(status_code=422, detail="重置密码至少 8 位")
            sql += ",password_hash=?"
            fields.append(hash_password(str(payload["password"])))
        sql += " WHERE username=?"
        fields.append(username)
        connection.execute(sql, fields)
        audit(
            connection,
            user["username"],
            AUDIT_ACTION_USER_UPDATED,
            "user",
            username,
            {"role": role, "active": active},
        )
    return {"username": username, "display_name": str(payload.get("display_name") or username), "role": role, "active": active}


@app.get("/api/dashboard")
def dashboard(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        system_configuration = get_system_configuration(connection)
        visible_schedule_types = allowed_schedule_types(system_configuration["deployment_process_type"])
        counts = {row["entity_type"]: row["count"] for row in connection.execute("SELECT entity_type,COUNT(*) AS count FROM master_records GROUP BY entity_type")}
        tasks: dict[str, int] = {}
        for row in connection.execute(
            "SELECT schedule_type,status,COUNT(*) AS count FROM schedule_tasks GROUP BY schedule_type,status"
        ):
            if row["schedule_type"] in visible_schedule_types:
                tasks[row["status"]] = tasks.get(row["status"], 0) + row["count"]
        versions: dict[str, int] = {}
        for row in connection.execute(
            "SELECT schedule_type,status,COUNT(*) AS count FROM schedule_versions GROUP BY schedule_type,status"
        ):
            if row["schedule_type"] in visible_schedule_types:
                versions[row["status"]] = versions.get(row["status"], 0) + row["count"]
        latest = [dict(row) for row in connection.execute("SELECT task_id,schedule_type,mode,status,created_by,created_at,completed_at,error_message FROM schedule_tasks ORDER BY created_at DESC LIMIT 8")]
        published = [
            dict(row)
            for row in connection.execute(
                "SELECT version_id,schedule_type,published_by,published_at "
                "FROM schedule_versions WHERE status=? ORDER BY published_at DESC",
                (VERSION_STATUS_PUBLISHED,),
            )
        ]
        visible_order_count = sum(
            order_visible_in_deployment(
                json.loads(row["payload_json"]).get("order_business_type"),
                system_configuration["deployment_process_type"],
            )
            for row in connection.execute(
                "SELECT payload_json FROM master_records WHERE entity_type='order'"
            ).fetchall()
        )
    counts["order"] = visible_order_count
    latest = [row for row in latest if row["schedule_type"] in visible_schedule_types]
    published = [row for row in published if row["schedule_type"] in visible_schedule_types]
    try:
        algorithm = algorithm_client.health()
    except Exception as exc:
        algorithm = {"status": HEALTH_STATUS_DOWN, "message": str(exc)}
    return {"master_counts": counts, "task_counts": tasks, "version_counts": versions, "latest_tasks": latest, "published_versions": published, "algorithm": algorithm, "system_configuration": system_configuration}


@app.get("/api/algorithm/health")
def algorithm_health(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    try:
        return {"reachable": True, "base_url": settings.algorithm_base_url, "response": algorithm_client.health()}
    except Exception as exc:
        return {"reachable": False, "base_url": settings.algorithm_base_url, "error": str(exc)}


@app.get("/api/algorithm/capabilities")
def algorithm_capabilities(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    try:
        return algorithm_client.capabilities()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/effective-schedule")
def effective_schedule(
    schedule_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    process_status: str | None = None,
    order_id: str | None = None,
    machine_id: str | None = None,
    worker_id: str | None = None,
    _: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with db() as connection:
        configuration = get_system_configuration(connection)
        fixed_types = allowed_schedule_types(configuration["deployment_process_type"])
        if configuration["deployment_process_type"] != DEPLOYMENT_PROCESS_TYPE_DEBUG:
            schedule_type = next(iter(fixed_types))
        return build_effective_schedule(
            connection,
            schedule_type=schedule_type,
            start_time=start_time,
            end_time=end_time,
            process_status=process_status,
            order_id=order_id,
            machine_id=machine_id,
            worker_id=worker_id,
        )


@app.post("/api/processes/{process_id}/lock")
def manually_lock_process(
    process_id: str,
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, Any]:
    try:
        return lock_process(process_id, payload, user["username"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 409 if "已变化" in str(exc) or "人工资源锁冲突" in str(exc) else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.delete("/api/processes/{process_id}/lock")
def manually_unlock_process(
    process_id: str,
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, Any]:
    try:
        return unlock_process(process_id, payload, user["username"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 409 if "已变化" in str(exc) else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/processes/{process_id}/adjustments/preview")
def preview_manual_process_adjustment(
    process_id: str,
    payload: dict[str, Any],
    _: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, Any]:
    try:
        return preview_process_adjustment(process_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 409 if "已变化" in str(exc) else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/processes/{process_id}/adjustments/execute")
def execute_manual_process_adjustment(
    process_id: str,
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, Any]:
    try:
        return execute_process_adjustment(process_id, payload, user["username"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 409 if "已变化" in str(exc) else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/api/master-data/snapshot")
def snapshot(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        result = build_snapshot(connection, include_all_calendars=True)
        configuration = get_system_configuration(connection)
    result["order_processes"] = [
        order
        for order in result.get("order_processes", [])
        if order_visible_in_deployment(
            order.get("order_business_type"), configuration["deployment_process_type"]
        )
    ]
    return result


@app.get("/api/master-data/validate")
def validate_master_data(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        machining_errors = validate_snapshot(build_snapshot(connection, "machining"))
        errors = [f"机加日历：{item}" if item.startswith("工厂日历") else item for item in machining_errors]
        for schedule_type, label in (("heat_treatment", "热表"), ("assembly", "装配")):
            calendar_errors = [
                item
                for item in validate_snapshot(build_snapshot(connection, schedule_type))
                if item.startswith("工厂日历")
            ]
            errors.extend(f"{label}日历：{item}" for item in calendar_errors)
    return {"valid": not errors, "errors": errors}


@app.post("/api/master-data/import")
def import_snapshot(
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, Any]:
    required_roots = {
        "machine_calendars",
        "machine_profiles",
        "worker_profiles",
        "resource_group_profiles",
        "order_processes",
    }
    missing_roots = sorted(required_roots - set(payload))
    if missing_roots:
        raise HTTPException(status_code=422, detail=f"完整业务快照缺少字段: {missing_roots}")
    with db() as connection:
        configuration = get_system_configuration(connection)
        imported = 0
        for entity_type, (snapshot_key, id_field) in ENTITY_CONFIG.items():
            source_value = (
                payload.get("machine_calendars")
                if entity_type == "calendar"
                else payload.get(snapshot_key)
            )
            if source_value is None:
                continue
            records = source_value if isinstance(source_value, list) else [source_value]
            if entity_type == "calendar":
                calendar_types = [str(record.get("schedule_type") or "") for record in records]
                if sorted(calendar_types) != ["assembly", "heat_treatment", "machining"]:
                    raise HTTPException(
                        status_code=422,
                        detail="machine_calendars 必须且只能包含机加、热表、装配三类日历各一份",
                    )
            for record in records:
                if not isinstance(record, dict) or not record.get(id_field):
                    raise HTTPException(status_code=422, detail=f"{snapshot_key} 记录缺少 {id_field}")
                try:
                    raise_master_temperature_errors(
                        entity_type,
                        record,
                        f"{snapshot_key} {record.get(id_field)}",
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                if entity_type in {"machine", "worker"}:
                    label = "设备" if entity_type == "machine" else "人员"
                    try:
                        raise_resource_calendar_errors(record, f"{label} {record.get(id_field)}")
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail=str(exc)) from exc
                if entity_type == "order" and not order_visible_in_deployment(
                    record.get("order_business_type"), configuration["deployment_process_type"]
                ):
                    raise HTTPException(status_code=422, detail=f"订单 {record.get(id_field)} 不属于当前部署业务单元")
                entity_id = str(record[id_field])
                connection.execute(
                    """INSERT INTO master_records(entity_type,entity_id,payload_json,revision,updated_by,updated_at) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(entity_type,entity_id) DO UPDATE SET payload_json=excluded.payload_json,revision=master_records.revision+1,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                    (entity_type, entity_id, json.dumps(record, ensure_ascii=False), 1, user["username"], now_text()),
                )
                imported += 1
        audit(
            connection,
            user["username"],
            AUDIT_ACTION_MASTER_DATA_IMPORTED,
            "master_data",
            "snapshot",
            {"count": imported},
        )
    return {"imported": imported}


@app.get("/api/master-data/{entity_type}")
def list_master(entity_type: str, _: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    if entity_type not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail="未知主数据类型")
    with db() as connection:
        rows = connection.execute("SELECT * FROM master_records WHERE entity_type=? ORDER BY entity_id", (entity_type,)).fetchall()
        configuration = get_system_configuration(connection)
    records = [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]
    if entity_type == "order":
        records = [
            record
            for record in records
            if order_visible_in_deployment(
                record["payload"].get("order_business_type"),
                configuration["deployment_process_type"],
            )
        ]
    return records


@app.delete("/api/master-data/{entity_type}")
def delete_master_batch(
    entity_type: str,
    schedule_type: str | None = None,
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, Any]:
    if entity_type not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail="未知主数据类型")

    scoped_entity_types = {"order", "calendar"}
    normalized_schedule_type = str(schedule_type or "").strip().lower()
    if entity_type in scoped_entity_types:
        if normalized_schedule_type not in SCHEDULE_TYPES:
            raise HTTPException(status_code=422, detail="订单和日历批量删除必须指定有效的 schedule_type")
    elif normalized_schedule_type:
        raise HTTPException(status_code=422, detail="当前主数据类型不支持 schedule_type 删除范围")

    with db() as connection:
        if entity_type == "order":
            ensure_schedule_type_access(connection, normalized_schedule_type)
        rows = connection.execute(
            "SELECT entity_id,payload_json FROM master_records WHERE entity_type=? ORDER BY entity_id",
            (entity_type,),
        ).fetchall()
        if entity_type == "order":
            rows = [
                row
                for row in rows
                if schedule_type_for_order_business_type(json.loads(row["payload_json"]).get("order_business_type"))
                == normalized_schedule_type
            ]
        elif entity_type == "calendar":
            rows = [
                row
                for row in rows
                if str(json.loads(row["payload_json"]).get("schedule_type") or "").lower()
                == normalized_schedule_type
            ]

        entity_ids = [str(row["entity_id"]) for row in rows]
        if entity_ids:
            connection.executemany(
                "DELETE FROM master_records WHERE entity_type=? AND entity_id=?",
                [(entity_type, entity_id) for entity_id in entity_ids],
            )
        audit(
            connection,
            user["username"],
            AUDIT_ACTION_MASTER_DATA_DELETED,
            entity_type,
            normalized_schedule_type or "all",
            {
                "batch": True,
                "count": len(entity_ids),
                "schedule_type": normalized_schedule_type or None,
            },
        )
    return {
        "message": "批量删除成功",
        "entity_type": entity_type,
        "schedule_type": normalized_schedule_type or None,
        "deleted": len(entity_ids),
    }


@app.get("/api/master-data/{entity_type}/batch")
def export_master_batch(entity_type: str, _: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    if entity_type not in {"order", "machine", "worker", "resource_group"}:
        raise HTTPException(status_code=404, detail="当前主数据类型不支持批量导出")
    with db() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM master_records WHERE entity_type=? ORDER BY entity_id", (entity_type,)
        ).fetchall()
        configuration = get_system_configuration(connection)
    records = [json.loads(row["payload_json"]) for row in rows]
    if entity_type == "order":
        records = [
            record
            for record in records
            if order_visible_in_deployment(
                record.get("order_business_type"), configuration["deployment_process_type"]
            )
        ]
    return records


@app.post("/api/master-data/{entity_type}/batch")
def import_master_batch(
    entity_type: str,
    payload: list[dict[str, Any]],
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, Any]:
    if entity_type not in {"order", "machine", "worker", "resource_group"}:
        raise HTTPException(status_code=404, detail="当前主数据类型不支持批量导入")
    if not payload:
        raise HTTPException(status_code=422, detail="批量导入数组不能为空")
    id_field = ENTITY_CONFIG[entity_type][1]
    order_configuration = None
    if entity_type == "order":
        with db() as connection:
            order_configuration = get_system_configuration(connection)
    seen_ids: set[str] = set()
    for index, record in enumerate(payload):
        entity_id = str(record.get(id_field, "")).strip()
        if not entity_id:
            raise HTTPException(status_code=422, detail=f"第 {index + 1} 条记录缺少 {id_field}")
        try:
            raise_master_temperature_errors(entity_type, record, f"第 {index + 1} 条记录")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if entity_type in {"machine", "worker"}:
            label = "设备" if entity_type == "machine" else "人员"
            try:
                raise_resource_calendar_errors(record, f"{label} {entity_id}")
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        if entity_type == "resource_group":
            errors = validate_resource_group_preference_fields(record)
            if errors:
                raise HTTPException(status_code=422, detail="；".join(errors))
        if entity_id in seen_ids:
            raise HTTPException(status_code=422, detail=f"批量文件中编号重复: {entity_id}")
        if order_configuration and not order_visible_in_deployment(
            record.get("order_business_type"), order_configuration["deployment_process_type"]
        ):
            raise HTTPException(status_code=422, detail=f"订单 {entity_id} 不属于当前部署业务单元")
        seen_ids.add(entity_id)

    stamp = now_text()
    with db() as connection:
        for record in payload:
            entity_id = str(record[id_field])
            connection.execute(
                """INSERT INTO master_records(entity_type,entity_id,payload_json,revision,updated_by,updated_at) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(entity_type,entity_id) DO UPDATE SET payload_json=excluded.payload_json,revision=master_records.revision+1,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (entity_type, entity_id, json.dumps(record, ensure_ascii=False), 1, user["username"], stamp),
            )
        audit(
            connection,
            user["username"],
            AUDIT_ACTION_MASTER_DATA_BATCH_IMPORTED,
            entity_type,
            "batch",
            {"count": len(payload), "entity_ids": sorted(seen_ids)},
        )
    return {"entity_type": entity_type, "imported": len(payload)}


@app.put("/api/master-data/{entity_type}/{entity_id}")
def put_master(
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, Any]:
    if entity_type not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail="未知主数据类型")
    id_field = ENTITY_CONFIG[entity_type][1]
    if str(payload.get(id_field, "")) != entity_id:
        raise HTTPException(status_code=422, detail=f"请求路径与 {id_field} 不一致")
    try:
        raise_master_temperature_errors(entity_type, payload, f"{entity_type} {entity_id}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if entity_type in {"machine", "worker"}:
        label = "设备" if entity_type == "machine" else "人员"
        try:
            raise_resource_calendar_errors(payload, f"{label} {entity_id}")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if entity_type == "resource_group":
        errors = validate_resource_group_preference_fields(payload)
        if errors:
            raise HTTPException(status_code=422, detail="；".join(errors))
    if entity_type == "calendar" and payload.get("schedule_type") not in {"machining", "heat_treatment", "assembly"}:
        raise HTTPException(status_code=422, detail="日历必须指定 machining、heat_treatment 或 assembly")
    with db() as connection:
        if entity_type == "order":
            configuration = get_system_configuration(connection)
            if not order_visible_in_deployment(
                payload.get("order_business_type"), configuration["deployment_process_type"]
            ):
                raise HTTPException(status_code=422, detail="订单不属于当前部署业务单元")
        if entity_type == "calendar":
            rows = connection.execute(
                "SELECT entity_id,payload_json FROM master_records WHERE entity_type='calendar' AND entity_id<>?",
                (entity_id,),
            ).fetchall()
            duplicate = next(
                (
                    row["entity_id"]
                    for row in rows
                    if json.loads(row["payload_json"]).get("schedule_type") == payload.get("schedule_type")
                ),
                None,
            )
            if duplicate:
                raise HTTPException(
                    status_code=409,
                    detail=f"该工艺类型已存在日历 {duplicate}，请直接编辑现有日历",
                )
        connection.execute(
            """INSERT INTO master_records(entity_type,entity_id,payload_json,revision,updated_by,updated_at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(entity_type,entity_id) DO UPDATE SET payload_json=excluded.payload_json,revision=master_records.revision+1,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (entity_type, entity_id, json.dumps(payload, ensure_ascii=False), 1, user["username"], now_text()),
        )
        audit(
            connection,
            user["username"],
            AUDIT_ACTION_MASTER_DATA_SAVED,
            entity_type,
            entity_id,
        )
        row = connection.execute("SELECT * FROM master_records WHERE entity_type=? AND entity_id=?", (entity_type, entity_id)).fetchone()
    return {**dict(row), "payload": json.loads(row["payload_json"])}


@app.delete("/api/master-data/{entity_type}/{entity_id}")
def delete_master(
    entity_type: str,
    entity_id: str,
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, str]:
    if entity_type not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail="未知主数据类型")
    if entity_type == "calendar":
        raise HTTPException(status_code=409, detail="机加、热表和装配日历为必需配置，请直接编辑现有日历")
    with db() as connection:
        cursor = connection.execute("DELETE FROM master_records WHERE entity_type=? AND entity_id=?", (entity_type, entity_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="记录不存在")
        audit(
            connection,
            user["username"],
            AUDIT_ACTION_MASTER_DATA_DELETED,
            entity_type,
            entity_id,
        )
    return {"message": "删除成功"}


@app.post("/api/tasks", status_code=status.HTTP_202_ACCEPTED)
def submit_task(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, str]:
    try:
        with db() as connection:
            configuration = get_system_configuration(connection)
        payload = dict(payload)
        payload["schedule_type"] = resolve_task_schedule_type(
            configuration["deployment_process_type"], payload.get("schedule_type")
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.get("mode", "static") not in {"static", "dynamic", "local"}:
        raise HTTPException(status_code=422, detail="mode 不合法")
    if payload.get("mode") == "local" and not payload.get("local_adjustments"):
        raise HTTPException(status_code=422, detail="局部微调必须提供 local_adjustments")
    if not payload.get("schedule_start"):
        raise HTTPException(status_code=422, detail="schedule_start 为必填字段")
    try:
        datetime.fromisoformat(str(payload["schedule_start"]))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="schedule_start 必须是 ISO 日期时间") from exc
    try:
        task_id = create_task(payload, user["username"])
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(status_code=409, detail="task_id 已存在") from exc
        raise
    background_tasks.add_task(execute_task, task_id)
    return {"task_id": task_id, "status": TASK_STATUS_QUEUED}


@app.get("/api/tasks")
def list_tasks(limit: int = 100, _: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute("""SELECT task_id,schedule_type,mode,dispatching_rule,status,error_message,created_by,created_at,started_at,completed_at,
                                            json_extract(request_json,'$.config_overrides.nsga3.population_size') AS configured_population_size,
                                            json_extract(request_json,'$.config_overrides.nsga3.generations') AS configured_generations
                                       FROM schedule_tasks ORDER BY created_at DESC LIMIT ?""", (min(max(limit, 1), 500),)).fetchall()
        configuration = get_system_configuration(connection)
    allowed_types = allowed_schedule_types(configuration["deployment_process_type"])
    rows = [row for row in rows if row["schedule_type"] in allowed_types]
    records = []
    for row in rows:
        item = dict(row)
        item.update(task_run_summary(item))
        records.append(item)
    return records


@app.get("/api/tasks/defaults")
def task_defaults(
    schedule_type: str | None = None,
    _: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    try:
        with db() as connection:
            configuration = get_system_configuration(connection)
            resolved_schedule_type = resolve_task_schedule_type(
                configuration["deployment_process_type"], schedule_type
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with db() as connection:
        calendar = select_calendar(connection, resolved_schedule_type)
        process_count = count_schedule_processes(connection, resolved_schedule_type)
    try:
        schedule_start = next_working_day_shift_start(calendar)
        source = "factory_calendar"
    except (KeyError, TypeError, ValueError):
        fallback_day = datetime.now().date() + timedelta(days=1)
        while fallback_day.weekday() >= 5:
            fallback_day += timedelta(days=1)
        schedule_start = datetime.combine(fallback_day, datetime.strptime("08:00", "%H:%M").time())
        source = "weekday_fallback"
    return {
        "schedule_start": schedule_start.isoformat(timespec="minutes"),
        "source": source,
        "schedule_type": resolved_schedule_type,
        "deployment_process_type": configuration["deployment_process_type"],
        **ga_parameters_for_process_count(process_count),
    }


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        row = connection.execute("SELECT * FROM schedule_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row:
            ensure_schedule_type_access(connection, row["schedule_type"])
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return parse_json_columns(dict(row), "request_json", "snapshot_json", "response_json")


@app.get("/api/tasks/{task_id}/summary")
def get_task_summary(task_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """返回任务弹框需要的轻量摘要，避免传输并渲染完整快照和排程结果。"""
    with db() as connection:
        row = connection.execute(
            """SELECT t.task_id,t.schedule_type,t.mode,t.dispatching_rule,t.status,t.error_message,
                      t.created_by,t.created_at,t.started_at,t.completed_at,
                      json_extract(t.request_json,'$.schedule_start') AS schedule_start,
                      json_extract(t.request_json,'$.config_overrides') AS config_overrides_json,
                      json_extract(t.request_json,'$.local_adjustments') AS local_adjustments_json,
                      json_array_length(t.snapshot_json,'$.order_processes') AS order_count,
                      COALESCE((
                          SELECT SUM(json_array_length(order_row.value,'$.processes'))
                          FROM json_each(t.snapshot_json,'$.order_processes') AS order_row
                      ),0) AS process_count,
                      json_array_length(t.snapshot_json,'$.machine_profiles') AS machine_count,
                      json_array_length(t.snapshot_json,'$.worker_profiles') AS worker_count,
                      json_array_length(t.snapshot_json,'$.resource_group_profiles') AS resource_group_count,
                      json_type(t.snapshot_json,'$.machine_calendar') AS calendar_type,
                      length(CAST(t.request_json AS BLOB)) AS request_bytes,
                      length(CAST(t.snapshot_json AS BLOB)) AS snapshot_bytes,
                      length(CAST(t.response_json AS BLOB)) AS response_bytes,
                      json_extract(t.response_json,'$.status') AS response_status,
                      json_extract(t.response_json,'$.completed_at') AS response_completed_at,
                      json_extract(t.response_json,'$.error') AS response_error_json,
                      json_extract(t.response_json,'$.result.metadata.schedule_type') AS result_schedule_type,
                      json_extract(t.response_json,'$.result.metadata.mode') AS result_mode,
                      json_extract(t.response_json,'$.result.metadata.dispatching_rule') AS result_dispatching_rule,
                      json_extract(t.response_json,'$.result.metadata.generated_at') AS result_generated_at,
                      json_extract(t.response_json,'$.result.metadata.schedule_start') AS result_schedule_start,
                      json_extract(t.response_json,'$.result.metadata.order_count') AS result_order_count,
                      json_extract(t.response_json,'$.result.metadata.task_count') AS result_task_count,
                      json_extract(t.response_json,'$.result.metadata.optimization_mode') AS optimization_mode,
                      json_extract(t.response_json,'$.result.metadata.optimizer') AS optimizer_json,
                      json_extract(t.response_json,'$.result.kpis') AS kpis_json,
                      json_extract(t.response_json,'$.result.best_objectives') AS best_objectives_json,
                      json_array_length(t.response_json,'$.result.schedule') AS schedule_count,
                      json_array_length(t.response_json,'$.result.pareto_front') AS pareto_count,
                      json_array_length(t.response_json,'$.result.optimization_trace') AS trace_count
               FROM schedule_tasks AS t
               WHERE t.task_id=?""",
            (task_id,),
        ).fetchone()
        if row:
            ensure_schedule_type_access(connection, row["schedule_type"])
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")

    item = dict(row)

    def load_json_value(key: str, default: Any) -> Any:
        value = item.get(key)
        if value in (None, ""):
            return default
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    response_available = bool(item.get("response_bytes"))
    response_summary = {
        "task_id": item["task_id"],
        "status": item.get("response_status") or item["status"],
        "completed_at": item.get("response_completed_at") or item.get("completed_at"),
        "error": load_json_value("response_error_json", None),
        "result": {
            "metadata": {
                "schedule_type": item.get("result_schedule_type"),
                "mode": item.get("result_mode"),
                "dispatching_rule": item.get("result_dispatching_rule"),
                "generated_at": item.get("result_generated_at"),
                "schedule_start": item.get("result_schedule_start"),
                "order_count": item.get("result_order_count"),
                "task_count": item.get("result_task_count"),
                "optimization_mode": item.get("optimization_mode"),
                "optimizer": load_json_value("optimizer_json", None),
            },
            "kpis": load_json_value("kpis_json", None),
            "best_objectives": load_json_value("best_objectives_json", None),
            "schedule_count": item.get("schedule_count") or 0,
            "pareto_count": item.get("pareto_count") or 0,
            "optimization_trace_count": item.get("trace_count") or 0,
        },
    }
    if not response_available:
        response_summary = {
            "status": item["status"],
            "message": (
                "任务尚未完成，暂无算法结果"
                if item["status"] in TASK_ACTIVE_STATUSES
                else "暂无结果数据"
            ),
        }

    return {
        "task_id": item["task_id"],
        "schedule_type": item["schedule_type"],
        "mode": item["mode"],
        "dispatching_rule": item["dispatching_rule"],
        "status": item["status"],
        "error_message": item.get("error_message"),
        "created_by": item["created_by"],
        "created_at": item["created_at"],
        "started_at": item.get("started_at"),
        "completed_at": item.get("completed_at"),
        "request": {
            "task_id": item["task_id"],
            "schedule_type": item["schedule_type"],
            "mode": item["mode"],
            "dispatching_rule": item["dispatching_rule"],
            "schedule_start": item.get("schedule_start"),
            "config_overrides": load_json_value("config_overrides_json", {}),
            "local_adjustments": load_json_value("local_adjustments_json", []),
        },
        "snapshot_summary": {
            "order_count": item.get("order_count") or 0,
            "process_count": item.get("process_count") or 0,
            "machine_count": item.get("machine_count") or 0,
            "worker_count": item.get("worker_count") or 0,
            "resource_group_count": item.get("resource_group_count") or 0,
            "calendar_configured": bool(item.get("calendar_type")),
        },
        "response": response_summary,
        "payload_sizes": {
            "request": item.get("request_bytes") or 0,
            "snapshot": item.get("snapshot_bytes") or 0,
            "response": item.get("response_bytes") or 0,
        },
        "response_available": response_available,
    }


@app.get("/api/tasks/{task_id}/payload/{payload_kind}")
def download_task_payload(
    task_id: str,
    payload_kind: str,
    _: dict[str, Any] = Depends(current_user),
) -> Response:
    """流式返回原始任务 JSON，供用户按需下载，不在弹框 DOM 中展开。"""
    columns = {
        "request": "request_json",
        "snapshot": "snapshot_json",
        "response": "response_json",
    }
    column = columns.get(payload_kind)
    if not column:
        raise HTTPException(status_code=422, detail="payload_kind 必须为 request、snapshot 或 response")
    with db() as connection:
        row = connection.execute(
            f"SELECT task_id,schedule_type,{column} AS payload_json FROM schedule_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row:
            ensure_schedule_type_access(connection, row["schedule_type"])
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not row["payload_json"]:
        raise HTTPException(status_code=404, detail="当前任务暂无该类数据")
    safe_task_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in task_id)
    return Response(
        content=row["payload_json"],
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_task_id}-{payload_kind}.json"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/tasks/{task_id}/retry", status_code=202)
def retry_task(
    task_id: str,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_PLANNER)),
) -> dict[str, str]:
    with db() as connection:
        row = connection.execute("SELECT status,schedule_type FROM schedule_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        ensure_schedule_type_access(connection, row["schedule_type"])
        if row["status"] != TASK_STATUS_FAILED:
            raise HTTPException(status_code=409, detail="只有失败任务可以重试")
        connection.execute(
            "UPDATE schedule_tasks SET status=?,error_message=NULL WHERE task_id=?",
            (TASK_STATUS_QUEUED, task_id),
        )
        audit(
            connection,
            user["username"],
            AUDIT_ACTION_TASK_RETRIED,
            "schedule_task",
            task_id,
        )
    background_tasks.add_task(execute_task, task_id)
    return {"task_id": task_id, "status": TASK_STATUS_QUEUED}


@app.post("/api/schedule-callbacks")
def schedule_callback(payload: dict[str, Any]) -> dict[str, Any]:
    task_id = str(payload.get("task_id", ""))
    if not task_id:
        raise HTTPException(status_code=422, detail="缺少 task_id")
    if payload.get("status") == TASK_STATUS_SUCCEEDED:
        try:
            version_id = save_task_result(task_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"accepted": True, "version_id": version_id}
    with db() as connection:
        exists = connection.execute("SELECT task_id FROM schedule_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="任务不存在")
        error = payload.get("error") or {}
        connection.execute(
            "UPDATE schedule_tasks SET status=?,response_json=?,error_message=?,completed_at=? WHERE task_id=?",
            (
                TASK_STATUS_FAILED,
                json.dumps(payload, ensure_ascii=False),
                error.get("message", "算法任务失败"),
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
    return {"accepted": True}


@app.get("/api/versions")
def list_versions(_: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute("""SELECT v.version_id,v.version_no,v.task_id,v.schedule_type,v.status,v.created_by,v.created_at,v.reviewed_by,v.reviewed_at,v.review_comment,v.published_by,v.published_at,t.mode,t.dispatching_rule,t.started_at,t.completed_at,
                                           json_extract(t.request_json,'$.config_overrides.nsga3.population_size') AS configured_population_size,
                                           json_extract(t.request_json,'$.config_overrides.nsga3.generations') AS configured_generations
                                   FROM schedule_versions v JOIN schedule_tasks t ON t.task_id=v.task_id ORDER BY v.version_no DESC""").fetchall()
        configuration = get_system_configuration(connection)
    allowed_types = allowed_schedule_types(configuration["deployment_process_type"])
    rows = [row for row in rows if row["schedule_type"] in allowed_types]
    records = []
    for row in rows:
        item = dict(row)
        item.update(task_run_summary(item))
        records.append(item)
    return records


@app.get("/api/versions/{version_id}")
def get_version(version_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        row = connection.execute(
            """SELECT v.*,t.snapshot_json,t.request_json,t.mode,t.dispatching_rule,t.started_at,t.completed_at,
                      json_extract(t.request_json,'$.config_overrides.nsga3.population_size') AS configured_population_size,
                      json_extract(t.request_json,'$.config_overrides.nsga3.generations') AS configured_generations
               FROM schedule_versions v
               JOIN schedule_tasks t ON t.task_id=v.task_id
               WHERE v.version_id=?""",
            (version_id,),
        ).fetchone()
        master_order_rows = connection.execute(
            "SELECT payload_json FROM master_records WHERE entity_type='order'"
        ).fetchall()
    if not row:
        raise HTTPException(status_code=404, detail="排程版本不存在")
    record = dict(row)
    with db() as connection:
        ensure_schedule_type_access(connection, record["schedule_type"])
    request_value = json.loads(record.get("request_json") or "{}")
    run_summary = task_run_summary(record)
    record.update(run_summary)
    record["schedule_parameters"] = {
        **run_summary,
        "config_overrides": request_value.get("config_overrides") or {},
    }
    record.pop("request_json", None)
    record.pop("configured_population_size", None)
    record.pop("configured_generations", None)
    result = json.loads(record.pop("result_json"))
    snapshot = json.loads(record.pop("snapshot_json"))
    record["machine_load_context"] = build_machine_load_context(
        snapshot,
        result,
        run_summary.get("schedule_start"),
    )
    process_index = {
        str(process.get("process_id")): process
        for order in snapshot.get("order_processes", [])
        for process in order.get("processes", [])
        if process.get("process_id")
    }
    machine_index = {
        str(machine.get("machine_id")): machine
        for machine in snapshot.get("machine_profiles", [])
        if machine.get("machine_id")
    }
    current_process_index = {
        str(process.get("process_id")): process
        for row in master_order_rows
        for order in [json.loads(row["payload_json"])]
        for process in order.get("processes", [])
        if process.get("process_id")
    }
    for item in result.get("schedule", []):
        process_id = str(item.get("process_id"))
        machine_id = str(item.get("machine_id") or "")
        source = process_index.get(process_id, {})
        current = current_process_index.get(process_id, {})
        if machine_id and not item.get("machine_name"):
            item["machine_name"] = machine_index.get(machine_id, {}).get("machine_name") or ""
        if "material_ready_time" not in item:
            item["material_ready_time"] = source.get("material_ready_time") or ""
        source_locks = source.get("locks") or {}
        item["manually_locked"] = is_manually_locked(source)
        item["lock_details"] = source_locks
        item["source_process_status"] = source.get("status") or item.get("source_status") or ""
        effective_version_id = str(current.get("schedule_version_id") or "")
        item["effective_schedule_version_id"] = effective_version_id
        item["is_effective_version"] = effective_version_id == version_id
        item["effective_status"] = (
            current.get("status") or "" if item["is_effective_version"] else ""
        )
    record["result"] = result
    return record


@app.post("/api/versions/{version_id}/review")
def review_version(
    version_id: str,
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_APPROVER)),
) -> dict[str, Any]:
    decision = str(payload.get("decision", "")).upper()
    if decision not in VERSION_REVIEW_DECISIONS:
        raise HTTPException(status_code=422, detail="decision 必须为 APPROVED 或 REJECTED")
    with db() as connection:
        version = connection.execute("SELECT schedule_type FROM schedule_versions WHERE version_id=?", (version_id,)).fetchone()
        if version:
            ensure_schedule_type_access(connection, version["schedule_type"])
    try:
        return review_schedule_version(
            version_id,
            decision,
            user["username"],
            str(payload.get("comment", "")),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/versions/{version_id}/publish")
def publish(
    version_id: str,
    user: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_APPROVER)),
) -> dict[str, Any]:
    with db() as connection:
        version = connection.execute("SELECT schedule_type FROM schedule_versions WHERE version_id=?", (version_id,)).fetchone()
        if version:
            ensure_schedule_type_access(connection, version["schedule_type"])
    try:
        updated = publish_version(version_id, user["username"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "version_id": version_id,
        "status": VERSION_STATUS_PUBLISHED,
        "updated_processes": updated,
    }


@app.get("/api/versions/compare/{left_id}/{right_id}")
def compare(left_id: str, right_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        left = connection.execute(
            """SELECT v.*,t.mode,t.dispatching_rule,t.started_at,t.completed_at,
                      json_extract(t.request_json,'$.config_overrides.nsga3.population_size') AS configured_population_size,
                      json_extract(t.request_json,'$.config_overrides.nsga3.generations') AS configured_generations
                 FROM schedule_versions v JOIN schedule_tasks t ON t.task_id=v.task_id
                WHERE v.version_id=?""",
            (left_id,),
        ).fetchone()
        right = connection.execute(
            """SELECT v.*,t.mode,t.dispatching_rule,t.started_at,t.completed_at,
                      json_extract(t.request_json,'$.config_overrides.nsga3.population_size') AS configured_population_size,
                      json_extract(t.request_json,'$.config_overrides.nsga3.generations') AS configured_generations
                 FROM schedule_versions v JOIN schedule_tasks t ON t.task_id=v.task_id
                WHERE v.version_id=?""",
            (right_id,),
        ).fetchone()
    if not left or not right:
        raise HTTPException(status_code=404, detail="对比版本不存在")
    with db() as connection:
        ensure_schedule_type_access(connection, left["schedule_type"])
        ensure_schedule_type_access(connection, right["schedule_type"])
    return compare_versions(dict(left), dict(right))


@app.post("/api/versions/compare-metrics")
def compare_metrics(
    payload: dict[str, Any], _: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    raw_version_ids = payload.get("version_ids")
    if not isinstance(raw_version_ids, list):
        raise HTTPException(status_code=422, detail="version_ids 必须是版本编号数组")
    version_ids: list[str] = []
    for value in raw_version_ids:
        version_id = str(value).strip() if isinstance(value, str) else ""
        if version_id and version_id not in version_ids:
            version_ids.append(version_id)
    if len(version_ids) < 2:
        raise HTTPException(status_code=422, detail="请至少选择两个不同的计划版本")

    placeholders = ",".join("?" for _ in version_ids)
    with db() as connection:
        rows = connection.execute(
            f"""SELECT version_id,version_no,schedule_type,status,created_at,result_json
                  FROM schedule_versions
                 WHERE version_id IN ({placeholders})""",
            version_ids,
        ).fetchall()
        records_by_id = {row["version_id"]: dict(row) for row in rows}
        missing = [version_id for version_id in version_ids if version_id not in records_by_id]
        if missing:
            raise HTTPException(status_code=404, detail=f"计划版本不存在：{', '.join(missing)}")
        for version_id in version_ids:
            ensure_schedule_type_access(connection, records_by_id[version_id]["schedule_type"])
    return compare_version_core_metrics([records_by_id[version_id] for version_id in version_ids])


@app.get("/api/audit-logs")
def audit_logs(
    limit: int = 100,
    _: dict[str, Any] = Depends(require_roles(USER_ROLE_ADMIN, USER_ROLE_APPROVER)),
) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()
    return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]


STATIC_DIR = BASE_DIR / "business_app" / "static"
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{path:path}", include_in_schema=False)
def spa_fallback(path: str, request: Request) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    return FileResponse(STATIC_DIR / "index.html")
