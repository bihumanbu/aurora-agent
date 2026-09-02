"""工具注册机制测试：名称/描述/参数 Schema、执行前校验、异常捕获。"""

import pytest

from aurora.exceptions import ToolArgumentError, ToolExecutionError
from aurora.runtime.registry import Tool, ToolRegistry, schema


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


def test_register_decorator(registry: ToolRegistry):
    @registry.register(name="ping", description="回显", params=schema({"msg": {"type": "string"}}))
    def ping(msg: str) -> dict:
        return {"echo": msg}

    assert "ping" in registry.list()
    tool = registry.get("ping")
    assert tool.name == "ping"
    assert tool.description == "回显"


def test_register_default_params_exact(registry: ToolRegistry):
    @registry.register(
        name="add",
        description="两数相加",
        params=schema({"a": {"type": "integer"}, "b": {"type": "integer"}}, required=["a", "b"]),
    )
    def add(a: int, b: int = 0) -> dict:
        return {"sum": a + b}

    tool = registry.get("add")
    assert tool.schema["required"] == ["a", "b"]
    assert "a" in tool.schema["properties"]
    assert "b" in tool.schema["properties"]


def test_duplicate_register_raises(registry: ToolRegistry):
    @registry.register(name="dup", description="一")
    def f1() -> dict:
        return {}

    with pytest.raises(ValueError):
        @registry.register(name="dup", description="二")
        def f2() -> dict:
            return {}


def test_execute_runs_handler(registry: ToolRegistry):
    @registry.register(
        name="calc",
        description="计算",
        params=schema({"expression": {"type": "string"}}, required=["expression"]),
    )
    def calc(expression: str) -> dict:
        return {"result": eval(expression)}

    result = registry.execute_sync("calc", {"expression": "1+1"})
    assert result["result"] == 2


def test_execute_missing_required_arg_raises(registry: ToolRegistry):
    @registry.register(
        name="calc",
        description="计算",
        params=schema({"expression": {"type": "string"}}, required=["expression"]),
    )
    def calc(expression: str) -> dict:
        return {"result": 0}

    with pytest.raises(ToolArgumentError):
        registry.execute_sync("calc", {})


def test_execute_unknown_tool_raises(registry: ToolRegistry):
    with pytest.raises(ToolExecutionError):
        registry.execute_sync("nope", {})


def test_execute_handler_exception_wrapped(registry: ToolRegistry):
    @registry.register(name="boom", description="抛异常")
    def boom() -> dict:
        raise RuntimeError("内部错误")

    with pytest.raises(ToolExecutionError) as ei:
        registry.execute_sync("boom", {})
    assert "内部错误" in str(ei.value)


def test_tool_spec_openai_format(registry: ToolRegistry):
    @registry.register(
        name="search",
        description="搜索",
        params=schema({"q": {"type": "string"}}, required=["q"]),
    )
    def search(q: str) -> dict:
        return {}

    spec = registry.spec()
    assert isinstance(spec, list)
    found = [s for s in spec if s["function"]["name"] == "search"]
    assert found, "spec 应包含已注册工具的 OpenAI function 格式"
    assert found[0]["type"] == "function"
    assert "properties" in found[0]["function"]["parameters"]


def test_manual_tool_insert(registry: ToolRegistry):
    t = Tool(
        name="manual",
        description="手动加入",
        handler=lambda **_: {"ok": True},
        params=schema({"x": {"type": "string"}}),
    )
    registry.add(t)
    assert registry.get("manual").description == "手动加入"
    assert registry.execute_sync("manual", {"x": "1"}) == {"ok": True}