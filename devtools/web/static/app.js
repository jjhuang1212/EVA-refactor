"use strict";
// EVA测评系统。纯原生 JS，不用 innerHTML 拼模型输出，防止回复里的标签被当成页面。

const $app = document.getElementById("app");
const BASE_TITLE = document.title;

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  el.append(...kids.flat(Infinity).filter((k) => k != null && k !== false));
  return el;
}

class ApiError extends Error {
  constructor(status, body) {
    const e = (body && body.error) || {};
    super(e.message || `请求失败 ${status}`);
    this.status = status;
    this.code = e.code;
    this.details = e.details || {};
  }
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.form) init.body = opts.form; // 表单自带 multipart 边界，不能手设 Content-Type
  if (opts.json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, init);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && path !== "/api/login") showLogin();
    throw new ApiError(res.status, body);
  }
  return body;
}

// 只有键盘操作（Tab）时才显示焦点框，鼠标点击不留框
addEventListener("keydown", (e) => { if (e.key === "Tab") document.documentElement.classList.add("kbd"); }, true);
addEventListener("pointerdown", () => document.documentElement.classList.remove("kbd"), true);

// 线条图标：路径是固定常量，不含任何外部输入，所以可以用 innerHTML
const ICON_PATH = { plus: "M8 3v10M3 8h10", up: "M8 13V3M4 7l4-4 4 4", chev: "M4 6l4 4 4-4", arrow: "M3 8h10M9 4l4 4-4 4" };
const icon = (name) => {
  const el = h("span", { class: "ic", "aria-hidden": "true" });
  el.innerHTML = `<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="${ICON_PATH[name]}"/></svg>`;
  return el;
};
// 状态点加文字：绿通过、红不通过、黄进行中、空心未标
const state = (kind, text) => h("span", { class: `state ${kind}` }, text);

// 错误码和消息原样显示，不换成笼统的"出错了"
function errorBanner(err) {
  const code = err.code ? `${err.code} ` : "";
  const extra = err.details && Object.keys(err.details).length ? JSON.stringify(err.details, null, 1) : "";
  return h("div", { class: "banner", role: "alert" }, h("b", {}, code), err.message, extra && h("pre", {}, extra));
}

const hint = (...text) => h("p", { class: "muted hint" }, ...text);
const q = encodeURIComponent;
const clock = (iso) => (iso ? String(iso).slice(11, 19) : "");
const day = (iso) => (iso ? String(iso).slice(0, 16).replace("T", " ") : "");
const yuan = (v) => (v == null ? "无" : `¥${v}`);

// ---------- 外壳与路由 ----------
const PAGES = [["sessions", "会话"], ["eval", "评测"], ["results", "结果"], ["compare", "对比"], ["versions", "版本"], ["files", "文件"], ["help", "帮助"]];
let jobStream = null;  // 评测进度的 SSE 连接，换页/结束后要关

function shell(active, content) {
  const toggle = h("button", { type: "button", class: "theme", onclick: () => { setTheme(isDark() ? "light" : "dark"); label(); } });
  toggle.innerHTML = ICONS; // 固定的内联图标，不含任何外部内容；显示哪个由 CSS 按当前主题决定
  const label = () => { const t = isDark() ? "切换到浅色" : "切换到深色"; toggle.title = t; toggle.setAttribute("aria-label", t); };
  label();
  const logout = h("button", { type: "button", onclick: async () => { await api("/api/logout", { method: "POST" }); showLogin(); } }, "退出");
  $app.replaceChildren(h("div", { class: "shell" },
    h("nav", { class: "nav", "aria-label": "主导航" },
      h("div", { class: "brand" }, h("b", {}, "EVA"), "测评系统"),
      PAGES.map(([id, label]) => h("a", { href: `#/${id}`, "aria-current": id === active ? "page" : null }, label)),
      h("div", { class: "spacer" }), toggle, logout),
    h("main", { id: "main-content" }, content)));
}

// 没手动选过时跟随系统，所以要看系统偏好才知道现在是不是深色
const isDark = () => (document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
const SVG = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';
const ICONS = `${SVG} class="sun"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`
  + `${SVG} class="moon"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>`;

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch (_) { /* 无痕窗口等读写不了，不影响使用 */ }
}
try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch (_) { /* 同上 */ }

function showLogin() {
  const pw = h("input", { type: "password", id: "pw", autocomplete: "current-password", "aria-label": "密码" });
  const msg = h("div");
  const submit = async (e) => {
    e.preventDefault();
    try {
      await api("/api/login", { method: "POST", json: { password: pw.value } });
      route();
    } catch (err) {
      const wait = err.details.retry_after_seconds;
      msg.replaceChildren(errorBanner(wait ? Object.assign(err, { message: `${err.message}，${wait} 秒后再试` }) : err));
      pw.select();
    }
  };
  $app.replaceChildren(h("form", { class: "login", onsubmit: submit },
    h("h1", {}, "EVA测评系统"), h("label", { for: "pw", class: "muted" }, "登录密码"), pw,
    h("button", { class: "btn primary", type: "submit" }, "登录"), msg));
  pw.focus();
}

// 分页：page 放在 URL query（和回放的 order/full 同机制），点页码改 query 后重新渲染当前路由
const curPage = () => Math.max(1, Number(new URLSearchParams(location.search).get("page")) || 1);
function pager(page, hasMore) {
  if (page <= 1 && !hasMore) return null;
  const go = (p) => {
    const u = new URL(location.href);
    u.searchParams.set("page", p);
    history.replaceState(null, "", u);
    route();
  };
  return h("div", { class: "pager" },
    h("button", { class: "btn", type: "button", disabled: page <= 1, onclick: () => go(page - 1) }, "上一页"),
    h("span", { class: "muted" }, `第 ${page} 页`),
    h("button", { class: "btn", type: "button", disabled: !hasMore, onclick: () => go(page + 1) }, "下一页"));
}

const routes = [];
const on = (re, fn) => routes.push([re, fn]);

async function route() {
  if (jobStream) { jobStream.close(); jobStream = null; }  // 离开页面时断开进度推送
  document.title = BASE_TITLE;
  const hash = location.hash.replace(/^#/, "") || "/sessions";
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      try { await fn(...m.slice(1).map((x) => (x === undefined ? x : decodeURIComponent(x)))); } catch (err) {
        if (!(err instanceof ApiError && err.status === 401)) shell(null, errorBanner(err));
      }
      return;
    }
  }
  shell(null, h("p", { class: "empty" }, `没有这个页面：${hash}`));
}
window.addEventListener("hashchange", route);

// ---------- 通用小件 ----------
const crumbs = (...parts) => h("div", { class: "crumbs" }, parts.flatMap((p, i) => (i ? [h("span", {}, "/"), p] : [p])));

function table(head, rows, numeric = []) {
  const th = (t, i) => (Array.isArray(t) ? h("th", { class: numeric.includes(i) ? "num" : null, title: t[1] }, t[0]) : h("th", { class: numeric.includes(i) ? "num" : null }, t));  // [名称, 说明] 的列头带悬停说明
  return h("div", { class: "scroll" }, h("table", {},
    h("thead", {}, h("tr", {}, head.map(th))),
    h("tbody", {}, rows)));
}

function linkRow(href, cells, numeric = []) {
  const go = () => { location.hash = href; };
  return h("tr", { class: "link", tabindex: "0", onclick: go, onkeydown: (e) => { if (e.key === "Enter") go(); } },
    cells.map((c, i) => h("td", { class: numeric.includes(i) ? "num mono" : null }, c)));
}

// ---------- 会话列表 ----------
// 租户编号以用途开头；除评测外的租户在这里只读，评测的运行、标注和认可版本只写在 eval_ 租户上
const REALMS = [["", "全部用途"], ["usr_", "真实用户"], ["sim_", "模拟器"], ["dev_", "终端"], ["eval_", "评测"]];
const realmName = (tenant) => (REALMS.find(([p]) => p && tenant.startsWith(p)) || [0, "其他"])[1];
const readonlyTag = (tenant) => (tenant.startsWith("eval_") ? [] : h("span", { class: "tag warn", title: "真实用户、模拟器、终端的租户只能查看，不能在这里改动" }, "只读"));

on(/^\/sessions$/, async () => {
  const realm = h("select", { "aria-label": "按用途筛选" }, REALMS.map(([prefix, name]) => h("option", { value: prefix }, name)));
  const input = h("input", { type: "search", class: "grow", placeholder: "搜索租户 ID、渠道、客服号、用户 ID", "aria-label": "搜索租户" });
  const list = h("div");
  const dash = (v) => (v ? h("span", { class: "mono" }, v) : h("span", { class: "muted" }, "—"));
  let seq = 0;
  const load = async () => {
    const mine = ++seq;
    const page = curPage();
    const r = await api(`/api/tenants?prefix=${q(realm.value)}&search=${q(input.value.trim())}&page=${page}`);
    if (mine !== seq) return;  // 输入很快时，先发出的请求后到，丢掉
    const filtered = realm.value || input.value.trim();
    list.replaceChildren(
      r.items.length ? table(["租户", ["用途", "租户的类型：真实用户、模拟器、终端、评测"], ["渠道", "这个用户从哪个渠道来：企微 wecom、终端 console"],
        ["客服号", "企微的客服号；同一个用户在不同客服号下是两个租户。其他渠道没有"], ["用户 ID", "这个用户在渠道里的 ID"], "创建时间"], r.items.map((t) =>
        linkRow(`#/sessions/${q(t.tenant_id)}`, [h("span", { class: "mono" }, t.tenant_id), realmName(t.tenant_id), dash(t.channel), dash(t.scope), dash(t.external_id), day(t.created_at)])))
        : h("p", { class: "empty" }, filtered ? "没有符合的租户" : "没有租户"),
      pager(page, r.has_more));
  };
  const show = (err) => list.replaceChildren(errorBanner(err));
  const resetPage = () => { const u = new URL(location.href); u.searchParams.delete("page"); history.replaceState(null, "", u); };
  let timer;
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { resetPage(); load().catch(show); }, 300); });
  realm.addEventListener("change", () => { resetPage(); load().catch(show); });
  shell("sessions", [h("h1", {}, "会话"), hint("选一个租户，再进它的会话回放对话。评测租户可以写（跑评测、标注），其他只读。"),
    card("租户", {}, h("div", { class: "row" }, realm, input), list)]);
  await load();
});

on(/^\/sessions\/([^/]+)$/, async (tenant) => {
  const page = curPage();
  const r = await api(`/api/sessions?tenant=${q(tenant)}&page=${page}`);
  shell("sessions", [h("h1", {}, "会话"), hint("点一行进入回放，看模型每一步做了什么。"),
    crumbs(h("a", { href: "#/sessions" }, "租户"), h("span", { class: "mono" }, tenant), realmName(tenant), readonlyTag(tenant)),
    card("会话", {}, r.items.length ? table(["会话", "渠道", "最近活动", "已结算费用"], r.items.map((s) =>
      linkRow(`#/replay/${q(tenant)}/${q(s.session_id)}`, [h("span", { class: "mono" }, s.session_id), s.channel, day(s.last_active_at), yuan(s.cost_cny)]), [3]))
      : h("p", { class: "empty" }, "这个租户下没有会话"),
    pager(page, r.has_more))]);
});

