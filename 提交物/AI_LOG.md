# AI 使用记录：Prompt 与问题解决日志

> 作业提交内容之一："AI Prompt 与问题解决记录"。
> 本文件真实记录本项目中 AI 辅助的关键环节、用到的 Prompt、踩过的坑与解法。

---

## 1. 使用方式总览

| 环节 | AI 作用 | 作者职责 |
|---|---|---|
| 需求理解 | 整理作业要求逐条对照 | 判断优先级、划范围 |
| 架构设计 | 推荐分层模型（core/UI 分离、四步循环 Agent Loop） | 决策技术路线、取舍 |
| 实现 | 生成代码骨架与测试 | 逐模块审阅、验证行为 |
| 排错 | 定位构建/测试失败根因 | 确认修复方向 |
| 文档 | 起草 README / 设计文档 | 校对技术准确性 |

## 2. 关键 Prompt 记录

### P1 架构探索（对 deepseek-harness）
```
请探索 E:/git Project/github/deepseek-harness 这个 monorepo，
回答它如何做成网页版：分层架构、四象限消息模型、rpcId 纪律、
WebSocket/HTTP 各承载哪些方向、前端 dist 由谁 serve。
输出：架构分层图、连接建立步骤、组件清单。
```
**产出**：据此确定本项目 `core/UI 分离 + 四步循环（Agent Loop）` 的设计依据。

### P2 视觉规范迁移（对极光深空 TUI）
```
提炼 src/ui.py 的深空主题配色规范：背景/主色/次色/强调/文字四级/边框
的 RGB 值与语义，以及进度条三色阈值。用于迁移到 Web CSS 变量。
```
**产出**：`theme.css` 的 CSS 变量体系与三色进度逻辑。

### P3 实现约束（TDD 开工前）
```
按 TDD 流程实现项目要求的 Agent：
1) 先写 pytest 定义行为契约，再实现。
2) 不依赖任何 agent 框架（langgraph/openai-agents 等）。
3) 每个工具带名称、描述、参数 Schema，LLM 基于 Schema 决策。
4) 异常处理：工具失败不中断 loop，错误回传 LLM 决定下一步。
```
**产出**：测试驱动 + 容错 loop 的实现纪律。

### P4 排错（测试失败定位）
```
tests/test_context.py 有 3 个失败：
a) estimate_tokens("abc") 期望 1 实际 0
b) clear() 后 system 消息仍在
c) [thinking]…[/thinking] 正则匹配空内容
分别分析实现缺陷并修复，不要改测试。
```
**产出**：round 保底 / clear 清 system / 成对闭合正则优先。

### P5 接手遗留 bug（Claude Code 会话中断时未解决的问题）
```
帮我看看这个目录 claude code 的会话内容，E:\My Project\AuroraAgent，
78833e02-aaa8-4f46-a4df-d392bfb439aa，这个是会话的 ID。
```
**产出**：解析 JSONL → 抽出 128 条真实用户输入 + 助手回复 → 定位"保存 API 配置"是
最后一条未解决问题（#121，会话中断时没拿到浏览器实测）→ 通过启服务 + httpx 复现
8 个场景，**修正了上轮关于根因的 3 条错误预判**（其中 #2 其实不成立）→ 修代码 →
补 5 个测试 + 端到端验证 → 顺手修了用户 #115 提出但未确认的"窗口序号递增" bug
（前端 init 改为 list 复用），补 2 个测试 → 更新本文档。

## 3. 问题解决记录

### Q1 服务启动报 ModuleNotFoundError: web.server
- **症状**：`python run.py --mock` 报 `No module named 'web.server'`。
- **根因**：目录初建时建了顶层 `src/web`，但 server.py 实际写在 `src/aurora/web/`，
  run.py 用了 `from web.server import`。
- **解决**：统一定位为 `aurora.web.*`；删除残留的空 `src/web` 目录。
- **教训**：目录规划与实际文件落位要一致，别留两个同名命名空间。

### Q2 mock 模式 FakeLLM 剧本耗尽
- **症状**：`chat.send` 返回"回复序列已耗尽"，答案为空。
- **根因**：run.py 里 `FakeLLM()` 未传 scenario，steps 为空；且 `ModelGateway` 单例被所有会话共享时脚本位置推进。
- **解决**：FakeLLM 增加 `exhausted_reply` 兜底回复；run.py 提供覆盖"工具→结果→答案"的演示剧本。
- **延展**：真实多用户并发时不应共享脚本化 FakeLLM（仅演示/测试用途，文档注明）。

