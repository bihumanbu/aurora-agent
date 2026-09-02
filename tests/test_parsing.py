"""LLM 输出解析测试：思考/工具调用/答案提取、双路通道、异常边界。"""

import pytest

from aurora import exceptions
from aurora.llm.parsing import ParsedOutput, parse_native, parse_text, parse_tool_calls_text


# ── 文本通道：提取工具调用 ───────────────────────────────────

def test_text_json_block():
    text = '用户的问题来了，答案是：\n```json\n{"name": "calculator", "arguments": {"expression": "1+1"}}\n```'
    calls = parse_tool_calls_text(text)
    assert len(calls) == 1
    assert calls[0].name == "calculator"
    assert calls[0].arguments["expression"] == "1+1"


def test_text_json_array_block():
    text = '```json\n[{"name":"a","arguments":{"x":"1"}},{"name":"b","arguments":{"y":"2"}}]\n```'
    calls = parse_tool_calls_text(text)
    assert len(calls) == 2
    assert [c.name for c in calls] == ["a", "b"]


def test_text_action_line():
    text = "我来查询一下\nAction: {\"name\": \"weather\", \"arguments\": {\"city\": \"北京\"}}"
    calls = parse_tool_calls_text(text)
    assert len(calls) == 1
    assert calls[0].name == "weather"
    assert calls[0].arguments["city"] == "北京"


def test_text_no_tool_is_empty():
    assert parse_tool_calls_text("就是普通回答，不调用任何工具。") == []


def test_text_args_as_string():
    text = '```json\n{"name":"x","arguments":"{\\"q\\":\\"hi\\"}"}\n```'
    calls = parse_tool_calls_text(text)
    assert calls[0].arguments == {"q": "hi"}


def test_text_brace_tool_style():
    text = "调用一下 {calculator, {\"expression\": \"2*3\"}}"
    calls = parse_tool_calls_text(text)
    assert len(calls) == 1
    assert calls[0].name == "calculator"


# ── parse_text：思考剥除 ─────────────────────────────────────

def test_parse_text_extracts_thinking_tags():
    text = "<thinking>先计算再回答</thinking>结果是 3。"
    out = parse_text(text)
    assert out.kind == "answer"
    assert out.reasoning == "先计算再回答"
    assert "结果是 3。" in out.content


def test_parse_text_extracts_bracket_thinking():
    text = "[thinking]查看天气[/thinking]明天北京有雨。"
    out = parse_text(text)
    assert out.reasoning == "查看天气"
    assert "明天北京有雨" in out.content


def test_parse_text_tool_call_with_thinking():
    text = "<thinking>需要工具</thinking>\n```json\n{\"name\":\"todo_add\",\"arguments\":{\"text\":\"写周报\"}}\n```"
    out = parse_text(text)
    assert out.kind == "tool_calls"
    assert out.reasoning == "需要工具"
    assert out.tool_calls[0].name == "todo_add"


def test_parse_text_empty_raises():
    with pytest.raises(exceptions.ParseError):
        parse_text("")
    with pytest.raises(exceptions.ParseError):
        parse_text("   \n  ")


# ── 原生 function-call 通道 ──────────────────────────────────

def test_parse_native_tool_calls():
    msg = {
        "role": "assistant",
        "content": "我帮你算一下",
        "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "calculator", "arguments": '{"expression":"2**10"}'}},
        ],
    }
    out = parse_native(msg)
    assert out.kind == "tool_calls"
    assert out.tool_calls[0].name == "calculator"
    assert out.tool_calls[0].arguments == {"expression": "2**10"}
    assert out.tool_calls[0].id == "call_1"


def test_parse_native_reasoning():
    msg = {"content": "最终答案", "reasoning_content": "深度思考"}
    out = parse_native(msg)
    assert out.kind == "answer"
    assert out.content == "最终答案"
    assert out.reasoning == "深度思考"


def test_parse_native_empty_raises():
    with pytest.raises(exceptions.ParseError):
        parse_native({"role": "assistant", "content": ""})


def test_parse_native_content_parts_array():
    msg = {"content": [{"type": "text", "text": "部分1"}, {"type": "text", "text": "部分2"}]}
    out = parse_native(msg)
    assert out.content == "部分1部分2"


def test_parse_native_bad_json_args():
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c", "function": {"name": "x", "arguments": "{bad json"}},
        ],
    }
    out = parse_native(msg)
    assert out.tool_calls[0].name == "x"
    assert out.tool_calls[0].arguments == {}


# ── 集成辅助 ─────────────────────────────────────────────────

def test_parsed_output_defaults():
    o = ParsedOutput()
    assert o.kind == "answer"
    assert o.tool_calls == []