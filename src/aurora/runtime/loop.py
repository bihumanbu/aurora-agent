"""Agent Loop — 核心：四步循环。

Step one 接收用户输入
Step two 判断是直接回复，还是调用工具（LLM 带工具 Schema 决策）
Step three 调用工具
Step four 根据工具结果判断是继续 loop，还是返回结果给用户

设计要点：
    1. 与 LLM 解耦：依赖 Protocol 风格的"model.complete(messages, tools) → ParsedOutput"。
    2. 事件流：每次迭代产生 AgentEvent（iteration/thinking/tool_call/tool_result/
       answer/error/done），由外部 handler（Web 推送 / trace 记录 / 测试断言）消费。
    3. 工具异常：调用工具抛 ToolError 时捕获，生成 tool_result(ok=False) 事件并回注
       context，让 LLM 看到错误后可自己决定继续还是收尾（不中断整个 loop）。
    4. max_iters 双保险：LoopSettings.max_iters 与 Context.max_turns 分别限制
       procedure 与 memory。
    5. trace 集成：每次工具调用/结果自动写入 Session.trace，供 Web Trace 面板回放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from aurora.exceptions import AuroraError
from aurora.llm.parsing import ParsedOutput
from aurora.runtime.messages import AgentEvent, EventKind, Message, Role, ToolCall, new_id
from aurora.runtime.registry import ToolRegistry
from aurora.runtime.session import Session


class Model(Protocol):
    """LLM 抽象。complete 返回已解析的输出（含工具调用/答案）。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ParsedOutput: ...


EventHandler = Callable[[AgentEvent], Awaitable[None] | None]


@dataclass
class LoopSettings:
    max_iters: int = 5
    verbose: bool = True
    system_prompt: str = (
        "你是一个极光 Agent。你可以直接回答用户，也可以选择调用工具来获取信息或执行操作。"
        "如需调用工具，给出精确的参数。工具执行结果会回传给你，你可以据此继续。"
    )


@dataclass
class LoopResult:
    success: bool
    answer: str = ""
    error: str = ""
    iterations: int = 0
    tool_calls: int = 0