### Q3 tool_result 事件错误信息层级不一致
- **症状**：测试断言 `payload["error"]` 找不到"爆炸"字样。
- **根因**：实现把错误放在 `payload["result"]["error"]["message"]`，测试按顶层断言。
- **解决**：统一事件契约——`tool_result` 的 payload 为 `{tool, ok, result, text}`，
  错误在 `result.error.message`；测试按契约断言。
- **教训**：事件 payload 结构先写进契约（测试），实现对齐契约。

### Q4 `_KIND_CTOR` 键名错误
- **症状**：`validate_message` 报"未知消息类型: 'client-request'"。
- **根因**：用 `ClientRequest.type`（property 访问类返回 property 对象）当 dict 键，
  而非字符串 "client-request"。
- **解决**：显式用字符串字面量作键。
- **教训**：dataclass 的 `@property` 在类上访问 ≠ 在实例上访问。

### Q5 正则思考标签空匹配
- **症状**：`[thinking]查看天气[/thinking]` 匹配到空内容，思考丢失。
- **根因**：`.*?` 懒匹配 + 闭合标签"可选"导致空匹配。
- **解决**：成对闭合正则 `(?:<t>…</t>|\[t\]…\[/t\])` 优先；无闭合时退化到 开始标签+残余截取。

### Q6 端口被旧进程占用
- **症状**：重启后 `bind 0.0.0.0:8000 失败`，实际响应的是旧代码。
- **解决**：`netstat -ano | grep :8000` 找 PID → `taskkill //F //PID <pid>` → 重启。
- **教训**：后台进程验证新代码前先确认端口归属。

### Q7 服务成功但 chat 无响应（第一版）
- **症状**：快乐路径 API 200，`chat.send` 失败。
- **根因**：见 Q2（FakeLLM 剧本）+ Q6（旧进程）叠加。
- **解决**：修完 Q2/Q6 后链路完整：`iteration → thinking → tool_call → tool_result → answer → done`。

### Q8 设置面板「保存 API 配置」无效
- **症状**：首次保存真实模型成功（`/api/llm.configure` HTTP 200 + 状态更新）；
  再次打开设置面板，key 框是空的；只改 model/base 再保存，前端拦截报"API Key 与 模型名 必填"。
  用户感知："我保存了，但保存不了"。
- **根因链**：
  1. `saveModelConfig` 保存成功后执行 `$("#cfgKey").value = ""` 主动清空 key 输入框。
  2. 后端只回返 `api_key_masked`（出于安全），前端不回显完整 key。
  3. 重新打开弹窗 → key 框空白 → 提交时前端 `if (!api_key)` 拦截 → 报"必填"。
- **错误预判**（项目复盘阶段）：一开始怀疑"前端不回填 provider/base/model"和"后端只给掩码"，都是次要；
  真正根因是 key 一次性消费 + 留空即视为"无 key"。
- **解决**：把"留空 = 沿用旧 key"作为一等语义，前后端协同：
  - 后端 `_llm_configure`：key 留空时复用 `self.gateway.config.api_key`，仅首次配置（mock 网关）才报"必填"；
    顺带加 `Hub._normalize_base` 自动补 `/v1`（仅纯域名场景）。
  - 前端 `state.keyIsMask` 标志 + `loadModelStatus` 把后端掩码回填到 key 框 +
    `saveModelConfig` 删掉清空行 + 检测掩码/留空不传 `api_key` + 监听 `input` 事件确保编辑即视为新 key。
- **教训**：涉及**一次性凭证**的表单，"清空+不回显"几乎是 bug 模板；要么持久化到安全存储，要么回显掩码。
  修复时先把"现象 ↔ 真正根因"区分清楚，避免堆叠假说。
- **新增测试**（共 5 条）：`keeps_saved_key_when_blank` / `can_change_model_without_key` /
  `requires_key_on_first_setup` / `requires_model` / `normalize_base_appends_v1_only_for_bare_host`。
  全量 133 → 135 个测试全过。

### Q9 刷新页面窗口序号 +1（"应该只有打开新窗口时递增"）
- **症状**：浏览器刷新后，窗口标签从 `窗口` 变成 `窗口#2`、再刷变 `窗口#3`。
- **根因**：后端 `SessionManager._unique_name` 本身正确（"占空位"逻辑），
  但前端 `init()` 无脑调 `createSession()`，从不 `session.list` 复用。
  浏览器刷新 = JS 重新加载，state 清空；但后端 in-memory 旧 session 还在，
  → `_unique_name` 一直找下一个 #N，表现为序号无限递增。