// ---------- 回放 ----------
const PREVIEW = 400;
const TOOL_PREVIEW = 240; // 工具结果不是人工要逐字看的，默认只露一小段
const KEY_PREVIEW = 1500; // 用户消息、最终回复这类要人工看的正文，多给一些字数
const asText = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 1));

function clip(value, full, limit = PREVIEW) {
  const text = value == null ? "" : asText(value);
  if (full || text.length <= limit) return h("div", { class: "body" }, text);
  const box = h("div", { class: "body" });
  const show = (open) => {
    const label = open ? "收起" : `展开（共 ${text.length} 字）`;
    box.replaceChildren(open ? text : text.slice(0, limit) + "…", " ", h("button", { class: "btn", type: "button", onclick: () => {
      show(!open);
      // 收起后正文变短，顶部已滚出视口时回到它，免得停在无关的下方内容
      if (open && box.getBoundingClientRect().top < 0) box.scrollIntoView({ block: "start" });
    } }, label));
  };
  show(false);
  return box;
}

const sha8 = (s) => (s ? s.slice(0, 8) : "无");

const fmtDur = (ms) => {
  if (!Number.isFinite(ms) || ms < 0) return null;
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(1)} 秒`;
  if (s < 60) return `${Math.round(s)} 秒`;
  const m = Math.floor(s / 60);
  return `${m} 分 ${Math.round(s - m * 60)} 秒`;
};

const oneLine = (t, n) => {
  const s = String(t || "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n) + "…" : s;
};

// 回放里要人工关注的节点：用户消息、被闸门拦下的回答、最终回复、向用户的提问。
// 闸门拦截的记录挂在它前面最近一条纯文字回复上；回合以 completed 结束时，最后一条没被拦的文字回复就是最终回复
function replayPlan(items) {
  const info = new Map(), moments = [];
  const textOf = (i) => String((i.payload || {}).content || "").trim();
  const isReply = (i) => i.kind === "assistant_message" && !((i.payload || {}).tool_calls || []).length && textOf(i) !== "";
  const closeTurn = (seg, end) => {
    let last = null;
    for (const i of seg) {
      const p = i.payload || {};
      if (isReply(i)) last = i;
      else if (i.kind === "hook" && p.phase === "result" && p.point === "stop" && p.block && last) {
        const rec = info.get(last) || {};
        rec.blocked = [...(rec.blocked || []), { hook: p.hook, reason: p.reason }];
        info.set(last, rec);
        info.set(i, { attached: true });
      } else if (i.kind === "user_message" && p.source === "stop_hook") info.set(i, { feedback: true });
    }
    const start = seg.find((i) => i.kind === "turn_start");
    if (end && start) info.set(end, { ms: new Date(end.at) - new Date(start.at) });
    const reason = end && (end.payload || {}).reason;
    const replies = seg.filter(isReply), closing = replies[replies.length - 1];
    if (closing && reason === "completed" && !(info.get(closing) || {}).blocked) info.set(closing, { final: true });
    if (closing && reason === "gate_failed" && (info.get(closing) || {}).blocked) info.get(closing).exhausted = true;
    for (const i of seg) {
      const p = i.payload || {}, m = info.get(i) || {};
      if (i.kind === "user_message" && !m.feedback && (p.source || "user") === "user") moments.push({ kind: "user", item: i, text: p.text });
      else if (m.blocked) moments.push({ kind: "blocked", item: i, text: textOf(i), reason: m.blocked.map((b) => b.reason).join("；") });
      else if (m.final) moments.push({ kind: "final", item: i, text: textOf(i) });
      else if (i.kind === "assistant_message") {
        const ask = (p.tool_calls || []).find((c) => c.name === "ask_user");
        if (ask) {
          let question = ask.arguments;
          try { question = JSON.parse(ask.arguments).question ?? question; } catch (e) { /* 参数不是合法 JSON 时直接显示原文，不隐藏 */ }
          moments.push({ kind: "ask", item: i, text: String(question) });
        }
      }
    }
  };
  let seg = [];
  for (const i of items) {
    seg.push(i);
    if (i.kind === "turn_end") { closeTurn(seg, i); seg = []; }
  }
  if (seg.length) closeTurn(seg, null);
  return { info, moments };
}

const BILLING_ZH = { tool_call: "工具计费", asr: "转写计费" };
const MOMENT_LABEL = { user: "用户", blocked: "闸门", final: "回复", ask: "提问" };
function momentsCard(moments, desc) {
  if (!moments.length) return null;
  const shown = desc ? [...moments].reverse() : moments;
  const jump = (item) => () => {
    const el = document.getElementById(`ev-${item.seq}`);
    if (!el) return;
    const turn = el.closest(".turn");
    if (turn && turn.classList.contains("folded")) {
      turn.classList.remove("folded");
      const head = turn.querySelector(".head");
      if (head) head.setAttribute("aria-expanded", "true");
    }
    el.scrollIntoView({ block: "center" });
  };
  const count = (kind) => moments.filter((m) => m.kind === kind).length;
  const kinds = ["user", "blocked", "final", ...(count("ask") ? ["ask"] : [])];
  return h("details", { class: "card fold" },
    h("summary", {}, h("span", { class: "chev", "aria-hidden": "true" }, icon("chev")), h("h2", {}, "关键节点"),
      kinds.map((k) => h("span", { class: `mtag ${k}${count(k) ? "" : " zero"}` }, `${MOMENT_LABEL[k]} ${count(k)}`))),
      h("div", { class: "cbody" }, h("p", { class: "muted" }, desc ? "按时间倒序列出（最新的在最上）。点一行跳到下面对应的位置。" : "按时间列出用户消息、被闸门拦下的回答、最终回复和向用户的提问。点一行跳到下面对应的位置。"),
      h("div", { class: "moments" }, shown.map((m) => h("button", { class: "moment", type: "button", onclick: jump(m.item) },
        h("span", { class: `mtag ${m.kind}` }, MOMENT_LABEL[m.kind]),
        h("span", { class: "mtext" }, oneLine(m.text, 120), m.reason ? h("small", {}, `闸门：${oneLine(m.reason, 120)}`) : null),
        h("span", { class: "mat mono" }, clock(m.item.at)))))));
}

function replayView(tl, full, desc) {
  const plan = replayPlan(tl.items);
  const toolNames = new Map();  // 调用编号 → 工具名，结果行靠它标出是哪个工具的结果
  for (const i of tl.items) if (i.kind === "assistant_message") for (const c of (i.payload || {}).tool_calls || []) toolNames.set(c.id, c.name);
  const prev = { system_sha: null, tools_sha: null };
  const meta = (call) => {
    const rec = call.record;
    if (rec === null && !call.expects_record) return { plain: [h("div", { class: "meta" }, "不经模型调用层，没有调用记录")], flags: [] };
    if (rec === null) return { plain: [], flags: [h("div", { class: "meta flag" }, "记录缺失：这次调用没有调用记录（开关没开，或写入前进程已退出）")] };
    const flags = [], lines = [h("div", { class: "meta" },
      `thinking ${rec.thinking ? "开" : "关"}  输出上限 ${rec.max_output_tokens}  代码版本 ${rec.code_version}  尝试 ${rec.idem_key.split(":").pop()}  system ${sha8(rec.system_sha)}  tools ${sha8(rec.tools_sha)}`)];
    for (const [key, label] of [["system_sha", "system prompt"], ["tools_sha", "tools"]]) {
      if (prev[key] !== null && prev[key] !== rec[key]) flags.push(h("div", { class: "meta flag" }, `${label} 和上一次调用不同：${sha8(prev[key])} → ${sha8(rec[key])}`));
      prev[key] = rec[key];
    }
    return { plain: lines, flags };
  };
  const toolCalls = (calls) => (calls || []).map((c) => h("div", { class: "call" },
    h("span", { class: "name" }, icon("arrow"), c.name), " ", h("span", { class: "meta" }, String(c.id).slice(0, 8)), clip(c.arguments, full)));
  const reasoning = (text, note) => text && h("details", {}, h("summary", {}, `思考（${text.length} 字${note || ""}）`), h("pre", {}, text));
  const ev = (kind, at, seq, ...body) => h("div", { class: `ev ${kind}`, id: seq != null ? `ev-${seq}` : null }, h("div", { class: "at" }, clock(at), seq != null ? ` #${seq}` : ""), h("div", {}, body));

  const modelCall = (i) => {
    const c = i.call, rec = c.record;
    const billing = c.tokens_in == null && c.tokens_out == null;  // 没有 token：这是付费工具（如企查查）的计费记录，不是模型调用
    const state = { settle: billing ? yuan(c.cost_cny) : `${yuan(c.cost_cny)}  in ${c.tokens_in} out ${c.tokens_out}`, release: "失败，预扣已释放" }[c.end] || "悬空：预扣没有结算也没有释放";
    const lost = c.purpose === "turn" && c.end === "settle" ? h("span", { class: "tag warn" }, "已结算但没写进日志") : null;
    const mm = meta(c), parts = [h("div", { class: "who" }, (billing ? `${BILLING_ZH[c.purpose] || "计费"} ${c.model}  ${state} ` : `模型调用 ${c.purpose}  ${c.model}  ${state} `), lost), mm.flags, mm.plain];
    if (rec) {
      if (c.purpose !== "turn" && rec.messages)
        parts.push(h("details", {}, h("summary", {}, `请求（${rec.messages.length} 条消息）`),
          rec.messages.map((m) => h("pre", {}, `[${m.role}] ${asText(m.content)}`))));
      if (rec.error) parts.push(h("pre", { class: "block" }, `失败：${asText(rec.error)}`));
      if (rec.reply) parts.push(reasoning(rec.reply.reasoning), clip(rec.reply.content, full), toolCalls(rec.reply.tool_calls));
    }
    return ev("mcall", i.at, null, parts);
  };

  const one = (i) => {
    const p = i.payload || {};
    switch (i.kind) {
      case "turn_start": return h("button", { class: "ev sep head", type: "button", "aria-expanded": "true", onclick: (e) => {
        const b = e.currentTarget, folded = b.parentElement.classList.toggle("folded");
        b.setAttribute("aria-expanded", String(!folded));
      } }, h("span", { class: "chev", "aria-hidden": "true" }, icon("chev")), `回合开始 ${clock(i.at)}`);
      case "turn_end": {
        const dur = fmtDur((plan.info.get(i) || {}).ms);
        return h("div", { class: "ev sep" }, h("div", {}, `回合结束 ${clock(i.at)}  ${p.reason}  步数 ${p.steps}${dur ? `  耗时 ${dur}` : ""}  ${yuan(p.cost_cny)}${p.code ? "  " + p.code : ""}`));
      }
      case "user_message": {
        if (p.source === "stop_hook") return ev("feedback", i.at, i.seq, h("details", {}, h("summary", {}, "闸门把理由发回给模型（系统消息，不是用户说的）"), h("pre", {}, asText(p.text))));
        const own = (p.source || "user") === "user";
        return ev(own ? "user key" : "user", i.at, i.seq, h("div", { class: "who" }, own ? "用户" : `用户消息（来源 ${p.source}）`), clip(p.text, full, KEY_PREVIEW));
      }
      case "assistant_message": {
        const call = i.call, rec = call && call.record, m = plan.info.get(i) || {};
        const full_r = rec && rec.reply && rec.reply.reasoning;
        const shown = full_r || (i.transcript_reasoning || {}).text;
        const mm = call ? meta(call) : { plain: [], flags: [h("div", { class: "meta flag" }, "账本里找不到对应的调用")] };
        const line = `模型 ${p.model}  in ${p.tokens_in} out ${p.tokens_out}  ${yuan(p.cost_cny)}  ${p.seconds != null ? `模型耗时 ${p.seconds}s  ` : ""}${p.finish_reason}`;
        const keyed = Boolean(m.blocked || m.final);
        const gate = m.blocked ? h("div", { class: "gate" }, m.blocked.map((b) => h("div", {}, h("b", {}, `闸门 ${b.hook}`), "：", b.reason)),
          m.exhausted ? h("div", {}, "重试次数用完，这个回合以 gate_failed 结束，这个回答没有交付给用户") : null) : null;
        const detail = keyed || mm.plain.length ? h("details", { class: "calldetail" }, h("summary", {}, "调用详情"), keyed ? h("div", { class: "meta" }, line) : null, mm.plain) : null;
        return ev(m.blocked ? "asst key blocked" : m.final ? "asst key final" : "asst", i.at, i.seq,
          h("div", { class: "who" }, m.blocked ? "被闸门拦下的回答" : m.final ? "最终回复" : line), mm.flags,
          reasoning(shown, full_r ? "" : "，日志里的截断版"), p.content ? clip(p.content, full, keyed ? KEY_PREVIEW : PREVIEW) : null, gate, toolCalls(p.tool_calls), detail);
      }
      case "tool_result": return ev("tool", i.at, i.seq,
        h("div", { class: "who" }, "结果 ", toolNames.has(p.call_id) ? h("b", { class: "tname" }, toolNames.get(p.call_id)) : null, ` ${String(p.call_id).slice(0, 8)} `, p.is_error ? h("span", { class: "tag err" }, "错误") : null),
        clip("content" in p ? p.content : p, full, TOOL_PREVIEW));
      case "tool_start": case "tool_call": return null; // 调用已在模型回复里列出
      case "hook":
        if ((plan.info.get(i) || {}).attached) return null; // 闸门拦截已经显示在被拦下的回答里
        if (p.phase === "result" && (p.decision !== "allow" || p.error))
          return ev("hook", i.at, i.seq, h("div", { class: "who" }, `hook ${p.hook}@${p.point} ${p.decision}`), clip(p.reason || p.error, full));
        return null; // 全放行的不显示
      case "model_call": return modelCall(i);
      case "error": return ev("err", i.at, i.seq, h("div", { class: "who" }, "错误事件"), clip(p, full));
      default: return ev("other", i.at, i.seq, h("div", { class: "who" }, i.kind), h("details", {}, h("summary", {}, "详情"), h("pre", {}, asText(p))));
    }
  };
  // 回合开始到回合结束之间的事件归入一个 section，折叠时只留首尾两行
  const nodes = [], allEvs = [], allTurns = [];
  const labelEvent = (el, i) => {
    if (!el) return el;
    const p = i.payload || {}, info = plan.info.get(i) || {};
    let fk;
    if (i.kind === "turn_start" || i.kind === "turn_end") fk = "sep";       // 结构行：始终显示，不参与筛选
    else if (i.kind === "user_message") fk = p.source === "stop_hook" ? "gate" : "user";
    else if (i.kind === "assistant_message") fk = info.blocked ? "asst gate" : "asst";
    else if (i.kind === "tool_result") fk = "tool";
    else if (i.kind === "error") fk = "error";
    else if (i.kind === "hook") fk = "gate";
    else if (i.kind === "model_call") fk = "call";
    else fk = "other";
    el.dataset.fk = fk;
    el.dataset.text = (el.textContent || "").toLowerCase();  // 用渲染后的全文做查找，连折叠的详情也能搜到
    allEvs.push(el);
    return el;
  };
  let turn = null;
  for (const i of tl.items) {
    const el = labelEvent(one(i), i);
    if (!el) continue;
    if (i.kind === "turn_start") { turn = h("section", { class: "turn" }, el); allTurns.push(turn); nodes.push(turn); continue; }
    (turn || { append: (x) => nodes.push(x) }).append(el);
    if (i.kind === "turn_end") turn = null;
  }
  if (desc) {
    // 回合级倒序：最新的整轮在最上面，只反转块顺序；每个回合内部仍是从提问到回复的正序。
    // 分析用的 plan 仍基于正序数据，耗时/最终回复/闸门判定都不受影响。
    nodes.reverse();
  }

  // G 查找与类型筛选 + H 全部折叠/展开。都在已渲染的节点上操作，与倒序无关。
  const searchIn = h("input", { type: "search", class: "grow", placeholder: "查找正文、工具、错误…", "aria-label": "查找回放内容" });
  const typeSel = h("select", { "aria-label": "按类型筛选" },
    h("option", { value: "all" }, "全部类型"), h("option", { value: "user" }, "用户消息"),
    h("option", { value: "asst" }, "模型回复"), h("option", { value: "tool" }, "工具结果"),
    h("option", { value: "gate" }, "闸门/拦截"), h("option", { value: "error" }, "错误"), h("option", { value: "call" }, "调用/计费"));
  const hitCount = h("span", { class: "muted" });
  const setFold = (fold) => {
    for (const sec of allTurns) {
      sec.classList.toggle("folded", fold);
      const head = sec.querySelector(":scope > .ev.head");
      if (head) head.setAttribute("aria-expanded", String(!fold));
    }
  };
  const applyFilter = () => {
    const kw = searchIn.value.trim().toLowerCase(), type = typeSel.value;
    const active = Boolean(kw) || type !== "all";
    let hits = 0;
    for (const el of allEvs) {
      if (el.dataset.fk === "sep") continue;
      const ok = (!kw || el.dataset.text.includes(kw)) && (type === "all" || el.dataset.fk.split(" ").includes(type));
      el.classList.toggle("filtered-out", active && !ok);
      if (active && ok) hits++;
    }
    for (const sec of allTurns) {
      const any = [...sec.querySelectorAll(".ev")].some((e) => e.dataset.fk && e.dataset.fk !== "sep" && !e.classList.contains("filtered-out"));
      sec.classList.toggle("turn-empty", active && !any);
      if (active && any && sec.classList.contains("folded")) {  // 命中的回合自动展开，否则看不到结果
        sec.classList.remove("folded");
        const head = sec.querySelector(":scope > .ev.head");
        if (head) head.setAttribute("aria-expanded", "true");
      }
    }
    hitCount.textContent = active ? `命中 ${hits} 条` : "";
  };
  searchIn.addEventListener("input", applyFilter);
  typeSel.addEventListener("change", applyFilter);
  const filterBar = h("div", { class: "filterbar" }, searchIn, typeSel, hitCount, h("span", { class: "spacer" }),
    h("button", { class: "btn", type: "button", onclick: () => setFold(true) }, "全部折叠"),
    h("button", { class: "btn", type: "button", onclick: () => setFold(false) }, "全部展开"));
  return [statsBar(tl.totals), momentsCard(plan.moments, desc), filterBar, h("div", { class: "tl" }, nodes), blobsView(tl.blobs)];
}