class AgentLoop:
    """最小可用的 Agent 主循环（自研）。"""

    def __init__(
        self,
        *,
        model: Model,
        registry: ToolRegistry,
        settings: LoopSettings | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        self.model = model
        self.registry = registry
        self.settings = settings or LoopSettings()
        self._on_event = on_event
        self.events: list[AgentEvent] = []
        self._seq = 0

    # ── 事件 ──────────────────────────────────────────────────

    async def _emit(self, kind: EventKind, session_id: str, payload: dict[str, Any]) -> AgentEvent:
        """发出一个事件并让出事件循环，确保下行帧实时推送到浏览器。

        await asyncio.sleep(0) 让 WS pump 协程有机会把 queue 里的帧真正发出去，
        而不是堆积到整个 loop 结束后批量推送——这是 Loop 可视化"动起来"的关键。
        """
        import asyncio

        self._seq += 1
        ev = AgentEvent(kind=kind, session_id=session_id, payload=payload, seq=self._seq)
        self.events.append(ev)
        if self._on_event is not None:
            try:
                result = self._on_event(ev)
                if hasattr(result, "__await__"):
                    await result
            except Exception:  # noqa: BLE001 — 事件广播失败不影响主循环
                pass
        # 让出事件循环：WS pump 协程此时把帧真正发给浏览器
        await asyncio.sleep(0)
        return ev

    # ── 主循环 ────────────────────────────────────────────────

    async def run(self, session: Session, user_input: str) -> LoopResult:
        if not user_input or not user_input.strip():
            raise ValueError("用户输入不能为空")
        session_id = session.session_id
        # Step one: 接收用户输入 → 写入 context（附 trace）
        session.context.append(Message(role=Role.USER, content=user_input))
        # 基础压缩：本回合 LLM 决策前，先把超出阈值的旧历史浓缩为摘要
        # （context 过长要有基础的压缩）。放在回合开始、工具调用之前，
        # 可避免误删正在进行中的 tool_use/tool_result 配对。
        session.context.compact()
        session.trace.record(kind="user", session_id=session_id, payload={"content": user_input})

        iter_count = 0
        tool_count = 0
        last_answer = ""

        while iter_count < self.settings.max_iters:
            iter_count += 1
            await self._emit(EventKind.ITERATION, session_id,
                       {"iteration": iter_count, "max_iters": self.settings.max_iters})

            # Step two: LLM 决策（直接回复 or 调工具）
            messages = [m.to_api_dict() for m in session.context.build_messages()]
            tools = self.registry.spec()

            output: ParsedOutput
            try:
                output = await self.model.complete(messages, tools)
            except AuroraError as e:
                await self._emit(EventKind.ERROR, session_id, {"error": str(e), "code": e.code})
                session.trace.record(kind="error", session_id=session_id, payload=e.to_dict())
                return LoopResult(success=False, error=str(e), iterations=iter_count,
                                  tool_calls=tool_count)

            # 思考过程 → 事件 + context
            if output.reasoning:
                await self._emit(EventKind.THINKING, session_id, {"text": output.reasoning})
                session.context.append(Message(role=Role.ASSISTANT, content="",
                                               reasoning=output.reasoning))

            if output.kind == "answer":
                # 直接回复用户 → Step four: 返回
                await self._emit(EventKind.ANSWER, session_id, {"text": output.content})
                session.context.append(Message(role=Role.ASSISTANT, content=output.content))
                session.trace.record(kind="answer", session_id=session_id,
                                     payload={"content": output.content})
                await self._emit(EventKind.DONE, session_id, {"success": True})
                return LoopResult(success=True, answer=output.content, iterations=iter_count,
                                  tool_calls=tool_count)

            # Step three: 调用工具
            # 关键修复：把"带 tool_calls 的 assistant 消息"也存入 context。
            # 否则下游客户端（OpenAI / Anthropic）无法把 tool_use 与 tool_result
            # 配对，Anthropic 会报 400: tool_use ids were found without tool_result
            # blocks immediately after。修复后 tool_result 由其 tool_call_id 归属到
            # 正确的 assistant，多轮对话也不会错位。
            session.context.append(Message(
                role=Role.ASSISTANT,
                content=output.content or "",
                tool_calls=[{"id": c.id, "name": c.name, "arguments": c.arguments}
                            for c in output.tool_calls],
            ))
            for call in output.tool_calls:
                tool_count += 1
                await self._emit(EventKind.TOOL_CALL, session_id, {
                    "tool": call.name,
                    "arguments": call.arguments,
                    "call_id": call.id,
                })
                ok = False
                result_payload: dict[str, Any] = {}
                try:
                    result_payload = await self.registry.execute(call.name, call.arguments)
                    ok = True
                except AuroraError as e:
                    result_payload = {"error": e.to_dict()}
                    # 工具异常：记录 trace，继续 loop（容错）
                    session.trace.record(kind="tool_error", session_id=session_id,
                                         payload={"tool": call.name, "error": e.to_dict()})
                except Exception as e:  # noqa: BLE001 — 不可预期异常也入 trace 容错
                    result_payload = {"error": {"code": "unexpected", "message": str(e)}}
                    session.trace.record(kind="tool_error", session_id=session_id,
                                         payload={"tool": call.name, "error": result_payload["error"]})

                # 工具结果事件（含成功/失败标记）
                result_text = _stringify(result_payload)
                await self._emit(EventKind.TOOL_RESULT, session_id,
                           {"tool": call.name, "ok": ok, "result": result_payload,
                            "text": result_text})
                # 回注 context：LLM 基于工具结果决定下一步（Step four → 继续 loop）
                session.context.append(Message(role=Role.TOOL, content=result_text,
                                               name=call.name, tool_call_id=call.id))
                session.trace.record(kind="tool_result", session_id=session_id,
                                     payload={"tool": call.name, "ok": ok,
                                              "result": result_payload})

        # 迭代上限：Step four → 终止
        await self._emit(EventKind.DONE, session_id,
                   {"success": False, "reason": f"达到最大迭代次数 {self.settings.max_iters}"})
        return LoopResult(success=False,
                          error=f"达到最大迭代次数 {self.settings.max_iters}，请简化任务或换一种问法。",
                          iterations=iter_count, tool_calls=tool_count)


def _stringify(payload: dict[str, Any]) -> str:
    """工具结果 → 文本（给 LLM 的 tool 消息）。"""
    import json

    if "error" in payload:
        return f"[工具错误] {json.dumps(payload['error'], ensure_ascii=False)}"
    if "result" in payload and isinstance(payload["result"], (int, float)):
        return str(payload["result"])
    try:
        return json.dumps(payload, ensure_ascii=False)
    except TypeError:
        return str(payload)