"""Shared display timestamps and fixed day/night themes."""

from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def display_time(value: float | None) -> str:
    if not value:
        return "未知"
    return datetime.fromtimestamp(value, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S UTC+8")


def light_theme(now: float) -> bool:
    return 6 <= datetime.fromtimestamp(now, SHANGHAI).hour < 18
