"""设备能力与工序温度要求的统一校验。"""

from __future__ import annotations

import math
from typing import Any


LEGACY_TEMPERATURE_FIELDS = ("temp_range_min", "temp_range_max")


def _temperature_range(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label} 必须是包含最低、最高温度两个数字的数组")
    normalized: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            position = "最低温度" if index == 0 else "最高温度"
            raise ValueError(f"{label} 的{position}必须是有限数字")
        normalized.append(float(item))
    if normalized[0] > normalized[1]:
        raise ValueError(f"{label} 的最低温度不能高于最高温度")
    return normalized[0], normalized[1]


def machine_temperature_range(machine: dict[str, Any], label: str) -> tuple[float, float] | None:
    capabilities = machine.get("capabilities", {}) or {}
    if not isinstance(capabilities, dict):
        raise ValueError(f"{label}.capabilities 必须是对象")
    if "temp_range" not in capabilities:
        return None
    return _temperature_range(capabilities.get("temp_range"), f"{label}.capabilities.temp_range")


def process_temperature_range(process: dict[str, Any], label: str) -> tuple[float, float] | None:
    requirements = process.get("resource_requirements", {}) or {}
    if not isinstance(requirements, dict):
        raise ValueError(f"{label}.resource_requirements 必须是对象")
    legacy = [field for field in LEGACY_TEMPERATURE_FIELDS if field in requirements]
    if legacy:
        raise ValueError(
            f"{label}.resource_requirements 不支持旧温度字段 {legacy}，请使用 temp_range: [最低温度, 最高温度]"
        )
    if "temp_range" not in requirements:
        return None
    return _temperature_range(requirements.get("temp_range"), f"{label}.resource_requirements.temp_range")


def machine_covers_temperature_requirement(
    process: dict[str, Any],
    machine: dict[str, Any],
    *,
    process_label: str,
    machine_label: str,
) -> bool:
    required = process_temperature_range(process, process_label)
    if required is None:
        return True
    capability = machine_temperature_range(machine, machine_label)
    if capability is None:
        return False
    return capability[0] <= required[0] and capability[1] >= required[1]


def raise_order_temperature_errors(order: dict[str, Any], label: str) -> None:
    processes = order.get("processes", []) or []
    if not isinstance(processes, list):
        raise ValueError(f"{label}.processes 必须是数组")
    for index, process in enumerate(processes):
        if not isinstance(process, dict):
            raise ValueError(f"{label}.processes[{index}] 必须是对象")
        process_temperature_range(process, f"{label}.processes[{index}]")
