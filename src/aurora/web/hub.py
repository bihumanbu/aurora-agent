"""Hub — 四象限 RPC 的组装与调度中心。

职责：
    - 注册 API methods（session.create / chat.send / trace.session …）
    - 调度 unary 调用（第一象限 ClientRequest → 第二象限 ServerResponse）
    - 广播下行事件（第三象限 ServerRequest）：loop 每次 tick、session 变更、
      trace 增量 —— 推给所有订阅的 WebSocket 连接
    - 持有 ToolRegistry / SessionManager / ModelGateway，把 Agent 运行时接入 Web

与传输层（FastAPI WebSocket / HTTP）解耦：Hub 只提供同步/异步的 call 方法与
on_event 订阅；server.py 负责把 HTTP / WS 帧翻译成 Hub 调用。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from aurora.llm.gateway import Backend
from aurora.runtime.loop import AgentLoop, LoopResult, LoopSettings
from aurora.runtime.messages import AgentEvent, new_id
from aurora.runtime.registry import ToolRegistry
from aurora.runtime.session import Session, SessionManager
from aurora.runtime.trace import TraceStore

MethodHandler = Callable[[dict[str, Any]], Any]

_EVENT_KINDS = ("iteration", "thinking", "tool_call", "tool_result",
                "answer", "error", "done")


class Hub:
    """API 调度中心。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.sessions = SessionManager()
        # 每个 session 一个 TraceStore；追加到 session.trace
        self._methods: dict[str, MethodHandler] = {}
        self.gateway: Backend | None = None
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._started = False

        # 注册内置 methods
        self._register_defaults()

    # ── 网关 ──────────────────────────────────────────────────

    def set_gateway(self, gateway: Backend) -> None:
        self.gateway = gateway

    # ── methods ───────────────────────────────────────────────

    def register(self, method: str, handler: MethodHandler) -> None:
        self._methods[method] = handler

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        handler = self._methods.get(method)
        if handler is None:
            raise KeyError(f"未知 API 方法: {method}")
        result = handler(payload)
        if hasattr(result, "__await__"):
            result = await result
        return result

    def _register_defaults(self) -> None:
        self.register("session.create", self._session_create)
        self.register("session.list", self._session_list)
        self.register("session.get", self._session_get)
        self.register("session.remove", self._session_remove)
        self.register("chat.send", self._chat_send)
        self.register("trace.session", self._trace_session)
        self.register("tools.list", self._tools_list)
        self.register("llm.status", self._llm_status)
        self.register("llm.configure", self._llm_configure)
        self.register("llm.test", self._llm_test)
        self.register("llm.use_mock", self._llm_use_mock)

    # ── 事件广播（第三象限 ServerRequest 的前瞻）─────────────────

    def on_event(self, listener: Callable[..., None]) -> None:
        """订阅下行事件：listener(kind, payload) 或 listener(kind) 均可。"""
        self._listeners.append(listener)

    def _broadcast(self, kind: str, payload: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind, payload)
            except TypeError:
                # 兼容只接收一个参数的监听器
                try:
                    listener(kind)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 — 单个订阅者失败不影响其他
                pass

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False
        for s in self.sessions.sessions():
            s.trace.clear()
        self.sessions.clear()

    # ── API handlers ──────────────────────────────────────────

    def _session_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = payload.get("name") or "窗口"
        s = self.sessions.create(name)
        self._broadcast("session.created", {"session_id": s.session_id, "name": s.name})
        return {"session_id": s.session_id, "name": s.name}

    def _session_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"sessions": [s.to_dict() for s in self.sessions.sessions()]}

    def _session_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        s = self.sessions.get(payload["session_id"])
        return s.to_dict()

    def _session_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        sid = payload["session_id"]
        s = self.sessions.remove(sid)
        self._broadcast("session.removed", {"session_id": sid})
        return {"removed": s}

    async def _chat_send(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = payload["session_id"]
        text = payload["text"]
        s: Session = self.sessions.get(session_id)
        if self.gateway is None:
            return {"error": "LLM 网关未配置", "response": ""}

        loop = AgentLoop(
            model=self.gateway,
            registry=self.registry,
            settings=LoopSettings(max_iters=8),
            on_event=lambda ev: self._on_loop_event(ev, s),
        )
        result: LoopResult = await loop.run(s, text)
        return {
            "response": result.answer,
            "success": result.success,
            "iterations": result.iterations,
            "tool_calls": result.tool_calls,
            "error": result.error,
        }

    def _on_loop_event(self, ev: AgentEvent, session: Session) -> None:
        """把 loop 事件转换为第三象限 ServerRequest 推送，并同步进 trace。"""
        self._broadcast(ev.kind.value, {
            "session_id": ev.session_id,
            "payload": ev.payload,
            "seq": ev.seq,
        })
        # 事件同时落入 trace（供 Trace 面板回放）
        session.trace.record(kind=ev.kind.value, session_id=ev.session_id,
                             payload=ev.payload)

    def _trace_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        sid = payload["session_id"]
        s = self.sessions.get(sid)
        return {"records": [r.to_dict() for r in s.trace.by_session(sid)]}

    def _tools_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"tools": self.registry.spec()}

    # ── LLM 配置（真实模型动态切换）──────────────────────────────

    def _llm_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """返回当前网关状态（脱敏：不回显 key，只给掩码与是否已配置）。"""
        gw = self.gateway
        if gw is None:
            return {"configured": False}
        cfg = getattr(gw, "config", None)
        if cfg is None:
            return {"configured": True, "mode": "real", "provider": "unknown"}
        key = cfg.api_key
        masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else "已设置"
        return {
            "configured": True,
            "mode": "mock" if cfg.mock else "real",
            "provider": cfg.provider,
            "model": cfg.model,
            "api_base": cfg.effective_base() or "",
            "api_key_masked": masked if not cfg.mock else "",
            "temperature": cfg.temperature,
        }

    @staticmethod
    def _normalize_base(base: str) -> str:
        """规范 base URL：仅当「只有协议+域名、不含任何路径」时补 /v1。

        例：https://api.deepseek.com      -> https://api.deepseek.com/v1
            https://api.deepseek.com/v1  -> 原样
            https://api.deepseek.com/beta-> 原样（尊重自定义端点）
        """
        b = (base or "").strip().rstrip("/")
        if not b:
            return ""
        rest = b.split("://", 1)[-1]           # 去掉协议
        if "/" in rest:                        # 已含路径（/v1、/beta …）→ 不干预
            return b
        return b + "/v1"

    def _llm_configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        """用面板填写的参数动态构建真实 LLM 网关（OpenAI 兼容多厂商）。

        api_key 留空表示「沿用上一次已保存的 key」——避免用户仅修改
        base/model 时被迫重新粘贴密钥（也是"无法保存"的根因修复）。
        """
        from aurora.llm.clients import OpenAICompatibleClient, AnthropicCompatibleClient
        from aurora.llm.gateway import LLMConfig, ModelGateway

        model = str(payload.get("model") or "").strip()
        if not model:
            raise ValueError("模型名必填")

        key = str(payload.get("api_key") or "").strip()
        if not key:
            old_cfg = getattr(self.gateway, "config", None)
            saved = getattr(old_cfg, "api_key", "")
            if old_cfg is not None and not old_cfg.mock and saved:
                key = saved                    # 沿用已保存的 key
            else:
                raise ValueError("首次配置必须填写 API Key")

        cfg = LLMConfig(
            mock=False,
            api_base=self._normalize_base(str(payload.get("api_base") or "")),
            api_key=key,
            model=model,
            provider=str(payload.get("provider") or "deepseek"),
            temperature=float(payload.get("temperature") or 0.3),
        )
        if not cfg.effective_base():
            raise ValueError("api_base 必填（如 https://api.deepseek.com/v1 或 "
                             "https://api.deepseek.com/anthropic）")
        client = AnthropicCompatibleClient(cfg) if cfg.provider == "anthropic" \
            else OpenAICompatibleClient(cfg)
        # 旧网关如为 FakeLLM（mock）直接丢弃；真实网关替换
        self.gateway = ModelGateway(cfg, backend=client)
        self._broadcast("llm.configured", {"model": cfg.model, "provider": cfg.provider})
        return self._llm_status({})

    async def _llm_use_mock(self, payload: dict[str, Any]) -> dict[str, Any]:
        """一键切回内置 FakeLLM 演示模式。

        真实模型配额耗尽（quota_exceeded）/ 鉴权失败 / 无 key 时，
        让 UI 仍可继续体验四象限 Loop 可视化，不必卡死在错误页。
        """
        from aurora.llm.fake import build_demo_gateway

        self.gateway = build_demo_gateway()
        self._broadcast("llm.configured", {"model": "demo-llm", "provider": "mock"})
        return self._llm_status({})

    async def _llm_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发送最小请求测试当前网关连通性。"""
        gw = self.gateway
        if gw is None:
            raise ValueError("LLM 网关未配置")
        cfg = getattr(gw, "config", None)
        if cfg is not None and cfg.mock:
            return {"ok": True, "mode": "mock", "latency_ms": 0,
                    "message": "当前为 mock 演示模式，无需测试"}
        import time

        start = time.time()
        try:
            out = await gw.complete(
                [{"role": "user", "content": "ping"}],
                [],
            )
            latency = int((time.time() - start) * 1000)
            return {"ok": True, "latency_ms": latency,
                    "mode": "real",
                    "message": out.content[:80] or "（空回复）"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "latency_ms": int((time.time() - start) * 1000),
                    "error": str(e), "mode": "real"}


def create_hub(*, registry: ToolRegistry, gateway: Backend | None = None) -> Hub:
    """便捷构造（run.py / 测试共用）。"""
    hub = Hub(registry=registry)
    if gateway is not None:
        hub.set_gateway(gateway)
    return hub