const topBtn = h("button", { class: "totop", type: "button", hidden: true, "aria-label": "回到顶部", title: "回到顶部", onclick: () => {
  scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
} }, icon("up"));
document.body.append(topBtn);
addEventListener("scroll", () => { topBtn.hidden = scrollY < 400; }, { passive: true });

// 工具调用：一个工具一个小标签，名称加次数，按次数从多到少
function toolsRow(tools) {
  const entries = Object.entries(tools || {}).sort((x, y) => y[1] - x[1]);
  if (!entries.length) return null;
  const total = entries.reduce((n, [, count]) => n + count, 0);
  return h("div", { class: "toolrow" }, h("span", { class: "tlabel" }, `工具调用 ${total} 次，${entries.length} 种`),
    h("div", { class: "tchips" }, entries.map(([name, count]) => h("span", { class: "toolchip" }, h("span", {}, name), h("b", {}, String(count))))));
}

function statsBar(t) {
  const cell = (label, value, warn, tip) => h("div", { title: tip }, h("span", {}, label), h("b", { style: warn ? "color:var(--warn)" : null }, String(value)));
  return [h("div", { class: "stats" },
    cell("回合", t.turns, 0, "用户发一条消息，agent 处理到回复，算一个回合"), cell("步数", t.steps, 0, "模型回复的次数，工具调用和结果不单算"),
    cell("费用", yuan(t.cost_cny), 0, "账本里已结算的总费用"), cell("token 入/出", `${t.tokens_in}/${t.tokens_out}`, 0, "输入给模型的和模型输出的 token 总数"),
    cell("失败尝试", t.failed_attempts, t.failed_attempts, "模型调用失败、预扣已释放的次数（含重试）"),
    cell("悬空预扣", t.open_holds, t.open_holds, "预扣了费用，既没结算也没释放，通常是进程中途退出"),
    cell("记录缺失", t.missing_records, t.missing_records, "该有调用记录的调用没有记录：记录开关没开，或写入前进程已退出"),
    cell("错误事件", t.errors, t.errors, "会话日志里的错误事件数")), toolsRow(t.tools)];
}

function blobsView(blobs) {
  const keys = Object.keys(blobs || {});
  if (!keys.length) return null;
  return [h("h2", {}, "提示词与工具全文"), keys.map((sha) => h("details", {}, h("summary", { class: "mono" }, sha8(sha)),
    h("pre", {}, blobs[sha] == null ? "（对象存储里没有这份内容）" : asText(blobs[sha]))))];
}

on(/^\/replay\/([^/]+)\/([^/]+)$/, async (tenant, session) => {
  const params = new URLSearchParams(location.search);
  const full = params.get("full") === "1";
  const order = params.get("order") === "asc" ? "asc" : "desc";  // 默认倒序，order=asc 时正序
  const desc = order === "desc";
  const tl = await api(`/api/replay?tenant=${q(tenant)}&session=${q(session)}${full ? "&full=1" : ""}`);
  const setParam = (key, val) => {
    const u = new URL(location.href);
    u.searchParams.set(key, val);
    history.replaceState(null, "", u);
    route();
  };
  const fullBtn = h("button", { class: "btn", type: "button", onclick: () => setParam("full", full ? "0" : "1") },
    full ? "正文截断显示" : "显示全文和提示词");
  const orderBtn = h("button", { class: "btn", type: "button", onclick: () => setParam("order", desc ? "asc" : "desc") },
    desc ? "切换为正序（旧→新）" : "切换为倒序（新→旧）");
  shell("sessions", [h("h1", {}, "回放"),
    crumbs(h("a", { href: "#/sessions" }, "租户"), h("a", { href: `#/sessions/${q(tenant)}`, class: "mono" }, tenant), h("span", { class: "mono" }, session), readonlyTag(tenant)),
    hint((desc ? "按回合倒序排列，最新的整轮在最上面（轮内仍是提问→回复）。" : "按回合正序排列，最早的整轮在最上面。") + "顶部橙色数字表示有失败、缺记录或错误，鼠标停上去看解释。"),
    h("div", { class: "row" }, orderBtn, fullBtn), replayView(tl, full, desc)]);
});

