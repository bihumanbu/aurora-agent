"""四象限消息模型 + Hub 测试。"""

from __future__ import annotations

import asyncio

import pytest

from aurora.web.hub import Hub
from aurora.web.protocol import (
    ClientRequest,
    ClientResponse,
    RpcReceipt,
    ServerRequest,
    ServerResponse,
    make_rpc_id,
    validate_message,
)


# ── 四象限消息模型 ───────────────────────────────────────────

def test_make_rpc_id_unique():
    a, b = make_rpc_id(), make_rpc_id()
    assert a != b
    assert a.startswith("rpc_")


def test_client_request_shape():
    req = ClientRequest(rpc_id="r1", method="session.list", payload={"a": 1})
    assert req.type == "client-request"
    d = req.to_dict()
    assert d["type"] == "client-request"
    assert d["method"] == "session.list"
    assert d["payload"] == {"a": 1}
    assert d["rpcId"] == "r1"


def test_server_response_shape():
    resp = ServerResponse(rpc_id="r1", ok=True, value={"sum": 2})
    d = resp.to_dict()
    assert d["type"] == "server-response"
    assert d["rpcId"] == "r1"
    assert d["result"]["ok"] is True
    assert d["result"]["value"] == {"sum": 2}


def test_server_response_error_shape():
    resp = ServerResponse(rpc_id="r1", ok=False, error={"code": "x", "message": "err"})
    d = resp.to_dict()
    assert d["result"]["ok"] is False
    assert d["result"]["error"]["code"] == "x"


def test_client_response_shape():
    resp = ClientResponse(rpc_id="r1", ok=True, value={"accepted": True})
    d = resp.to_dict()
    assert d["type"] == "client-response"
    assert d["rpcId"] == "r1"


def test_server_request_downlink():
    req = ServerRequest(rpc_id="r1", method="loop.tick", payload={"iteration": 1})
    d = req.to_dict()
    assert d["type"] == "server-request"
    assert d["method"] == "loop.tick"


def test_rpc_receipt():
    assert RpcReceipt(accepted=True).to_dict() == {"accepted": True}


# ── 校验 ─────────────────────────────────────────────────────

def test_validate_client_request_from_dict():
    msg = validate_message({
        "type": "client-request",
        "rpcId": "r9",
        "method": "chat.send",
        "payload": {"text": "hi"},
    })
    assert isinstance(msg, ClientRequest)
    assert msg.method == "chat.send"


def test_validate_unknown_type_rejected():
    with pytest.raises(ValueError):
        validate_message({"type": "nope", "rpcId": "r1"})


def test_validate_missing_field_rejected():
    with pytest.raises(ValueError):
        validate_message({"type": "client-request", "rpcId": "r1"})


# ── Hub 集成（配合 registry 跑真实 Agent）─────────────────────

def _build_hub():
    from aurora.llm.fake import FakeLLM, ScriptedScenario
    from aurora.llm.gateway import ModelGateway, LLMConfig
    from aurora.runtime.registry import ToolRegistry
    from aurora.runtime.session import Session
    from aurora.web.hub import Hub

    from aurora.tools import register_all_tools

    registry = ToolRegistry()
    register_all_tools(registry)
    fake = FakeLLM(scenario=ScriptedScenario(steps=[
        ("tool_calls", "用工具算一下", "",
         [{"name": "calculator", "arguments": {"expression": "2*3"}}]),
        ("answer", None, "结果是 6", None),
    ]))
    hub = Hub(registry=registry)
    hub.set_gateway(ModelGateway(LLMConfig(mock=True), backend=fake))
    return hub, registry, fake


def test_hub_session_lifecycle():
    hub, _, _ = _build_hub()
    asyncio.run(hub.start())
    try:
        s = asyncio.run(hub.call("session.create", {"name": "窗口A"}))
        assert "session_id" in s
        s2 = asyncio.run(hub.call("session.list", {}))
        assert len(s2["sessions"]) == 1
        asyncio.run(hub.call("session.remove", {"session_id": s["session_id"]}))
        s3 = asyncio.run(hub.call("session.list", {}))
        assert len(s3["sessions"]) == 0
    finally:
        asyncio.run(hub.stop())


