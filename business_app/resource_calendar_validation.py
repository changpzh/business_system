"""设备和人员资源日历字段的统一结构校验。"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Mapping

from .constants import (
    CALENDAR_ENTRY_STATUSES,
    RESOURCE_CALENDAR_FIELDS,
)


_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def validate_resource_calendar_fields(
    profile: Mapping[str, Any],
    label: str,
) -> list[str]:
    """返回设备或人员档案中的资源日历字段校验错误。"""
    errors: list[str] = []
    for field_name in RESOURCE_CALENDAR_FIELDS:
        if field_name not in profile:
            continue

        entries = profile[field_name]
        field_label = f"{label}.{field_name}"
        if not isinstance(entries, list):
            errors.append(f"{field_label} 必须是数组")
            continue

        for index, entry in enumerate(entries):
            entry_label = f"{field_label}[{index}]"
            if not isinstance(entry, Mapping):
                errors.append(f"{entry_label} 必须是对象")
                continue

            _validate_entry_date(entry, entry_label, errors)
            _validate_entry_status(entry, entry_label, errors)
            _validate_entry_text(entry, "reason", entry_label, errors)
            _validate_entry_text(entry, "type", entry_label, errors)
            _validate_entry_segments(entry, entry_label, errors)

    return errors


def _validate_entry_date(
    entry: Mapping[str, Any],
    entry_label: str,
    errors: list[str],
) -> None:
    exact_date = entry.get("date")
    date_start = entry.get("date_start")
    date_end = entry.get("date_end")
    has_exact_date = exact_date not in (None, "")
    has_date_start = date_start not in (None, "")
    has_date_end = date_end not in (None, "")

    if has_exact_date and (has_date_start or has_date_end):
        errors.append(f"{entry_label} 不能同时配置 date 和 date_start/date_end")
        return

    if has_exact_date:
        _validate_iso_date(exact_date, f"{entry_label}.date", errors)
        return

    if has_date_start != has_date_end:
        errors.append(f"{entry_label} 必须同时配置 date_start 和 date_end")
        return

    if not has_date_start:
        errors.append(f"{entry_label} 必须配置 date 或 date_start/date_end")
        return

    start = _parse_iso_date(date_start, f"{entry_label}.date_start", errors)
    end = _parse_iso_date(date_end, f"{entry_label}.date_end", errors)
    if start is not None and end is not None and start > end:
        errors.append(f"{entry_label}.date_start 不能晚于 date_end")


def _validate_iso_date(value: Any, field_label: str, errors: list[str]) -> None:
    _parse_iso_date(value, field_label, errors)


def _parse_iso_date(value: Any, field_label: str, errors: list[str]) -> date | None:
    if not isinstance(value, str):
        errors.append(f"{field_label} 必须是 YYYY-MM-DD 日期字符串")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{field_label} 必须是有效的 YYYY-MM-DD 日期")
        return None


def _validate_entry_status(
    entry: Mapping[str, Any],
    entry_label: str,
    errors: list[str],
) -> None:
    if "status" not in entry:
        return
    status = entry["status"]
    if not isinstance(status, str) or status.strip().upper() not in CALENDAR_ENTRY_STATUSES:
        errors.append(
            f"{entry_label}.status 必须是 {sorted(CALENDAR_ENTRY_STATUSES)} 之一"
        )


def _validate_entry_text(
    entry: Mapping[str, Any],
    field_name: str,
    entry_label: str,
    errors: list[str],
) -> None:
    if field_name in entry and not isinstance(entry[field_name], str):
        errors.append(f"{entry_label}.{field_name} 必须是字符串")


def _validate_entry_segments(
    entry: Mapping[str, Any],
    entry_label: str,
    errors: list[str],
) -> None:
    segments = entry.get("segments")
    if not isinstance(segments, list) or not segments:
        errors.append(f"{entry_label}.segments 必须是非空数组")
        return

    for index, segment in enumerate(segments):
        segment_label = f"{entry_label}.segments[{index}]"
        if not isinstance(segment, Mapping):
            errors.append(f"{segment_label} 必须是对象")
            continue
        for field_name in ("start", "end"):
            value = segment.get(field_name)
            if not isinstance(value, str) or not _TIME_PATTERN.fullmatch(value):
                errors.append(f"{segment_label}.{field_name} 必须是 HH:MM 格式")
        if (
            isinstance(segment.get("start"), str)
            and isinstance(segment.get("end"), str)
            and _TIME_PATTERN.fullmatch(segment["start"])
            and _TIME_PATTERN.fullmatch(segment["end"])
        ):
            # 00:00 到 00:00 表示完整 24 小时，其他 end <= start 表示跨午夜。
            datetime.strptime(segment["start"], "%H:%M")
            datetime.strptime(segment["end"], "%H:%M")


def raise_resource_calendar_errors(profile: Mapping[str, Any], label: str) -> None:
    """校验资源日历字段，存在错误时抛出 ValueError。"""
    errors = validate_resource_calendar_fields(profile, label)
    if errors:
        raise ValueError("；".join(errors))
