"""AuroraAgent — 极光 Agent OS。

从零自研的最小可用 Agent（2026 后端 Agent 方向最小可用 Agent）。
核心 Agent Runtime 不依赖任何第三方 agent 框架（如 langgraph/autogen 等）。

分层：
    aurora.runtime  — Agent Runtime（loop / registry / messages / session / context）
    aurora.tools    — 内置工具（calculator / web_search / todo / weather / read_docs）
    aurora.llm      — 多厂商 LLM API 适配 + 输出解析
    web             — 四象限 RPC 协议层（HTTP 上行 + WebSocket 下行）
"""

__version__ = "1.0.0"