"""weather — 天气查询。

默认返回演示数据（mock），可通过 ``provider`` 注入真实数据源。

内置真实来源：``build_open_meteo_provider()``（Open-Meteo，免费、免 API key）。
城市名 → 经纬度（Open-Meteo 地理编码）→ 实时天气（WMO 天气代码映射为中文）。
网络异常或城市无解时返回 ``None``，由 ``build_weather`` 自动回退到内置 mock，
因此无论联网与否工具都可用，且返回体 ``mock`` 字段如实标注数据真伪。

schema: {"city": str} → {"city", "temp_c", "condition", "humidity", "mock"}
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable

WeatherProvider = Callable[[str], "dict[str, Any] | None"]

# Open-Meteo 实时天气用 WMO weather code，这里映射为常见中文描述
_WMO_CODES: dict[int, str] = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷暴伴冰雹",
}

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


def _http_json(http_get: Callable[[str], Any] | None, base: str, params: dict[str, Any]) -> Any:
    """GET 一个 JSON 接口。http_get 可注入（测试用），缺省走 urllib。"""
    url = base + "?" + urllib.parse.urlencode(params)
    if http_get is not None:
        raw = http_get(url)
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "aurora-agent/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            raw = resp.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def build_open_meteo_provider(*, http_get: Callable[[str], Any] | None = None) -> WeatherProvider:
    """构造 Open-Meteo 真实天气 provider（免 API key）。

    成功返回 ``{"temp_c", "condition", "humidity"}``；任何失败（网络/解析/城市无解）
    返回 ``None``，交由 ``build_weather`` 回退 mock。
    """

    def provider(city: str) -> dict[str, Any] | None:
        try:
            geo_params = {"name": city, "count": 1, "language": "zh", "format": "json"}
            geo = _http_json(http_get, "https://geocoding-api.open-meteo.com/v1/search", geo_params)
            results = (geo or {}).get("results") or []
            # 中文裸城市名（如「厦门」）地理编码常为空，补「市」后缀重试
            if not results and not city.endswith(("市", "区", "县", "省", "州")):
                geo = _http_json(
                    http_get,
                    "https://geocoding-api.open-meteo.com/v1/search",
                    {**geo_params, "name": city + "市"},
                )
                results = (geo or {}).get("results") or []
            if not results:
                return None
            lat = results[0]["latitude"]
            lon = results[0]["longitude"]
            cur = _http_json(
                http_get,
                "https://api.open-meteo.com/v1/forecast",
                {
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,weather_code",
                    "timezone": "auto",
                },
            )
            c = (cur or {}).get("current") or {}
            code = c.get("weather_code")
            return {
                "temp_c": c.get("temperature_2m"),
                "condition": _WMO_CODES.get(code, "未知"),
                "humidity": c.get("relative_humidity_2m"),
            }
        except Exception:  # noqa: BLE001 — 任意失败都回退 mock，绝不抛错影响主流程
            return None

    return provider


def build_weather(
    *,
    provider: WeatherProvider | None = None,
    fallback: WeatherProvider | None = None,
) -> Callable[[str], dict[str, Any]]:
    """构造天气工具。

    - ``provider=None``：纯 mock（默认）。
    - ``provider=真实来源``：成功用真实数据（``mock=False``）；来源返回 ``None``
      （网络异常/城市无解）时自动回退 ``fallback``（缺省为内置 mock，``mock=True``）。
    """
    lookup = provider or _default_provider
    fb = fallback or _default_provider

    def weather(city: str) -> dict[str, Any]:
        data = lookup(city)
        real = provider is not None and data is not None
        if data is None:
            data = fb(city)
        return {
            "city": city,
            "temp_c": data.get("temp_c"),
            "condition": data.get("condition", ""),
            "humidity": data.get("humidity"),
            "mock": not real,
        }

    return weather


DEFAULT_TOOL = build_weather()  # 纯 mock，供测试与无需联网场景使用