def test_hub_chat_runs_agent():
    hub, _, _ = _build_hub()
    asyncio.run(hub.start())
    try:
        s = asyncio.run(hub.call("session.create", {"name": "T"}))
        resp = asyncio.run(hub.call("chat.send", {
            "session_id": s["session_id"], "text": "2*3 等于几",
        }))
        assert "6" in resp["response"]
    finally:
        asyncio.run(hub.stop())


def test_hub_trace_queryable():
    hub, _, _ = _build_hub()
    asyncio.run(hub.start())
    try:
        s = asyncio.run(hub.call("session.create", {"name": "Tr"}))
        asyncio.run(hub.call("chat.send", {"session_id": s["session_id"],
                                           "text": "算一下"}))
        rows = asyncio.run(hub.call("trace.session", {"session_id": s["session_id"]}))
        assert rows["records"] and len(rows["records"]) >= 2
        assert any(r["kind"] == "tool_result" for r in rows["records"])
    finally:
        asyncio.run(hub.stop())


def test_hub_unknown_method_raises():
    hub, _, _ = _build_hub()
    with pytest.raises(KeyError):
        asyncio.run(hub.call("no.such.method", {}))


def test_hub_event_listener_via_loop():
    """loop 事件应能被订阅者看到（四象限第三象限的 ServerRequest 通道）。"""
    hub, _, _ = _build_hub()
    received: list[str] = []
    hub.on_event(lambda kind: received.append(kind))
    asyncio.run(hub.start())
    try:
        s = asyncio.run(hub.call("session.create", {"name": "Ev"}))
        asyncio.run(hub.call("chat.send", {"session_id": s["session_id"], "text": "hi"}))
        assert "iteration" in received
        assert "tool_call" in received
    finally:
        asyncio.run(hub.stop())


def test_llm_status_mock_mode():
    hub, _, _ = _build_hub()
    status = asyncio.run(hub.call("llm.status", {}))
    assert status["configured"] is True
    assert status["mode"] == "mock"


def test_llm_use_mock_switches_back_to_mock():
    """llm.use_mock 在真实模型配额耗尽后，能把网关切回演示模式。"""
    from aurora.llm.fake import FakeLLM
    from aurora.llm.gateway import LLMConfig, ModelGateway

    hub, _, _ = _build_hub()
    # 先装一个"真实"网关（假的真实客户端，仅用于确认切换）
    hub.set_gateway(ModelGateway(
        LLMConfig(mock=False, api_base="https://x/v1", api_key="k", model="m"),
        backend=FakeLLM(),
    ))
    assert asyncio.run(hub.call("llm.status", {}))["mode"] == "real"

    status = asyncio.run(hub.call("llm.use_mock", {}))
    assert status["mode"] == "mock"
    assert status["model"] == "demo-llm"
    # 切回后 chat 仍可用
    s = asyncio.run(hub.call("session.create", {"name": "t"}))
    res = asyncio.run(hub.call("chat.send",
                               {"session_id": s["session_id"], "text": "hi"}))
    assert res["success"] is True



def test_llm_configure_requires_key():
    hub, _, _ = _build_hub()
    with pytest.raises(ValueError):
        asyncio.run(hub.call("llm.configure", {"api_key": "", "model": "x"}))