// ---------- 评测 ----------
const lines = (s) => s.split("\n").map((x) => x.trim()).filter(Boolean);
const tag = (text, kind) => h("span", { class: `tag ${kind || ""}` }, text);
const field = (label, input, hint, required = false, wide = false) => h("label", { class: wide ? "field wide" : "field" },
  h("span", { class: "lab" }, label, required ? h("em", { class: "req" }, "必填") : h("small", {}, "可选")), input, hint ? h("small", { class: "sub" }, hint) : null);
const TERM_CLASS = { 回合: "a", 步: "b" };
// 文本里用【】标出的术语渲染成带底色的小标签，两个术语颜色不同
const terms = (text) => text.split(/【(.+?)】/).map((t, i) => (i % 2 ? h("span", { class: `term ${TERM_CLASS[t]}` }, t) : t));
// 圆形 i 图标，悬停或键盘聚焦时弹出长说明；说明同时放进 aria-label，读屏也能读到
const tip = (text) => h("span", { class: "tip", tabindex: "0", role: "img", "aria-label": text, "data-tip": text }, "i");
// 分组卡片：标题栏（标题、徽标、提示、右侧操作）加内容区
const card = (title, { badge, tip: tipText, action } = {}, ...kids) => h("section", { class: "card" },
  h("header", {}, h("h2", {}, title), badge ? h("span", { class: "badge" }, badge) : null, tipText ? tip(tipText) : null, h("span", { class: "spacer" }), action || null),
  h("div", { class: "cbody" }, ...kids));
// 大块输入放进对话框，页面里只留摘要和入口。点背景或按 Esc 关闭
function dialogBox(title, ...kids) {
  const dlg = h("dialog", { class: "dlg", "aria-label": title }, h("header", {}, h("h2", {}, title)), h("div", { class: "dbody" }, ...kids));
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  return dlg;
}

// 行级粗略统计：新增和删除各多少行，按行内容的出现次数比较，不是严格的 diff
function lineStats(a, b) {
  const count = (text) => {
    const m = new Map();
    for (const line of text.split("\n")) m.set(line, (m.get(line) || 0) + 1);
    return m;
  };
  const ca = count(a), cb = count(b);
  let added = 0, removed = 0;
  for (const [line, n] of cb) added += Math.max(0, n - (ca.get(line) || 0));
  for (const [line, n] of ca) removed += Math.max(0, n - (cb.get(line) || 0));
  return { added, removed, same: a === b };
}

const section = (title, ...kids) => h("div", { class: "fsec" }, h("h3", {}, title), ...kids);
const JOB_STATE = { running: "运行中", done: "已完成", cancelled: "已取消", failed: "失败" };

function caseAsserts(c) {
  const a = [];
  if (c.reply_contains.length) a.push(`含 ${c.reply_contains.join("、")}`);
  if (c.reply_not_contains.length) a.push(`不含 ${c.reply_not_contains.join("、")}`);
  if (c.max_steps != null) a.push(`≤${c.max_steps} 步`);
  if (c.max_questions != null) a.push(`提问≤${c.max_questions} 次`);
  if (c.max_cost_cny != null) a.push(`≤¥${c.max_cost_cny}`);
  return a.join("；") || "无";
}

const INTER_HELP = {
  none: "agent 一提问，这次运行就停下，判不通过。",
  human: "问题会出现在下面的进度里，你回答后继续。等待期间占着评测位，不能开别的评测，也不能改文件。",
  model: "模型按案例的「补充信息」替你回答。它的费用单独记，不算进案例的费用上限。",
};
const INTERACTION = { none: "不交互", human: "人工回答", model: "模型扮演用户" };

function newCaseForm(done, cancel) {
  const name = h("input", { type: "text", required: true, maxlength: 40, pattern: "[A-Za-z0-9_\\-]{1,40}" });
  const message = h("textarea", { rows: 3, required: true });
  const files = h("input", { type: "file", multiple: true });
  const has = h("textarea", { rows: 2 }), hasNot = h("textarea", { rows: 2 });
  const steps = h("input", { type: "number", min: 1, step: 1 }), cost = h("input", { type: "number", min: 0, step: "any" });
  const questions = h("input", { type: "number", min: 0, step: 1 });
  const persona = h("textarea", { rows: 3 });
  const msg = h("div");
  const submit = async (e) => {
    e.preventDefault();
    const fd = new FormData();
    fd.append("name", name.value);
    fd.append("message", message.value);
    for (const f of files.files) fd.append("files", f);
    for (const [key, el] of [["reply_contains", has], ["reply_not_contains", hasNot]]) lines(el.value).forEach((v) => fd.append(key, v));
    if (steps.value) fd.append("max_steps", steps.value);
    if (cost.value) fd.append("max_cost_cny", cost.value);
    if (questions.value) fd.append("max_questions", questions.value);
    if (persona.value.trim()) fd.append("persona", persona.value);
    try { done(await api("/api/cases", { method: "POST", form: fd })); } catch (err) { msg.replaceChildren(errorBanner(err)); }
  };
  return h("form", { class: "cform", onsubmit: submit },
    section("基本信息",
      h("div", { class: "fgrid" },
        field("案例名", name, "字母、数字、_、-，最长 40 个字符", true), field("样本文件", files, "开跑时会在第一条消息前上传给 agent"),
        field("给 agent 的第一条消息", message, "", true, true),
        field("补充信息", persona, "选「模型扮演用户」时，模型据此替你回答：用户是谁、手上有什么事实。没写的它会说不清楚", false, true))),
    section("判定条件（都可以留空，留空就不检查）",
      h("div", { class: "fgrid two" }, field("回复必须包含", has, "每行一个词"), field("回复不能包含", hasNot, "每行一个词")),
      h("div", { class: "fgrid" },
        field("步数上限", steps), field("费用上限（元）", cost), field("agent 最多提问几次", questions, "0 表示不该提问"))),
    h("div", { class: "row" }, h("button", { class: "btn primary", type: "submit" }, "创建案例"),
      cancel ? h("button", { class: "btn", type: "button", onclick: cancel }, "取消") : null), msg);
}

function overrideText(o) {
  if (!o) return "无";
  const p = [];
  if (o.think_first_steps != null) p.push(`每回合前 ${o.think_first_steps} 步思考`);
  if (o.think_after_tools) p.push(o.think_after_tools.length ? `工具后思考：${o.think_after_tools.join("、")}` : "工具后不思考");
  if (o.max_steps != null) p.push(`每回合步数上限 ${o.max_steps}`);
  if (o.system_prompt != null) p.push(`系统提示词已改（${o.system_prompt.length} 字）`);
  if (o.user_sim_prompt != null) p.push(`模拟用户提示词已改（${o.user_sim_prompt.length} 字）`);
  return p.join("；");
}

// 单次覆盖：只对这一批评测运行生效，不改磁盘上的真文件。d 是磁盘上现在生效的值，onChange 在任何覆盖项变化后调用。
// 提示词在对话框里改，点「应用到本次评测」才暂存，点「开始评测」时随这次运行发出去
function overridesBox(d, onChange) {
  const mode = h("select", { "aria-label": "思考方式" },
    h("option", { value: "keep" }, `当前：每回合前 ${d.think_first_steps} 步思考`), h("option", { value: "always" }, "全程开启"),
    h("option", { value: "off" }, "全程关闭"), h("option", { value: "first" }, "每回合前 N 步思考"));
  const cap = h("input", { type: "number", min: 1, step: 1, placeholder: String(d.max_steps), "aria-label": "每回合步数上限", style: "max-width:100px" });
  const n = h("input", { type: "number", min: 0, step: 1, value: d.think_first_steps, "aria-label": "前几步", style: "max-width:88px" });
  n.hidden = true;
  const staged = { sys: null, sim: null };  // 对话框里应用过的提示词，null 表示用磁盘版本
  let simulating = false;
  const tools = d.think_after_tools.join("、") || "无";
  const afterTools = d.think_after_tools.length ? `只有上一步调用了 ${tools} 里的工具，下一步才接着思考；调用其他工具不会。` : "调用工具不会触发思考。";
  const THINK_HELP = {
    keep: [`每个回合的前 ${d.think_first_steps} 步思考，其余步不思考。`, afterTools],
    always: ["每一步都思考。"],
    off: ["每一步都不思考，调用工具后也不思考。"],
    first: ["每个回合的前 N 步思考，其余步不思考。", afterTools],
  };
  const lines = (key) => THINK_HELP[key].map((l) => h("p", {}, l));
  const help = h("div", { class: "muted think-help" }, ...lines("keep"));

  const active = () => {
    const out = [];
    if (mode.value === "off") out.push("全程关闭思考");
    else if (mode.value === "always") out.push("全程开启思考");
    else if (mode.value === "first") out.push(`每回合前 ${n.value || "?"} 步思考`);
    const c = cap.value.trim();
    if (c !== "" && Number(c) !== d.max_steps) out.push(`步数上限 ${c}`);
    if (staged.sys !== null) out.push("系统提示词已改");
    if (simulating && staged.sim !== null) out.push("模拟用户提示词已改");
    return out;
  };
  const ovstate = h("span", { class: "ovstate" });
  const rows = h("div", { class: "prows" });
  const promptEditor = (title, base, get, set, note) => {
    const area = h("textarea", { class: "pedit", spellcheck: "false", "aria-label": title });
    const stat = h("span", { class: "muted" });
    const refresh = () => {
      const st = lineStats(base, area.value);
      stat.textContent = st.same ? "和磁盘版本相同" : `相对磁盘版本：约 +${st.added} −${st.removed} 行`;
    };
    area.addEventListener("input", refresh);
    const dlg = dialogBox(title, h("p", { class: "muted" }, note), area,
      h("div", { class: "dfoot" }, stat, h("span", { class: "spacer" }),
        h("button", { class: "btn", type: "button", onclick: () => { area.value = base; refresh(); } }, "恢复磁盘版本"),
        h("button", { class: "btn", type: "button", onclick: () => dlg.close() }, "取消"),
        h("button", { class: "btn primary", type: "button", onclick: () => {
          if (!area.value.trim()) { stat.textContent = "提示词不能为空"; return; }
          set(area.value === base ? null : area.value);
          dlg.close();
        } }, "应用到本次评测")));
    return { dlg, open: () => { area.value = get() === null ? base : get(); refresh(); dlg.showModal(); area.focus(); } };
  };
  const editors = {
    sys: promptEditor("修改系统提示词", d.system_prompt, () => staged.sys, (v) => { staged.sys = v; changed(); },
      "只对这一批评测生效，不改磁盘上的 configs/system_prompt.md。skill 清单照常追加在后面。"),
    sim: promptEditor("修改模拟用户提示词", d.user_sim_prompt, () => staged.sim, (v) => { staged.sim = v; changed(); },
      `只在 agent 中途提问、且选了「模型扮演用户」时用，最多替用户回答 ${d.max_sim_rounds} 次。案例里的「补充信息」会接在它后面。`)};
  const renderRows = () => {
    const mk = (key, label, base, enabled, why) => {
      const text = staged[key];
      const status = text === null ? h("span", { class: "muted" }, "磁盘版本")
        : h("span", { class: "tag warn" }, `本次已修改 约 +${lineStats(base, text).added} −${lineStats(base, text).removed} 行`);
      return h("div", { class: "prow" }, h("span", { class: "plabel" }, label), status, h("span", { class: "spacer" }),
        enabled ? null : h("small", { class: "sub" }, why),
        text !== null ? h("button", { class: "btn", type: "button", onclick: () => { staged[key] = null; changed(); } }, "撤销") : null,
        h("button", { class: "btn", type: "button", disabled: !enabled, onclick: editors[key].open }, "修改…"));
    };
    rows.replaceChildren(mk("sys", "系统提示词", d.system_prompt, true), mk("sim", "模拟用户提示词", d.user_sim_prompt, simulating, "选「模型扮演用户」后可修改"));
  };
  const renderState = () => {
    const a = active();
    ovstate.replaceChildren(...(a.length ? a.map((t) => h("span", { class: "tag warn" }, t)) : [h("span", { class: "muted" }, "无覆盖，用当前配置")]));
  };
  const changed = () => { renderRows(); renderState(); onChange(); };
  mode.addEventListener("change", () => { n.hidden = mode.value !== "first"; help.replaceChildren(...lines(mode.value)); changed(); });
  cap.addEventListener("input", changed);
  n.addEventListener("input", changed);

  const value = () => {
    const o = {};
    const capText = cap.value.trim();
    if (capText !== "") {
      if (!/^\d+$/.test(capText) || Number(capText) < 1) throw { message: "每回合步数上限要填一个不小于 1 的整数", details: {} };
      if (Number(capText) !== d.max_steps) o.max_steps = Number(capText);
    }
    if (mode.value === "off") Object.assign(o, { think_first_steps: 0, think_after_tools: [] });
    else if (mode.value === "first") {
      if (n.value === "" || !Number.isInteger(Number(n.value)) || Number(n.value) < 0) throw { message: "前几步要填一个不小于 0 的整数", details: {} };
      o.think_first_steps = Number(n.value);
    } else if (mode.value === "always") o.think_first_steps = o.max_steps ?? d.max_steps;  // 一个回合最多这么多步，全部要求思考
    if (staged.sys !== null && staged.sys !== d.system_prompt) o.system_prompt = staged.sys;
    if (simulating && staged.sim !== null && staged.sim !== d.user_sim_prompt) o.user_sim_prompt = staged.sim;  // 没选模型扮演时这项没有用，不记进覆盖
    return Object.keys(o).length ? o : undefined;
  };
  const concept = h("div", { class: "concept" },
    h("span", {}, ...terms("【回合】"), " 用户发消息，到 agent 回复（或停下提问）"),
    h("span", {}, ...terms("【步】"), " 模型的一次回复，可带工具调用；一个回合有若干步"));
  const ovgrid = h("div", { class: "ovgrid" },
    h("div", { class: "field" }, h("span", { class: "lab" }, "思考策略"), h("div", { class: "row" }, mode, n), help),
    h("div", { class: "field" }, h("span", { class: "lab" }, "每回合步数上限"), h("div", { class: "row" }, cap, h("span", {}, "步")),
      h("small", { class: "sub" }, `留空不改（现在 ${d.max_steps}）。到了上限，模型只能收尾并用文字总结。`)));
  const deepseek = h("p", { class: "info-line" }, h("span", { class: "i" }, "i"),
    "备选模型 deepseek-flash 还没配思考开关：主模型额度用完切到它时，它一直思考，这里的设置对它不起作用。",
    tip("官方支持开关思考；要生效，需要在 model.yaml 里给它配 thinking_extra_body。"));
  const el = h("details", { class: "card fold" },
    h("summary", {}, h("span", { class: "chev", "aria-hidden": "true" }, icon("chev")), h("h2", {}, "本次覆盖（可选）"),
      h("span", { class: "badge" }, "仅本次生效，不改动源文件"), ovstate),
    h("div", { class: "cbody" }, concept, ovgrid, deepseek, h("div", { class: "psec" }, h("h3", {}, "提示词"), rows),
      h("p", { class: "muted" }, "觉得好，跑完在进度里点「结果」进运行详情，点「采用到真配置」写回真文件；模拟用户提示词不能写回。"),
      editors.sys.dlg, editors.sim.dlg));
  renderRows();
  renderState();
  return { el, value, active, setSimulating: (v) => { simulating = v; changed(); } };
}

