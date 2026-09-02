"""weather — 天气查询。

允许 mock。提供：
    - 内置若干城市的演示天气（mock）
    - 未知城市返回通用兜底数据
    - 可通过 ``provider`` 注入真实天气来源（预留）

schema: {"city": str} → {"city", "temp_c", "condition", "humidity", "mock"}
"""

from __future__ import annotations

from typing import Any, Callable

WeatherProvider = Callable[[str], dict[str, Any]]

_MOCK_WEATHER: dict[str, dict[str, Any]] = {
    "北京": {"temp_c": 24, "condition": "晴", "humidity": 38},
    "上海": {"temp_c": 27, "condition": "多云", "humidity": 55},
    "广州": {"temp_c": 30, "condition": "阵雨", "humidity": 78},
    "深圳": {"temp_c": 29, "condition": "多云", "humidity": 70},
    "杭州": {"temp_c": 26, "condition": "晴", "humidity": 52},
    "成都": {"temp_c": 25, "condition": "阴", "humidity": 60},
}


def _default_provider(city: str) -> dict[str, Any]:
    base = _MOCK_WEATHER.get(city, {"temp_c": 22, "condition": "未知（演示数据）", "humidity": 50})
    return dict(base)


def build_weather(*, provider: WeatherProvider | None = None) -> Callable[[str], dict[str, Any]]:
    """构造天气工具。默认内置 mock，可替换 provider 接真实数据源。"""
    lookup = provider or _default_provider

    def weather(city: str) -> dict[str, Any]:
        data = lookup(city)
        return {
            "city": city,
            "temp_c": data.get("temp_c"),
            "condition": data.get("condition", ""),
            "humidity": data.get("humidity"),
            "mock": provider is None,
        }

    return weather


DEFAULT_TOOL = build_weather()