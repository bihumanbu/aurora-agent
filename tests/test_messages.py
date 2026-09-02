"""消息模型测试。"""

from aurora.runtime.messages import (
    AgentEvent,
    EventKind,
    Message,
    Role,
    ToolCall,
    new_id,
)


def test_message_roles():
    m = Message(role=Role.USER, content="你好")
    assert m.role is Role.USER
    assert m.content == "你好"


def test_tool_message_fields():
    m = Message(role=Role.TOOL, content="42", name="calculator", tool_call_id="c1")
    assert m.name == "calculator"
    assert m.tool_call_id == "c1"


def test_to_api_dict_plain():
    m = Message(role=Role.USER, content="hi")
    assert m.to_api_dict() == {"role": "user", "content": "hi"}


def test_to_api_dict_tool():
    m = Message(role=Role.TOOL, content="42", name="calculator", tool_call_id="c1")
    d = m.to_api_dict()
    assert d["role"] == "tool"
    assert d["name"] == "calculator"
    assert d["tool_call_id"] == "c1"


def test_to_api_dict_reasoning():
    m = Message(role=Role.ASSISTANT, content="答案", reasoning="思考")
    d = m.to_api_dict()
    assert d["reasoning_content"] == "思考"


def test_agent_event_default_fields():
    e = AgentEvent(kind=EventKind.THINKING, session_id="s1", payload={"text": "x"})
    assert e.seq == 0
    assert e.session_id == "s1"
    d = e.to_dict()
    assert d["kind"] == "thinking"


def test_agent_event_default_session():
    e = AgentEvent(kind=EventKind.DONE, session_id="")
    assert e.session_id == "default"


def test_tool_call_fields():
    tc = ToolCall(id="t1", name="calculator", arguments={"expression": "1+1"})
    assert tc.raw_arguments == ""
    assert tc.arguments["expression"] == "1+1"


def test_new_id_unique():
    a, b = new_id("t_"), new_id("t_")
    assert a != b
    assert a.startswith("t_")