- **解决**：前端新增 `loadSessions()`，init 顺序改为「list 复用 → 仅空时新建」。
  后端 `_unique_name` 的"占空位"行为也补了测试（`fills_gap_after_remove`）形成前后端契约。
- **教训**：纯后端 in-memory 状态 + 客户端无状态刷新，是非常常见的"看上去诡异" bug 模板。
  修复时必须**前后端一起核对**——只看后端会漏。

## 4. 对"AI 用思考而非代劳"的自我约束

- **决策不外包**：协议选型（四步循环 Agent Loop）、异常策略（错误回传 LLM）、依赖裁剪（零 agent 框架）均为作者拍板，AI 只提供选项与依据。
- **代码可审计**：每个 AI 生成的模块，作者都会读一遍并跑测试确认行为；测试即行为契约。
- **记录透明**：本文档如实记录 AI 参与度与问题过程，供评审核查。

### Q10 迭代卡片 "迭代N" 一直闪烁 + "处理中…" 不消失
- **症状**：一轮对话已完整回复（answer/done 都到了），但"迭代 1"卡片仍在闪、`处理中…` 文案常驻。
- **根因**：`handleDownlink` 里"把上一条事件标记完成"的生命周期逻辑只覆盖 `thinking` / `tool_call`，
  漏掉了 `iteration`。导致 `iteration` 事件的 `done` 永远是 `false`：
  - `renderMessages` 里 `hb.textContent = m.ev.done ? "已处理" : "处理中…"` → 永远显示"处理中…"；
  - `if (!m.ev.done) header.classList.add("live")` → 一直加 `live` 类，
    CSS `.step-header.live .step-badge { animation: pulse 1.2s infinite }` 触发持续闪烁。
- **解决**：生命周期标记补上 `iteration`：
  `if (k === "thinking" || k === "tool_call" || k === "iteration") m.ev.done = true;`
  当下一个 `iteration` / `answer` / `done` / `error` / `tool_call` 到达时，上一条迭代即被标完成。
- **教训**：可视化状态的"终态"必须由事件流明确驱动；凡是有"进行中/已完成"二态的卡片，
  标记完成的集合要和被标记的卡片类型逐一核对，漏一类就出现"假死/永动"状态。

### Q11 模型回复不渲染 Markdown（纯文本直出）
- **症状**：模型回复里的 `**粗体**`、列表、代码块、标题等都原样当纯文本显示。
- **根因**：气泡渲染用 `bubble.textContent = ...` 直接塞原始字符串，没有走 Markdown→HTML。
- **解决**：新增零依赖轻量渲染器 `renderMarkdown()`（先 `escapeHtml` 转义防 XSS，再套标题/粗斜体/
  行内代码/代码块/有序无序列表/引用/链接/分隔线），并：
  - 聊天气泡（`m.kind==="chat"` 的 user/assistant）改为 `bubble.innerHTML = renderMarkdown(m.text)`；
  - 最终答复卡片（`answer` 事件）同样改用 `renderMarkdown`；
  - 气泡加 `markdown` class，`theme.css` 补一整套 `.msg-bubble.markdown` 排版样式。
- **安全**：`escapeHtml` 先行转义，`javascript:` 等危险协议链接被降级为 `#`，避免模型输出注入页面。

### Q12 Loop 可视化改造成 harness/ReAct 风格（去掉"迭代 N"术语）
- **症状/诉求**：用户看不懂"迭代一 / 迭代二"这种显示，要求参考 deepseek-harness，
  把 Loop 可视化改成"调用工具的过程"——即显示 `工具名(参数)`，而不是无意义的"迭代 N 处理中"。
  项目要求（需求文档）：调用工具要展示成 `name(args)` 形式；至少实现 3 个工具。
- **现状核查**：工具早已齐备（calculator / web_search(mock) / weather（真实 Open-Meteo） / todo_add·list·done / read_docs，共 6 类工具，
  满足 ≥3）；Loop 也一直 emit `tool_call`/`tool_result` 事件，只是被埋在"迭代 N 处理中…"容器里、参数还是裸 JSON。
