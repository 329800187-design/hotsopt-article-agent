from __future__ import annotations

from hot_sources.dailyhot import DailyHotSource


def get_daily_source(endpoint: str, timeout_seconds: int = 8, network_settings: dict | None = None) -> DailyHotSource:
    if not endpoint:
        raise ValueError("今日头条主源地址不能为空")
    return DailyHotSource(endpoint, timeout_seconds=timeout_seconds, network_settings=network_settings)