// agent 中途提问、选了人工回答时：把问题摆出来，让人在这里回答
function questionBox(qn, onError) {
  const ta = h("textarea", { rows: 3, "aria-label": "你的回答" });
  const send = h("button", { class: "btn primary", type: "button" }, "发送回答");
  send.addEventListener("click", async () => {
    send.disabled = true;
    try {
      await api("/api/runs/current/answer", { method: "POST", json: { text: ta.value } });
      ta.disabled = true;
      send.textContent = "已发送，等 agent 继续";
    } catch (e) { send.disabled = false; onError(e); }
  });
  return h("div", { class: "banner warn" }, h("div", {}, h("b", {}, "agent 在等你回答："), `案例 ${qn.case}，第 ${qn.n} 次提问`),
    clip(qn.question, true), ta, h("div", { class: "row" }, send, h("span", { class: "muted" }, "等太久会超时，这次运行判不通过")));
}

function jobView(job, onCancel, questionEl) {
  if (!job) return h("p", { class: "empty" }, "这次启动后还没有跑过评测任务");
  const total = job.cases.length * job.runs, finished = job.items.filter((x) => x.done).length;
  const rows = job.items.map((x) => h("tr", {},
    h("td", {}, x.case), h("td", { class: "num mono" }, `${x.run}/${job.runs}`),
    h("td", {}, x.done ? state(x.passed ? "ok" : "err", x.passed ? "通过" : "不通过") : state("warn", "运行中")),
    h("td", {}, x.done ? (x.error_code || x.reason) : ""), h("td", { class: "num mono" }, x.done ? x.steps : ""),
    h("td", { class: "num mono" }, x.done ? yuan(x.cost_cny) : ""),
    h("td", {}, x.done ? [h("a", { href: `#/results/${q(x.case)}/${q(x.run_id)}` }, "结果"), " ", h("a", { href: `#/replay/${q(x.tenant_id)}/${q(x.session_id)}` }, "回放")] : "")));
  return [
    h("div", { class: "row" }, state(job.state === "failed" ? "err" : job.state === "running" ? "warn" : "ok", JOB_STATE[job.state] || job.state),
      h("span", { class: "muted" }, `已完成 ${finished} / ${total}  案例 ${job.cases.join("、")}  标签 ${job.label || "无"}  覆盖 ${overrideText(job.overrides)}  agent 提问时 ${INTERACTION[job.interaction]}  开始于 ${day(job.started_at)}`),
      job.state === "running" ? h("button", { class: "btn", type: "button", disabled: job.cancelling, onclick: onCancel },
        job.cancelling ? "正在取消，进行中的这次在步骤边界收尾" : "取消") : null),
    job.error ? errorBanner(job.error) : null,
    questionEl,
    rows.length ? table(["案例", "第几次", "状态", "结束原因", "步数", "费用", ""], rows, [1, 4, 5]) : null];
}

on(/^\/eval$/, async () => {
  let [cs, cur, defaults] = await Promise.all([api("/api/cases"), api("/api/runs/current"), api("/api/defaults")]);
  const picked = new Set(), progress = h("div"), msg = h("div");
  const start = h("button", { class: "btn primary big", type: "button", disabled: true }, "开始评测");
  const runs = h("input", { type: "number", min: 1, step: 1, value: 1, "aria-label": "每个案例跑几次" });
  const tenant = h("input", { type: "text", maxlength: 40, placeholder: "留空自动生成", "aria-label": "评测租户名" });
  const tenantBox = h("div", { class: "group" }, h("span", { class: "addon mono" }, "eval_"), tenant);  // 前缀固定，只填后缀
  const label = h("input", { type: "text", placeholder: "before", "aria-label": "标签", maxlength: 40 });
  const inter = h("select", { "aria-label": "agent 提问时" },
    h("option", { value: "none" }, "不交互"), h("option", { value: "human" }, "我在页面上回答"), h("option", { value: "model" }, "模型扮演用户"));
  const interField = field("agent 中途提问时", inter, INTER_HELP.none);
  let job = cur.job, qKey = null, qEl = null;
  const summary = h("span", {}), ovNote = h("span", { class: "muted" });
  const ov = overridesBox(defaults, () => sync());
  const sync = () => {
    const running = job && job.state === "running", n = picked.size, r = Number(runs.value) || 1, a = ov.active();
    start.disabled = !n || running;
    summary.textContent = running ? "评测运行中，结束或取消后才能再开" : n ? `已选 ${n} 个案例，每个跑 ${r} 次，共 ${n * r} 次运行` : "先在上面勾选要跑的案例";
    ovNote.textContent = a.length ? `带 ${a.length} 项覆盖：${a.join("、")}` : "";
  };
  runs.addEventListener("input", sync);
  inter.addEventListener("change", () => { interField.lastChild.textContent = INTER_HELP[inter.value]; ov.setSimulating(inter.value === "model"); });

  // 进度只在跑过任务后出现，放在最上面
  const progressCard = card("进度", {}, progress), progressSlot = h("div");
  const draw = async () => {
    const pend = job && job.question, key = pend ? `${pend.run_id}:${pend.n}` : null;
    if (key !== qKey) { qKey = key; qEl = pend ? questionBox(pend, (e) => msg.replaceChildren(errorBanner(e))) : null; }  // 同一个问题复用同一个输入框，轮询重画不会丢掉已打的字
    document.title = key ? `（有问题待回答）${BASE_TITLE}` : BASE_TITLE;
    progress.replaceChildren(...[jobView(job, async () => { try { job = (await api("/api/runs/current/cancel", { method: "POST" })); draw(); } catch (e) { msg.replaceChildren(errorBanner(e)); } }, qEl)].flat(Infinity).filter(Boolean));
    progressSlot.replaceChildren(...(job ? [progressCard] : []));
    sync();
    ensureStream();
  };
  // 评测进度用 SSE 实时推送替代轮询：只在任务运行时保持连接，收到状态就重画，结束自动断开。
  function ensureStream() {
    if (jobStream || !job || job.state !== "running") return;
    const es = new EventSource("/api/runs/stream");
    jobStream = es;
    es.onmessage = (e) => {
      try { job = JSON.parse(e.data); } catch (_) { return; }
      draw();  // jobStream 已存在，不会重复建连
      if (!job || job.state !== "running") { es.close(); if (jobStream === es) jobStream = null; }
    };
    // onerror 不处理：EventSource 会自动重连；离开页面时由 route() 统一 close
  }
  start.addEventListener("click", async () => {
    msg.replaceChildren();
    if (jobStream) { jobStream.close(); jobStream = null; }  // 开新任务前断开旧流
    const body = { cases: cs.items.filter((c) => picked.has(c.name)).map((c) => c.name), runs: Number(runs.value) };
    if (tenant.value.trim()) body.tenant = tenant.value.trim();
    if (label.value.trim()) body.label = label.value.trim();
    body.interaction = inter.value;
    try {
      const overrides = ov.value();
      if (overrides) body.overrides = overrides;
      job = await api("/api/runs", { method: "POST", json: body });
      await draw();
      scrollTo(0, 0);  // 进度在最上面
    } catch (e) { msg.replaceChildren(errorBanner(e)); }
  });

  // 案例列表和新建案例对话框
  const casesBody = h("div", { class: "cases" });
  const caseDlgBody = h("div"), caseDlg = dialogBox("新建评测案例", caseDlgBody);
  const openCaseDialog = () => {
    caseDlgBody.replaceChildren(newCaseForm(async (created) => {
      caseDlg.close();
      cs = await api("/api/cases");
      picked.add(created.name);  // 刚建的案例直接勾上
      renderCases();
      sync();
    }, () => caseDlg.close()));
    caseDlg.showModal();
    const first = caseDlgBody.querySelector("input");
    if (first) first.focus();
  };
  const addBtn = h("button", { class: "btn", type: "button", onclick: openCaseDialog }, icon("plus"), "新建案例");
  const renderCases = () => {
    const rows = cs.items.map((c) => {
      if (c.error) return h("tr", {}, h("td", {}), h("td", {}, c.name), h("td", { colspan: 3 }, errorBanner(c.error)));
      const box = h("input", { type: "checkbox", checked: picked.has(c.name), "aria-label": `选择 ${c.name}`, onchange: () => { box.checked ? picked.add(c.name) : picked.delete(c.name); sync(); } });
      return h("tr", {}, h("td", {}, box), h("td", { class: "mono" }, c.name), h("td", {}, h("div", { class: "clamp" }, c.message)),
        h("td", { class: "num mono" }, c.files.length), h("td", { class: "muted" }, caseAsserts(c)));
    });
    casesBody.replaceChildren(rows.length ? table(["", "案例", "第一条消息", "样本", "断言"], rows, [3])
      : h("div", { class: "emptybox" }, h("p", {}, "还没有案例"), h("button", { class: "btn primary", type: "button", onclick: openCaseDialog }, icon("plus"), "新建第一个案例")));
  };
  renderCases();

  shell("eval", [h("h1", {}, "评测"),
    h("div", { class: "banner info", role: "note" }, "每次运行都会真调模型并产生费用，请合理设置步数上限。"),
    progressSlot,
    card("1  选案例", { action: addBtn }, casesBody, caseDlg),
    card("2  运行设置", {},
      h("div", { class: "fgrid" }, field("每个案例跑几次", runs, "默认 1 次"), field("租户名", tenantBox, "留空按时间生成；对比改动前后，两次填同一个名字"),
        field("标签", label, "对比时按它选一组，如 before、after"), interField)),
    ov.el, msg,
    h("div", { class: "runbar" }, h("div", { class: "rsum" }, summary, ovNote), start)]);
  await draw();
});

