/* ═══════════════════════════════════════════════════════════
   极光 Agent OS — Web 前端主逻辑
   四象限 RPC 协议客户端：
     上行: POST /api/<method>  {type, rpcId, method, payload}  (ClientRequest)
     下行: WS  /ws             每帧 {type, rpcId, method, payload}  (ServerRequest)
   Loop 可视化: iteration / thinking / tool_call / tool_result / answer / done
   Session:     顶部多窗口标签，独立历史互不串扰
   Trace:       右侧审计面板，回放每次工具调用
   ═══════════════════════════════════════════════════════════ */
"use strict";

const state = {
  sessions: new Map(),   // session_id -> {id, name, messages: [], events: []}
  active: null,          // 当前 session_id
  ws: null,
  seq: 0,
  busy: false,
  keyIsMask: false,      // key 输入框当前显示的是后端掩码（= 未修改，保存时沿用旧 key）
};

const $ = (sel) => document.querySelector(sel);

// ── 四象限协议：rpcId 纪律 ─────────────────────────────────
function makeRpcId() {
  state.seq += 1;
  return "rpc_" + Date.now().toString(36) + "_" + state.seq;
}

async function call(method, payload) {
  const req = { type: "client-request", rpcId: makeRpcId(), method, payload };
  const resp = await fetch("/api/" + method, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  const data = await resp.json();
  // ServerResponse {type, rpcId, result:{ok, value|error}}
  if (data && data.result && data.result.ok) return data.result.value;
  const err = (data && data.result && data.result.error) || { message: "未知错误" };
  throw new Error(err.message || err.code);
}

// ── Markdown 渲染（轻量、零依赖，先转义再套格式，避免 XSS） ──
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function inlineMarkdown(text) {
  // 入参已是 HTML 转义后的安全文本
  // 行内代码
  text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
  // 粗体
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  // 斜体（避免误伤 **）
  text = text.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  text = text.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
  // 链接 [text](url)，仅放行安全协议
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, t, u) => {
    const safe = /^(https?:\/\/|mailto:|\/)/.test(u) ? u : "#";
    return '<a href="' + safe + '" target="_blank" rel="noopener">' + t + "</a>";
  });
  // 段内换行
  text = text.replace(/\n/g, "<br/>");
  return text;
}

function renderMarkdown(src) {
  if (!src) return "";
  const lines = String(src).replace(/\r\n/g, "\n").split("\n");
  let html = "";
  let listType = null;       // 'ul' | 'ol'
  let para = [];

  const flushPara = () => {
    if (para.length) {
      html += "<p>" + inlineMarkdown(para.join("\n")) + "</p>";
      para = [];
    }
  };
  const closeList = () => {
    if (listType) { html += "</" + listType + ">"; listType = null; }
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // 代码块 ```
    if (/^```/.test(line)) {
      flushPara(); closeList();
      let code = "";
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { code += lines[i] + "\n"; i++; }
      i++; // 跳过结尾 ```
      html += "<pre><code>" + escapeHtml(code.replace(/\n$/, "")) + "</code></pre>";
      continue;
    }
    // 标题
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara(); closeList();
      const lv = h[1].length;
      html += "<h" + lv + ">" + inlineMarkdown(escapeHtml(h[2])) + "</h" + lv + ">";
      i++; continue;
    }
    // 引用
    if (/^>\s?/.test(line)) {
      flushPara(); closeList();
      let q = "";
      while (i < lines.length && /^>\s?/.test(lines[i])) { q += lines[i].replace(/^>\s?/, "") + "\n"; i++; }
      html += "<blockquote>" + inlineMarkdown(escapeHtml(q.replace(/\n$/, ""))) + "</blockquote>";
      continue;
    }
    // 分隔线
    if (/^---+\s*$/.test(line)) { flushPara(); closeList(); html += "<hr/>"; i++; continue; }
    // 无序列表
    const ul = line.match(/^[-*+]\s+(.*)$/);
    if (ul) {
      flushPara();
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += "<li>" + inlineMarkdown(escapeHtml(ul[1])) + "</li>";
      i++; continue;
    }
    // 有序列表
    const ol = line.match(/^\d+\.\s+(.*)$/);
    if (ol) {
      flushPara();
      if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
      html += "<li>" + inlineMarkdown(escapeHtml(ol[1])) + "</li>";
      i++; continue;
    }
    // 空行
    if (/^\s*$/.test(line)) { flushPara(); closeList(); i++; continue; }
    // 普通段落行
    para.push(escapeHtml(line));
    i++;
  }
  flushPara(); closeList();
  return html;
}

