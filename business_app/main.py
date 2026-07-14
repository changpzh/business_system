from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .algorithm_client import algorithm_client
from .config import BASE_DIR, settings
from .database import audit, db, initialize_database, now_text
from .security import create_token, decode_token, verify_password
from .services import ENTITY_CONFIG, build_effective_schedule, build_snapshot, compare_versions, create_task, execute_task, is_manually_locked, lock_process, parse_json_columns, publish_version, save_task_result, unlock_process, validate_snapshot


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


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "UP", "component": "business-system", "version": "1.0.0"}


@app.post("/api/auth/login")
def login(payload: dict[str, Any]) -> dict[str, Any]:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    with db() as connection:
        row = connection.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        user = dict(row)
        audit(connection, username, "USER_LOGIN", "user", username)
    return {"token": create_token(user), "user": {key: user[key] for key in ("username", "display_name", "role")}}


@app.get("/api/auth/me")
def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return user


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
        audit(connection, user["username"], "PASSWORD_CHANGED", "user", user["username"])
    return {"message": "密码修改成功"}


@app.get("/api/users")
def list_users(_: dict[str, Any] = Depends(require_roles("admin"))) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute("SELECT username,display_name,role,active,created_at,updated_at FROM users ORDER BY username").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/users", status_code=201)
def create_user(payload: dict[str, Any], user: dict[str, Any] = Depends(require_roles("admin"))) -> dict[str, Any]:
    from .security import hash_password
    username = str(payload.get("username", "")).strip()
    role = str(payload.get("role", "viewer"))
    password = str(payload.get("password", ""))
    if not username or len(password) < 8 or role not in {"admin", "planner", "approver", "viewer"}:
        raise HTTPException(status_code=422, detail="用户名、至少 8 位密码和合法角色为必填项")
    stamp = now_text()
    try:
        with db() as connection:
            connection.execute(
                "INSERT INTO users(username,display_name,password_hash,role,active,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                (username, str(payload.get("display_name") or username), hash_password(password), role, stamp, stamp),
            )
            audit(connection, user["username"], "USER_CREATED", "user", username, {"role": role})
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(status_code=409, detail="用户名已存在") from exc
        raise
    return {"username": username, "display_name": str(payload.get("display_name") or username), "role": role, "active": 1}


@app.put("/api/users/{username}")
def update_user(username: str, payload: dict[str, Any], user: dict[str, Any] = Depends(require_roles("admin"))) -> dict[str, Any]:
    from .security import hash_password
    role = str(payload.get("role", "viewer"))
    active = 1 if payload.get("active", True) else 0
    if role not in {"admin", "planner", "approver", "viewer"}:
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
        audit(connection, user["username"], "USER_UPDATED", "user", username, {"role": role, "active": active})
    return {"username": username, "display_name": str(payload.get("display_name") or username), "role": role, "active": active}