// ---------- 结果 ----------
const note = (text, kind = "warn") => h("div", { class: `banner ${kind}` }, text);
const humanTag = (v) => (v ? state(v === "pass" ? "ok" : "err", v === "pass" ? "人工通过" : "人工不通过") : state("none", "未标"));

on(/^\/results(?:\/([^/]+))?$/, async (caseName) => {
  const page = curPage();
  const [cs, r] = await Promise.all([api("/api/cases"), api(`/api/results?${caseName ? `case=${q(caseName)}&` : ""}page=${page}`)]);
  const pick = h("select", { "aria-label": "按案例筛选", onchange: () => {
    const u = new URL(location.href); u.searchParams.delete("page"); history.replaceState(null, "", u);
    location.hash = pick.value ? `#/results/${q(pick.value)}` : "#/results";
  } },
    h("option", { value: "" }, "全部案例"), cs.items.map((c) => h("option", { value: c.name, selected: c.name === caseName }, c.name)));
  const rows = r.items.map((x) => linkRow(`#/results/${q(x.case)}/${q(x.run_id)}`, [
    day(x.started_at), x.case, h("span", { class: "mono" }, x.run_id), x.label || h("span", { class: "muted" }, "无"),
    state(x.passed ? "ok" : "err", x.passed ? "通过" : "不通过"), caseName ? humanTag(x.human) : "", x.error_code || x.reason, x.steps, yuan(x.cost_cny),
    h("span", { class: "mono" }, x.code_version || ""), h("span", { class: "mono" }, sha8(x.inputs_sha))], [7, 8]));
  shell("results", [h("h1", {}, "结果"), hint("每行一次运行，点进去看断言、回复和费用，并标人工判定。选一个案例才显示人工判定列。"),
    card("运行记录", { action: pick },
      rows.length ? table(["开始", "案例", "运行", "标签", "自动判定", caseName ? "人工" : "", "结束原因", "步数", "费用", "代码版本", "输入"], rows, [7, 8]) : h("p", { class: "empty" }, "没有运行记录"),
      pager(page, r.has_more))]);
});

// 这次运行带着单次覆盖：试好了可以一键写回真文件（文件在运行之后被改过则后端拒绝）
function adoptBox(c, runId, overrides) {
  const msg = h("div");
  const btn = h("button", { class: "btn", type: "button", onclick: async () => {
    try {
      const r = await api(`/api/results/${q(c)}/${q(runId)}/adopt`, { method: "POST" });
      msg.replaceChildren(h("div", { class: "banner ok" }, `已写回：${r.adopted.map((x) => x.path).join("、")}。之后的评测和真实服务都会用到它`));
    } catch (e) { msg.replaceChildren(errorBanner(e)); }
  } }, "采用到真配置");
  return h("div", { class: "banner warn" }, h("div", {}, h("b", {}, "这次运行用了单次覆盖："), overrideText(overrides), "。真文件没有被改动。"),
    h("div", { class: "row" }, btn, h("span", { class: "muted" }, "满意再点：会把上面的覆盖写进 configs/，终端、企微等服务也会用到")), msg);
}

function annotateBox(c, runId, human) {
  const state = h("span", {}, humanTag(human));
  const noteIn = h("input", { type: "text", placeholder: "备注（可选）", "aria-label": "备注" });
  const msg = h("div");
  const send = (verdict) => async () => {
    try {
      const r = await api(`/api/results/${q(c)}/${q(runId)}/annotation`, { method: "POST", json: { verdict, note: noteIn.value } });
      state.replaceChildren(humanTag(r.verdict));
      msg.replaceChildren();
    } catch (e) { msg.replaceChildren(errorBanner(e)); }
  };
  return h("div", {}, h("div", { class: "row" }, "人工判定", state),
    h("div", { class: "row" }, noteIn, h("button", { class: "btn", type: "button", onclick: send("pass") }, "标为通过"),
      h("button", { class: "btn", type: "button", onclick: send("fail") }, "标为不通过")), msg);
}

function versionBox(c, res) {
  if (!res.inputs_sha) return h("p", { class: "muted" }, "这次运行没有记录输入版本（早期运行），不能认可");
  const name = h("input", { type: "text", placeholder: "版本名，如 prompt-v3", "aria-label": "版本名" });
  const noteIn = h("input", { type: "text", placeholder: "备注（可选）", "aria-label": "备注" });
  const msg = h("div");
  const send = async () => {
    try {
      const r = await api("/api/versions", { method: "POST", json: { case: c, run_id: res.run_id, name: name.value, note: noteIn.value } });
      msg.replaceChildren(h("div", { class: "banner ok" }, `已认可：${r.name}（${r.files} 个文件） `, h("a", { href: `#/versions/${q(r.name)}` }, "查看")));
    } catch (e) { msg.replaceChildren(errorBanner(e)); }
  };
  return h("div", {}, h("p", { class: "muted" }, `把这次运行用的那一版提示词、skill 和配置（${sha8(res.inputs_sha)}）起名保存，之后能取回。`),
    h("div", { class: "row" }, name, noteIn, h("button", { class: "btn", type: "button", onclick: send }, "认可这一版")), msg);
}

on(/^\/results\/([^/]+)\/([^/]+)$/, async (c, runId) => {
  const res = await api(`/api/results/${q(c)}/${q(runId)}`);
  const cell = (label, value) => h("div", {}, h("span", {}, label), h("b", {}, value == null ? "无" : String(value)));
  const warns = [
    res.stopped_by_cost != null ? note(`中途按费用停止：停下时已结算 ${res.stopped_by_cost} 元`) : null,
    res.inputs_changed ? note("运行期间提示词、skill 或配置被改动，这次结果和别的运行不可比") : null,
    res.unanswered_question ? note(`agent 提问停下，${{ none: "这次评测没有选人工回答", human: "等人回答超时", model: "模拟用户回答到上限了 agent 仍在提问" }[res.interaction]}：${res.unanswered_question}`) : null,
    res.error ? errorBanner(res.error) : null];
  const facts = h("dl", { class: "kv" }, [["结束原因", res.reason], ["步数", res.steps], ["费用", yuan(res.cost.total_cny)], ["标签", res.label],
    ["代码版本", res.code_version], ["输入版本", sha8(res.inputs_sha)], ["agent 提问", res.asked == null ? null : `${res.asked} 次（${INTERACTION[res.interaction]}）`],
    res.user_sim ? ["模拟用户费用", yuan(res.user_sim.cost_cny)] : null].filter(Boolean).map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, v == null ? "无" : String(v)))));
  const adoptable = res.overrides && ["think_first_steps", "think_after_tools", "max_steps", "system_prompt"].some((k) => k in res.overrides);
  shell("results", [h("div", { class: "pagehead" }, h("h1", {}, "运行详情"), state(res.passed ? "ok" : "err", res.passed ? "自动判定通过" : "自动判定不通过")),
    crumbs(h("a", { href: "#/results" }, "结果"), h("a", { href: `#/results/${q(c)}` }, c), h("span", { class: "mono" }, runId)),
    hint("自动判定只查关键词、步数和费用，回复对不对要你自己看，看完在页面下方标人工判定。"),
    warns, adoptable ? adoptBox(c, runId, res.overrides) : null,
    card("概览", { action: h("a", { class: "btn", href: `#/replay/${q(res.tenant_id)}/${q(res.session_id)}` }, "看回放") },
      facts, h("span", { class: "muted mono" }, `${res.tenant_id} / ${res.session_id}`)),
    card("断言", {}, res.asserts.length ? h("div", { class: "alist" }, res.asserts.map((a) => h("div", { class: "arow" },
      state(a.ok ? "ok" : "err", a.ok ? "通过" : "不通过"), h("span", { class: "mono" }, a.name), h("span", { class: "muted" }, a.detail)))) : h("p", { class: "empty" }, "案例没有写断言")),
    card("最终回复", {}, res.reply ? clip(res.reply, false) : h("p", { class: "empty" }, "没有回复")),
    (res.turns || []).length > 1 ? card(`多轮（${res.turns.length} 轮）`, {}, table(["用户消息", "结束", "步数"], res.turns.map((t) => h("tr", {}, h("td", {}, h("div", { class: "clamp" }, t.message)), h("td", {}, t.reason), h("td", { class: "num mono" }, t.steps))), [2])) : null,
    card("费用分项", {}, res.cost.items.length ? table(["用途", "模型", "次数", "费用"], res.cost.items.map((i) => h("tr", {}, h("td", {}, i.purpose), h("td", { class: "mono" }, i.model), h("td", { class: "num mono" }, i.count), h("td", { class: "num mono" }, yuan(i.cny)))), [2, 3])
      : h("p", { class: "empty" }, "没有产生费用")),
    res.files.length ? card("交付文件", {}, res.files.map((n) => h("div", {}, h("a", { href: `/api/results/${q(c)}/${q(runId)}/files/${q(n)}` }, n)))) : null,
    card("人工标注", {}, annotateBox(c, runId, res.human)),
    card("认可这一版输入", {}, versionBox(c, res))]);
});