def test_llm_configure_switches_to_real():
    """用 MockTransport 的真实客户端替换 mock 网关，验证动态切换。"""
    import httpx

    from aurora.runtime.registry import ToolRegistry

    registry = ToolRegistry()
    hub = Hub(registry=registry)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "已连接"}}],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    # 给 Hub 一个可注入 http_client 的入口 —— 手工构造 gateway
    from aurora.llm.clients import OpenAICompatibleClient
    from aurora.llm.gateway import LLMConfig, ModelGateway

    cfg = LLMConfig(api_base="https://api.deepseek.com/v1", api_key="sk-abcdefgh12345678",
                    model="deepseek-chat", provider="deepseek")
    hub.gateway = ModelGateway(cfg, backend=OpenAICompatibleClient(cfg, http_client=client))

    status = asyncio.run(hub.call("llm.status", {}))
    assert status["mode"] == "real"
    assert status["model"] == "deepseek-chat"
    assert "sk-" in status["api_key_masked"]
    assert "abcdefgh" not in status["api_key_masked"]  # 不回显完整 key


def test_llm_test_mock_mode():
    hub, _, _ = _build_hub()
    r = asyncio.run(hub.call("llm.test", {}))
    assert r["ok"] is True
    assert r["mode"] == "mock"


def test_llm_status_unconfigured():
    from aurora.runtime.registry import ToolRegistry

    hub = Hub(ToolRegistry())
    status = asyncio.run(hub.call("llm.status", {}))
    assert status["configured"] is False


# ── LLM 配置：key 沿用与 base 规范化 ─────────────────────────
# 背景：面板保存成功后旧实现会清空 key 输入框，导致用户二次保存时
# 因 key 为空被拒，表现为「保存不了」。修复语义：key 留空 = 沿用已保存的 key。

def _hub_with_real_gateway():
    """构造一个**已保存过真实 key** 的 Hub，模拟用户第二次打开设置面板。"""
    from aurora.llm.clients import OpenAICompatibleClient
    from aurora.llm.gateway import LLMConfig, ModelGateway
    from aurora.runtime.registry import ToolRegistry

    hub = Hub(registry=ToolRegistry())
    cfg = LLMConfig(api_base="https://api.deepseek.com/v1",
                    api_key="sk-savedkey12345678",
                    model="deepseek-chat", provider="deepseek")
    hub.gateway = ModelGateway(cfg, backend=OpenAICompatibleClient(cfg))
    return hub


def test_llm_configure_keeps_saved_key_when_blank():
    """api_key 留空 → 沿用已保存的 key，而不是报错。"""
    hub = _hub_with_real_gateway()
    status = asyncio.run(hub.call("llm.configure", {
        "provider": "deepseek",
        "api_base": "https://api.deepseek.com",
        "model": "deepseek-chat",
    }))
    assert status["mode"] == "real"
    assert hub.gateway.config.api_key == "sk-savedkey12345678"   # key 未被覆盖
    assert status["api_key_masked"] == "sk-s…5678"


def test_llm_configure_can_change_model_without_key():
    """只改模型名、不动 key —— 旧实现在此报 "api_key 必填"。"""
    hub = _hub_with_real_gateway()
    status = asyncio.run(hub.call("llm.configure", {"model": "deepseek-reasoner"}))
    assert status["model"] == "deepseek-reasoner"
    assert hub.gateway.config.api_key == "sk-savedkey12345678"


def test_llm_configure_requires_key_on_first_setup():
    """从未保存过真实 key（仍是 mock 网关）时，留空应明确报错。"""
    hub, _, _ = _build_hub()
    with pytest.raises(ValueError):
        asyncio.run(hub.call("llm.configure", {"api_key": "", "model": "x"}))


def test_llm_configure_requires_model():
    hub = _hub_with_real_gateway()
    with pytest.raises(ValueError):
        asyncio.run(hub.call("llm.configure", {"api_key": "sk-x", "model": ""}))


def test_normalize_base_appends_v1_only_for_bare_host():
    """仅「协议+域名」补 /v1；已含路径（/v1、/beta）不干预。"""
    n = Hub._normalize_base
    assert n("https://api.deepseek.com") == "https://api.deepseek.com/v1"
    assert n("https://api.deepseek.com/") == "https://api.deepseek.com/v1"
    assert n("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"
    assert n("https://api.deepseek.com/beta") == "https://api.deepseek.com/beta"
    assert n("http://127.0.0.1:8000") == "http://127.0.0.1:8000/v1"
    assert n("") == ""