// ── 工具调用格式化（harness 风格：name(args)） ──
function _val2str(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try { return JSON.stringify(v); } catch { return String(v); }
}
function formatToolCall(name, args) {
  const a = args || {};
  const keys = Object.keys(a);
  const n = name || "工具";
  if (keys.length === 0) return n + "()";
  if (keys.length === 1) return n + "(" + _val2str(a[keys[0]]) + ")";
  return n + "(" + keys.map((k) => k + "=" + _val2str(a[k])).join(", ") + ")";
}
function formatArgsLine(args) {
  const a = args || {};
  const keys = Object.keys(a);
  if (keys.length === 0) return "(无参数)";
  return keys.map((k) => k + "=" + _val2str(a[k])).join(", ");
}

// ── 渲染：会话标签 ──────────────────────────────────────────
function renderSessions() {
  const wrap = $("#sessions");
  wrap.replaceChildren();
  for (const s of state.sessions.values()) {
    const tab = document.createElement("div");
    tab.className = "session-tab" + (s.id === state.active ? " active" : "");
    tab.dataset.id = s.id;
    const name = document.createElement("span");
    name.textContent = s.name;
    const close = document.createElement("span");
    close.className = "close";
    close.textContent = "×";
    close.addEventListener("click", (e) => { e.stopPropagation(); removeSession(s.id); });
    tab.appendChild(name);
    tab.appendChild(close);
    tab.addEventListener("click", () => switchSession(s.id));
    wrap.appendChild(tab);
  }
}

// ── 渲染：消息 + Loop 可视化 ───────────────────────────────
function current() {
  return state.sessions.get(state.active);
}

function renderMessages() {
  const s = current();
  const box = $("#messages");
  box.replaceChildren();

  if (!s || s.messages.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    const logo = document.createElement("div");
    logo.className = "logo"; logo.textContent = "▲";
    const t1 = document.createElement("div");
    t1.textContent = "极光 Agent OS";
    const w1 = document.createElement("div");
    w1.className = "why"; w1.textContent = "多窗口独立会话 · Loop 可视化 · 工具 Trace 审计";
    const w2 = document.createElement("div");
    w2.className = "why"; w2.textContent = '输入"查一下北京天气，并记一条待办"试试连环工具调用';
    empty.append(logo, t1, w1, w2);
    box.appendChild(empty);
    return;
  }

  // Loop 可视化：扁平时间线（harness/ReAct 风格，工具调用是主角，轮次只做轻分隔）
  for (const m of s.messages) {
    if (m.kind === "chat") {
      const el = document.createElement("div");
      el.className = "msg " + m.role;
      const label = document.createElement("div");
      label.className = "msg-label";
      label.textContent = m.role === "user" ? "你" : "极光";
      const bubble = document.createElement("div");
      bubble.className = "msg-bubble markdown";
      bubble.innerHTML = renderMarkdown(m.text);
      el.appendChild(label); el.appendChild(bubble);
      box.appendChild(el);
    } else if (m.kind === "timeline") {
      const kind = m.ev.kind;
      if (kind === "iteration") {
        // 安静的轮次分隔线（不再用「迭代 N · 处理中…」这种令人困惑的闪烁头）
        const sep = document.createElement("div");
        sep.className = "loop-round";
        const n = (m.ev.payload && m.ev.payload.iteration) || 1;
        sep.innerHTML = '<span class="lr-dot"></span>第 ' + n + ' 轮 · Agent Loop';
        box.appendChild(sep);
      } else {
        const card = buildLoopCard(m.ev);
        if (card) box.appendChild(card);
      }
    }
  }
  box.scrollTop = box.scrollHeight;
}

