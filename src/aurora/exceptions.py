"""统一异常层次。

所有可预测错误都以结构化异常类型向上抛，便于：
    - Loop 捕获并按类型决定降级策略（跳过工具继续 / 终止）
    - Web 层映射为 RpcResult {ok:false, error:{code,details}}
    - Trace 层记录错误码，前端展示红标

分层：
    AuroraError            — 基类
    ├── ParseError         — LLM 输出解析失败
    ├── ToolError          — 工具层错误
    │   ├── ToolArgumentError   — 参数不符合 Schema
    │   └── ToolExecutionError  — 工具执行期错误（含未知工具）
    ├── LLMError           — LLM API 调用错误
    └── SessionError       — 会话已关闭 / 不存在等
"""

from __future__ import annotations

from typing import Any


class AuroraError(Exception):
    """基类。code 用于 RPC 错误映射，details 携带结构化上下文。"""

    code: str = "aurora-error"

    def __init__(self, message: str = "", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


class ParseError(AuroraError):
    code = "parse-error"


class ToolError(AuroraError):
    code = "tool-error"


class ToolArgumentError(ToolError):
    code = "tool-argument-error"


class ToolExecutionError(ToolError):
    code = "tool-execution-error"

    def __init__(self, message: str = "", tool_name: str = "", **details: Any) -> None:
        super().__init__(message, details={"tool_name": tool_name, **details})


class LLMError(AuroraError):
    code = "llm-error"


class LLMRateLimitError(LLMError):
    """429 中的「可恢复限流」：TPM/RPM 超限，退避后可重试。"""

    code = "llm-rate-limit"


class LLMQuotaError(LLMError):
    """429/402/403 中的「不可恢复额度问题」：套餐耗尽 / 余额不足 / 账单异常。

    与 LLMRateLimitError 的关键区别：**重试无意义**，必须换 key 或充值。
    带上 retryable=False 让上层（Loop / Web）直接终止，不再空转退避。
    """

    code = "llm-quota-error"

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message, details={"retryable": False, **details})


class SessionError(AuroraError):
    code = "session-error"


class ProtocolError(AuroraError):
    code = "protocol-error"