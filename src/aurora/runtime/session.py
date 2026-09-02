"""Session 管理 — 笔试"session管理"。

多窗口独立会话：
    用户 A 开窗口 1（查天气记待办）、窗口 2（写周报），
    两个窗口是独立 session，各自拥有独立的 Context，互不串扰；
    可从任意现有 session 继续对话（会话恢复）。

Session 持有：
    - context（BucketedContext）
    - 可选 trace store 引用（由更高层注入）

SessionManager 负责创建/获取/删除，name 保证唯一（同名自动加序号）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from aurora.runtime.context import BucketedContext
from aurora.runtime.messages import new_id
from aurora.runtime.trace import TraceStore


@dataclass
class Session:
    name: str
    session_id: str = field(default_factory=lambda: new_id("s_"))
    context: BucketedContext = field(default_factory=BucketedContext)
    trace: TraceStore = field(default_factory=TraceStore)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "turns": len([m for m in self.context.build_messages() if m.role.name == "USER"]),
        }


class SessionManager:
    """会话管理器：创建/获取/删除/列出，保证 name 唯一。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def _unique_name(self, name: str) -> str:
        """基于当前已有同名 session 数量推导序号，而非全局计数器。

        刷新页面后只有 1 个窗口 → 叫"窗口"；再开一个 → "窗口#2"。
        删掉#2 后再开 → 仍叫"窗口#2"（占下一个空缺），不会无限递增。
        """
        if not any(s.name == name for s in self._sessions.values()):
            return name
        n = 2
        while any(s.name == f"{name}#{n}" for s in self._sessions.values()):
            n += 1
        return f"{name}#{n}"

    def create(self, name: str = "窗口") -> Session:
        s = Session(name=self._unique_name(name))
        self._sessions[s.session_id] = s
        return s

    async def acreate(self, name: str = "窗口") -> Session:
        return self.create(name)

    def get(self, session_id: str) -> Session:
        return self._sessions[session_id]

    async def aget(self, session_id: str) -> Session:
        return self._sessions[session_id]

    def remove(self, session_id: str) -> bool:
        existed = session_id in self._sessions
        self._sessions.pop(session_id, None)
        return existed

    def sessions(self) -> list[Session]:
        return list(self._sessions.values())

    def clear(self) -> None:
        self._sessions.clear()

    def count(self) -> int:
        return len(self._sessions)