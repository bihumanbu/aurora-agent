# AuroraAgent · 从零实现的最小可用 Agent

> 从零自研的最小可用 Agent：核心 Runtime 不依赖任何 agent 框架，配套 Web UI 用于工具调用可视化与多轮会话演示。

## 一、运行方式

### 环境要求
- Python 3.11+
- 运行时依赖：`fastapi / uvicorn / httpx / pydantic`；测试依赖：`pytest`

```bash
pip install -r requirements.txt
```

### 启动

**Windows（推荐，一键启动）**：直接双击根目录 `start.bat`。脚本自动按 `.env` 是否配置 `AURORA_API_KEY` 选择真实 / 演示模式；也可 `start.bat mock` / `start.bat real` 手动指定。

**命令行（跨平台）**：
```bash
# 演示模式（无需 API key，内置脚本化 FakeLLM，可完整演示 Loop / 工具 / 多窗口 / Trace）
python run.py --mock

# 真实 LLM API —— OpenAI 兼容厂商，以 DeepSeek 为例
python run.py --api-base https://api.deepseek.com/v1 --api-key sk-你的key --model deepseek-chat

# 真实 LLM API —— Anthropic 兼容协议（如 DeepSeek 的 /anthropic 端点）
python run.py --provider anthropic --api-base https://api.deepseek.com/anthropic --api-key sk-你的key --model deepseek-chat
```
启动后浏览器访问 http://127.0.0.1:8000（Web UI 为原生 HTML/CSS/JS，零构建，用于演示与工具调用可视化）。

### 运行测试
```bash
pytest        # 全部离线（LLM 全程 mock），155 个用例
```

## 二、系统设计

### 2.1 四步循环（核心）
`src/aurora/runtime/loop.py` 实现 Agent 主循环：
1. 接收用户输入 → 写入 `session.context`
2. LLM 带工具 Schema 决策：直接回复 or 调用工具（输出解析双路兜底：原生 function-call / 文本内 JSON 提取）
3. 执行工具（Schema 校验 → handler；异常不中断 Loop，错误回传 LLM）
4. 工具结果回注 context → 继续 loop 或返回结果（停止条件：返回 answer / 达 max_iters / 致命错误）

### 2.2 工具注册机制与工具清单
`src/aurora/runtime/registry.py`：每个工具绑定「名称 / 描述 / 参数 JSON Schema」，执行前轻量校验（type/required/enum/min/max）；`registry.spec()` 导出 OpenAI function-calling 格式供 LLM 自主决策调用。

已实现工具（覆盖计算 / 搜索 / 天气 / 待办 / 文档等）：
| 工具 | 本实现 | 说明 |
|---|---|---|
| calculator | `calculator` | 真实计算（AST 白名单求值，拒绝函数调用/导入/属性访问，防代码注入） |
| search（可 mock） | `web_search` | mock 返回示例搜索结果 |
| weather（真实数据） | `weather` | Open-Meteo 实时天气（免费免 key，城市名→经纬度→实时数据；断网/无解自动回退内置 mock） |
| todo | `todo_add` / `todo_list` / `todo_done` | 内存待办管理 |
| read_docs | `read_docs` | 读取项目内文档（限制根目录，防路径穿越） |

### 2.3 LLM 输出解析
`src/aurora/llm/parsing.py`：从模型响应提取「思考过程 / 工具调用 / 最终答案」，兼容 OpenAI function-call 与文本内 JSON 两种格式。

### 2.4 多窗口 Session 管理
`src/aurora/runtime/session.py`：每个窗口独立 Session，独立持有 Context + Trace，互不串扰；可随时回到任一窗口续聊（会话恢复）。

### 2.5 Context 有效管理
`src/aurora/runtime/context.py`（BucketedContext）：
- **最大轮次限制**（`max_turns`）
- **记住之前状态**：滚动历史保留最近若干轮
- **支持追问**：纯对话追问（命中滚动历史）/ 带工具的追问（命中工具结果桶）
- **基础压缩**：超过阈值将最旧非关键轮次摘要化（复杂压缩不实现）

### 2.6 异常处理与工具 Trace
`src/aurora/exceptions.py` + `src/aurora/runtime/trace.py`：工具参数/执行错误收敛为结构化异常，**不中断 Loop**，错误文本回传 LLM 自主决策（换参数 / 降级 / 收尾）；每次工具调用记录「名称/参数/结果/错误/耗时」，可供审计与回放。

### 2.7 真实 LLM API
`src/aurora/llm/clients.py`：OpenAI 兼容多厂商 + Anthropic 兼容协议（Messages API）。API key 仅存于 `.env`（已 gitignore），不硬编码进代码。

## 三、Memory 召回时机与放置方式

### 放置方式（分桶）
1. **工作上下文（实时）** —— `session.context`（BucketedContext）：
   - `system` 桶：system prompt
   - `rolling_history` 桶：最近 `max_turns=20` 轮（用户输入 + 思考 + 助手回答）
   - `tool_results` 桶：最近工具结果（带工具追问的基础）
   - `compacted_summary` 桶：被压缩掉的旧轮摘要
2. **情景记忆（审计）** —— `session.trace`：每次工具调用的结构化记录，可查询与回放。
3. **会话态** —— `Session` 独立持有 Context + Trace，随会话创建/删除。

### 召回时机
| 时机 | 召回内容 | 放在 messages 的位置 |
|---|---|---|
| 每轮发 LLM 前 | system + 摘要 + 滚动历史 + 工具结果 | `context.build_messages()` |
| 工具调用后 | 本次参数 / 结果 | 回注为 tool 消息 + 写入 trace |
| 追问（纯对话 / 带工具） | 最近 N 轮 + 最近工具结果 | 直接命中 rolling_history / tool_results 桶 |
| context 超阈值 | 最旧非关键轮 → 轻量摘要 | 移入 compacted_summary 桶 |

> 取舍：复杂压缩不在当前实现范围，当前为「分桶 + 滚动窗口 + 轻量摘要」。**Context 是给 LLM 看的记忆，Trace 是给人看的审计日志**，职责分离。

