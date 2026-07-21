"""生产日历特殊规则的统一解析实现。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping

from .constants import (
    CALENDAR_RULE_STATUSES,
    CALENDAR_STATUS_CONFIRMED,
    RECORD_STATUS_ACTIVE,
)


TimeRange = tuple[datetime, datetime]


def calendar_day_shifts(calendar: Mapping[str, Any], target_day: date) -> list[dict[str, Any]]:
    """
    返回指定日期最终生效的班次。

    优先级固定为：指定日期的 ``special_shifts``、匹配到的最高优先级
    ``special_rules``、普通 ``weekly_shifts``。空列表表示当天明确不可用。
    """
    special_shifts = special_calendar_shifts(calendar, target_day)
    if special_shifts is not None:
        return special_shifts

    day_key = str((target_day.weekday() + 1) % 7)
    return _extract_shifts((calendar.get("weekly_shifts") or {}).get(day_key, []))


def special_calendar_shifts(
    calendar: Mapping[str, Any],
    target_day: date,
) -> list[dict[str, Any]] | None:
    """
    返回指定日期的特殊覆盖班次。

    返回 ``None`` 表示没有特殊覆盖，应继续读取周班次；返回空列表表示
    特殊规则明确将当天设置为休息日。
    """
    day_key = target_day.isoformat()
    special_shifts = calendar.get("special_shifts") or {}
    if day_key in special_shifts:
        return _extract_shifts(special_shifts[day_key])

    matched_rules = [
        rule
        for rule in calendar.get("special_rules", []) or []
        if isinstance(rule, Mapping)
        and rule_matches_day(rule, target_day)
        and is_effective_rule(rule)
    ]
    if not matched_rules:
        return None

    selected_rule = max(
        enumerate(matched_rules),
        key=lambda item: (_rule_priority(item[1]), -item[0]),
    )[1]
    return _extract_shifts(selected_rule.get("shifts", []))


def rule_matches_day(rule: Mapping[str, Any], target_day: date) -> bool:
    """判断特殊规则是否命中指定日期。"""
    rule_type = str(rule.get("rule_type", "")).strip().lower()

    if rule.get("date") == target_day.isoformat():
        return True

    date_start = rule.get("date_start")
    date_end = rule.get("date_end")
    if date_start and date_end:
        return date.fromisoformat(str(date_start)) <= target_day <= date.fromisoformat(str(date_end))

    if rule_type == "fixed_date":
        return _integer(rule.get("month")) == target_day.month and _integer(rule.get("day")) == target_day.day

    if rule_type == "fixed_date_range":
        month = _integer(rule.get("month"))
        day_start = _integer(rule.get("day_start"))
        day_end = _integer(rule.get("day_end"))
        return target_day.month == month and day_start <= target_day.day <= day_end

    if rule_type == "lunar":
        return _lunar_rule_matches(rule, target_day)

    return False


def is_effective_rule(rule: Mapping[str, Any]) -> bool:
    """只有启用、已确认或已批准的特殊规则才参与日历计算。"""
    return str(rule.get("status", "ACTIVE")).strip().upper() in CALENDAR_RULE_STATUSES


def available_minutes_for_profile(
    calendar: Mapping[str, Any],
    profile: Mapping[str, Any],
    start: datetime,
    end: datetime,
) -> float:
    """计算资源在统一计划周期内的日历可用分钟数。"""
    if end <= start:
        return 0.0
    if str(profile.get("status", RECORD_STATUS_ACTIVE)).strip().upper() != RECORD_STATUS_ACTIVE:
        return 0.0

    total = 0.0
    current_day = start.date()
    while current_day <= end.date():
        for slot_start, slot_end in _resource_available_ranges(calendar, profile, current_day):
            overlap_start = max(slot_start, start)
            overlap_end = min(slot_end, end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds() / 60.0
        current_day += timedelta(days=1)
    return total


def _resource_available_ranges(
    calendar: Mapping[str, Any], profile: Mapping[str, Any], target_day: date
) -> list[TimeRange]:
    base_ranges = _base_calendar_ranges(calendar, target_day)
    overtime_ranges = _profile_ranges(profile.get("availability_overrides", []), target_day)
    unavailable_ranges = _profile_ranges(profile.get("unavailability", []), target_day)
    return _subtract_ranges(_merge_ranges(base_ranges + overtime_ranges), unavailable_ranges)


def _base_calendar_ranges(calendar: Mapping[str, Any], target_day: date) -> list[TimeRange]:
    special_shifts = special_calendar_shifts(calendar, target_day)
    if special_shifts is not None:
        return _clip_ranges_to_day(_shift_ranges(special_shifts, target_day), target_day)

    previous_day = target_day - timedelta(days=1)
    ranges = _shift_ranges(calendar_day_shifts(calendar, previous_day), previous_day)
    ranges.extend(_shift_ranges(calendar_day_shifts(calendar, target_day), target_day))
    return _clip_ranges_to_day(ranges, target_day)


def _shift_ranges(shifts: list[dict[str, Any]], target_day: date) -> list[TimeRange]:
    ranges: list[TimeRange] = []
    for shift in shifts:
        for segment in shift.get("segments", []) or []:
            if isinstance(segment, Mapping):
                ranges.append(_segment_to_range(segment, target_day))
    return _merge_ranges(ranges)


def _profile_ranges(entries: Any, target_day: date) -> list[TimeRange]:
    ranges: list[TimeRange] = []
    for anchor_day in (target_day - timedelta(days=1), target_day):
        for entry in entries or []:
            if not isinstance(entry, Mapping) or not _profile_entry_applies(entry, anchor_day):
                continue
            status = str(entry.get("status", CALENDAR_STATUS_CONFIRMED)).strip().upper()
            if status not in CALENDAR_RULE_STATUSES:
                continue
            for segment in entry.get("segments", []) or []:
                if isinstance(segment, Mapping):
                    ranges.append(_segment_to_range(segment, anchor_day))
    return _clip_ranges_to_day(ranges, target_day)


def _profile_entry_applies(entry: Mapping[str, Any], target_day: date) -> bool:
    if entry.get("date") == target_day.isoformat():
        return True
    date_start = entry.get("date_start")
    date_end = entry.get("date_end")
    if date_start and date_end:
        try:
            return date.fromisoformat(str(date_start)) <= target_day <= date.fromisoformat(str(date_end))
        except ValueError:
            return False
    return False


def _segment_to_range(segment: Mapping[str, Any], target_day: date) -> TimeRange:
    start = datetime.combine(
        target_day, datetime.strptime(str(segment["start"]), "%H:%M").time()
    )
    end = datetime.combine(
        target_day, datetime.strptime(str(segment["end"]), "%H:%M").time()
    )
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _clip_ranges_to_day(ranges: list[TimeRange], target_day: date) -> list[TimeRange]:
    day_start = datetime.combine(target_day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    return _merge_ranges(
        [
            (max(start, day_start), min(end, day_end))
            for start, end in ranges
            if end > day_start and start < day_end
        ]
    )


def _merge_ranges(ranges: list[TimeRange]) -> list[TimeRange]:
    ordered = sorted((start, end) for start, end in ranges if end > start)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _subtract_ranges(base_ranges: list[TimeRange], blocked_ranges: list[TimeRange]) -> list[TimeRange]:
    available = _merge_ranges(base_ranges)
    for blocked_start, blocked_end in _merge_ranges(blocked_ranges):
        next_ranges: list[TimeRange] = []
        for start, end in available:
            if blocked_end <= start or blocked_start >= end:
                next_ranges.append((start, end))
                continue
            if start < blocked_start:
                next_ranges.append((start, blocked_start))
            if blocked_end < end:
                next_ranges.append((blocked_end, end))
        available = next_ranges
    return available


def _lunar_rule_matches(rule: Mapping[str, Any], target_day: date) -> bool:
    """判断农历规则是否命中；依赖缺失时忽略农历规则。"""
    try:
        from lunardate import LunarDate
    except ModuleNotFoundError:
        return False

    lunar_date = LunarDate.from_solar_date(target_day.year, target_day.month, target_day.day)
    lunar_month = _integer(rule.get("lunar_month"))
    lunar_day = _integer(rule.get("lunar_day"))
    duration_days = max(_integer(rule.get("duration_days"), default=1), 1)
    if lunar_date.month != lunar_month:
        return False
    return lunar_day <= lunar_date.day < lunar_day + duration_days


def _extract_shifts(value: Any) -> list[dict[str, Any]]:
    """兼容班次列表、单个班次对象和包含 shifts 的包装对象。"""
    if isinstance(value, list):
        shifts = value
    elif isinstance(value, Mapping):
        shifts = [value] if "segments" in value else value.get("shifts", [])
    else:
        shifts = []
    return [dict(shift) for shift in shifts or [] if isinstance(shift, Mapping)]


def _rule_priority(rule: Mapping[str, Any]) -> int:
    """读取规则优先级；空值按零处理。"""
    return _integer(rule.get("priority"))


def _integer(value: Any, default: int = 0) -> int:
    """把日历配置值转换为整数。"""
    if value is None or value == "":
        return default
    return int(value)
