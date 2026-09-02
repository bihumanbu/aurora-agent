"""FastAPI server 集成测试（HTTP 上行 + 静态托管）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from aurora.llm.fake import FakeLLM, ScriptedScenario
from aurora.llm.gateway import LLMConfig, ModelGateway
from aurora.runtime.registry import ToolRegistry
from aurora.tools import register_all_tools
from aurora.web.hub import Hub
from aurora.web.server import build_app


def _setup(tmp_path=None) -> tuple[TestClient, Hub]:
    registry = ToolRegistry()
    register_all_tools(registry)
    hub = Hub(registry=registry)
    fake = FakeLLM(scenario=ScriptedScenario(steps=[
        ("tool_calls", "计算", "", [{"name": "calculator", "arguments": {"expression": "3*3"}}]),
        ("answer", None, "答案是 9", None),
    ]))
    hub.set_gateway(ModelGateway(LLMConfig(mock=True), backend=fake))
    app = build_app(hub, ui_dir=_tmp_ui())
    client = TestClient(app)
    return client, hub


def _tmp_ui():
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp(prefix="aurora_ui_"))
    (d / "index.html").write_text("<html>aurora</html>", encoding="utf-8")
    return d


def test_api_session_create_list_remove():
    client, _ = _setup()
    r = client.post("/api/session.create", json={"rpcId": "r1", "payload": {"name": "W"}})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "server-response"
    assert body["rpcId"] == "r1"
    assert body["result"]["ok"] is True
    sid = body["result"]["value"]["session_id"]

    r2 = client.post("/api/session.list", json={"rpcId": "r2", "payload": {}})
    assert r2.json()["result"]["value"]["sessions"][0]["session_id"] == sid


def test_api_unknown_method_404():
    client, _ = _setup()
    r = client.post("/api/no.such", json={"rpcId": "r9", "payload": {}})
    assert r.status_code == 404
    assert r.json()["result"]["ok"] is False


def test_api_chat_runs_full_agent():
    client, _ = _setup()
    sid = client.post("/api/session.create", json={"rpcId": "a", "payload": {"name": "A"}}) \
        .json()["result"]["value"]["session_id"]
    r = client.post("/api/chat.send",
                    json={"rpcId": "b", "payload": {"session_id": sid, "text": "3*3?"}})
    assert r.status_code == 200
    body = r.json()["result"]["value"]
    assert "9" in body["response"]


def test_api_trace_returns_records():
    client, _ = _setup()
    sid = client.post("/api/session.create", json={"rpcId": "a", "payload": {"name": "T"}}) \
        .json()["result"]["value"]["session_id"]
    client.post("/api/chat.send", json={"rpcId": "b", "payload": {"session_id": sid, "text": "hi"}})
    r = client.post("/api/trace.session", json={"rpcId": "c", "payload": {"session_id": sid}})
    records = r.json()["result"]["value"]["records"]
    assert len(records) >= 2
    assert any(rec["kind"] == "tool_result" for rec in records)


def test_api_bad_payload_400():
    client, _ = _setup()
    r = client.post("/api/session.create", json="not-a-dict")  # FastAPI 422
    assert r.status_code in (400, 422)


def test_frontend_served():
    client, _ = _setup()
    r = client.get("/")
    assert r.status_code == 200
    assert "aurora" in r.text