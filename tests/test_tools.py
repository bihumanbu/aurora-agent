"""内置工具行为测试：calculator / web_search / weather / todo / read_docs。"""

import pytest

from aurora.exceptions import ToolArgumentError
from aurora.runtime.registry import ToolRegistry


def _exec(registry: ToolRegistry, name: str, args: dict | None = None) -> dict:
    return registry.execute_sync(name, args or {})


def test_calculator_basic(tool_registry):
    assert _exec(tool_registry, "calculator", {"expression": "12*(3+4)"})["result"] == 84


def test_calculator_float(tool_registry):
    assert _exec(tool_registry, "calculator", {"expression": "7/2"})["result"] == 3.5


def test_calculator_secure_rejects_import(tool_registry):
    with pytest.raises((ToolArgumentError, Exception)):
        _exec(tool_registry, "calculator", {"expression": "__import__('os').system('x')"})


def test_calculator_secure_rejects_call(tool_registry):
    with pytest.raises((ToolArgumentError, Exception)):
        _exec(tool_registry, "calculator", {"expression": "print(1)"})


def test_calculator_zero_division_is_error(tool_registry):
    from aurora.exceptions import ToolError

    with pytest.raises(ToolError):
        _exec(tool_registry, "calculator", {"expression": "1/0"})


def test_web_search_mock(tool_registry):
    r = _exec(tool_registry, "web_search", {"query": "极光"})
    assert r["query"] == "极光"
    assert isinstance(r["results"], list)
    assert len(r["results"]) >= 1
    assert "url" in r["results"][0]


def test_web_search_requires_query(tool_registry):
    with pytest.raises(ToolArgumentError):
        _exec(tool_registry, "web_search", {})


def test_weather_mock(tool_registry):
    r = _exec(tool_registry, "weather", {"city": "北京"})
    assert r["city"] == "北京"
    assert "temp_c" in r
    assert "condition" in r


def test_weather_unknown_city_fallback(tool_registry):
    r = _exec(tool_registry, "weather", {"city": "不存在的城市"})
    assert "temp_c" in r


def _fake_open_meteo_xiamen(url: str) -> str:
    """模拟 Open-Meteo：地理编码 + 实时天气，固定返回「厦门下雨」示例。"""
    if "geocoding-api" in url:
        return '{"results":[{"latitude":24.48,"longitude":118.09,"name":"厦门"}]}'
    return '{"current":{"temperature_2m":21.5,"relative_humidity_2m":88,"weather_code":61}}'


def test_weather_real_provider_returns_live_data():
    from aurora.tools.weather import build_open_meteo_provider, build_weather

    provider = build_open_meteo_provider(http_get=_fake_open_meteo_xiamen)
    r = build_weather(provider=provider)("厦门")
    assert r["mock"] is False
    assert r["temp_c"] == 21.5
    assert r["condition"] == "小雨"  # WMO 61 → 小雨
    assert r["humidity"] == 88


def test_weather_real_provider_falls_back_on_network_error():
    from aurora.tools.weather import build_open_meteo_provider, build_weather

    def boom(url: str) -> str:
        raise RuntimeError("network down")

    provider = build_open_meteo_provider(http_get=boom)
    r = build_weather(provider=provider)("厦门")
    assert r["mock"] is True  # 网络失败回退 mock
    assert "temp_c" in r


def test_weather_real_provider_unknown_city_falls_back():
    from aurora.tools.weather import build_open_meteo_provider, build_weather

    def empty(url: str) -> str:
        if "geocoding-api" in url:
            return '{"results":[]}'  # 城市无解
        return "{}"

    provider = build_open_meteo_provider(http_get=empty)
    r = build_weather(provider=provider)("不存在的城市")
    assert r["mock"] is True  # 城市无解回退 mock
    assert "temp_c" in r


def test_todo_add_list_done(tool_registry):
    _exec(tool_registry, "todo_add", {"text": "写周报"})
    items = _exec(tool_registry, "todo_list")["items"]
    assert any("写周报" in i["text"] for i in items)
    tid = items[-1]["id"]
    r = _exec(tool_registry, "todo_done", {"task_id": tid})
    assert r["ok"] is True
    items2 = _exec(tool_registry, "todo_list")["items"]
    assert [i for i in items2 if i["id"] == tid][0]["done"] is True


def test_todo_store_isolation_between_registries():
    r1, r2 = ToolRegistry(), ToolRegistry()
    from aurora.tools import register_all_tools

    register_all_tools(r1)
    register_all_tools(r2)  # 每个 registry 独立 TodoStore
    r1.execute_sync("todo_add", {"text": "A 的待办"})
    items2 = r2.execute_sync("todo_list", {})["items"]
    assert len(items2) == 0


def test_read_docs(tool_registry):
    r = _exec(tool_registry, "read_docs", {"path": "DESIGN.md"})
    assert "path" in r
    assert isinstance(r.get("content", ""), str)


def test_read_docs_missing_file(tool_registry):
    r = _exec(tool_registry, "read_docs", {"path": "not_exist.md"})
    assert "error" in r or "found" in str(r).lower()


def test_all_builtin_tools_registered(tool_registry):
    names = set(tool_registry.list())
    assert {"calculator", "web_search", "weather", "todo_add", "todo_list",
            "todo_done", "read_docs"} <= names


def test_spec_contains_builtin_functions(tool_registry):
    spec = tool_registry.spec()
    names = {s["function"]["name"] for s in spec}
    assert "calculator" in names
    assert "web_search" in names