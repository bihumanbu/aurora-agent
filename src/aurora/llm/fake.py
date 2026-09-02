"""FakeLLM — 演示/测试用脚本化 LLM。

无真实 API key 时以 --mock 模式驱动整个 Agent（笔试要求"真实 LLM API"，
但无 key 的现场演示、录屏、离线测试都需要可复现的 LLM 行为）。

ScriptedScenario 定义一笔回复序列，步骤为四元组：
    (kind, reasoning, content, tool_calls)
    例: ("tool_calls", "需要查天气", "", [{"name":"weather","arguments":{"city":"北京"}}])
        ("answer", None, "北京今天晴天", None)

序列耗尽后再调用会抛 LLMError（用于测试探测 Loop 是否过早结束）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aurora.exceptions import LLMError
from aurora.llm.parsing import ParsedOutput
from aurora.runtime.messages import ToolCall, new_id


@dataclass
class ScriptedScenario:
    steps: list[tuple] = field(default_factory=list)


class FakeLLM:
    """按脚本回复的假 LLM，同时记录每次调用的请求（可断言）。

    exhausted_reply 非空时，脚本耗尽后返回该固定回答（演示友好）；
    为 None 时耗尽抛 LLMError（测试探测 Loop 是否过早结束）。
    """

    def __init__(self, scenario: ScriptedScenario | None = None,
                 exhausted_reply: str = "") -> None:
        self.scenario = scenario or ScriptedScenario()
        self._idx = 0
        self.calls: list[dict[str, Any]] = []
        self.exhausted_reply = exhausted_reply

    def config(self):
        from aurora.llm.gateway import LLMConfig

        return LLMConfig(mock=True, model="fake-llm")

    async def complete(self, messages: list[dict], tools: list[dict]) -> ParsedOutput:
        self.calls.append({"messages": messages, "tools": tools})
        if self._idx >= len(self.scenario.steps):
            if self.exhausted_reply:
                return ParsedOutput(kind="answer", content=self.exhausted_reply)
            raise LLMError("FakeLLM 回复序列已耗尽（Loop 请求了过多轮）")
        kind, reasoning, content, tool_calls = self.scenario.steps[self._idx]
        self._idx += 1
        if kind == "answer":
            return ParsedOutput(kind="answer", content=content or "", reasoning=reasoning or "")
        # tool_calls
        parsed_calls = []
        for tc in tool_calls or []:
            parsed_calls.append(ToolCall(
                id=str(tc.get("id") or new_id("tc_")),
                name=str(tc.get("name") or ""),
                arguments=dict(tc.get("arguments") or {}),
                raw_arguments=str(tc.get("arguments") or {}),
            ))
        return ParsedOutput(kind="tool_calls", content=content or "",
                            reasoning=reasoning or "", tool_calls=parsed_calls)


def build_demo_gateway() -> "ModelGateway":
    """构造演示用 mock 网关（脚本化 FakeLLM）。

    run.py（--mock 启动）与 llm.use_mock（运行期一键切回）共用，
    保证演示剧本只定义一处。
    """
    from aurora.llm.gateway import LLMConfig, ModelGateway

    demo = ScriptedScenario(steps=[
        ("tool_calls", "用户可能想查询信息，我先调用工具", "",
         [{"name": "calculator", "arguments": {"expression": "3*3"}}]),
        ("answer", "工具结果已返回", "我计算了一下：3 × 3 = 9。需要我查天气或记待办吗？", None),
        ("tool_calls", "用户要求查天气，我调用天气工具", "",
         [{"name": "weather", "arguments": {"city": "北京"}}]),
        ("answer", "天气结果已返回", "北京今天 24°C，晴，湿度 38%。需要我帮你记一条待办吗？", None),
        ("tool_calls", "用户要求记录待办，我调用 todo_add", "",
         [{"name": "todo_add", "arguments": {"text": "写周报"}}]),
        ("answer", "待办已记录", "已记下待办：写周报 ✅ 还有什么可以帮你？", None),
    ])
    cfg = LLMConfig(mock=True, model="demo-llm")
    return ModelGateway(cfg, backend=FakeLLM(
        scenario=demo,
        exhausted_reply="（演示剧本结束）还有其他问题吗？",
    ))