"""LLM Gateway — 统一接口，多厂商兼容。

ModelGateway.complete(messages, tools) → ParsedOutput
    - 真实后端（OpenAICompatibleClient）：DeepSeek / 通义 / Kimi / OpenAI 等
      凡兼容 /chat/completions + function-call 的厂商均可。
    - mock 后端（FakeLLM）：--mock 演示 / 测试 / 无 key 录屏。

所有后端统一返回 ParsedOutput（解析逻辑收敛在 parsing.py），
Gateway 是薄封装，负责把 Loop 的「消息 + 工具 Schema」路由到后端。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from aurora.llm.parsing import ParsedOutput


@dataclass
class LLMConfig:
    mock: bool = False
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    provider: str = "openai_compatible"
    temperature: float = 0.3
    max_tokens: int = 1024
    timeout: float = 60.0

    def effective_base(self) -> str:
        """真正请求的 base URL。显式 api_base 优先，否则按 provider 推断。"""
        if self.api_base:
            return self.api_base.rstrip("/")
        known = {
            "deepseek": "https://api.deepseek.com/v1",
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.deepseek.com/anthropic",
        }
        return known.get(self.provider, "")


class Backend(Protocol):
    """后端统一接口：任何实现 complete → ParsedOutput 的类。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ParsedOutput: ...


class ModelGateway:
    """统一 LLM 入口（依赖注入后端）。"""

    def __init__(self, config: LLMConfig, backend: Backend) -> None:
        self.config = config
        self.backend = backend

    async def complete(self, messages: list[dict], tools: list[dict]) -> ParsedOutput:
        return await self.backend.complete(messages, tools)