function buildLoopCard(ev) {
  const kind = ev.kind;
  const card = document.createElement("div");

  if (kind === "thinking") {
    const done = ev.done;
    card.className = "lc-think" + (done ? " done" : " active");
    const head = document.createElement("div");
    head.className = "lc-think-head";
    if (done) {
      head.textContent = "🧠 已思考";
      head.addEventListener("click", () => { ev.expanded = !ev.expanded; renderMessages(); });
    } else {
      const d = document.createElement("span");
      d.className = "dots";
      head.textContent = "思考中 ";
      head.appendChild(d);
    }
    card.appendChild(head);
    if (!done || ev.expanded) {
      const body = document.createElement("div");
      body.className = "lc-text lc-think-body";
      body.textContent = ev.payload.text || "";
      card.appendChild(body);
    }
  } else if (kind === "tool_call") {
    const done = ev.done;
    const result = ev.result || {};
    card.className = "lc-tool" + (done ? " done" : " active");
    const head = document.createElement("div");
    head.className = "lc-tool-head";
    const mark = document.createElement("span");
    mark.className = "lc-tool-badge " + (done ? (result.ok ? "ok" : "err") : "run");
    mark.textContent = done ? (result.ok ? "✓" : "✗") : "⟳";
    const call = document.createElement("span");
    call.className = "lc-tool-call";
    call.textContent = formatToolCall(ev.payload.tool, ev.payload.arguments);
    head.append(mark, call);
    card.appendChild(head);
    // 完成后内联展示参数与结果（harness：调用过程一目了然，无需点击展开）
    if (done) {
      const detail = document.createElement("div");
      detail.className = "lc-tool-detail";
      const args = document.createElement("div");
      args.className = "lc-line";
      args.innerHTML = '<span class="lc-k">参数</span>' + escapeHtml(formatArgsLine(ev.payload.arguments));
      const res = document.createElement("div");
      res.className = "lc-line" + (result.ok ? "" : " err");
      res.innerHTML = '<span class="lc-k">结果</span>' + escapeHtml(result.text || (result.ok ? "" : "调用失败"));
      detail.append(args, res);
      card.appendChild(detail);
    }
  } else if (kind === "answer") {
    // 最终答复：作为正常消息气泡常驻
    card.className = "msg assistant final";
    const label = document.createElement("div");
    label.className = "msg-label"; label.textContent = "极光";
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble markdown";
    bubble.innerHTML = renderMarkdown(ev.payload.text || "");
    card.append(label, bubble);
  } else if (kind === "error") {
    card.className = "loop-card error err";
    const title = document.createElement("div");
    title.className = "lc-title"; title.textContent = "⚠ 模型错误";
    const body = document.createElement("div");
    body.className = "lc-text";
    const err = ev.payload.error || ev.payload.message || "";
    const code = ev.payload.code ? `（${ev.payload.code}）` : "";
    body.textContent = `${err}${code}`;
    card.append(title, body);
    // 当真实模型不可用（配额耗尽/鉴权失败）时，给一个一键切回 Mock 的逃生通道
    const isLLM = (ev.payload.code || "").toString().startsWith("llm");
    if (isLLM) {
      const btn = document.createElement("button");
      btn.className = "loop-err-btn";
      btn.textContent = "切回 Mock 演示模式";
      btn.style.marginTop = "8px";
      btn.style.cursor = "pointer";
      btn.onclick = async () => {
        btn.disabled = true; btn.textContent = "切换中…";
        try {
          const st = await call("llm.use_mock", {});
          await loadModelStatus();
          renderModelBadge(st);
          setStatus("已切回 Mock 演示模式，可继续体验 UI。", true);
          card.remove();
        } catch (e) {
          btn.disabled = false; btn.textContent = "切回 Mock 演示模式";
          setStatus("切换失败：" + e.message, false);
        }
      };
      card.append(btn);
      // 另一条路：去设置面板换一个有额度的 key / 换 provider
      const cfgBtn = document.createElement("button");
      cfgBtn.className = "loop-err-btn";
      cfgBtn.textContent = "去配置真实模型";
      cfgBtn.style.marginTop = "8px";
      cfgBtn.style.marginLeft = "8px";
      cfgBtn.style.cursor = "pointer";
      cfgBtn.onclick = () => openSettings();
      card.append(cfgBtn);
    }
  } else {
    return null;
  }
  return card;
}

