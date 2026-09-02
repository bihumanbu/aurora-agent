"""内置工具注册入口。

register_all_tools(registry, project_root=None) 一次性注册要求的
≥3 个工具（实际 6 个函数）：

    calculator  — 安全算术求值（AST 白名单，拒绝任意代码执行）
    web_search  — 搜索（默认 mock，可替换 provider）
    weather     — 天气（默认 mock，可替换 provider）
    todo_add / todo_list / todo_done — 待办（每 registry 独立 store）
    read_docs   — 读取项目文档（限制在项目根内）

todo 数据随 ToolRegistry 实例独立，支撑"多窗口独立 session"在工具侧隔离。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aurora.exceptions import ToolArgumentError
from aurora.runtime.registry import ToolRegistry, schema


def register_all_tools(registry: ToolRegistry, project_root: Path | None = None) -> None:
    """注册全部内置工具到一个 ToolRegistry。

    project_root 用于 read_docs 的安全边界；缺省用运行目录。
    """
    from aurora.tools.calculator import calculator
    from aurora.tools.todo import TodoStore, build_todo_tools
    from aurora.tools.weather import build_open_meteo_provider, build_weather
    from aurora.tools.web_search import build_search

    root = project_root or Path.cwd()

    @registry.register(
        name="calculator",
        description="安全地计算数学表达式，如 12*(3+4)、7/2、2**10。只接受数值表达式，不允许函数调用或变量。",
        params=schema({"expression": {"type": "string", "description": "要计算的数学表达式"}},
                      required=["expression"]),
    )
    def _calc(expression: str) -> dict[str, Any]:
        return calculator(expression)

    @registry.register(
        name="web_search",
        description="搜索互联网（演示环境为模拟数据）。返回标题/链接/摘要。",
        params=schema(
            {"query": {"type": "string", "description": "搜索关键词"},
             "max_results": {"type": "integer", "description": "返回条数，默认 3"}},
            required=["query"],
        ),
    )
    def _search(query: str, max_results: int = 3) -> dict[str, Any]:
        fn = build_search()
        try:
            return fn(query=query, max_results=max_results)
        except Exception as e:  # noqa: BLE001
            raise ToolArgumentError(f"搜索失败: {e}") from e

    @registry.register(
        name="weather",
        description="查询城市实时天气（数据来自 Open-Meteo，免费免 API key；网络异常时回退演示数据）。",
        params=schema({"city": {"type": "string", "description": "城市名，如 厦门"}},
                      required=["city"]),
    )
    def _weather(city: str) -> dict[str, Any]:
        return build_weather(provider=build_open_meteo_provider())(city)

    # todo 三件套：每个 registry 独立 store，实现"多窗口隔离"的工具侧
    todo_tools = build_todo_tools(TodoStore())

    @registry.register(
        name="todo_add",
        description="添加一条待办事项。",
        params=schema({"text": {"type": "string", "description": "待办内容"}},
                      required=["text"]),
    )
    def _todo_add(text: str) -> dict[str, Any]:
        return todo_tools["todo_add"](text)

    @registry.register(
        name="todo_list",
        description="列出当前会话的所有待办事项。",
    )
    def _todo_list() -> dict[str, Any]:
        return todo_tools["todo_list"]()

    @registry.register(
        name="todo_done",
        description="把某条待办标记为已完成。",
        params=schema({"task_id": {"type": "string", "description": "待办 id"}},
                      required=["task_id"]),
    )
    def _todo_done(task_id: str) -> dict[str, Any]:
        return todo_tools["todo_done"](task_id)

    @registry.register(
        name="read_docs",
        description="读取项目文档（README/DESIGN 等），用于了解系统本身。",
        params=schema(
            {"path": {"type": "string", "description": "相对路径，如 README.md 或 doc/DESIGN.md"},
             "max_chars": {"type": "integer", "description": "最大读取字符数，默认 4000"}},
            required=["path"],
        ),
    )
    def _read_docs(path: str, max_chars: int = 4000) -> dict[str, Any]:
        from aurora.tools.read_docs import DocsReader

        return DocsReader(root).read(path=path, max_chars=max_chars)