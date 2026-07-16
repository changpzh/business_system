"""业务单元、订单归属和虚拟资源的统一业务枚举。"""

from __future__ import annotations

from typing import Any

from .constants import (
    DEPLOYMENT_PROCESS_TYPE_DEBUG,
    DEPLOYMENT_SCHEDULE_TYPES,
    ORDER_BUSINESS_TYPES,
    SCHEDULE_ORDER_BUSINESS_TYPES,
    SCHEDULE_TYPES,
)


def normalize_order_business_type(value: Any) -> str:
    return str(value or "").strip().upper()


def order_business_types_for_schedule_type(schedule_type: str) -> set[str]:
    return set(SCHEDULE_ORDER_BUSINESS_TYPES.get(str(schedule_type or "").lower(), set()))


def schedule_type_for_order_business_type(value: Any) -> str | None:
    normalized = normalize_order_business_type(value)
    for schedule_type, order_types in SCHEDULE_ORDER_BUSINESS_TYPES.items():
        if normalized in order_types:
            return schedule_type
    return None


def deployment_schedule_type(deployment_process_type: str) -> str | None:
    return DEPLOYMENT_SCHEDULE_TYPES.get(str(deployment_process_type or "").upper())


def allowed_schedule_types(deployment_process_type: str) -> set[str]:
    fixed = deployment_schedule_type(deployment_process_type)
    return {fixed} if fixed else set(SCHEDULE_TYPES)


def resolve_task_schedule_type(deployment_process_type: str, requested: Any) -> str:
    """正式部署强制使用固定工艺；调试部署必须显式选择。"""
    deployment = str(deployment_process_type or "").upper()
    fixed = deployment_schedule_type(deployment)
    if fixed:
        return fixed
    requested_type = str(requested or "").strip().lower()
    if requested_type not in SCHEDULE_TYPES:
        raise ValueError("调试部署必须选择 machining、heat_treatment 或 assembly")
    return requested_type


def order_visible_in_deployment(order_business_type: Any, deployment_process_type: str) -> bool:
    deployment = str(deployment_process_type or "").upper()
    if deployment == DEPLOYMENT_PROCESS_TYPE_DEBUG:
        return normalize_order_business_type(order_business_type) in ORDER_BUSINESS_TYPES
    fixed = deployment_schedule_type(deployment)
    return normalize_order_business_type(order_business_type) in order_business_types_for_schedule_type(fixed or "")


def is_virtual_resource(record: dict[str, Any] | None) -> bool:
    return bool((record or {}).get("virtual_resource", False) is True)