// ── Trace 渲染 ─────────────────────────────────────────────
function renderTrace() {
  const s = current();
  const list = $("#traceList");
  $("#traceCount").textContent = s ? s.traces.length : 0;
  list.innerHTML = "";
  if (!s || s.traces.length === 0) {
    list.innerHTML = `<div class="trace-hint">暂无工具调用。发送一条带工具的指令即可看到 Trace 记录。</div>`;
    return;
  }
  for (const t of s.traces) {
    const el = document.createElement("div");
    el.className = "trace-item " + t.cls;
    const kw = document.createElement("div");
    kw.className = "kw " + (t.cls === "" ? "cal" : "");
    kw.textContent = t.tool;
    const time = document.createElement("span");
    time.className = "tr-time";
    time.textContent = " " + new Date(t.ts * 1000).toLocaleTimeString();
    kw.appendChild(time);
    if (t.badge) {
      const b = document.createElement("span");
      b.className = "tr-badge " + t.badge.cls;
      b.textContent = t.badge.text;
      kw.appendChild(b);
    }
    el.appendChild(kw);
    const args = document.createElement("div");
    args.className = "tr-args";
    args.textContent = JSON.stringify(t.args);
    el.appendChild(args);
    list.appendChild(el);
  }
  list.scrollTop = list.scrollHeight;
}

// ── 会话操作 ───────────────────────────────────────────────
async function createSession(name) {
  const value = await call("session.create", { name: name || "窗口" });
  state.sessions.set(value.session_id, {
    id: value.session_id, name: value.name, messages: [], traces: [],
  });
  state.active = value.session_id;
  renderSessions();
  renderMessages();
  renderTrace();
}

// 刷新页面时复用后端已有的 session，而不是无脑新建。
// 解决「刷新一次窗口序号 +1」的 bug：旧 init 调 createSession，
// 后端 in-memory 还留着上次创建的"窗口"，_unique_name 就找下一个 #N。
async function loadSessions() {
  try {
    const r = await call("session.list", {});
    for (const s of (r.sessions || [])) {
      state.sessions.set(s.session_id, {
        id: s.session_id, name: s.name, messages: [], traces: [],
      });
    }
    if (state.sessions.size > 0) {
      state.active = state.sessions.keys().next().value;
    }
  } catch (e) {
    setStatus("加载会话失败：" + e.message, false);
  }
  renderSessions();
  renderMessages();
  renderTrace();
}

async function removeSession(id) {
  await call("session.remove", { session_id: id });
  state.sessions.delete(id);
  if (state.active === id) {
    const first = state.sessions.keys().next();
    state.active = first.done ? null : first.value;
  }
  renderSessions();
  renderMessages();
  renderTrace();
}

function switchSession(id) {
  state.active = id;
  renderSessions();
  renderMessages();
  renderTrace();
}

// ── 发送与下行事件 ─────────────────────────────────────────
async function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text || state.busy) return;
  const s = current();
  if (!s) { await createSession(); }

  input.value = "";
  state.busy = true;
  $("#btnSend").disabled = true;

  const cur = current();
  cur.messages.push({ kind: "chat", role: "user", text });
  renderMessages();

  try {
    const value = await call("chat.send", { session_id: cur.id, text });
    // 协议层 ok，但 loop 可能返回 success=false（如 LLM 限流）
    if (value && value.success === false && value.error) {
      cur.messages.push({ kind: "chat", role: "assistant", text: "⚠ " + value.error });
      renderMessages();
    }
  } catch (e) {
    cur.messages.push({ kind: "chat", role: "assistant", text: "⚠ " + e.message });
    renderMessages();
  } finally {
    state.busy = false;
    $("#btnSend").disabled = false;
    renderTrace();
  }
}

function onServerFrame() {}

