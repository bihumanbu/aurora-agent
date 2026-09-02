"""工具注册机制。

笔试要求：工具注册机制，每个工具包含名称、描述、参数 Schema，
LLM 基于 Schema 自主决策调用。

设计：
    - @registry.register(name, description, params) 装饰器注册工具
    - Tool 持有 handler + schema，注册后进入 ToolRegistry
    - execute_sync/execute 执行前用 Schema 校验参数，执行期异常包装为
      ToolExecutionError（不裸抛，便于 Loop 降级与 Trace 记录）
    - spec() 输出 OpenAI function-calling 标准格式，供 LLM 决策

参数校验采用轻量 JSON Schema 子集校验（自研，避免依赖 jsonschema，
符合"核心自研 + 依赖克制"）：
    type / required / properties.type / enum / minimum / maximum
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from aurora.exceptions import ToolArgumentError, ToolError, ToolExecutionError

Handler = Callable[..., dict[str, Any]]


def schema(
    properties: dict[str, dict[str, Any]] | None = None,
    *,
    required: list[str] | None = None,
    enum: dict[str, list[str]] | None = None,
    minimum: dict[str, float] | None = None,
    maximum: dict[str, float] | None = None,
) -> dict[str, Any]:
    """构造一个 JSON Schema 片段（自研的轻量子集）。

    示例:
        schema({"q": {"type": "string"}}, required=["q"])
        schema({"x": {"type": "integer"}, "s": {"type": "string"}},
               minimum={"x": 0}, maximum={"x": 100})
    """
    props: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (properties or {}).items()}
    for k, choices in (enum or {}).items():
        if k in props:
            props[k]["enum"] = list(choices)
    for k, lo in (minimum or {}).items():
        if k in props:
            props[k]["minimum"] = lo
    for k, hi in (maximum or {}).items():
        if k in props:
            props[k]["maximum"] = hi
    return {"type": "object", "properties": props, "required": required or []}


@dataclass(frozen=True)
class Tool:
    """一个可注册工具。"""

    name: str
    description: str
    handler: Handler
    params: dict[str, Any] | None = None

    @property
    def schema(self) -> dict[str, Any]:
        """参数 Schema；未提供时从函数注解推导（仅支持简单类型）。"""
        if self.params is not None:
            return self.params
        return _infer_schema(self.handler)

    def to_function_spec(self) -> dict[str, Any]:
        """OpenAI function-calling 格式（可供 LLM 决策）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    **self.schema,
                    # 兼容部分实现要求 parameters 必须是 object
                } if self.schema.get("type") == "object" else self.schema,
            },
        }


class ToolRegistry:
    """工具注册表：注册、查询、执行、导出 spec。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        *,
        name: str | None = None,
        description: str = "",
        params: dict[str, Any] | None = None,
    ) -> Callable[[Handler], Tool]:
        """装饰器：把函数注册进本 registry，返回 Tool。"""

        def deco(fn: Handler) -> Tool:
            tool_name = name or fn.__name__
            if tool_name in self._tools:
                raise ValueError(f"工具重复注册: {tool_name}")
            tool = Tool(name=tool_name, description=description or (fn.__doc__ or "").strip(),
                        handler=fn, params=params)
            self._tools[tool_name] = tool
            return tool

        return deco

    def add(self, tool: Tool) -> None:
        """手动插入一个 Tool 对象。"""
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def list(self) -> list[str]:
        return list(self._tools)

    def spec(self) -> list[dict[str, Any]]:
        return [t.to_function_spec() for t in self._tools.values()]

    def execute_sync(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolExecutionError(f"未知工具: {name}", tool_name=name)
        args = validate_args(tool.schema, arguments or {})
        try:
            result = tool.handler(**args)
            if inspect.isawaitable(result):
                raise ToolExecutionError(
                    f"工具 {name} 是异步的，请使用 execute()", tool_name=name)
            return _coerce_result(result)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 — 统一包装，保留信息
            raise ToolExecutionError(f"工具 {name} 执行失败: {e}", tool_name=name) from e

    async def execute(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolExecutionError(f"未知工具: {name}", tool_name=name)
        args = validate_args(tool.schema, arguments or {})
        try:
            result = tool.handler(**args)
            if inspect.isawaitable(result):
                result = await result
            return _coerce_result(result)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolExecutionError(f"工具 {name} 执行失败: {e}", tool_name=name) from e


# ── 参数校验（自研轻量 JSON Schema 子集）──────────────────────

def validate_args(schema_: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """按 Schema 校验参数，返回清洗后的参数；失败抛 ToolArgumentError。"""
    errors: list[str] = []
    props = schema_.get("properties", {})
    required = schema_.get("required", [])

    clean: dict[str, Any] = dict(arguments)
    for key in required:
        if key not in arguments or arguments[key] is None:
            errors.append(f"缺少必需参数: {key}")
    for key, value in arguments.items():
        spec = props.get(key)
        if spec is None:
            continue  # 未在 Schema 中声明的参数放行（宽松）或可改为报错
        errors.extend(_check_type(key, value, spec))
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"参数 {key}={value!r} 不在枚举范围 {spec['enum']}")
        if isinstance(value, (int, float)):
            if "minimum" in spec and value < spec["minimum"]:
                errors.append(f"参数 {key} 小于最小值 {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                errors.append(f"参数 {key} 大于最大值 {spec['maximum']}")
    if errors:
        raise ToolArgumentError("; ".join(errors), details={"errors": errors})
    return clean


def _check_type(key: str, value: Any, spec: dict[str, Any]) -> list[str]:
    expect = spec.get("type")
    if expect is None:
        return []
    ok = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "null": value is None,
    }.get(expect)
    if ok:
        return []
    return [f"参数 {key} 期望 {expect}，实际 {type(value).__name__}({value!r})"]


def _coerce_result(result: Any) -> dict[str, Any]:
    """工具返回值统一为 dict。"""
    if isinstance(result, dict):
        return result
    return {"value": result}


def _infer_schema(fn: Handler) -> dict[str, Any]:
    """从函数签名推断参数 Schema（用于未显式提供 params 的工具）。

    简单类型映射: str→string, int→integer, float→number, bool→boolean,
    list→array, dict→object。默认参数作为 optional。
    """
    sig = inspect.signature(fn)
    props: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    type_map = {
        str: "string", int: "integer", float: "number",
        bool: "boolean", list: "array", dict: "object",
    }
    for pname, p in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        typ = p.annotation if p.annotation is not inspect.Parameter.empty else None
        jt = type_map.get(typ, "string")
        entry: dict[str, Any] = {"type": jt}
        if p.default is not inspect.Parameter.empty:
            entry["default"] = p.default
        else:
            required.append(pname)
        props[pname] = entry
    return {"type": "object", "properties": props, "required": required}