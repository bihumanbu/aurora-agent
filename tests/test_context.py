"""Context 管理测试：最大轮次、分桶、压缩、追问。"""

from aurora.runtime.context import BucketedContext, estimate_tokens
from aurora.runtime.messages import Message, Role


def _u(text: str) -> Message:
    return Message(role=Role.USER, content=text)


def _a(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def test_estimate_tokens():
    assert estimate_tokens("一二三") == 3
    assert estimate_tokens("abc") == 1  # 4 * 0.25


def test_append_and_turns():
    ctx = BucketedContext(max_turns=3)
    ctx.append(_u("问题1"))
    ctx.append(_a("回答1"))
    ctx.append(_u("问题2"))
    assert ctx.turns == 2


def test_max_turns_trims(capfd=None):
    ctx = BucketedContext(max_turns=2)
    for i in range(1, 6):
        ctx.append(_u(f"问题{i}"))
        ctx.append(_a(f"回答{i}"))
    assert ctx.turns == 5  # 总轮数仍累计
    msgs = ctx.build_messages()
    # 只剩最近 2 轮
    user_texts = [m.content for m in msgs if m.role is Role.USER]
    assert user_texts == ["问题4", "问题5"]


def test_append_tool_result_capped():
    ctx = BucketedContext()
    ctx._tool_results_cap = 3
    for i in range(5):
        ctx.append(Message(role=Role.TOOL, content=f"r{i}", name="t", tool_call_id=str(i)))
    assert len(ctx._tool_results) == 3
    assert ctx._tool_results[0].content == "r2"


def test_tool_results_cap_covers_full_demo_session():
    """一次典型演示（10~15 次工具调用）的早期结果不应被挤出桶。

    挤出本身不会让协议报错（转换层补 is_error 占位），但模型会读到
    "工具结果缺失"，导致追问前面的调用时答不上来——这是体验缺陷，
    故要求默认容量能覆盖完整演示。
    """
    from aurora.llm.clients import _to_anthropic_messages

    ctx = BucketedContext()
    for i in range(15):
        ctx.append(_u(f"第{i}轮问题"))
        ctx.append(Message(role=Role.ASSISTANT, content="",
                           tool_calls=[{"id": f"c{i}", "name": "weather",
                                        "arguments": {"city": "厦门"}}]))
        ctx.append(Message(role=Role.TOOL, content=f'{{"temp_c": {20 + i}}}',
                           name="weather", tool_call_id=f"c{i}"))
    assert len(ctx._tool_results) == 15  # 未触发上限

    out = _to_anthropic_messages([m.to_api_dict() for m in ctx.build_messages()], {})
    placeholders = [
        b for m in out if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if b.get("type") == "tool_result" and b.get("is_error")
    ]
    assert placeholders == []


def test_system_message_bucket():
    ctx = BucketedContext()
    ctx.set_system("你是助手")
    msgs = ctx.build_messages()
    assert msgs[0].role is Role.SYSTEM
    assert msgs[0].content == "你是助手"


def test_build_messages_order():
    ctx = BucketedContext()
    ctx.set_system("SYS")
    ctx.append(_u("U1"))
    ctx.append(_a("A1"))
    msgs = ctx.build_messages()
    assert [m.content for m in msgs] == ["SYS", "U1", "A1"]


def test_compact_when_under_threshold_skips():
    ctx = BucketedContext(compact_threshold=10_000)
    ctx.append(_u("短问题"))
    ctx.append(_a("短回答"))
    assert ctx.compact() is False
    assert len(ctx.build_messages()) == 2


def test_compact_triggers_summary():
    ctx = BucketedContext(compact_threshold=100)
    for i in range(10):
        ctx.append(_u(f"第{i}个很长很长很长很长很长很长的问题内容"))
        ctx.append(_a(f"第{i}个很长很长很长很长很长很长的回答内容"))
    before = len(ctx.build_messages())
    assert ctx.compact(summarizer=lambda s: "摘要")
    after = len(ctx.build_messages())
    assert after < before
    assert ctx.build_messages()[0].content.startswith("[早前对话摘要]") or \
        ctx.build_messages()[0].content == "摘要"


def test_compact_keeps_last_user_turn():
    ctx = BucketedContext(compact_threshold=200)
    for i in range(6):
        ctx.append(_u(f"问题内容{i}很长很长"))
        ctx.append(_a(f"回答内容{i}很长很长"))
    ctx.append(_u("最新追问"))
    assert ctx.compact(summarizer=lambda s: "摘要")
    user_texts = [m.content for m in ctx.build_messages() if m.role is Role.USER]
    assert "最新追问" in user_texts


def test_compact_force():
    ctx = BucketedContext(compact_threshold=10000)
    ctx.append(_u("话"))
    ctx.append(_a("答"))
    assert ctx.compact(force=True) is True


def test_build_messages_optional_tool_results():
    ctx = BucketedContext()
    ctx.append(_u("U"))
    ctx.append(Message(role=Role.TOOL, content="42", name="calc"))
    assert any(m.role is Role.TOOL for m in ctx.build_messages())
    assert not any(m.role is Role.TOOL for m in ctx.build_messages(include_tool_results=False))
    # tool_results 不应计入 build_messages 的默认用户轮数
    assert ctx.turns == 1


def test_clear():
    ctx = BucketedContext()
    ctx.set_system("S")
    ctx.append(_u("U"))
    ctx.clear()
    assert ctx.build_messages() == []
    assert ctx.turns == 0