// WS 下行事件帧（第三象限 ServerRequest）
function handleDownlink(frame) {
  // frame: {type:'server-request', rpcId, method:'event.<kind>', payload:{session_id,payload,seq}}
  const kind = (frame.method || "").replace(/^event\./, "");
  const pl = frame.payload || {};
  const sid = pl.session_id;
  const s = state.sessions.get(sid);
  if (!s) return;
  const evKind = typeof pl.payload === "object" && pl.payload ? pl.payload.kind : kind;
  const evPayload = typeof pl.payload === "object" && pl.payload && pl.payload.payload
    ? pl.payload.payload : pl.payload;

  // Loop 事件 → 加入该 session 的消息流（作为 Loop 可视化时间线）
  const LOOP_KINDS = ["iteration", "thinking", "tool_call", "tool_result", "answer", "error", "done"];
  if (LOOP_KINDS.includes(kind)) {
    // 生命周期管理：下一个事件到达时，标记上一条 thinking/工具调用/迭代 为已完成
    if (kind === "iteration" || kind === "tool_call" || kind === "answer" || kind === "error" || kind === "done") {
      for (const m of s.messages) {
        if (m.kind === "timeline" && !m.ev.done) {
          const k = m.ev.kind;
          if (k === "thinking" || k === "tool_call" || k === "iteration") m.ev.done = true;
        }
      }
    }
    if (kind === "tool_result") {
      // 标记其对应的 tool_call 完成，并带上结果摘要
      for (let i = s.messages.length - 1; i >= 0; i--) {
        const m = s.messages[i];
        if (m.kind === "timeline" && m.ev.kind === "tool_call" && !m.ev.done) {
          m.ev.done = true;
          m.ev.result = { ok: evPayload.ok, text: evPayload.text || "" };
          break;
        }
      }
    }

    const norm = {
      kind,
      payload: evPayload || {},
      session_id: sid,
      seq: pl.seq || 0,
      done: false,
      expanded: false,
    };
    if (kind === "answer") norm.done = false;   // 结论常驻
    if (kind === "done") return;                // done 不渲染
    s.messages.push({ kind: "timeline", ev: norm });
    if (sid === state.active) renderMessages();

    // trace 面板同步
    if (kind === "tool_call") {
      s.traces.push({
        tool: evPayload.tool || "工具",
        args: evPayload.arguments || {},
        ts: Date.now() / 1000,
        cls: "",
      });
    } else if (kind === "tool_result") {
      const last = s.traces.find((t) => t.tool === (evPayload.tool || "") && !t.badge);
      if (last) {
        last.badge = evPayload.ok
          ? { text: "成功", cls: "ok" }
          : { text: "失败", cls: "err" };
        last.cls = evPayload.ok ? "" : "err";
      } else {
        s.traces.push({
          tool: evPayload.tool || "工具",
          args: { ok: evPayload.ok },
          ts: Date.now() / 1000,
          cls: evPayload.ok ? "" : "err",
          badge: evPayload.ok ? { text: "成功", cls: "ok" } : { text: "失败", cls: "err" },
        });
      }
    }
    if (sid === state.active) renderTrace();
  } else if (kind === "session.created" || kind === "session.removed") {
    // 标签由上游 renderSessions 管理
  }
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.onmessage = (e) => {
    try { handleDownlink(JSON.parse(e.data)); } catch (_) { /* 忽略坏帧 */ }
  };
  state.ws.onclose = () => setTimeout(connectWS, 1000);
}

