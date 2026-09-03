"""OpenAICompatibleClient — 真实 LLM API 适配。

兼容 OpenAI / DeepSeek / 通义千问 / Kimi / 智谱 等厂商的
POST {base}/chat/completions + function-call 协议。

请求体：
    {"model", "messages", "tools", "temperature", "max_tokens", "stream": false}
响应：
    choices[0].message  → 交给 parsing.parse_native 解析（支持 reasoning_content
                        与原生 tool_calls 双通道）。

网络层用 httpx；测试注入 MockTransport 不发起真实请求。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from aurora.exceptions import (
    LLMError,
    LLMQuotaError,
    LLMRateLimitError,
)
from aurora.llm.gateway import LLMConfig
from aurora.llm.parsing import ParsedOutput, parse_native
from aurora.runtime.messages import ToolCall, new_id

# ── 错误文本特征词 ────────────────────────────────────────────────
# 不可恢复：额度/账单类。重试纯属空转，必须换 key 或充值。
_QUOTA_HINTS = (
    "quota", "entitlement", "exhausted", "insufficient_quota", "balance",
    "billing", "plan", "credit", "top_up", "topup", "purchase",
    "subscription", "arrears", "额度", "余额", "欠费", "套餐", "已用完",
)
# 可恢复：速率类。退避后大概率能成。
_RATE_HINTS = (
    "rate_limit", "rate limit", "ratelimit", "too many requests",
    "tpm", "rpm", "requests per minute", "tokens per minute",
    "concurrent", "throttl", "限速", "并发",
)


def _extract_error_text(body: str) -> str:
    """从各家厂商形态各异的错误体里抠出可读文本（message + type + code）。"""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return body[:300]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            parts = [
                str(err.get("message") or ""),
                str(err.get("type") or ""),
                str(err.get("code") or ""),
            ]
            return " ".join(p for p in parts if p).strip()
        return str(err)
    return str(data)[:300]


def classify_http_error(status: int, body: str) -> tuple[bool, str]:
    """判断一个 HTTP 错误是否值得重试。

    返回 (retryable, human_message)。

    设计要点：**429 不等于都可重试**。厂商把两类完全不同的错误都塞进 429：
      - 速率限流（TPM/RPM 超限）→ 退避后能恢复，值得重试
      - 额度耗尽（套餐用尽/余额不足/账单异常）→ 重试一万次也没用，
        只会让用户白等 5.6 秒、并往一个已死的 key 上重复打请求

    判定时先匹配「不可恢复」特征词，命中即短路——宁可不重试，也不空转。
    """
    text = _extract_error_text(body)
    low = text.lower()

    if any(h in low for h in _QUOTA_HINTS):
        return False, (
            f"API 额度已耗尽，无法继续调用：{text[:200]}\n"
            "→ 这不是限流，重试也不会恢复。请更换有额度的 API Key，"
            "或先切回 mock 演示模式继续体验。"
        )

    if status == 429 or any(h in low for h in _RATE_HINTS):
        return True, f"触发速率限流，稍后自动重试：{text[:200]}"

    if status in (500, 502, 503, 504):
        return True, f"服务端暂时不可用（{status}），稍后自动重试：{text[:200]}"

    if status in (401, 403):
        return False, f"鉴权失败（{status}）：{text[:200]}\n→ 请检查 API Key 是否正确、是否已过期。"

    if status == 404:
        return False, f"接口不存在（404）：{text[:200]}\n→ 请检查 api_base 与 model 名称。"

    return False, f"LLM API 返回 {status}: {text[:300]}"


# ── 共享重试逻辑（OpenAI / Anthropic 客户端共用）─────────────────
async def request_with_retry(
    http_client: "httpx.Client | httpx.AsyncClient",
    url: str,
    headers: dict[str, str],
    payload: dict,
) -> "httpx.Response":
    """带重试的请求：仅对「可恢复」错误做指数退避。

    与朴素实现的关键差异：**先分类再决定是否重试**。
    额度耗尽类错误（quota_exceeded / entitlement exhausted …）
    直接返回响应，交由上层抛出 LLMQuotaError——
    不再白等 5.6 秒、也不再往已经失效的 key 上重复打请求。

    OpenAI / Anthropic 两类客户端共用，保证重试语义一致。
    """
    max_retries = 3
    base_delay = 0.8
    last_exc: Exception | None = None
    resp: "httpx.Response | None" = None

    for attempt in range(max_retries + 1):
        try:
            if isinstance(http_client, httpx.AsyncClient):
                resp = await http_client.post(url, headers=headers, json=payload)
            else:
                resp = await asyncio.to_thread(
                    lambda: http_client.post(url, headers=headers, json=payload))
        except Exception as e:  # noqa: BLE001 — 网络错误：重试
            last_exc = e
            if attempt < max_retries:
                await asyncio.sleep(base_delay * (2 ** attempt))
                continue
            raise LLMError(f"LLM 请求失败: {e}") from e

        if resp.status_code < 400:
            return resp

        retryable, _msg = classify_http_error(resp.status_code, resp.text)
        if not retryable:
            # 不可恢复（额度/鉴权/404）→ 立即交给上层抛，不做无谓退避
            return resp

        if attempt < max_retries:
            await asyncio.sleep(_backoff_delay(resp, attempt, base_delay))
            continue
        return resp

    if resp is not None:
        return resp
    raise LLMError(f"LLM 请求失败: {last_exc}") from last_exc


def _backoff_delay(resp: "httpx.Response", attempt: int, base_delay: float) -> float:
    """退避时长：优先遵循服务端 Retry-After，否则指数退避。"""
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            return min(float(raw), 30.0)   # 上限 30s，避免被服务端拖死
        except (TypeError, ValueError):
            pass
    return base_delay * (2 ** attempt)


# ── Anthropic 协议辅助（与 OpenAI 格式互转）─────────────────────
def _extract_system(messages: list[dict]) -> str:
    """从 OpenAI 格式消息里抽出 system 文本（Anthropic 用顶层字段而非消息）。"""
    parts = [m.get("content") or "" for m in messages if m.get("role") == "system"]
    return "\n\n".join(p for p in parts if p).strip()


def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """OpenAI tools → Anthropic tools（parameters 改名为 input_schema）。"""
    out: list[dict] = []
    for t in tools or []:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        if not isinstance(fn, dict):
            fn = {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        schema = fn.get("parameters") or fn.get("input_schema") or {}
        if not isinstance(schema, dict):
            schema = {}
        out.append({
            "name": name,
            "description": str(fn.get("description") or ""),
            "input_schema": schema,
        })
    return out


def _to_anthropic_messages(messages: list[dict], tool_cache: dict) -> list[dict]:
    """把 OpenAI 格式消息转成 Anthropic 格式（协议级兜底，绝不 400）。

    Anthropic 两条硬约束：
      1. 角色严格交替（user / assistant 不可相邻重复）；
      2. 每个 tool_use 块必须紧随其后有一个 user 消息承载对应 tool_result。

    旧实现依赖 context.build_messages 把 tool_result「相邻归位」到 assistant 之后，
    一旦上游因跨轮累积触发 compact/trim、或 assistant(tool_calls) 与 tool_result
    错位，就出现孤立 tool_use → 400。这里改为**完全自包含**地重建：
      - 先扫描全部 role=tool 消息，建立 tool_call_id → 结果内容 的全局映射；
      - 渲染每条 assistant 的 tool_use 后，**立即**输出一个 user(tool_result)
        消息（缺失则补 is_error 占位），从根本上消除「相邻依赖」带来的 400。
      - 真实 user 文本合并进紧邻的 user 消息（可与 tool_result 共存于一个
        user 消息的多个 block），保证角色交替。
    tool_cache 仍由 _parse_anthropic_response 维护，作为兜底（本函数不再读取）。
    """
    # 1) 全局收集 tool_result：tool_call_id -> content
    results: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "tool":
            rid = m.get("tool_call_id") or ""
            if rid:
                results[rid] = str(m.get("content") or "")

    out: list[dict] = []

    def _append_user_text(text: str) -> None:
        """真实用户输入：化为 text 块并入 user 消息（与前面 tool_result 合并）。"""
        block = {"type": "text", "text": text}
        if out and out[-1]["role"] == "user":
            prev = out[-1]["content"]
            if isinstance(prev, list):
                out[-1]["content"] = prev + [block]
            else:
                out[-1]["content"] = [{"type": "text", "text": prev}, block]
        else:
            out.append({"role": "user", "content": [block]})

    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "assistant":
            blocks: list[dict] = []
            content = m.get("content") or ""
            reasoning = m.get("reasoning_content") or ""
            if content:
                blocks.append({"type": "text", "text": str(content)})
            if reasoning:
                blocks.append({"type": "text", "text": str(reasoning)})
            tool_ids: list[str] = []
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", tc) if isinstance(tc, dict) else {}
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                name = str(fn.get("name") or (tc.get("name") if isinstance(tc, dict) else "") or "")
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (ValueError, TypeError):
                        args = {}
                elif args is None:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc_id, "name": name, "input": args})
                tool_ids.append(tc_id)
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            # 紧跟 tool_result：与其 tool_use 配对，缺失补占位避免 400
            if tool_ids:
                tr_blocks: list[dict] = []
                for tid in tool_ids:
                    if tid in results:
                        tr_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": results[tid],
                        })
                    else:
                        tr_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": "(工具结果缺失，已自动占位)",
                            "is_error": True,
                        })
                out.append({"role": "user", "content": tr_blocks})
        elif role == "user":
            text = m.get("content") or ""
            if text:
                _append_user_text(str(text))
            elif not (out and out[-1]["role"] == "user"):
                # 空 user 也补一个空块，避免破坏角色交替
                out.append({"role": "user", "content": [{"type": "text", "text": ""}]})
        # role == "tool" 已在 results 收集，跳过
    return out


def _parse_anthropic_response(data: dict, tool_cache: dict) -> ParsedOutput:
    """解析 Anthropic messages 响应 → ParsedOutput。"""
    blocks = data.get("content", []) or []
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    calls: list[ToolCall] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            text_parts.append(str(b.get("text") or ""))
        elif t == "thinking":
            thinking_parts.append(str(b.get("thinking") or b.get("text") or ""))
        elif t == "tool_use":
            name = str(b.get("name") or "")
            arguments = b.get("input") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            tc = ToolCall(
                id=str(b.get("id") or new_id("tc_")),
                name=name,
                arguments=arguments,
                raw_arguments=json.dumps(arguments, ensure_ascii=False),
            )
            calls.append(tc)
            tool_cache[tc.id] = {"name": name, "arguments": arguments}
    reasoning = "\n".join(thinking_parts)
    content = "\n".join(text_parts)
    if calls:
        return ParsedOutput(kind="tool_calls", content=content,
                            reasoning=reasoning, tool_calls=calls)
    return ParsedOutput(kind="answer", content=content, reasoning=reasoning)


class OpenAICompatibleClient:
    """OpenAI /v1/chat/completions 兼容客户端。"""

    def __init__(self, config: LLMConfig, http_client: httpx.Client | None = None) -> None:
        self.config = config
        if not config.effective_base():
            raise LLMError("未配置 api_base（如 https://api.deepseek.com/v1）")
        # 传入时用注入的（测试用 MockTransport），否则自建
        self._client = http_client or httpx.Client(timeout=config.timeout)

    async def complete(self, messages: list[dict], tools: list[dict]) -> ParsedOutput:
        url = f"{self.config.effective_base()}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        resp = await self._request_with_retry(url, headers, payload)

        if resp.status_code != 200:
            self._raise_for_status(resp)

        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"LLM 响应不是合法 JSON: {e}") from e

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"LLM 响应格式异常: {str(data)[:200]}") from e

        if message.get("finish_reason") == "length":
            # 截断：内容不完整，抛出走 loop 容错
            pass
        return parse_native(message)

    async def _request_with_retry(
        self, url: str, headers: dict[str, str], payload: dict
    ) -> httpx.Response:
        """带重试的请求：仅对「可恢复」错误做指数退避（逻辑见模块级 request_with_retry）。"""
        return await request_with_retry(self._client, url, headers, payload)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """把非 200 响应翻译成带语义的异常类型（决定上层是否重试）。"""
        retryable, message = classify_http_error(resp.status_code, resp.text)
        details = {"status": resp.status_code, "retryable": retryable}
        if resp.status_code == 429 and not retryable:
            raise LLMQuotaError(message, status=429)
        if resp.status_code == 429:
            raise LLMRateLimitError(message, status=429)
        if resp.status_code in (402, 403) and "quota" in message.lower():
            raise LLMQuotaError(message, status=resp.status_code)
        raise LLMError(message, details=details)

class AnthropicCompatibleClient:
    """Anthropic Messages API 兼容客户端（DeepSeek / 第三方 Anthropic 兼容网关）。

    与 OpenAICompatibleClient 共享「先分类再重试」逻辑与 ParsedOutput 输出，
    差异只在协议层：
      - 请求：POST {base}/v1/messages，消息/工具采用 Anthropic 格式
      - 响应：content 数组含 text / thinking / tool_use 块

    app 的 Loop 在调用工具后只回注 tool 结果、不保留 assistant 的 tool_calls，
    因此这里用 tool_cache 按 tool_call_id 重建 tool_use 块（见 _to_anthropic_messages）。
    """

    def __init__(self, config: LLMConfig, http_client: httpx.Client | None = None) -> None:
        self.config = config
        if not config.effective_base():
            raise LLMError("未配置 api_base（如 https://api.deepseek.com/anthropic）")
        self._client = http_client or httpx.Client(timeout=config.timeout)
        # 跨轮缓存：让 tool_result 能正确归属到前一条 assistant（见模块级函数说明）
        self._tool_cache: dict[str, dict] = {}

    @staticmethod
    def _messages_url(base: str) -> str:
        """由 base 推导 messages 端点，兼容三种写法：
        https://api.deepseek.com/anthropic        -> .../anthropic/v1/messages
        https://api.deepseek.com/anthropic/v1     -> .../anthropic/v1/messages
        https://api.deepseek.com/anthropic/v1/messages -> 原样
        """
        b = (base or "").rstrip("/")
        if b.endswith("/messages"):
            return b
        if b.endswith("/v1"):
            return b + "/messages"
        return b + "/v1/messages"

    async def complete(self, messages: list[dict], tools: list[dict]) -> ParsedOutput:
        url = self._messages_url(self.config.effective_base())
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "system": _extract_system(messages),
            "messages": _to_anthropic_messages(messages, self._tool_cache),
            "stream": False,
        }
        anth_tools = _to_anthropic_tools(tools)
        if anth_tools:
            payload["tools"] = anth_tools

        resp = await request_with_retry(self._client, url, headers, payload)

        if resp.status_code != 200:
            self._raise_for_status(resp)

        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"LLM 响应不是合法 JSON: {e}") from e

        return _parse_anthropic_response(data, self._tool_cache)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """把非 200 响应翻译成带语义的异常类型（决定上层是否重试）。"""
        retryable, message = classify_http_error(resp.status_code, resp.text)
        details = {"status": resp.status_code, "retryable": retryable}
        if resp.status_code == 429 and not retryable:
            raise LLMQuotaError(message, status=429)
        if resp.status_code == 429:
            raise LLMRateLimitError(message, status=429)
        if resp.status_code in (402, 403) and "quota" in message.lower():
            raise LLMQuotaError(message, status=resp.status_code)
        raise LLMError(message, details=details)