- **改造要点**：
  - `renderMessages`：删除 `.agent-step` 容器，改为扁平时间线 + 安静的"第 N 轮 · Agent Loop"分隔线（`.loop-round`，不再闪烁、不再显示"处理中"）。
  - 新增 `formatToolCall(name, args)`：单参数显示 `name(value)`（如 `calculator(3*3)`），多参数 `name(k=v, ...)`（如 `web_search(query=Agent 框架, max_results=3)`），无参 `name()`。
  - `buildLoopCard` 的 tool_call 卡片：头部显示 `🔧 工具名(参数)`，完成后内联展示「参数」与「结果」两行（harness：调用过程一目了然，无需点击展开）。
  - `theme.css`：删掉 `.agent-step`/`.step-*` 死样式，新增 `.loop-round` / `.lc-tool-call` / `.lc-tool-detail` / `.lc-line` / `.lc-k`。
- **验证**：`node --check` 通过；formatter 单测覆盖 6 种工具签名均正确；用 `build_demo_gateway` 跑通 mock 链路，
  事件流确认 `tool_call{tool,arguments}` / `tool_result{ok,text}` 与前端消费完全一致。
- **教训**：可视化术语要对使用者有意义。"迭代"是内部实现概念，直接暴露给用户是抽象泄漏；
  harness/ReAct 的「思考→行动(name(args))→观察(结果)」才是用户关心的"过程"。展示层应优先呈现动作与结果，而非内部循环计数。

## Q13｜修复 Anthropic 协议 400：tool_use 缺少紧邻的 tool_result
- **现象**：用 DeepSeek Anthropic 兼容端点（`https://api.deepseek.com/anthropic`）真实调用工具时，
  返回 `400: messages.N: tool_use ids were found without tool_result blocks immediately after`。
- **根因（两处叠加）**：
  1. `runtime/loop.py` 在模型决定调工具时**只把 `role=tool` 结果存入 context，从不保存那条带 `tool_calls` 的 assistant 消息**；
     导致下游客户端无法重建 `tool_use`，只能靠缓存"就近挂到最近 assistant"——多轮下错位。
  2. `runtime/context.py` 把 `Role.TOOL` 消息单独存进 `_tool_results` 桶并在 `build_messages()` 里**全部追加到末尾**，
     多轮对话时第 0 轮的 tool_result 被排到第 1 轮 user 之后，破坏"tool_use 紧跟 tool_result"的相邻约束。
  3. `llm/clients.py` 的 `_merge_consecutive` 在相邻 user 消息一条是纯文本字符串、一条是 tool_result 列表时
     **因类型不一致直接丢弃 tool_result**（`else` 分支），正是 400 的直接成因。
- **修复**：
  - `messages.py`：`Message` 新增 `tool_calls` 字段；`to_api_dict()` 在 assistant 上输出 OpenAI 原生 `tool_calls`。
  - `loop.py`：模型返回 `tool_calls` 时，先把"带 `tool_calls` 的 assistant 消息"存入 context（含 content/reasoning/tool_calls）。
  - `context.py`：`build_messages()` 按 `tool_call_id` 把每个 `tool_result` **插入到拥有它的 assistant 之后**，多轮顺序正确。
  - `clients.py`：`_to_anthropic_messages` 改为直接读 assistant 的 `tool_calls` 渲染 `tool_use`（不再依赖 cache 重建）；
    `_merge_consecutive` 修复字符串/列表混合合并时丢弃 tool_result 的 bug（统一转 text 块再合并）。
- **验证**：  自写复现脚本覆盖 4 类场景（单轮单/双工具、两轮各单/双工具），修复前 C/turn1、D/turn1 必现 400，修复后全部通过 Anthropic 约束；
  全量测试通过（当前项目共 150 个用例）；`test_anthropic_loop_tool_call_integration` 端到端用 MockTransport 验证真实 Loop+客户端可跑通「调工具→再回答」。
- **作用范围**：同时修好了 OpenAI 兼容协议的同类缺陷（assistant 此前也无 `tool_calls`，工具调用链路同样不完整）。

## Q14｜read_docs 报"未找到文件"：路径解析优先返回了不存在的候选

- **现象**：真实演示多轮对话时，模型调用 `read_docs` 读取 `doc/DESIGN.md`、`docs/README.md` 均返回"未找到文件"，
  但同一轮里 `DESIGN.md`、`README.md` 却能读出来。
- **根因**：`tools/read_docs.py` 的 `_resolve_safe()` 要在一个候选矩阵（`base × 变体`）里挑路径，
  原实现是"**双重循环取第一个通过安全校验的候选**"，而不是"取第一个**真实存在**的候选"。
  当 `base=项目根/doc` 排在 `base=项目根` 之前时，`doc/README.md` 通过了安全校验（在项目根内）便立即返回，
  于是真实存在的 `README.md`（项目根下）根本没机会被尝试——路径合法但文件不存在。