// ── 模型设置（齿轮） ───────────────────────────────────────
const PROVIDER_DEFAULTS = {
  deepseek: { base: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  openai: { base: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  anthropic: { base: "https://api.deepseek.com/anthropic", model: "deepseek-chat" },
  openai_compatible: { base: "", model: "" },
};

function setStatus(text, ok) {
  const el = $("#cfgStatus");
  el.textContent = text;
  el.className = "cfg-status " + (ok ? "ok" : ok === false ? "err" : "");
}

async function loadModelStatus() {
  try {
    const st = await call("llm.status", {});
    if (!st.configured) {
      setStatus("尚未配置模型，将使用 mock 演示模式。");
      return st;
    }
    // 填入当前配置到表单（不回显完整 key）
    $("#cfgProvider").value = st.provider || "deepseek";
    $("#cfgBase").value = st.api_base || "";
    $("#cfgModel").value = st.model || "";
    $("#cfgTemp").value = st.temperature != null ? st.temperature : 0.3;
    // 回显 key：后端只给掩码（安全），回填进输入框并标记为"未修改"
    // 用户不动它 → 保存时沿用原 key；一旦编辑 → 视为新 key
    if (st.api_key_masked) {
      $("#cfgKey").value = st.api_key_masked;
      state.keyIsMask = true;
    } else {
      $("#cfgKey").value = "";
      state.keyIsMask = false;
    }
    if (st.mode === "mock") {
      setStatus("当前为 mock 演示模式（无真实模型）。配好下方信息可切换真实模型。");
    } else {
      setStatus(`已连接真实模型：${st.model}（${st.api_base}）· Key 已保存，留空即沿用`, true);
    }
    renderModelBadge(st);
    return st;
  } catch (e) {
    setStatus("读取状态失败：" + e.message, false);
    return null;
  }
}

function renderModelBadge(st) {
  let badge = $("#modelBadge");
  if (!badge) {
    badge = document.createElement("span");
    badge.id = "modelBadge";
    badge.className = "model-badge";
    $("#btnSettings").parentElement.insertBefore(badge, $("#btnSettings"));
  }
  if (!st || !st.configured) badge.remove();
  else if (st.mode === "mock") {
    badge.textContent = "● 演示模式";
    badge.style.color = "var(--text-muted)";
  } else {
    badge.textContent = `● ${st.model}`;
    badge.style.color = "var(--success)";  // 真实模型：绿点
    badge.style.borderColor = "rgba(74,222,128,0.4)";
  }
}

function openSettings() {
  $("#settingsModal").hidden = false;
  loadModelStatus();
}

function closeSettings() {
  $("#settingsModal").hidden = true;
}

async function testConnection() {
  setStatus("测试中…");
  const btn = $("#btnTestModel");
  btn.disabled = true;
  try {
    const t = await call("llm.test", {});
    if (t.ok) setStatus(t.mode === "mock" ? t.message : `连接成功 ✓ ${t.latency_ms}ms · ${t.message}`, true);
    else setStatus("连接失败：" + (t.error || "未知错误"), false);
  } catch (e) {
    setStatus("测试失败：" + e.message, false);
  } finally {
    btn.disabled = false;
  }
}

async function saveModelConfig() {
  setStatus("保存中…");
  const rawKey = $("#cfgKey").value.trim();
  // 留空 / 仍是后端掩码 = 不改 key，交给后端沿用已保存的那把
  const keepKey = !rawKey || state.keyIsMask;

  const payload = {
    provider: $("#cfgProvider").value,
    api_base: $("#cfgBase").value.trim(),
    model: $("#cfgModel").value.trim(),
    temperature: parseFloat($("#cfgTemp").value) || 0.3,
  };
  if (!keepKey) payload.api_key = rawKey;

  if (!payload.model) {
    setStatus("模型名必填。", false);
    return;
  }
  if (!keepKey && !rawKey) {
    setStatus("首次配置需填写 API Key。", false);
    return;
  }
  try {
    const st = await call("llm.configure", payload);
    // 回填掩码而非清空 —— 让用户看得见"已保存"，且下次可只改其他项
    if (st.api_key_masked) {
      $("#cfgKey").value = st.api_key_masked;
      state.keyIsMask = true;
    }
    setStatus(`已保存并启用：${st.model} @ ${st.api_base} ✓（Key 已保存，留空即沿用，更换请重新粘贴）`, true);
    renderModelBadge(st);
  } catch (e) {
    setStatus("保存失败：" + e.message, false);
  }
}

// ── 初始化 ─────────────────────────────────────────────────
function init() {
  $("#btnNewSession").addEventListener("click", () => createSession());
  $("#btnSend").addEventListener("click", send);
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });

  // 模型设置
  $("#btnSettings").addEventListener("click", openSettings);
  $("#btnCloseSettings").addEventListener("click", closeSettings);
  $("#settingsModal").addEventListener("click", (e) => {
    if (e.target === $("#settingsModal")) closeSettings();
  });
  $("#btnTestModel").addEventListener("click", testConnection);
  $("#btnSaveModel").addEventListener("click", saveModelConfig);
  $("#cfgProvider").addEventListener("change", (e) => {
    const d = PROVIDER_DEFAULTS[e.target.value] || {};
    if (d.base) $("#cfgBase").value = d.base;
    if (d.model) $("#cfgModel").value = d.model;
  });
  // 用户一旦编辑 key 框，就不再是"掩码/未修改"状态
  $("#cfgKey").addEventListener("input", () => { state.keyIsMask = false; });

  connectWS();
  loadModelStatus();
  // 先 list 已有 session：刷新时复用，避免「刷新一次窗口序号 +1」
  (async () => {
    await loadSessions();
    if (state.sessions.size === 0) {
      await createSession();                 // 全空才建第一个
    }
    $("#input").focus();
  })();
}

init();