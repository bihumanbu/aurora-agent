"""消息模型。

定义 Agent 循环中流转的消息与事件类型。不依赖任何外部框架。

消息角色参照 OpenAI Chat 协议的 role 语义，但结构自研：
    - system / user / assistant / tool
    - tool 消息携带 tool_call_id 与工具名，与 LLM 的 function-call 协议对齐。

事件 (AgentEvent) 是 Loop 向外部（Web / 测试）暴露的执行轨迹：
    thinking / tool_call / tool_result / answer / error / iteration / done
    事件流由 trace 层持久化，供 Web 的 Loop 可视化面板逐帧渲染。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class Message:
    """Agent 循环中的一条消息。"""

    role: Role
    content: str
    name: str | None = None          # tool 消息：工具名
    tool_call_id: str | None = None  # tool 消息：对应的工具调用 id
    reasoning: str | None = None     # assistant 消息：思考过程（若有）
    tool_calls: list[dict] | None = None  # assistant 消息：发起的工具调用（OpenAI 原生格式）

    def to_api_dict(self) -> dict[str, Any]:
        """转换为 OpenAI Chat 兼容格式的单个消息。"""
        if self.role is Role.TOOL:
            return {
                "role": "tool",
                "content": self.content,
                "name": self.name,
                "tool_call_id": self.tool_call_id,
            }
        d: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.role is Role.ASSISTANT:
            if self.reasoning:
                d["reasoning_content"] = self.reasoning
            if self.tool_calls:
                d["tool_calls"] = [
                    {
                        "id": tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", ""),
                        "type": "function",
                        "function": {
                            "name": (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")) or "",
                            "arguments": json.dumps(
                                (tc.get("arguments") if isinstance(tc, dict) else {}) or {},
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for tc in self.tool_calls
                ]
        return d


@dataclass(frozen=True)
class ToolCall:
    """LLM 发起的工具调用请求。"""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


class EventKind(str, Enum):
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ANSWER = "answer"
    ERROR = "error"
    ITERATION = "iteration"
    DONE = "done"


@dataclass
class AgentEvent:
    """Loop 迭代中产生的一个事件（由外部 handler 消费）。"""

    kind: EventKind
    session_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    seq: int = 0

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "session_id": self.session_id,
            "payload": self.payload,
            "ts": self.ts,
            "seq": self.seq,
        }


def new_id(prefix: str = "") -> str:
    """生成短 id（工具调用 / 会话 / trace 通用）。"""
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}{raw}" if prefix else raw