- **修复**：把选择策略从"第一个合法候选"改为"**第一个真实存在的候选**"：
  先遍历整个 `base × 变体` 矩阵找 `exists()` 命中的路径并立即返回；全部不存在时，才退回第一个通过安全校验的候选
  （保留原有的清晰报错信息）。`../` 等越界路径仍被拦截。
- **验证**：新增 `read_docs` 路径变体用例；实测 `doc/DESIGN.md → doc\DESIGN.md`、`docs/README.md → README.md`、
  `DESIGN.md → doc\DESIGN.md`、`README.md → README.md` 全部命中，`../README.md` 仍被拒。
- **教训**："路径解析"这类函数的正确性判据是**能否命中真实文件**，不是"字符串是否合法"。
  候选矩阵一旦有多个 base，就必须以 `exists()` 为准，合法性校验只用来兜底报错与防穿越。

## Q15｜Anthropic 400 复发：孤立 tool_use + 相邻 assistant 同角色

- **现象**：Q13 修完后，四轮真实对话的第 4 轮仍报
  `400: messages.1.2: tool_use ids were found without tool_result blocks immediately after: call_0b7692...`。
- **根因（两个独立缺陷，Q13 只覆盖了 1/3）**：
  1. **孤立 tool_use**：转换层仍依赖 `context.build_messages()` 把 tool_result "相邻归位"。
     一旦跨轮累积触发 `compact()`/`trim`、或工具执行抛错没回注结果，assistant 上的 `tool_use` 就没有配对结果。
  2. **相邻同角色**：`loop.py` 对"先 reasoning、再 tool_calls"会连写**两条 assistant 消息**，
     Anthropic 要求 user/assistant 严格交替，相邻两条 assistant 同样触发 400。
- **修复**（`llm/clients.py` 的 `_to_anthropic_messages` 改为**完全自包含重建**）：
  - 先全局扫描 `role=tool` 消息建立 `tool_call_id → 结果内容` 映射；
  - 渲染每条 assistant 的 `tool_use` 后**立即**输出一条 `user(tool_result)`；结果缺失时补
    `is_error: true` 的占位块——从根上消除"相邻依赖"造成的 400；
  - append assistant 前若上一条也是 assistant，则**合并 blocks** 并剔除空 text 块，保证角色严格交替；
  - 真实 user 文本并入紧邻的 user 消息（text 块可与 tool_result 共存于同一条 user）。
- **验证**：新增 `test_to_anthropic_messages_roles_strictly_alternate`；
  用真实 `AgentLoop` + 真实 `read_docs` 复刻用户四轮对话结构，孤立 `tool_use=0`、角色未交替=0，
  最终消息序列 `user / a(tu) / u(tr) / a(tu,tu) / u(tr,tr) / …` 完全合法；测试 150→154。
- **教训**：协议适配层不能假设上游消息序列是干净的。凡是"经过多轮累积/压缩/异步写入"的消息数组，
  适配层都应**自己重建不变量**（配对、交替、首条角色），而不是把不变量寄托在上游的构造顺序上。

## Q16｜工具结果桶容量 8 太小：多轮演示的早期结果被挤出

- **现象**：按演示脚本跑满一轮（计算器 + 天气 + 待办增查改 + 读文档 + 多窗口，约 10~15 次工具调用）后，
  再追问前面的调用，模型回答"工具结果缺失"——因为转换层给早期 `tool_use` 补了 `is_error` 占位。
- **根因**：`context.py` 的 `_tool_results_cap = 8`，桶满后最旧的工具结果被丢弃；
  转换层为保证协议合法补了占位块（不报错），但模型读到的是"结果缺失"而非真实数据。
- **修复**：容量 8 → 24（一次完整演示约 10~15 次调用，留足余量）；
  在 `build_messages` 之外同步更新 `doc/DESIGN.md` 的分桶描述。
- **验证**：新增 `test_tool_results_cap_covers_full_demo_session`（15 次工具调用后不应出现任何占位块）；测试 154→155。
- **教训**：协议层兜底（补占位）只保证"不报错"，不保证"体验正确"。
  这类兜底一旦在真实链路里被触发，说明容量/时序假设错了，应回溯修正假设，而不是接受兜底结果。