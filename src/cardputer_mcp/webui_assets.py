"""The web UI's page, script and stylesheet, served from memory (no packaged files).

The script renders broker values with textContent only, never as HTML.
"""

from html import escape


def page(
    csrf: str | None,
    *,
    redirect: bool = False,
    logged_out: bool = False,
    message: str | None = None,
) -> str:
    head = (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        "<title>Cardputer</title>"
        '<link rel="stylesheet" href="/static/app.css">'
    )
    if redirect:
        # After Google's cross-site redirect: continue with a same-site navigation.
        return (
            f'<!doctype html><html lang="ja"><head>{head}'
            '<meta http-equiv="refresh" content="0;url=/"></head>'
            '<body><main class="notice"><p>ログインしました。</p>'
            '<p><a href="/">開く</a></p></main></body></html>'
        )
    if csrf is None:
        text = "ログアウトしました。" if logged_out else (message or "")
        return (
            f'<!doctype html><html lang="ja"><head>{head}</head>'
            f'<body><main class="notice"><p>{escape(text)}</p>'
            '<p><a href="/login">Google でログイン</a></p></main></body></html>'
        )
    return (
        f'<!doctype html><html lang="ja"><head>{head}'
        f'<meta name="csrf-token" content="{escape(csrf, quote=True)}"></head>'
        "<body>"
        '<header><h1>Cardputer</h1><span id="live" class="live">接続中…</span>'
        '<form method="post" action="/logout">'
        f'<input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">'
        '<button type="submit">ログアウト</button></form></header>'
        '<main><section id="state" class="state"></section>'
        '<section><h2>ジョブ</h2><ol id="jobs" class="jobs"></ol>'
        '<p id="more" class="muted" hidden>古いジョブは省略されています。</p></section>'
        '<section id="logs" class="logs" hidden><h2 id="logs-title"></h2>'
        '<div class="controls"><label>'
        '<input type="checkbox" id="all-levels"> すべてのレベル</label>'
        '<button type="button" id="close-logs">閉じる</button></div>'
        '<pre id="log-lines"></pre>'
        '<button type="button" id="older" hidden>続きを読む</button></section>'
        "</main>"
        '<script src="/static/app.js"></script>'
        "</body></html>"
    )


APP_CSS = """
:root { --bg: #f7f7f5; --fg: #1d1d1b; --muted: #6b6b66; --card: #ffffff; --line: #deded8;
  --ok: #1f7a3a; --bad: #b42318; --warn: #a15c00; --run: #1d4ed8; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #151514; --fg: #ececea; --muted: #a3a39d; --card: #1f1f1d; --line: #34342f;
    --ok: #5fcf80; --bad: #ff7b72; --warn: #e3a64b; --run: #7aa2ff; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; align-items: center; gap: 12px; padding: 12px 16px;
  border-bottom: 1px solid var(--line); position: sticky; top: 0; background: var(--bg); }
header h1 { font-size: 17px; margin: 0; flex: 1; }
header form { margin: 0; }
main { padding: 12px 16px 48px; max-width: 880px; margin: 0 auto; }
h2 { font-size: 15px; margin: 20px 0 8px; }
button { font: inherit; padding: 6px 12px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--card); color: var(--fg); }
.live { font-size: 13px; color: var(--muted); }
.live.on { color: var(--ok); }
.live.off { color: var(--bad); }
.state { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.state div { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 8px 10px; }
.state dt { font-size: 12px; color: var(--muted); }
.state dd { margin: 0; overflow-wrap: anywhere; }
.jobs { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.jobs li { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 12px; cursor: pointer; }
.jobs li.selected { outline: 2px solid var(--run); }
.jobs .top { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.jobs .name { font-weight: 600; }
.jobs .meta { font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
.badge { font-size: 12px; padding: 1px 8px; border-radius: 999px; border: 1px solid currentColor; }
.s-succeeded { color: var(--ok); } .s-failed, .s-cancelled { color: var(--bad); }
.s-running, .s-queued { color: var(--run); } .s-needs_review { color: var(--warn); }
.muted { color: var(--muted); font-size: 13px; }
.logs pre { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px; overflow-x: auto; white-space: pre-wrap; overflow-wrap: anywhere;
  font: 12px/1.45 ui-monospace, "Cascadia Mono", Consolas, monospace; max-height: 60vh; }
.logs .controls { display: flex; gap: 12px; align-items: center; margin-bottom: 8px; }
.l-error { color: var(--bad); } .l-warning { color: var(--warn); }
.notice { padding: 48px 16px; text-align: center; }
"""

