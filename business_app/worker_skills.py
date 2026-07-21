"""人员技能数组与工序技能要求的统一校验。"""

from __future__ import annotations

from typing import Any


def _skill_set(value: Any, label: str) -> set[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是字符串数组")
    normalized: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{label}[{index}] 必须是非空字符串")
        normalized.add(item.strip())
    return normalized


def worker_skill_set(worker: dict[str, Any], label: str) -> set[str]:
    if "skills" not in worker:
        return set()
    return _skill_set(worker.get("skills"), f"{label}.skills")


def required_skill_set(process: dict[str, Any], label: str) -> set[str]:
    requirements = process.get("resource_requirements", {}) or {}
    if not isinstance(requirements, dict):
        raise ValueError(f"{label}.resource_requirements 必须是对象")
    required: set[str] = set()
    if "required_skills" in process:
        required.update(_skill_set(process.get("required_skills"), f"{label}.required_skills"))
    if "required_skills" in requirements:
        required.update(
            _skill_set(
                requirements.get("required_skills"),
                f"{label}.resource_requirements.required_skills",
            )
        )
    return required


def raise_worker_profile_errors(worker: dict[str, Any], label: str) -> None:
    worker_skill_set(worker, label)
    if "workshop_type" in worker and worker.get("workshop_type") is not None and not isinstance(
        worker.get("workshop_type"), str
    ):
        raise ValueError(f"{label}.workshop_type 必须是字符串或不填写")


def raise_order_skill_errors(order: dict[str, Any], label: str) -> None:
    processes = order.get("processes", []) or []
    if not isinstance(processes, list):
        raise ValueError(f"{label}.processes 必须是数组")
    for index, process in enumerate(processes):
        if not isinstance(process, dict):
            raise ValueError(f"{label}.processes[{index}] 必须是对象")
        required_skill_set(process, f"{label}.processes[{index}]")