@app.get("/api/dashboard")
def dashboard(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        counts = {row["entity_type"]: row["count"] for row in connection.execute("SELECT entity_type,COUNT(*) AS count FROM master_records GROUP BY entity_type")}
        tasks = {row["status"]: row["count"] for row in connection.execute("SELECT status,COUNT(*) AS count FROM schedule_tasks GROUP BY status")}
        versions = {row["status"]: row["count"] for row in connection.execute("SELECT status,COUNT(*) AS count FROM schedule_versions GROUP BY status")}
        latest = [dict(row) for row in connection.execute("SELECT task_id,schedule_type,mode,status,created_by,created_at,completed_at,error_message FROM schedule_tasks ORDER BY created_at DESC LIMIT 8")]
        published = [dict(row) for row in connection.execute("SELECT version_id,schedule_type,published_by,published_at FROM schedule_versions WHERE status='PUBLISHED' ORDER BY published_at DESC")]
    try:
        algorithm = algorithm_client.health()
    except Exception as exc:
        algorithm = {"status": "DOWN", "message": str(exc)}
    return {"master_counts": counts, "task_counts": tasks, "version_counts": versions, "latest_tasks": latest, "published_versions": published, "algorithm": algorithm}


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
    user: dict[str, Any] = Depends(require_roles("admin", "planner")),
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
    user: dict[str, Any] = Depends(require_roles("admin", "planner")),
) -> dict[str, Any]:
    try:
        return unlock_process(process_id, payload, user["username"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 409 if "已变化" in str(exc) else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/api/master-data/snapshot")
def snapshot(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        return build_snapshot(connection)


@app.get("/api/master-data/validate")
def validate_master_data(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        errors = validate_snapshot(build_snapshot(connection))
    return {"valid": not errors, "errors": errors}


@app.post("/api/master-data/import")
def import_snapshot(payload: dict[str, Any], user: dict[str, Any] = Depends(require_roles("admin", "planner"))) -> dict[str, Any]:
    with db() as connection:
        imported = 0
        for entity_type, (snapshot_key, id_field) in ENTITY_CONFIG.items():
            if snapshot_key not in payload:
                continue
            records = payload[snapshot_key] if isinstance(payload[snapshot_key], list) else [payload[snapshot_key]]
            for record in records:
                if not isinstance(record, dict) or not record.get(id_field):
                    raise HTTPException(status_code=422, detail=f"{snapshot_key} 记录缺少 {id_field}")
                entity_id = str(record[id_field])
                connection.execute(
                    """INSERT INTO master_records(entity_type,entity_id,payload_json,revision,updated_by,updated_at) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(entity_type,entity_id) DO UPDATE SET payload_json=excluded.payload_json,revision=master_records.revision+1,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                    (entity_type, entity_id, json.dumps(record, ensure_ascii=False), 1, user["username"], now_text()),
                )
                imported += 1
        audit(connection, user["username"], "MASTER_DATA_IMPORTED", "master_data", "snapshot", {"count": imported})
    return {"imported": imported}


@app.get("/api/master-data/{entity_type}")
def list_master(entity_type: str, _: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    if entity_type not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail="未知主数据类型")
    with db() as connection:
        rows = connection.execute("SELECT * FROM master_records WHERE entity_type=? ORDER BY entity_id", (entity_type,)).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]


@app.get("/api/master-data/{entity_type}/batch")
def export_master_batch(entity_type: str, _: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    if entity_type not in {"order", "machine", "worker", "resource_group"}:
        raise HTTPException(status_code=404, detail="当前主数据类型不支持批量导出")
    with db() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM master_records WHERE entity_type=? ORDER BY entity_id", (entity_type,)
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


@app.post("/api/master-data/{entity_type}/batch")
def import_master_batch(
    entity_type: str,
    payload: list[dict[str, Any]],
    user: dict[str, Any] = Depends(require_roles("admin", "planner")),
) -> dict[str, Any]:
    if entity_type not in {"order", "machine", "worker", "resource_group"}:
        raise HTTPException(status_code=404, detail="当前主数据类型不支持批量导入")
    if not payload:
        raise HTTPException(status_code=422, detail="批量导入数组不能为空")
    id_field = ENTITY_CONFIG[entity_type][1]
    seen_ids: set[str] = set()
    for index, record in enumerate(payload):
        entity_id = str(record.get(id_field, "")).strip()
        if not entity_id:
            raise HTTPException(status_code=422, detail=f"第 {index + 1} 条记录缺少 {id_field}")
        if entity_id in seen_ids:
            raise HTTPException(status_code=422, detail=f"批量文件中编号重复: {entity_id}")
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
            "MASTER_DATA_BATCH_IMPORTED",
            entity_type,
            "batch",
            {"count": len(payload), "entity_ids": sorted(seen_ids)},
        )
    return {"entity_type": entity_type, "imported": len(payload)}


@app.put("/api/master-data/{entity_type}/{entity_id}")
def put_master(entity_type: str, entity_id: str, payload: dict[str, Any], user: dict[str, Any] = Depends(require_roles("admin", "planner"))) -> dict[str, Any]:
    if entity_type not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail="未知主数据类型")
    id_field = ENTITY_CONFIG[entity_type][1]
    if str(payload.get(id_field, "")) != entity_id:
        raise HTTPException(status_code=422, detail=f"请求路径与 {id_field} 不一致")
    with db() as connection:
        connection.execute(
            """INSERT INTO master_records(entity_type,entity_id,payload_json,revision,updated_by,updated_at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(entity_type,entity_id) DO UPDATE SET payload_json=excluded.payload_json,revision=master_records.revision+1,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (entity_type, entity_id, json.dumps(payload, ensure_ascii=False), 1, user["username"], now_text()),
        )
        audit(connection, user["username"], "MASTER_DATA_SAVED", entity_type, entity_id)
        row = connection.execute("SELECT * FROM master_records WHERE entity_type=? AND entity_id=?", (entity_type, entity_id)).fetchone()
    return {**dict(row), "payload": json.loads(row["payload_json"])}


@app.delete("/api/master-data/{entity_type}/{entity_id}")
def delete_master(entity_type: str, entity_id: str, user: dict[str, Any] = Depends(require_roles("admin", "planner"))) -> dict[str, str]:
    if entity_type not in ENTITY_CONFIG:
        raise HTTPException(status_code=404, detail="未知主数据类型")
    with db() as connection:
        cursor = connection.execute("DELETE FROM master_records WHERE entity_type=? AND entity_id=?", (entity_type, entity_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="记录不存在")
        audit(connection, user["username"], "MASTER_DATA_DELETED", entity_type, entity_id)
    return {"message": "删除成功"}


@app.post("/api/tasks", status_code=status.HTTP_202_ACCEPTED)
def submit_task(payload: dict[str, Any], background_tasks: BackgroundTasks, user: dict[str, Any] = Depends(require_roles("admin", "planner"))) -> dict[str, str]:
    if payload.get("schedule_type") not in {"machining", "heat_treatment", "assembly"}:
        raise HTTPException(status_code=422, detail="schedule_type 不合法")
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
    return {"task_id": task_id, "status": "QUEUED"}


@app.get("/api/tasks")
def list_tasks(limit: int = 100, _: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute("SELECT task_id,schedule_type,mode,dispatching_rule,status,error_message,created_by,created_at,started_at,completed_at FROM schedule_tasks ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        row = connection.execute("SELECT * FROM schedule_tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return parse_json_columns(dict(row), "request_json", "snapshot_json", "response_json")


@app.post("/api/tasks/{task_id}/retry", status_code=202)
def retry_task(task_id: str, background_tasks: BackgroundTasks, user: dict[str, Any] = Depends(require_roles("admin", "planner"))) -> dict[str, str]:
    with db() as connection:
        row = connection.execute("SELECT status FROM schedule_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        if row["status"] != "FAILED":
            raise HTTPException(status_code=409, detail="只有失败任务可以重试")
        connection.execute("UPDATE schedule_tasks SET status='QUEUED',error_message=NULL WHERE task_id=?", (task_id,))
        audit(connection, user["username"], "TASK_RETRIED", "schedule_task", task_id)
    background_tasks.add_task(execute_task, task_id)
    return {"task_id": task_id, "status": "QUEUED"}


@app.post("/api/schedule-callbacks")
def schedule_callback(payload: dict[str, Any]) -> dict[str, Any]:
    task_id = str(payload.get("task_id", ""))
    if not task_id:
        raise HTTPException(status_code=422, detail="缺少 task_id")
    if payload.get("status") == "SUCCEEDED":
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
        connection.execute("UPDATE schedule_tasks SET status='FAILED',response_json=?,error_message=?,completed_at=? WHERE task_id=?", (json.dumps(payload, ensure_ascii=False), error.get("message", "算法任务失败"), now_text(), task_id))
        audit(connection, "algorithm", "TASK_FAILED", "schedule_task", task_id, error)
    return {"accepted": True}


@app.get("/api/versions")
def list_versions(_: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute("""SELECT v.version_id,v.version_no,v.task_id,v.schedule_type,v.status,v.created_by,v.created_at,v.reviewed_by,v.reviewed_at,v.review_comment,v.published_by,v.published_at,t.mode,t.dispatching_rule
                                   FROM schedule_versions v JOIN schedule_tasks t ON t.task_id=v.task_id ORDER BY v.version_no DESC""").fetchall()
    return [dict(row) for row in rows]


@app.get("/api/versions/{version_id}")
def get_version(version_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        row = connection.execute(
            """SELECT v.*,t.snapshot_json
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
    result = json.loads(record.pop("result_json"))
    snapshot = json.loads(record.pop("snapshot_json"))
    process_index = {
        str(process.get("process_id")): process
        for order in snapshot.get("order_processes", [])
        for process in order.get("processes", [])
        if process.get("process_id")
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
        source = process_index.get(process_id, {})
        current = current_process_index.get(process_id, {})
        if "material_ready" not in item:
            item["material_ready"] = source.get("material_ready")
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
def review_version(version_id: str, payload: dict[str, Any], user: dict[str, Any] = Depends(require_roles("admin", "approver"))) -> dict[str, str]:
    decision = str(payload.get("decision", "")).upper()
    if decision not in {"APPROVED", "REJECTED"}:
        raise HTTPException(status_code=422, detail="decision 必须为 APPROVED 或 REJECTED")
    with db() as connection:
        version = connection.execute("SELECT status FROM schedule_versions WHERE version_id=?", (version_id,)).fetchone()
        if not version:
            raise HTTPException(status_code=404, detail="排程版本不存在")
        if version["status"] != "DRAFT":
            raise HTTPException(status_code=409, detail="只有草稿版本可以审批")
        connection.execute("UPDATE schedule_versions SET status=?,reviewed_by=?,reviewed_at=?,review_comment=? WHERE version_id=?", (decision, user["username"], now_text(), str(payload.get("comment", "")), version_id))
        audit(connection, user["username"], f"VERSION_{decision}", "schedule_version", version_id, {"comment": payload.get("comment", "")})
    return {"version_id": version_id, "status": decision}


@app.post("/api/versions/{version_id}/publish")
def publish(version_id: str, user: dict[str, Any] = Depends(require_roles("admin", "approver"))) -> dict[str, Any]:
    try:
        updated = publish_version(version_id, user["username"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"version_id": version_id, "status": "PUBLISHED", "updated_processes": updated}


@app.get("/api/versions/compare/{left_id}/{right_id}")
def compare(left_id: str, right_id: str, _: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        left = connection.execute("SELECT * FROM schedule_versions WHERE version_id=?", (left_id,)).fetchone()
        right = connection.execute("SELECT * FROM schedule_versions WHERE version_id=?", (right_id,)).fetchone()
    if not left or not right:
        raise HTTPException(status_code=404, detail="对比版本不存在")
    return compare_versions(dict(left), dict(right))


@app.get("/api/audit-logs")
def audit_logs(limit: int = 100, _: dict[str, Any] = Depends(require_roles("admin", "approver"))) -> list[dict[str, Any]]:
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
