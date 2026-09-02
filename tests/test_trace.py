"""工具调用 trace / 执行日志测试。"""

import time

from aurora.runtime.trace import TraceRecord, TraceStore


def _mk() -> TraceStore:
    return TraceStore()


def test_record_tool_call():
    s = _mk()
    rec = s.record(
        kind="tool_call", session_id="s1",
        payload={"tool": "calculator", "arguments": {"expression": "1+1"}},
    )
    assert rec.kind == "tool_call"
    assert rec.session_id == "s1"
    assert rec.payload["tool"] == "calculator"


def test_auto_id_and_ts():
    s = _mk()
    r1 = s.record(kind="answer", session_id="s1", payload={"text": "a"})
    assert r1.id
    assert r1.ts > 0
    assert isinstance(r1.created_at, (int, float))


def test_query_by_session():
    s = _mk()
    s.record(kind="tool_call", session_id="a", payload={"tool": "x"})
    s.record(kind="tool_call", session_id="b", payload={"tool": "y"})
    s.record(kind="tool_result", session_id="a", payload={"tool": "x", "ok": True})
    rows = s.by_session("a")
    assert len(rows) == 2
    assert all(r.session_id == "a" for r in rows)


def test_query_all_recent():
    s = _mk()
    s.record(kind="tool_call", session_id="a", payload={})
    s.record(kind="answer", session_id="b", payload={})
    rows = s.recent(limit=10)
    assert len(rows) >= 2


def test_recent_limit():
    s = _mk()
    for _ in range(5):
        s.record(kind="tool_call", session_id="a", payload={})
    assert len(s.recent(limit=3)) == 3


def test_record_error_marks_ok_false():
    s = _mk()
    rec = s.record(kind="tool_error", session_id="s1",
                   payload={"tool": "calc", "error": {"code": "tool-execution-error"}})
    assert rec.payload.get("error", {}).get("code") == "tool-execution-error"


def test_async_record_and_get():
    import asyncio

    async def main():
        s = _mk()
        rec = await s.arecord(kind="tool_call", session_id="s1", payload={"tool": "z"})
        got = await s.aget(rec.id)
        return got is not None and got.id == rec.id

    assert asyncio.run(main()) is True


def test_clear_and_seq_growth():
    s = _mk()
    s.record(kind="answer", session_id="s1", payload={})
    s.record(kind="answer", session_id="s1", payload={})
    assert len(s.recent()) == 2


def test_record_interleaves_seq_global():
    s = _mk()
    r1 = s.record(kind="tool_call", session_id="a", payload={})
    r2 = s.record(kind="tool_call", session_id="b", payload={})
    assert r1.seq < r2.seq