// ---------- 对比 ----------
const STATUS_ZH = { added: "新增", removed: "删除", changed: "改动" };
const rng = (r) => (r ? `${r.min} / ${r.median} / ${r.max}` : "无");
const frac = (v) => (v ? `${v[0]}/${v[1]}` : "无");

function diffView(files) {
  return files.map((f) => h("div", {}, h("div", { class: "row" }, tag(STATUS_ZH[f.status] || f.status, ""), h("span", { class: "mono" }, f.path)),
    h("pre", { class: "diff" }, f.diff && f.diff.length ? renderDiffLines(f.diff) : "（不是文本文件，内容已变）")));
}

function compareView(c) {
  const { a, b } = c;
  const human = (g) => (g.human ? `${g.human.pass} / ${g.human.fail} / ${g.human.unmarked}` : "无");
  const ended = (g) => Object.entries(g.ended).map(([k, v]) => `${k}×${v}`).join("  ");
  const num = (v) => (Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/0+$/, "").replace(/\.$/, ""));
  const signed = (v, unit) => (v === 0 ? "持平" : `${v > 0 ? "+" : "-"}${unit(Math.abs(v))}`);
  const median = (g, key) => (g[key] ? Number(g[key].median) : null);
  const delta = (x, y, unit) => (x == null || y == null ? "" : signed(y - x, unit));
  const rate = (g) => (g.runs ? g.passed / g.runs : null);
  const markedRate = (g) => (g.human && g.human.pass + g.human.fail ? g.human.pass / (g.human.pass + g.human.fail) : null);
  const pct = (v) => `${Math.round(v * 100)} 个百分点`;
  const markedText = (g) => (g.human && g.human.pass + g.human.fail ? `${g.human.pass}/${g.human.pass + g.human.fail}` : "未标注");
  const big = [
    ["自动通过", `${a.passed}/${a.runs}`, `${b.passed}/${b.runs}`, delta(rate(a), rate(b), pct)],
    ["步数（中位）", a.steps ? num(median(a, "steps")) : "无", b.steps ? num(median(b, "steps")) : "无", delta(median(a, "steps"), median(b, "steps"), (v) => `${num(v)} 步`)],
    ["费用（中位）", a.cost_cny ? yuan(num(median(a, "cost_cny"))) : "无", b.cost_cny ? yuan(num(median(b, "cost_cny"))) : "无", delta(median(a, "cost_cny"), median(b, "cost_cny"), (v) => `¥${v.toFixed(2)}`)],
    ["人工通过（已标注）", markedText(a), markedText(b), delta(markedRate(a), markedRate(b), pct)]];
  const head = [["运行次数", a.runs, b.runs], ["步数 小/中/大", rng(a.steps), rng(b.steps)], ["费用 小/中/大", rng(a.cost_cny), rng(b.cost_cny)],
    ["结束原因", ended(a), ended(b)], ["代码版本", a.code_versions.join("、"), b.code_versions.join("、")], ["人工 通过/不通过/未标", human(a), human(b)]];
  const mixed = [a, b].filter((g) => Object.keys(g.inputs).filter((k) => k !== "null").length > 1)
    .map((g) => note(`组 ${g.label} 里混了多版输入，对比可能不干净（按用得最多的一版比较）`));
  const d = c.inputs_diff;
  const inputs = d.state === "same" ? h("div", { class: "banner ok" }, "输入（提示词、skill、配置）两组完全相同，差别只来自模型每次的随机性")
    : d.state === "unrecorded" ? note("有一组没有记录输入，无法比较提示词、skill、配置的差异")
    : [h("p", {}, `输入有差异 ${sha8(d.a)} → ${sha8(d.b)}，共 ${d.files.length} 个文件`), diffView(d.files)];
  return [mixed,
    card("结果对比", { badge: "B 相对 A 的变化" }, h("div", { class: "versus" },
      h("div", { class: "vrow vhead" }, h("span", {}), h("b", {}, `A ${a.label}`), h("b", {}, `B ${b.label}`), h("span", {}, "变化")),
      big.map((r) => h("div", { class: "vrow" }, h("span", { class: "vlabel" }, r[0]), h("span", { class: /^[0-9¥]/.test(r[1]) ? "vnum mono" : "vnum dim" }, r[1]), h("span", { class: /^[0-9¥]/.test(r[2]) ? "vnum mono" : "vnum dim" }, r[2]), h("span", { class: "vdelta mono" }, r[3]))))),
    card("详细对照", {}, table(["", a.label, b.label], head.map((r) => h("tr", {}, h("td", { class: "muted" }, r[0]), h("td", { class: "mono" }, String(r[1])), h("td", { class: "mono" }, String(r[2])))))),
    card("断言（通过次数/运行次数）", {}, table(["断言", a.label, b.label, ""], c.asserts.map((x) => {
      const ra = x.a && x.a[0] / x.a[1], rb = x.b && x.b[0] / x.b[1];
      return h("tr", {}, h("td", { class: "mono" }, x.name), h("td", { class: "mono" }, frac(x.a)), h("td", { class: "mono" }, frac(x.b)), h("td", {}, ra === rb ? "" : tag("← 变化", "warn")));
    }))),
    card("输入差异", {}, inputs)];
}

on(/^\/compare(?:\/([^/]+)\/([^/]+)\/([^/]+))?$/, async (c, la, lb) => {
  const cs = await api("/api/cases");
  const pick = h("select", { "aria-label": "案例" }, cs.items.map((x) => h("option", { value: x.name, selected: x.name === c }, x.name)));
  const ia = h("input", { type: "text", value: la || "", placeholder: "改动之前", "aria-label": "标签 A", required: true });
  const ib = h("input", { type: "text", value: lb || "", placeholder: "改动之后", "aria-label": "标签 B", required: true });
  const out = h("div");
  const chips = h("div", { class: "row" });
  const toEval = () => h("a", { href: "#/eval" }, "去评测页");
  const fill = async () => {
    let content;
    if (!pick.value) {
      // 没有案例 / 还没选案例：给出可点的下一步，而不是留一片空白
      content = cs.items.length
        ? [h("span", { class: "muted" }, "先选一个案例。")]
        : [h("span", { class: "muted" }, "还没有案例，"), toEval(), h("span", { class: "muted" }, "建一个再回来对比")];
    } else {
      const r = await api(`/api/labels?case=${q(pick.value)}`);
      const labels = r.items;
      content = labels.length
        ? [h("span", { class: "muted" }, "已有标签，点一下填入："),
          ...labels.map(({ label, count }) => h("button", { class: "chip", type: "button", title: `${count} 次运行`, onclick: () => { (ia.value ? ib : ia).value = label; } }, label))]
        : [h("span", { class: "muted" }, "这个案例还没有带标签的运行，"), toEval(), h("span", { class: "muted" }, "跑两次、标签分别填 before 和 after，再回来对比")];
    }
    chips.replaceChildren(...content);
  };
  pick.addEventListener("change", () => fill().catch((e) => out.replaceChildren(errorBanner(e))));
  const form = h("form", { class: "cform", onsubmit: (e) => { e.preventDefault(); location.hash = `#/compare/${q(pick.value)}/${q(ia.value.trim())}/${q(ib.value.trim())}`; } },
    h("div", { class: "fgrid" }, field("案例", pick, "", true), field("标签 A（改动之前）", ia, "", true), field("标签 B（改动之后）", ib, "", true)),
    chips, h("div", { class: "row" }, h("button", { class: "btn primary", type: "submit" }, "对比")));
  shell("compare", [h("h1", {}, "对比"), hint("选案例，填改动前后的标签。只读已有结果，不重跑，不花钱；最下面的「输入差异」就是两组之间改了什么。"),
    card("选择两组", {}, form), out]);
  await fill();
  if (c) out.replaceChildren(...[compareView(await api(`/api/compare?case=${q(c)}&a=${q(la)}&b=${q(lb)}`))].flat(Infinity));
  else out.replaceChildren(h("p", { class: "empty" }, "选好案例和两个标签，点「对比」，结果显示在这里。还没有带标签的运行？", toEval(), h("span", {}, " 先跑两组。")));
});

// ---------- 版本 ----------
on(/^\/versions$/, async () => {
  const r = await api("/api/versions");
  shell("versions", [h("h1", {}, "版本"), hint("认可过的提示词、skill 和配置组合，及人工标注统计。认可一版：结果 → 某次运行 → 页面下方「认可这一版输入」。"),
    card("已认可的版本", {}, r.items.length ? table(["名字", "输入", "租户", "标注运行", "自动通过", "人工通过", "人工不通过", "备注", "认可时间"], r.items.map((v) =>
      linkRow(`#/versions/${q(v.name)}`, [v.name, h("span", { class: "mono" }, sha8(v.inputs_sha)), h("span", { class: "mono" }, v.tenant_id),
        v.runs, v.auto_passed, v.human_pass, v.human_fail, v.note || "", day(v.created_at)], [3, 4, 5, 6])), [3, 4, 5, 6]) : h("p", { class: "empty" }, "还没有认可过的版本，在某次运行的详情页下方可以认可"))]);
});

on(/^\/versions\/([^/]+)$/, async (name) => {
  const r = await api(`/api/versions/${q(name)}`), v = r.version;
  const cell = (label, value) => h("div", {}, h("span", {}, label), h("b", {}, String(value)));
  shell("versions", [h("h1", {}, name), crumbs(h("a", { href: "#/versions" }, "版本"), h("span", { class: "mono" }, sha8(v.inputs_sha))),
    card("概览", {}, h("dl", { class: "kv" }, [["标注运行", r.runs], ["自动通过", r.auto_passed], ["人工通过", r.human_pass], ["人工不通过", r.human_fail], ["文件", r.files.length]]
      .map(([k, val]) => h("div", {}, h("dt", {}, k), h("dd", {}, String(val))))), v.note ? h("p", {}, v.note) : null),
    card("包含的文件", {}, r.files.map((f) => h("div", { class: "mono" }, f))),
    card("标注过的运行", {}, r.annotations.length ? table(["案例", "运行", "自动判定", "人工", "备注"], r.annotations.map((x) =>
      linkRow(`#/results/${q(x.case_name)}/${q(x.run_id)}`, [x.case_name, h("span", { class: "mono" }, x.run_id), state(x.auto_passed ? "ok" : "err", x.auto_passed ? "通过" : "不通过"), humanTag(x.verdict), x.note || ""])))
      : h("p", { class: "empty" }, "这一版还没有标注"))]);
});

// ---------- 文件 ----------
const fileUrl = (p) => p.split("/").map(q).join("/");
const FILE_GROUPS = [["系统提示词", (p) => p === "configs/system_prompt.md"], ["配置", (p) => p.startsWith("configs/") && p !== "configs/system_prompt.md"], ["skills", (p) => p.startsWith("skills/")]];

