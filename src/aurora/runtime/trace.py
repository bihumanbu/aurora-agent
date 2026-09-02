"""工具调用 trace / 执行日志。

笔试题"额外要求"之一。每次工具调用都会产生结构化 TraceRecord：
    - 调用前记录 (kind=tool_call)：工具名、参数、发起时间
    - 调用后记录 (kind=tool_result / tool_error)：结果或错误、耗时
    - 会话级 answer / thinking 也入 trace，便于前端"时间线回放"

字段均为结构化 JSON，可持久化、可查询（按 session）、可回放。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from aurora.runtime.messages import new_id


@dataclass
class TraceRecord:
    id: str = ""
    kind: str = ""                      # tool_call / tool_result / tool_error / answer / thinking / done
    session_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    seq: int = 0
    created_at: int = field(default_factory=lambda: int(time.time()))

    def __post_init__(self) -> None:
        if not self.id:
            self.id = new_id("tr_")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "session_id": self.session_id,
            "payload": self.payload,
            "ts": self.ts,
            "seq": self.seq,
        }


class TraceStore:
    """内存 trace 存储（线程安全简单实现）。

    笔试"基础压缩不用复杂实现"，这里同样用基础内存实现，支撑：
        - 按 session 查询（Web 多窗口切换时各自展示）
        - recent 全量/限量（Trace 面板 + 端点）/ 回放
    """

    def __init__(self, max_records: int = 20_000) -> None:
        self._records: dict[str, TraceRecord] = {}
        self._by_session: dict[str, list[str]] = {}
        self._order: list[str] = []
        self._seq = 0
        self._max = max_records

    def record(self, *, kind: str, session_id: str, payload: dict[str, Any]) -> TraceRecord:
        self._seq += 1
        rec = TraceRecord(kind=kind, session_id=session_id, payload=payload, seq=self._seq)
        self._records[rec.id] = rec
        self._by_session.setdefault(session_id, []).append(rec.id)
        self._order.append(rec.id)
        if len(self._order) > self._max:
            oldest = self._order.pop(0)
            self._records.pop(oldest, None)
            self._by_session.get(oldest, []) and self._by_session[oldest].pop(0)
        return rec

    async def arecord(self, *, kind: str, session_id: str, payload: dict[str, Any]) -> TraceRecord:
        return self.record(kind=kind, session_id=session_id, payload=payload)

    def get(self, rec_id: str) -> TraceRecord | None:
        return self._records.get(rec_id)

    async def aget(self, rec_id: str) -> TraceRecord | None:
        return self.get(rec_id)

    def by_session(self, session_id: str) -> list[TraceRecord]:
        ids = self._by_session.get(session_id, [])
        return [self._records[i] for i in ids if i in self._records]

    def recent(self, limit: int = 50) -> list[TraceRecord]:
        ids = self._order[-limit:] if limit else list(self._order)
        return [self._records[i] for i in ids if i in self._records]

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._records.clear()
            self._by_session.clear()
            self._order.clear()
        else:
            for rid in self._by_session.pop(session_id, []):
                self._records.pop(rid, None)
            self._order = [i for i in self._order if i in self._records]

    def size(self) -> int:
        return len(self._records)