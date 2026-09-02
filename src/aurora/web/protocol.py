"""四象限消息模型 — 借鉴 deepseek-harness 的 RPC 协议思想，Python 从零实现。

四个消息种类（"谁发起 × 请求/响应"）：

                 client 发起              server 发起
  request   ① ClientRequest          ③ ServerRequest
            (POST /api/<method>)     (WebSocket 下行：loop/session/trace 事件)
  response  ② ServerResponse         ④ ClientResponse
            (该 POST 的响体)          (POST /api/respond 回填 rpcId)

rpcId 纪律：
    - 谁发起谁铸 id（make_rpc_id），应答必须 echo 原 rpcId，从不新铸。
    - unary 通道（①→②）由 Hub.call 内部维护 pending 映射。

所有消息均为 dataclass + to_dict()，跨层（协议/传输/前端）共享同一形状。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


def make_rpc_id() -> str:
    return f"rpc_{uuid.uuid4().hex[:12]}"


# ── 第一象限：ClientRequest ──────────────────────────────────

@dataclass
class ClientRequest:
    method: str
    payload: dict[str, Any] = field(default_factory=dict)
    rpc_id: str = field(default_factory=make_rpc_id)

    @property
    def type(self) -> str:
        return "client-request"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "rpcId": self.rpc_id, "method": self.method,
                "payload": self.payload}


# ── 第二象限：ServerResponse ─────────────────────────────────
# 占位避免误判（ServerResponse 定义紧随其后）

@dataclass
class ServerResponse:
    rpc_id: str
    ok: bool
    value: Any = None
    error: dict[str, Any] | None = None

    @property
    def type(self) -> str:
        return "server-response"

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            result: dict[str, Any] = {"ok": True, "value": self.value}
        else:
            result = {"ok": False, "error": self.error or {"code": "unknown"}}
        return {"type": self.type, "rpcId": self.rpc_id, "result": result}


# ── 第三象限：ServerRequest（下行推送）────────────────────────

@dataclass
class ServerRequest:
    method: str
    payload: dict[str, Any] = field(default_factory=dict)
    rpc_id: str = field(default_factory=make_rpc_id)

    @property
    def type(self) -> str:
        return "server-request"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "rpcId": self.rpc_id, "method": self.method,
                "payload": self.payload}


# ── 第四象限：ClientResponse ─────────────────────────────────

@dataclass
class ClientResponse:
    rpc_id: str
    ok: bool
    value: Any = None
    error: dict[str, Any] | None = None

    @property
    def type(self) -> str:
        return "client-response"

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            result: dict[str, Any] = {"ok": True, "value": self.value}
        else:
            result = {"ok": False, "error": self.error or {"code": "unknown"}}
        return {"type": self.type, "rpcId": self.rpc_id, "result": result}


# ── 载体回执 ─────────────────────────────────────────────────

@dataclass
class RpcReceipt:
    accepted: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        if self.accepted:
            return {"accepted": True}
        return {"accepted": False, "reason": self.reason}


# ── 校验入口 ─────────────────────────────────────────────────

_KIND_CTOR = {
    "client-request": ClientRequest,
    "server-request": ServerRequest,
    "server-response": ServerResponse,
    "client-response": ClientResponse,
}


def validate_message(raw: dict[str, Any]):
    """把 wire dict 校验并还原为对应消息。字段不齐 / 类型未知抛 ValueError。"""
    kind = raw.get("type")
    ctor = _KIND_CTOR.get(kind)
    if ctor is None:
        raise ValueError(f"未知消息类型: {kind!r}")
    if ctor is ClientRequest or ctor is ServerRequest:
        method = raw.get("method")
        if not method:
            raise ValueError("缺少 method 字段")
        return ctor(method=method, payload=raw.get("payload") or {},
                    rpc_id=raw.get("rpcId") or make_rpc_id())
    if ctor is ServerResponse or ctor is ClientResponse:
        rpc_id = raw.get("rpcId")
        if not rpc_id:
            raise ValueError("缺少 rpcId 字段")
        result = raw.get("result") or {}
        ok = bool(result.get("ok"))
        return ctor(rpc_id=rpc_id, ok=ok, value=result.get("value"),
                    error=result.get("error"))
    raise ValueError(f"无法解析消息: {raw}")


def server_response_from(rpc_id: str, ok: bool, value: Any = None,
                         error: dict[str, Any] | None = None) -> ServerResponse:
    """便捷构造第二象限应答。"""
    return ServerResponse(rpc_id=rpc_id, ok=ok, value=value, error=error)