"""共享测试夹具。"""

from __future__ import annotations

import pytest


@pytest.fixture
def tool_registry():
    """一个注册了全部内置工具的 ToolRegistry（每个测试独立，避免串扰）。"""
    from aurora.runtime.registry import ToolRegistry
    from aurora.tools import register_all_tools

    r = ToolRegistry()
    register_all_tools(r)
    return r