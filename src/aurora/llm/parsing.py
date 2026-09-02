"""LLM 输出解析 — 笔试核心：提取思考过程 / 工具调用 / 最终答案。

双路兜底：
    1. 原生 function-call 通道：LLM 返回结构化 tool_calls 字段（OpenAI 兼容，
       DeepSeek/通义/Kimi 均支持）→ 直接提取。
    2. 文本 + JSON 通道：无原生 tool_calls 的模型 → 检测 ```json 代码块 或
       Action: 行，正则 + JSON 解析提取；失败则视为"直接回复"。

思考过程提取：
    - 优先 LLM 返回的 reasoning_content 字段（deepseek-reasoner）
    - 其次 文本中的 <thinking>...</thinking> 标签
    - 都没有则整段视为内容

输出统一为 ParsedOutput：
    kind: "answer" | "tool_calls"
    content / reasoning / tool_calls
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from aurora.exceptions import ParseError
from aurora.runtime.messages import ToolCall, new_id

# --- 文本/JSON 通道的匹配模式 ---
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_ACTION_JSON_RE = re.compile(
    r"(?:^|\n)\s*(?:Action|tool_call|工具调用)\s*[:：]\s*(\{.*\})",
    re.DOTALL,
)
# 成对思考标签：<thinking>…</thinking> 或 [thinking]…[/thinking]
_PAIRED_THINK_RE = re.compile(
    r"(?:<(thought|thinking)>|\[(?:thought|thinking)\])(.*?)(?:</\1>|\[/?(?:thought|thinking)\])",
    re.DOTALL,
)
# 无闭合的退化标签（模型只输出开始标签）
_OPEN_THINK_RE = re.compile(
    r"<(?:thought|thinking)>([^<]{0,2000})|\[(?:thought|thinking)\]([^\[]{0,2000})",
    re.DOTALL,
)
_TOOL_SELECT_RE = re.compile(r'[`"\']?([a-z_][a-z0-9_]{1,30})[`"\']?\s*\(')


@dataclass
class ParsedOutput:
    kind: str = "answer"                     # "answer" | "tool_calls"
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def _extract_reasoning(text: str) -> tuple[list[str], str]:
    """从文本中剥离/提取思考过程。返回 (思考块, 剩余文本)。"""
    thinks: list[str] = []
    rest = text
    # 优先成对闭合标签；提取后从中删除
    paired = _PAIRED_THINK_RE.findall(rest)
    if paired:
        for _, body in paired:
            body = body.strip()
            if body:
                thinks.append(body)
        rest = _PAIRED_THINK_RE.sub("", rest)
    # 无闭合的退化情况：只取开始标签之后的部分作为思考，并删除
    opens = _OPEN_THINK_RE.search(rest)
    if not thinks and opens:
        body = (opens.group(1) or opens.group(2) or "").strip()
        if body:
            thinks.append(body)
        rest = rest[opens.end():]
    return thinks, rest.strip()


def parse_tool_calls_text(text: str) -> list[ToolCall]:
    """文本通道：从回复中提取工具调用列表。提取不到返回 []。"""
    calls: list[ToolCall] = []

    # 1) json 代码块：{name, arguments} 或 [{...}]
    json_blocks = [_m.group(1) for _m in _JSON_BLOCK_RE.finditer(text)]
    for block in json_blocks:
        parsed = _parse_block(block)
        if parsed:
            calls.extend(parsed)

    # 2) Action: {...} 行
    if not calls:
        for _m in _ACTION_JSON_RE.finditer(text):
            parsed = _parse_block(_m.group(1))
            if parsed:
                calls.extend(parsed)

    # 3) `tool(args)` 风格兜底（例如 `web_search("北京")`）
    if not calls:
        args_extract = re.search(
            r"(?:```(?:json)?\s*)?\{(?P<name>[a-z_][a-z0-9_]{1,30})\s*,\s*(?P<args>\{.*?\})",
            text, re.DOTALL,
        )
        if args_extract:
            try:
                args = json.loads(args_extract.group("args"))
                calls.append(ToolCall(id=new_id("tc_"),
                                      name=args_extract.group("name"),
                                      raw_arguments=args_extract.group("args"),
                                      arguments=args))
            except json.JSONDecodeError:
                pass
    return calls


def _parse_block(block: str) -> list[ToolCall]:
    try:
        data = json.loads(block)
    except json.JSONDecodeError:
        return []
    calls: list[ToolCall] = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("tool") or "").strip()
        if not name:
            continue
        args = item.get("arguments") or item.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append(ToolCall(id=new_id("tc_"), name=name,
                              raw_arguments=json.dumps(item.get("arguments") or {}, ensure_ascii=False),
                              arguments=args))
    return calls


def parse_text(text: str) -> ParsedOutput:
    """解析纯文本回复（无原生 function-call）。"""
    if not text or not text.strip():
        raise ParseError("LLM 返回空内容")
    thinks, rest = _extract_reasoning(text)
    calls = parse_tool_calls_text(rest)
    if calls:
        return ParsedOutput(kind="tool_calls", content=rest.strip(),
                            reasoning="\n".join(thinks), tool_calls=calls)
    return ParsedOutput(kind="answer", content=rest.strip()
                        if rest.strip() else text.strip(),
                        reasoning="\n".join(thinks))


def parse_native(message: dict[str, Any]) -> ParsedOutput:
    """解析 OpenAI 兼容的 assistant message（可含 reasoning_content 与 tool_calls）。"""
    content = message.get("content") or ""
    if isinstance(content, list):
        content = _concat_content_parts(content)
    reasoning = message.get("reasoning_content") or ""
    tool_calls_raw = message.get("tool_calls") or []
    calls: list[ToolCall] = []
    for tc in tool_calls_raw:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "").strip()
        raw_args = str(fn.get("arguments") or "")
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {}
        calls.append(ToolCall(id=str(tc.get("id") or new_id("tc_")),
                              name=name, raw_arguments=raw_args, arguments=args))
    if calls:
        return ParsedOutput(kind="tool_calls", content=content or "",
                            reasoning=reasoning, tool_calls=calls)
    # 无原生 tool_calls：回落到文本解析（兼容纯文本模型）
    if not reasoning:
        reason, rest = _extract_reasoning(content)
        result = parse_tool_calls_text(rest)
        if result:
            return ParsedOutput(kind="tool_calls", content=rest, reasoning="\n".join(reason),
                                tool_calls=result)
        content = rest or content
    if not content and not reasoning:
        raise ParseError("LLM 返回空消息")
    return ParsedOutput(kind="answer", content=content, reasoning=reasoning)


def _concat_content_parts(parts: list[Any]) -> str:
    out: list[str] = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            out.append(str(p.get("text") or p.get("content") or ""))
    return "".join(out)