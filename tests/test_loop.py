"""Agent Loop 测试：四步循环、直接回复/调工具分支、max_iters、事件回调、异常容错。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from aurora.llm.parsing import ParsedOutput
from aurora.runtime.loop import AgentLoop, LoopResult, LoopSettings
from aurora.runtime.messages import Role, ToolCall
from aurora.runtime.registry import ToolRegistry
from aurora.runtime.session import Session


# ── 假 LLM：可控返回序列 ─────────────────────────────────────

@dataclass
class FakeModel:
    """按脚本顺序返回输出；耗尽后抛错（用于探测是否多问）。"""

    replies: list[ParsedOutput] = field(default_factory=list)
    calls: list[list[dict]] = field(default_factory=list)

    async def complete(self, messages: list[dict], tools: list[dict]) -> ParsedOutput:
        self.calls.append(messages)
        if not self.replies:
            raise AssertionError("FakeModel 回复已耗尽，但 Loop 还在请求")
        return self.replies.pop(0)


def _tool_registry() -> ToolRegistry:
    r = ToolRegistry()

    @r.register(name="add", description="加法", params={"type": "object",
                 "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                 "required": ["a", "b"]})
    def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    @r.register(name="boom", description="抛异常")
    def boom() -> dict:
        raise RuntimeError("工具爆炸")

    return r


def _make(model, registry=None, settings=None) -> AgentLoop:
    return AgentLoop(
        model=model,
        registry=registry or _tool_registry(),
        settings=settings or LoopSettings(max_iters=5),
    )


async def _run(model, registry=None, settings=None, user_input="1+1"):
    loop = _make(model, registry, settings)
    session = Session(name="test")
    result = await loop.run(session, user_input)
    return loop, session, result


def test_direct_answer_path():
    model = FakeModel(replies=[ParsedOutput(kind="answer", content="直接回答")])
    loop, session, result = asyncio.run(_run(model))
    assert result.success is True
    assert result.answer == "直接回答"
    assert len(model.calls) == 1


def test_direct_answer_with_reasoning():
    model = FakeModel(replies=[ParsedOutput(kind="answer", content="答案", reasoning="思考")])
    loop, session, result = asyncio.run(_run(model))
    assert result.answer == "答案"
    # 思考过程应在会话中可见
    events = [e for e in loop.events if e.kind.value == "thinking"]
    assert len(events) == 1


def test_tool_call_then_answer():
    model = FakeModel(replies=[
        ParsedOutput(kind="tool_calls", tool_calls=[
            ToolCall(id="c1", name="add", arguments={"a": 2, "b": 3})]),
        ParsedOutput(kind="answer", content="和是 5"),
    ])
    loop, session, result = asyncio.run(_run(model))
    assert result.answer == "和是 5"
    # 工具结果应回注 context（带工具追问的基础）
    ctx_msgs = [m.role for m in session.context.build_messages() if m.role is Role.TOOL]
    assert len(ctx_msgs) == 1


def test_multiple_tool_calls_then_done():
    model = FakeModel(replies=[
        ParsedOutput(kind="tool_calls", tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 1})]),
        ParsedOutput(kind="tool_calls", tool_calls=[ToolCall(id="c2", name="add", arguments={"a": 10, "b": 5})]),
        ParsedOutput(kind="answer", content="最终结果"),
    ])
    loop, session, result = asyncio.run(_run(model))
    assert result.success
    assert result.iterations == 3
    tool_evts = [e for e in loop.events if e.kind.value == "tool_call"]
    assert len(tool_evts) == 2


def test_max_iters_stops_loop():
    model = FakeModel(replies=[
        ParsedOutput(kind="tool_calls", tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 1})])
        for _ in range(10)
    ])
    loop, session, result = asyncio.run(_run(model, settings=LoopSettings(max_iters=3)))
    assert result.success is False
    assert result.answer == "" or "迭代" in result.error
    assert len(model.calls) <= 3


def test_tool_error_recorded_not_fatal():
    model = FakeModel(replies=[
        ParsedOutput(kind="tool_calls", tool_calls=[ToolCall(id="c1", name="boom", arguments={})]),
        ParsedOutput(kind="answer", content="工具报错了但我继续"),
    ])
    loop, session, result = asyncio.run(_run(model))
    assert result.success
    # 工具异常应入事件流（ok=False，错误信息在 result.error.message）
    err_events = [e for e in loop.events if e.kind.value == "tool_result" and e.payload.get("ok") is False]
    assert len(err_events) == 1
    message = err_events[0].payload["result"]["error"]["message"]
    assert "爆炸" in str(message)


def test_events_emitted_in_order():
    model = FakeModel(replies=[
        ParsedOutput(kind="tool_calls", tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 1})]),
        ParsedOutput(kind="answer", content="完成"),
    ])
    loop, session, result = asyncio.run(_run(model))
    kinds = [e.kind.value for e in loop.events]
    assert kinds[0] == "iteration"
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "done"
    # seq 递增
    seqs = [e.seq for e in loop.events]
    assert seqs == sorted(seqs)


def test_user_input_recorded_in_context():
    model = FakeModel(replies=[ParsedOutput(kind="answer", content="回")])
    loop, session, result = asyncio.run(_run(model, user_input="今天天气"))
    texts = [m.content for m in session.context.build_messages()]
    assert "今天天气" in texts


def test_loop_uses_session_context_history():
    model = FakeModel(replies=[ParsedOutput(kind="answer", content="第二次回答")])
    loop = _make(model)
    session = Session(name="历史")
    # 预置历史
    asyncio.run(_fill_history(session))
    result = asyncio.run(loop.run(session, "追问"))
    # 追问应携带历史（model.calls 里是 OpenAI 兼容 dict）
    texts = [m.get("content", "") for m in model.calls[0]]
    assert any("历史问题" in t for t in texts)


async def _fill_history(session: Session) -> None:
    from aurora.runtime.messages import Message

    session.context.append(Message(role=Role.USER, content="历史问题"))
    session.context.append(Message(role=Role.ASSISTANT, content="历史回答"))


def test_tool_result_fed_back_to_model():
    model = FakeModel(replies=[
        ParsedOutput(kind="tool_calls", tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 2, "b": 2})]),
        ParsedOutput(kind="answer", content="4"),
    ])
    loop, session, result = asyncio.run(_run(model))
    # 第二次模型调用应包含工具结果消息
    second_call = model.calls[1]
    roles = [m.get("role") for m in second_call]
    assert "tool" in roles


def test_loop_rejects_empty_input():
    model = FakeModel(replies=[ParsedOutput(kind="answer", content="x")])
    loop = _make(model)
    with pytest.raises(ValueError):
        asyncio.run(loop.run(Session(name="t"), "  "))


# ── 基础压缩：真实 Loop 中触发（context 过长时做基础压缩）───

@dataclass
class _CompactingModel:
    """每回合：第一次 complete 返回工具调用，第二次返回最终答案。"""

    n: int = 0

    async def complete(self, messages: list[dict], tools: list[dict]) -> ParsedOutput:
        self.n += 1
        if self.n % 2 == 1:
            return ParsedOutput(kind="tool_calls", tool_calls=[
                ToolCall(id=f"c{self.n}", name="add", arguments={"a": 1, "b": 1})])
        return ParsedOutput(kind="answer", content="本轮已处理完成")


def test_loop_triggers_context_compaction():
    """低压缩阈值下，多轮对话应自动把旧历史浓缩为摘要（而不是无限增长）。"""
    from aurora.runtime.context import BucketedContext

    model = _CompactingModel()
    loop = _make(model, registry=_tool_registry(), settings=LoopSettings(max_iters=5))
    session = Session(name="压缩测试")
    # 注入一个很容易触发压缩的 context（阈值压到极低，便于确定性触发）
    session.context = BucketedContext(max_turns=50, compact_threshold=40)

    for i in range(5):
        result = asyncio.run(loop.run(session, f"第{i}个问题，请帮我算一下并总结"))
        assert result.success

    # 压缩确实发生：产生了"早前对话摘要"，且对话仍能继续
    summary_msgs = [m for m in session.context.build_messages()
                    if m.role is Role.USER and m.content.startswith("[早前对话摘要]")]
    assert summary_msgs, "跑了 5 轮后应有压缩摘要生成"
    assert len(session.context._summary) >= 1
    # 最新一轮的用户输入仍保留（不丢当前轮）
    assert any("第4个问题" in m.content for m in session.context.build_messages())