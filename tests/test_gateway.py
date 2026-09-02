"""LLM Gateway 测试：mock 后端 / 真实 API 请求格式 / 多厂商配置。"""

from __future__ import annotations

import asyncio
import json

import pytest

from aurora.exceptions import LLMError
from aurora.llm.clients import OpenAICompatibleClient
from aurora.llm.fake import FakeLLM, ScriptedScenario
from aurora.llm.gateway import LLMConfig, ModelGateway


def test_config_defaults():
    cfg = LLMConfig()
    assert cfg.mock is False
    assert cfg.api_base == ""


def test_config_provider_string():
    cfg = LLMConfig(api_base="https://api.deepseek.com/v1", api_key="k",
                    model="deepseek-chat")
    assert "deepseek" in cfg.effective_base()


def test_fake_llm_scripted_answer():
    fake = FakeLLM(scenario=ScriptedScenario(
        steps=[("answer", None, "你好！", None)],
    ))

    async def run():
        return await fake.complete([], [])

    out = asyncio.run(run())
    assert out.kind == "answer"
    assert out.content == "你好！"


def test_gateway_mock_mode_returns_answer():
    cfg = LLMConfig(mock=True, model="fake")
    fake = FakeLLM(scenario=ScriptedScenario(steps=[("answer", None, "mock 回答", None)]))
    gw = ModelGateway(cfg, backend=fake)

    async def run():
        return await gw.complete([{"role": "user", "content": "hi"}], [])

    out = asyncio.run(run())
    assert out.content == "mock 回答"


def test_gateway_tool_calls_pass_through():
    cfg = LLMConfig(mock=True)
    fake = FakeLLM(scenario=ScriptedScenario(
        steps=[("tool_calls", None, "",
                [{"name": "calculator", "arguments": {"expression": "2+2"}}])],
    ))
    gw = ModelGateway(cfg, backend=fake)

    async def run():
        out = await gw.complete([], [])
        assert out.kind == "tool_calls"
        assert out.tool_calls[0].name == "calculator"
        assert out.tool_calls[0].arguments == {"expression": "2+2"}

    asyncio.run(run())


def test_gateway_parses_reasoning_content():
    cfg = LLMConfig(mock=True)
    fake = FakeLLM(scenario=ScriptedScenario(
        steps=[("answer", "推理过程", "最终答案", None)],
    ))
    gw = ModelGateway(cfg, backend=fake)

    async def run():
        out = await gw.complete([], [])
        assert out.reasoning == "推理过程"
        assert out.content == "最终答案"

    asyncio.run(run())


def test_gateway_scripted_sequence():
    """脚本序列演示：先工具后答案（对应 Agent loop 的多步）。"""
    fake = FakeLLM(scenario=ScriptedScenario(steps=[
        ("tool_calls", None, "", [{"name": "weather", "arguments": {"city": "北京"}}]),
        ("answer", None, "北京天气晴", None),
    ]))
    gw = ModelGateway(fake.config(), backend=fake)

    async def run():
        first = await gw.complete([], [])
        second = await gw.complete([], [])
        return first, second

    f, s = asyncio.run(run())
    assert f.kind == "tool_calls"
    assert f.tool_calls[0].name == "weather"
    assert s.kind == "answer"
    assert s.content == "北京天气晴"


def test_openai_client_request_payload_shape():
    """真实 API 请求：验证 tools/messages 进入请求体、tool_calls 解析正确。"""
    import httpx

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.read().decode()))
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "用工具",
                    "tool_calls": [{
                        "id": "call_x",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expression":"3*3"}'},
                    }],
                },
            }],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = LLMConfig(api_base="https://api.example.com/v1", api_key="k", model="m")
    oc = OpenAICompatibleClient(cfg, http_client=client)

    async def run():
        return await oc.complete(
            [{"role": "user", "content": "算一下"}],
            [{"type": "function", "function": {"name": "calculator"}}],
        )

    out = asyncio.run(run())
    assert out.kind == "tool_calls"
    assert out.tool_calls[0].name == "calculator"
    # 请求体形状正确
    assert "tools" in captured
    assert "messages" in captured
    assert captured["model"] == "m"


def test_openai_client_http_error_raises_aurora():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = LLMConfig(api_base="https://api.example.com/v1", api_key="k", model="m")
    oc = OpenAICompatibleClient(cfg, http_client=client)

    async def run():
        await oc.complete([{"role": "user", "content": "hi"}], [])

    with pytest.raises(LLMError):
        asyncio.run(run())


def test_fake_llm_exhausted_raises():
    fake = FakeLLM(scenario=ScriptedScenario(steps=[]))
    with pytest.raises(LLMError):
        asyncio.run(fake.complete([], []))


def test_fake_llm_tracks_calls():
    fake = FakeLLM(scenario=ScriptedScenario(steps=[("answer", None, "ok", None)]))
    asyncio.run(fake.complete([{"role": "user", "content": "hi"}], [{"type": "function"}]))
    assert len(fake.calls) == 1
    assert fake.calls[0]["messages"][0]["content"] == "hi"