"""Context 管理 — "context的有效管理"。

实现：
    - 分桶模型：system | compacted_summary | rolling_history | tool_results
      再拼接成发给 LLM 的 messages。
    - 最大轮次限制 max_turns：只保留最近 max_turns 轮（user+assistant 为一轮）。
    - 基础压缩 compact：估算 token 数超阈值时，把最旧的非关键消息浓缩为一行摘要。
      注：复杂压缩不做复杂实现，此处做轻量摘要（截断+保留要点）。
    - 追问支持：rolling_history 保留最近 N 轮，工具结果始终回注，
      "纯对话追问"与"带工具追问"都可连续。

与 llm/gateway 的 token 估算解耦：这里用内置的粗略估算（汉字≈1 token，
ASCII≈0.25 token），不额外依赖分词器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from aurora.runtime.messages import Message, Role

_TOK_PER_CHAR = 0.25
_TOKEN_SAFE_MAX = 60_000


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（汉字按 1 token/字，其余按 0.25，向上取整保底）。"""
    cjk = len(re.findall(r"[一-鿿]", text))
    other = len(text) - cjk
    return max(1, round(cjk * 1.0 + other * _TOK_PER_CHAR))


def estimate_messages(messages: Sequence[Message]) -> int:
    return sum(estimate_tokens(m.content or "") + 16 for m in messages)


class BucketedContext:
    """分桶的对话上下文。"""

    def __init__(self, *, max_turns: int = 20, compact_threshold: int = 3000) -> None:
        self.max_turns = max_turns
        self.compact_threshold = compact_threshold
        # 存 Message 列表，维护"轮"计数
        self._messages: list[Message] = []
        self._system: Message | None = None
        self._summary: list[str] = []        # compacted_summary 桶
        self._tool_results: list[Message] = []  # tool_results 桶（只留最近）
        # 容量取 24：一次典型多轮演示（计算器+天气+待办增删改查+读文档+多窗口）
        # 约 10~15 次工具调用，留足余量后早期结果不会被挤出。
        # 挤出并不破坏协议（转换层会补 is_error 占位，不会 400），但模型会
        # 读到"工具结果缺失"，导致追问早期调用时答不上来，故适当放大。
        self._tool_results_cap = 24
        self._total_turns = 0

    @property
    def turns(self) -> int:
        return self._total_turns

    # ── 写入 ──────────────────────────────────────────────────

    def set_system(self, system: str) -> None:
        self._system = Message(role=Role.SYSTEM, content=system)

    def append(self, message: Message) -> None:
        if message.role is Role.SYSTEM:
            self._system = message
            return
        if message.role is Role.TOOL:
            self._tool_results.append(message)
            if len(self._tool_results) > self._tool_results_cap:
                self._tool_results = self._tool_results[-self._tool_results_cap:]
            return
        if message.role is Role.USER:
            self._total_turns += 1
        self._messages.append(message)
        self._trim_to_max_turns()

    def _trim_to_max_turns(self) -> None:
        """按 max_turns 淘汰最旧的完整"轮"。"""
        while self._count_turns(self._messages) > self.max_turns:
            if not self._messages:
                break
            # 弹出最早的一条消息（可能有多条属于同一轮，逐条弹直到余下 turns 达标）
            self._messages.pop(0)

    @staticmethod
    def _count_turns(messages: list[Message]) -> int:
        turn = 0
        for m in messages:
            if m.role is Role.USER:
                turn += 1
        return turn

    # ── 压缩 ──────────────────────────────────────────────────

    def compact(self, *, summarizer: Callable[[str], str] | None = None,
                force: bool = False) -> bool:
        """基础压缩：估算超出阈值时，把最旧的非关键消息替换为一行摘要。

        返回是否发生了压缩。
        """
        if not force and estimate_messages(self._messages) <= self.compact_threshold:
            return False
        # 保留最后一轮不压，其余交给 summarizer（缺省直接截断）
        text_parts: list[str] = []
        keep: list[Message] = []
        dropped_user = 0
        last_user_idx = self._last_user_index()
        for i, m in enumerate(self._messages):
            if m.role is Role.USER and i != last_user_idx and self._total_turns > 2:
                dropped_user += 1
                text_parts.append(f"用户: {m.content[:200]}")
                continue
            keep.append(m)
        if not text_parts:
            # 可压内容太少但超出阈值：直接裁掉最早的 assistant 消息
            while self._messages and estimate_messages(self._messages) > self.compact_threshold \
                    and len(self._messages) > 4:
                popped = self._messages.pop(0)
                if popped.role is Role.USER:
                    text_parts.append(f"用户: {popped.content[:200]}")
                else:
                    text_parts.append("(早前助手回复，已省略)")
        else:
            self._messages = keep
        if text_parts:
            joined = "\n".join(text_parts)
            if summarizer:
                try:
                    joined = summarizer(joined)
                except Exception:  # noqa: BLE001
                    joined = f"(早前对话摘要，共{dropped_user}个问题)"
            self._summary.append(joined)
        self._total_turns = self._count_turns(self._messages)
        return True

    def _last_user_index(self) -> int:
        for i in range(len(self._messages) - 1, -1, -1):
            if self._messages[i].role is Role.USER:
                return i
        return -1

    # ── 读取 ──────────────────────────────────────────────────

    def build_messages(self, *, include_tool_results: bool = True) -> list[Message]:
        """组装分桶消息：system → 摘要 → 滚动历史 → （按归属插入的工具结果）。

        工具结果默认追加在末尾（保持旧行为），但会按 tool_call_id 归位到
        「发起该调用的 assistant」之后，确保 OpenAI / Anthropic 协议下
        tool_use 与 tool_result 紧邻配对（Anthropic 强制要求）。
        """
        out: list[Message] = []
        if self._system is not None:
            out.append(self._system)
        for s in self._summary:
            out.append(Message(role=Role.USER, content=f"[早前对话摘要] {s}"))
        out.extend(self._messages)
        if include_tool_results and self._tool_results:
            # 建立 tool_call_id -> 拥有它的 assistant 消息（_messages 中同一对象引用）
            owner: dict[str, Message] = {}
            for m in out:
                if m.role is Role.ASSISTANT and m.tool_calls:
                    for tc in m.tool_calls:
                        cid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                        if cid:
                            owner[cid] = m
            pending: dict[int, list[Message]] = {}
            unmatched: list[Message] = []
            for t in self._tool_results:
                o = owner.get(t.tool_call_id)
                if o is not None:
                    pending.setdefault(id(o), []).append(t)
                else:
                    unmatched.append(t)
            if pending or unmatched:
                rebuilt: list[Message] = []
                for m in out:
                    rebuilt.append(m)
                    if id(m) in pending:
                        rebuilt.extend(pending[id(m)])
                rebuilt.extend(unmatched)
                out = rebuilt
        return out

    def estimate(self) -> int:
        return estimate_messages(self.build_messages())

    def clear(self) -> None:
        self._messages.clear()
        self._summary.clear()
        self._tool_results.clear()
        self._system = None
        self._total_turns = 0