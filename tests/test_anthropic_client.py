"""Anthropic 协议客户端测试（DeepSeek Anthropic 兼容入口）。

不发起任何真实网络请求：用 httpx.MockTransport 在本地模拟 /v1/messages 端点。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from aurora.exceptions import LLMQuotaError
from aurora.llm.clients import (
    AnthropicCompatibleClient,
    _extract_system,
    _parse_anthropic_response,
    _to_anthropic_messages,
)
from aurora.llm.gateway import LLMConfig
from aurora.runtime.registry import ToolRegistry
from aurora.web.hub import Hub


def _cfg(api_base: str = "https://api.deepseek.com/anthropic") -> LLMConfig:
    return LLMConfig(
        mock=False,
        api_base=api_base,
        api_key="sk-test-xxxx",
        model="deepseek-chat",
        provider="anthropic",
        temperature=0.3,
        max_tokens=1024,
    )


# ── 端点推导 ────────────────────────────────────────────────
def test_messages_url_variants():
    base = "https://api.deepseek.com/anthropic"
    assert AnthropicCompatibleClient._messages_url(base) == base + "/v1/messages"
    assert AnthropicCompatibleClient._messages_url(base + "/v1") == base + "/v1/messages"
    assert AnthropicCompatibleClient._messages_url(base + "/v1/messages") == base + "/v1/messages"


# ── system 抽取 ─────────────────────────────────────────────
def test_extract_system():
    msgs = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "保持简洁"},
    ]
    assert _extract_system(msgs) == "你是助手\n\n保持简洁"


# ── OpenAI→Anthropic 消息转换（tool_use 直接由 assistant.tool_calls 渲染）──
def test_to_anthropic_messages_rebuilds_tool_use():
    """Loop 已把带 tool_calls 的 assistant 消息存入 context；
    转换器直接读取 tool_calls 渲染 tool_use，并由 tool_result 紧随其后配对。"""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "算一下 6*7"},
        {"role": "assistant", "content": "", "reasoning_content": "让我想想",
         "tool_calls": [{"id": "tc_1", "type": "function",
                         "function": {"name": "calculator",
                                      "arguments": json.dumps({"expression": "6*7"})}}]},
        {"role": "tool", "content": "42", "name": "calculator", "tool_call_id": "tc_1"},
    ]
    out = _to_anthropic_messages(msgs, {})

    # 首条非 system 是 user；assistant 携带 tool_use；其后是 user(tool_result)
    assert out[0]["role"] == "user"
    assert out[1]["role"] == "assistant"
    a_blocks = out[1]["content"]
    tool_use = next(b for b in a_blocks if b["type"] == "tool_use")
    assert tool_use["id"] == "tc_1"
    assert tool_use["name"] == "calculator"
    assert tool_use["input"] == {"expression": "6*7"}
    # 思考过程作为文本块一并带出
    assert any(b["type"] == "text" and "让我想想" in b["text"] for b in a_blocks)

    assert out[2]["role"] == "user"
    tr = out[2]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "tc_1"
    assert tr["content"] == "42"


def test_to_anthropic_messages_multiple_tools_merge():
    msgs = [
        {"role": "user", "content": "查天气和待办"},
        {"role": "assistant", "content": "", "reasoning_content": "r",
         "tool_calls": [
             {"id": "t1", "type": "function",
              "function": {"name": "weather", "arguments": json.dumps({"city": "北京"})}},
             {"id": "t2", "type": "function",
              "function": {"name": "todo_add", "arguments": json.dumps({"text": "周报"})}},
         ]},
        {"role": "tool", "content": "晴", "name": "weather", "tool_call_id": "t1"},
        {"role": "tool", "content": "已记", "name": "todo_add", "tool_call_id": "t2"},
    ]
    out = _to_anthropic_messages(msgs, {})
    # 两个 tool_use 在同一 assistant；两个 tool_result 合并进一个 user
    assert out[0]["role"] == "user"
    assert out[1]["role"] == "assistant"
    tool_uses = [b for b in out[1]["content"] if b["type"] == "tool_use"]
    assert len(tool_uses) == 2
    assert out[2]["role"] == "user"
    assert len(out[2]["content"]) == 2
    assert all(b["type"] == "tool_result" for b in out[2]["content"])


# ── 响应解析 ───────────────────────────────────────────────
def test_parse_anthropic_tool_use():
    data = {
        "content": [
            {"type": "text", "text": "好的，我来查"},
            {"type": "tool_use", "id": "tu_1", "name": "weather", "input": {"city": "北京"}},
        ],
        "stop_reason": "tool_use",
    }
    cache: dict = {}
    po = _parse_anthropic_response(data, cache)
    assert po.kind == "tool_calls"
    assert po.content == "好的，我来查"
    assert po.tool_calls[0].name == "weather"
    assert po.tool_calls[0].arguments == {"city": "北京"}
    # 缓存供下一轮重建 tool_use
    assert cache["tu_1"]["name"] == "weather"


def test_parse_anthropic_answer():
    data = {"content": [{"type": "text", "text": "北京今天晴"}], "stop_reason": "end_turn"}
    po = _parse_anthropic_response(data, {})
    assert po.kind == "answer"
    assert po.content == "北京今天晴"


# ── 端到端（MockTransport，无真实网络）──────────────────────
def _mock_client(json_response: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=json_response)
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_anthropic_complete_answer_request_shape():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"content": [{"type": "text", "text": "你好"}],
                                         "stop_reason": "end_turn"})

    client = AnthropicCompatibleClient(_cfg(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = asyncio.run(client.complete(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], []))

    # 协议正确性
    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"].get("x-api-key") == "sk-test-xxxx"
    assert captured["headers"].get("anthropic-version") == "2023-06-01"
    body = captured["body"]
    assert body["model"] == "deepseek-chat"
    assert body["max_tokens"] == 1024
    assert body["system"] == "sys"
    assert body["messages"][0]["role"] == "user"
    assert body["stream"] is False
    # 解析正确性
    assert out.kind == "answer"
    assert out.content == "你好"


def test_anthropic_quota_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={
            "error": {"message": "token plan entitlement exhausted",
                      "type": "quota_exceeded_error", "code": "8"}})
    client = AnthropicCompatibleClient(_cfg(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(LLMQuotaError):
        asyncio.run(client.complete([{"role": "user", "content": "hi"}], []))


# ── Hub 路由：anthropic provider 选中 Anthropic 客户端 ──────
def test_hub_configure_anthropic_selects_client():
    hub = Hub(registry=ToolRegistry())
    st = asyncio.run(hub.call("llm.configure", {
        "provider": "anthropic",
        "api_base": "https://api.deepseek.com/anthropic",
        "model": "deepseek-chat",
        "api_key": "sk-test-xxxx",
    }))
    assert st["configured"] is True
    assert type(hub.gateway.backend).__name__ == "AnthropicCompatibleClient"


# ── 端到端 Loop 集成：Anthropic 客户端驱动真实多轮工具调用 ──
def _add_registry() -> ToolRegistry:
    r = ToolRegistry()

    @r.register(name="add", description="加法", params={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    })
    def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    return r


def test_anthropic_loop_tool_call_integration():
    """用真实的 AgentLoop + Anthropic 客户端跑一轮「调工具→再回答」。

    MockTransport 模拟 DeepSeek Anthropic 端点：第一次返回 tool_use，
    第二次返回答案。验证 tool_cache 在真实 Loop 里的重建正确生效。
    """
    from aurora.runtime.loop import AgentLoop, LoopSettings
    from aurora.runtime.session import Session

    responses = [
        {"content": [{"type": "tool_use", "id": "tu_1", "name": "add",
                      "input": {"a": 2, "b": 3}}], "stop_reason": "tool_use"},
        {"content": [{"type": "text", "text": "和是 5"}], "stop_reason": "end_turn"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    client = AnthropicCompatibleClient(
        _cfg(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    loop = AgentLoop(model=client, registry=_add_registry(),
                    settings=LoopSettings(max_iters=5))
    session = Session(name="t")
    result = asyncio.run(loop.run(session, "2+3 等于几"))

    assert result.success is True
    assert "5" in result.answer
    tool_evts = [e for e in loop.events if e.kind.value == "tool_call"]
    assert len(tool_evts) == 1
    assert tool_evts[0].payload["tool"] == "add"
    # 第二轮请求里必须重建出 tool_use 块（否则 Anthropic 会拒收 tool_result）
    assert len(client._tool_cache) == 1


def test_to_anthropic_messages_orphan_tool_use_is_paired():
    """协议级兜底：即使上游 build_messages 因 compact/trim 把 tool_result 错位到
    末尾（unmatched），转换层也必须按 tool_call_id 全局配对，绝不产生孤立 tool_use。

    这是修复「tool_use ids were found without tool_result」400 复发的核心。
    """
    msgs = [
        {"role": "user", "content": "读文档"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc_1", "type": "function",
                         "function": {"name": "read_docs", "arguments": '{"path":"DESIGN.md"}'}}]},
        # 故意不紧跟 role=tool，模拟归位失败 + 跨轮追问
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc_2", "type": "function",
                         "function": {"name": "read_docs", "arguments": "{}"}}]},
        # tool 结果错位到末尾
        {"role": "tool", "content": "[DESIGN 内容]", "tool_call_id": "tc_1"},
        {"role": "tool", "content": "[工具错误]", "tool_call_id": "tc_2"},
    ]
    out = _to_anthropic_messages(msgs, {})
    # 每条 assistant 后必须紧跟 user 且含对应 tool_result
    for i, m in enumerate(out):
        if m["role"] == "assistant":
            assert i + 1 < len(out) and out[i + 1]["role"] == "user", f"msg[{i}] 后缺 user"
            trs = [x for x in out[i + 1]["content"] if x["type"] == "tool_result"]
            assert trs, f"msg[{i}] 后无 tool_result"
            for b in m["content"]:
                if b["type"] == "tool_use":
                    assert any(t["tool_use_id"] == b["id"] for t in trs), f"孤立 {b['id']}"


def test_to_anthropic_messages_missing_result_gets_placeholder():
    """tool_use 完全没有对应 tool_result 时（极端边界），补 is_error 占位，
    保证发给 Anthropic 的请求合法、不 400（而非漏发导致 400）。"""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc_x", "type": "function",
                         "function": {"name": "read_docs", "arguments": "{}"}}]},
    ]
    out = _to_anthropic_messages(msgs, {})
    assert out[1]["role"] == "assistant"
    assert out[2]["role"] == "user"
    tr = out[2]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "tc_x"
    assert tr.get("is_error") is True