APP_JS = r"""
"use strict";
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let state = null;
let cards = [];
let hasMore = false;
let selected = null;
let logCursor = null;
let source = null;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function since(utc) {
  if (!utc) return "";
  const date = new Date(utc);
  return isNaN(date) ? String(utc) : date.toLocaleString();
}

function setLive(text, cls) {
  const live = document.getElementById("live");
  live.textContent = text;
  live.className = "live " + (cls || "");
}

function renderState() {
  const box = document.getElementById("state");
  box.replaceChildren();
  if (!state) return;
  const label = (value, key) =>
    !value ? "なし" : typeof value === "object" ? (value[key] || "あり") : String(value);
  const owner = !state.owner ? "なし"
    : [state.owner.owner, state.owner.principal_id].filter(Boolean).join(" ") || "あり";
  const count = (value) => Array.isArray(value) ? value.length : value ? 1 : 0;
  const active = label(state.active_job, "job_id");
  const items = [
    ["バックエンド", state.backend || "-"],
    ["所有者", owner],
    ["実行中のジョブ", active],
    ["待ち", count(state.waiters)],
    ["要確認", state.needs_review === true ? "あり" : "なし"],
  ];
  for (const [label, value] of items) {
    const div = el("div");
    const dl = el("dl");
    dl.append(el("dt", null, label), el("dd", null, value));
    div.append(dl);
    box.append(div);
  }
}

function renderJobs() {
  const list = document.getElementById("jobs");
  list.replaceChildren();
  for (const card of cards) {
    const item = el("li");
    if (card.job_id === selected) item.classList.add("selected");
    const top = el("div", "top");
    top.append(
      el("span", "name", card.test || card.job_id),
      el("span", "badge s-" + (card.state || "unknown"), card.state || "unknown")
    );
    if (card.stage) top.append(el("span", "muted", card.stage));
    const meta = el("div", "meta");
    const parts = [card.job_id];
    if (card.result) parts.push("結果: " + card.result);
    if (card.failure_reason) parts.push("理由: " + card.failure_reason);
    if (card.created_utc) parts.push("開始: " + since(card.created_utc));
    if (card.finished_utc) parts.push("終了: " + since(card.finished_utc));
    meta.textContent = parts.join(" ・ ");
    item.append(top, meta);
    item.addEventListener("click", () => openLogs(card));
    list.append(item);
  }
  document.getElementById("more").hidden = !hasMore;
}

function apply(message) {
  if (!message || message.ok !== true) {
    setLive("停止: " + ((message && message.error) || "unknown"), "off");
    if (source) source.close();
    return;
  }
  if (message.kind === "snapshot" || message.kind === "reset") {
    state = message.state;
    cards = message.timeline.cards || [];
    hasMore = !!message.timeline.has_more;
  } else if (message.kind === "delta") {
    const byId = new Map(cards.map((card) => [card.job_id, card]));
    for (const id of message.remove || []) byId.delete(id);
    for (const card of message.upsert || []) byId.set(card.job_id, card);
    cards = (message.order || []).map((id) => byId.get(id)).filter(Boolean);
    hasMore = !!message.has_more;
    if (message.state) state = message.state;
    if (message.logs_changed && selected) loadLogs(logCursor !== null);
  }
  renderState();
  renderJobs();
}

function connect() {
  source = new EventSource("/api/events");
  source.onopen = () => setLive("ライブ", "on");
  source.onmessage = (event) => {
    try { apply(JSON.parse(event.data)); } catch (error) { setLive("受信エラー", "off"); }
  };
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) {
      setLive("切断（再読み込みしてください）", "off");
    } else {
      setLive("再接続中…", "");
    }
  };
}

function openLogs(card) {
  selected = card.job_id;
  logCursor = null;
  document.getElementById("logs").hidden = false;
  document.getElementById("logs-title").textContent =
    "ログ: " + (card.test || "") + " " + card.job_id;
  document.getElementById("log-lines").replaceChildren();
  renderJobs();
  loadLogs(false);
}

async function loadLogs(older) {
  if (!selected) return;
  const levels = document.getElementById("all-levels").checked
    ? ["error", "warning", "info", "debug", "unknown"]
    : ["error", "warning"];
  const body = { job_id: selected, levels: levels, context: 2, limit: 100 };
  if (older && logCursor) body.cursor = logCursor;
  const response = await fetch("/api/job-logs", {
    method: "POST",
    headers: { "content-type": "application/json", "x-csrf-token": csrf },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  const lines = document.getElementById("log-lines");
  if (!older) lines.replaceChildren();
  let result = null;
  try { result = await response.json(); } catch (error) { result = null; }
  if (!response.ok || !result || result.ok !== true) {
    const reason = (result && result.error) || response.status;
    lines.append(el("span", "l-error", "読めません: " + reason + "\n"));
    document.getElementById("older").hidden = true;
    return;
  }
  if (result.status === "expired") {
    lines.append(el("span", "muted", "（ログの保持期限が切れています）\n"));
  }
  for (const entry of result.entries || []) {
    lines.append(el("span", "l-" + (entry.level || "unknown"), (entry.text || "") + "\n"));
  }
  logCursor = result.next_cursor || null;
  document.getElementById("older").hidden = !result.has_more;
}

document.getElementById("close-logs").addEventListener("click", () => {
  selected = null;
  document.getElementById("logs").hidden = true;
  renderJobs();
});
document.getElementById("all-levels").addEventListener("change", () => loadLogs(false));
document.getElementById("older").addEventListener("click", () => loadLogs(true));
connect();
"""
