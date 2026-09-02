"""web_search — 搜索工具。

允许 search 用 mock。提供：
    - 默认内置一组示例结果（mock），便于无网/演示
    - 可通过 ``results`` 注入自定义结果提供方（模拟真实搜索引擎）

外部协议通过统一的 ToolResult dict 生效，schema 稳定：
    {"query", "results": [{"title", "url", "snippet"}], "mock": true}
"""

from __future__ import annotations

from typing import Any, Callable

# 示例结果提供方（默认 mock），每个查询给出 3 条示例
ExampleSearcher = Callable[[str, int], list[dict[str, str]]]


def _default_mock(query: str, max_results: int) -> list[dict[str, str]]:
    return [
        {
            "title": f"{query} — 示例结果 {i}",
            "url": f"https://example.com/result?q={query}&i={i}",
            "snippet": f"这是关于「{query}」的第 {i} 条演示快照，来源于 mock 搜索引擎。",
        }
        for i in range(1, max_results + 1)
    ]


def build_search(*, provider: ExampleSearcher | None = None) -> Callable[..., dict[str, Any]]:
    """构造搜索工具。provider 默认用内置 mock，可替换为真实引擎适配。

    用法：
        search = build_search()                                                    # mock
        search = build_search(provider=my_real_searcher)                           # 真实
    """
    searcher = provider or _default_mock

    def web_search(query: str, max_results: int = 3) -> dict[str, Any]:
        max_results = max(1, min(max_results or 3, 10))
        return {
            "query": query,
            "results": searcher(query, max_results),
            "mock": provider is None,
        }

    return web_search


DEFAULT_TOOL = build_search()