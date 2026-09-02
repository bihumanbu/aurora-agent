"""todo — 待办事项（内存存储，演示 session 持久化的工具侧）。

每个 ToolRegistry 通过 register_all_tools 创建独立的 TodoStore，
从而支持"多窗口独立 session"在工具数据侧也隔离：
    - 窗口 1 记的待办，窗口 2 不可见
    - todo 数据随 session 生命周期存续（会话恢复后仍在）
"""

from __future__ import annotations

import time
import uuid
from typing import Any


class TodoStore:
    """内存待办存储（线程安全，按需扩容）。"""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []

    def add(self, text: str) -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "done": False,
            "created_at": int(time.time()),
        }
        self._items.append(item)
        return item

    def list(self) -> list[dict[str, Any]]:
        return list(self._items)

    def done(self, task_id: str) -> dict[str, Any]:
        for item in self._items:
            if item["id"] == task_id:
                item["done"] = True
                return item
        return {"error": f"未找到待办: {task_id}"}


def build_todo_tools(store: TodoStore | None = None) -> dict[str, Any]:
    """构造 todo_add / todo_list / todo_done 三个工具的 handler。"""
    store = store or TodoStore()

    def todo_add(text: str) -> dict[str, Any]:
        item = store.add(text)
        return {"ok": True, "item": item}

    def todo_list() -> dict[str, Any]:
        return {"ok": True, "items": store.list()}

    def todo_done(task_id: str) -> dict[str, Any]:
        item = store.done(task_id)
        return {"ok": "error" not in item, "item": item}

    return {"todo_add": todo_add, "todo_list": todo_list, "todo_done": todo_done}


DEFAULT_TOOLS = build_todo_tools()