// 逐行 diff（前端算，保存前预览）。这些文件都很小，LCS 足够；超大文件放弃以免卡住。
function lineOps(oldText, newText) {
  const a = String(oldText).split("\n"), b = String(newText).split("\n");
  const n = a.length, m = b.length;
  if (n * m > 4_000_000) return null;
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const ops = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { ops.push({ t: " ", x: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ t: "-", x: a[i++] }); }
    else { ops.push({ t: "+", x: b[j++] }); }
  }
  while (i < n) ops.push({ t: "-", x: a[i++] });
  while (j < m) ops.push({ t: "+", x: b[j++] });
  return ops;
}
// 只留改动行及其上下文，折叠未变的大段
function diffHunks(ops, ctx = 3) {
  const ranges = [];
  ops.forEach((o, idx) => {
    if (o.t === " ") return;
    const s = Math.max(0, idx - ctx), e = Math.min(ops.length - 1, idx + ctx);
    const last = ranges[ranges.length - 1];
    if (last && s <= last[1] + 1) last[1] = Math.max(last[1], e);
    else ranges.push([s, e]);
  });
  const lines = [];
  ranges.forEach(([s, e], k) => {
    if (k > 0) lines.push(`@@ 省略 ${s - ranges[k - 1][1] - 1} 行 @@`);
    for (let p = s; p <= e; p++) lines.push(ops[p].t + ops[p].x);
  });
  return lines;
}
// 已带 +/-/@@ 前缀的行 → 着色 span（对比页和文件预览共用）
function renderDiffLines(lines) {
  return lines.map((ln) => {
    const t = String(ln).replace(/\n$/, "");
    const kind = t.startsWith("+") && !t.startsWith("+++") ? "add" : t.startsWith("-") && !t.startsWith("---") ? "del" : t.startsWith("@@") ? "hunk" : "";
    return h("span", { class: `dl ${kind}` }, t || " ");
  });
}

function fileEditor(file) {
  let base = file.sha, saved = file.content;
  const area = h("textarea", { class: "editor", spellcheck: "false", "aria-label": file.path });
  area.value = file.content;
  const status = h("span", {}), msg = h("div");
  const save = h("button", { class: "btn primary", type: "button" }, "保存");
  const diffBtn = h("button", { class: "btn", type: "button", disabled: true }, "改动预览");
  const diffBox = h("div");
  let showDiff = false;
  const dirty = () => area.value !== saved;
  const renderDiff = () => {
    if (!showDiff || !dirty()) { diffBox.replaceChildren(); return; }
    const ops = lineOps(saved, area.value);
    if (!ops) { diffBox.replaceChildren(h("p", { class: "muted" }, "文件太大，不显示逐行差异")); return; }
    const lines = diffHunks(ops);
    diffBox.replaceChildren(lines.length
      ? h("div", {}, h("div", { class: "muted", style: "margin:8px 0 4px" }, "相对上次保存的版本（绿新增 / 红删除）："), h("pre", { class: "diff" }, renderDiffLines(lines)))
      : h("p", { class: "muted" }, "没有行级差异（可能只是行尾或换行符不同）"));
  };
  const sync = () => {
    save.disabled = !dirty();
    diffBtn.disabled = !dirty();
    status.replaceChildren(dirty() ? tag("有未保存的修改", "warn") : h("span", { class: "muted mono" }, base ? sha8(base) : "新文件"));
    renderDiff();
  };
  const doSave = async () => {
    if (!dirty()) return;
    try {
      const r = await api(`/api/files/${fileUrl(file.path)}`, { method: "PUT", json: { content: area.value, base_sha: base } });
      base = r.sha; saved = area.value; msg.replaceChildren(h("div", { class: "banner ok" }, "已保存，下一次评测运行会读到新内容"));
      if (!file.sha) { file = { ...file, sha: r.sha }; history.replaceState(null, "", `#/files/${fileUrl(file.path)}`); }
    } catch (e) {
      msg.replaceChildren(errorBanner(e), e.code === "E3006" ? h("button", { class: "btn", type: "button", onclick: () => route() }, "重新读取（丢弃我的修改）") : null);
    }
    sync();
  };
  save.addEventListener("click", doSave);
  diffBtn.addEventListener("click", () => { showDiff = !showDiff; diffBtn.textContent = showDiff ? "收起预览" : "改动预览"; renderDiff(); });
  area.addEventListener("input", sync);
  area.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); doSave(); } });
  sync();
  return { el: h("div", {}, h("div", { class: "row" }, h("h2", { class: "mono", style: "margin:0" }, file.path), status, diffBtn, save), msg, diffBox, area), dirty };
}

on(/^\/files(?:\/(.+))?$/, async (path) => {
  const [list, file] = await Promise.all([api("/api/files"), path ? api(`/api/files/${fileUrl(path)}`) : null]);
  const pane = h("div");
  let editor = null;
  const open = (f) => { editor = fileEditor(f); pane.replaceChildren(editor.el); };
  const leave = (e) => { if (editor && editor.dirty() && !confirm("有未保存的修改，离开会丢失。继续吗？")) e.preventDefault(); };
  const links = FILE_GROUPS.map(([title, test]) => {
    const mine = list.items.filter((f) => test(f.path));
    return mine.length ? [h("div", { class: "group" }, title), mine.map((f) => h("a", { href: `#/files/${fileUrl(f.path)}`, class: "mono", onclick: leave, "aria-current": f.path === path ? "page" : null }, f.path.replace(/^(configs|skills)\//, "")))] : null;
  });
  const fresh = h("input", { type: "text", placeholder: "skills/名字/SKILL.md", "aria-label": "新文件路径" });
  const create = h("button", { class: "btn", type: "button", onclick: () => {
    if (!fresh.value.trim().startsWith("skills/")) { pane.replaceChildren(errorBanner({ message: "新文件只能建在 skills/ 下", details: {} })); return; }
    if (editor && editor.dirty() && !confirm("有未保存的修改，离开会丢失。继续吗？")) return;
    open({ path: fresh.value.trim(), content: "", sha: null });
  } }, "新建");
  shell("files", [h("h1", {}, "文件"), hint("直接改工作区里的真文件，没有撤销，重要的改动先提交 git。保存后下次评测就会读到；评测在跑时不能保存。只想试一次，用评测页的「本次覆盖」。"),
    h("div", { class: "files" }, h("div", { class: "filelist" }, links, h("div", { class: "group" }, "新建 skill 文件"), fresh, create),
      pane)]);
  if (file) open(file); else pane.replaceChildren(h("p", { class: "empty" }, "从左边选一个文件来编辑，或在左下角新建一个 skill 文件"));
});

// ---------- 帮助 ----------
const go = (label, href) => h("a", { href }, label);

on(/^\/help$/, async () => {
  const flow = (title, steps) => [h("h2", {}, title), h("ol", { class: "steps" }, steps.map((s) => h("li", {}, s)))];
  shell("help", h("div", { class: "help" }, [
    h("h1", {}, "使用说明"),
    hint("这个网页做两件事：看 agent 一次对话里到底发生了什么；改了提示词或流程后，验证是变好还是变差。"),
    h("h2", {}, "几个词"),
    table(["词", "意思"], [
      ["案例", "一份测试题：一条用户消息，加上样本文件，加上“怎样算通过”的判定"],
      ["运行", "把一个案例交给真实的 agent 跑一次。会真调模型，花钱"],
      ["标签", "给一批运行起的名字（如 before、after），用来在对比页分组"],
      ["租户", "一次运行或一个用户的数据空间。租户编号以用途开头：评测 eval_、终端 dev_、模拟器 sim_、真实用户 usr_"],
      ["回放", "把一次对话还原出来：模型每步收到什么、想了什么、调了什么工具、花了多少钱"],
    ].map((r) => h("tr", {}, h("td", { class: "term" }, r[0]), h("td", {}, r[1])))),
    flow("看一次对话哪里出了问题", [
      ["点「", go("会话", "#/sessions"), "」，输入租户前缀（评测用 eval_，终端用 dev_），点筛选。"],
      ["点租户，再点会话，进入回放。"],
      ["先看顶部汇总里有没有橙色数字（失败尝试、记录缺失、错误事件），鼠标停在标题上有解释。"],
      ["往下读时间线：用户消息、模型回复、工具调用和结果。点「显示全文和提示词」能看完整的提示词。"],
      ["从评测结果里点「回放」也能进来。"]]),
    flow("改提示词，验证好不好", [
      ["点「", go("评测", "#/eval"), "」，勾选案例，标签填 before，租户名填一个固定名字（如 t1），点开始运行。每个案例建议跑 3 次以上，因为模型每次输出不同。"],
      ["点「", go("文件", "#/files"), "」，改 system_prompt.md 或某个 skill，保存。"],
      ["回到评测页，同样的案例，标签填 after，租户名和上次一样（这样复用转写缓存，不用重新转写），再跑。"],
      ["点「", go("对比", "#/compare"), "」，选案例，填 before 和 after，看通过率、步数、费用，以及两次之间提示词改了哪几行（绿色是新增，红色是删除）。"]]),
    flow("让 agent 中途提问时有人回答", [
      ["评测页开跑前，在「agent 提问」那里选「我在页面上回答」。案例里可以用「agent 最多提问几次」限制它问多少次，写 0 表示这个案例不该提问。"],
      ["跑起来后，agent 一提问，「进度」里会出现问题和输入框，浏览器标签页标题也会变成（有问题待回答）。回答后点发送，运行继续。"],
      ["一直没人回答会超时，这次运行判不通过；取消评测会立刻结束等待。选「不交互」时，agent 一提问这次运行就停下，也判不通过。"],
      ["不想自己守着：选「模型扮演用户」，模型按案例里的「补充信息」替你回答，费用单独记在「模拟用户费用」里，不算进案例的费用上限。补充信息里没有的事，它会说不清楚。"]]),
    flow("试一个思考或提示词的改动，不动真文件", [
      ["在「", go("评测", "#/eval"), "」页展开「本次覆盖」：思考可选全程关闭、每回合前 N 步、全程开启，也可以直接改系统提示词。只对这一次评测生效，真文件不变。"],
      ["跑完在「结果」页看这次的运行详情，顶部会写明用了什么覆盖。「对比」页里，覆盖和别的输入改动一样会显示出差异。"],
      ["觉得好，在运行详情里点「采用到真配置」，才会写进 configs/，终端、企微等服务也才会用到。写入前会检查文件在这次运行之后有没有被别人改过。"]]),
    flow("给一次运行打分，保存好的版本", [
      ["点「", go("结果", "#/results"), "」，选案例，点某次运行进入详情。"],
      ["读最终回复和交付文件，在页面下方标通过或不通过。自动判定只查关键词、步数和费用，回复内容对不对要你看。"],
      ["觉得这一版提示词好，在「认可这一版输入」里起个名字，之后在「", go("版本", "#/versions"), "」页能查到它和它的标注统计。"]]),
    flow("新建案例", [
      ["在评测页点「新建案例」：填案例名、给 agent 的第一条消息、样本文件。判定都可以不填：回复必须包含的词、不能包含的词、步数上限、费用上限。"]]),
    h("h2", {}, "注意"),
    h("ul", { class: "steps" }, [
      "每次运行都花钱。案例里写了费用上限，超了会自动停下这一次。",
      "评测在跑时不能改文件，也不能同时开第二个评测。",
      "改文件是直接改工作区里的真文件，没有撤销。重要的改动先提交 git。",
      "保存时提示“文件被改过”，说明你在别处（如编辑器）改过。先把页面上的内容复制出来，再点「重新读取」。",
      "运行详情里的黄色提示（中途按费用停止、运行期间输入被改动、agent 等回答）说明这次结果不能直接拿去比。"].map((s) => h("li", {}, s)))]));
});

// ---------- 启动 ----------
api("/api/me").then(() => route()).catch((err) => { if (!(err instanceof ApiError && err.status === 401)) $app.replaceChildren(errorBanner(err)); });
