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
        '<button type="button" id="pip" hidden>PiP</button>'
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
        '<video id="pip-video" class="pipvideo" muted playsinline></video>'
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
header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; padding: 12px 16px;
  border-bottom: 1px solid var(--line); position: sticky; top: 0; background: var(--bg); }
header h1 { font-size: 17px; margin: 0; flex: 1 1 auto; }
header form { margin: 0; }
main { padding: 12px 16px 48px; max-width: 880px; margin: 0 auto; }
h2 { font-size: 15px; margin: 20px 0 8px; }
button { font: inherit; padding: 6px 12px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--card); color: var(--fg); white-space: nowrap; flex-shrink: 0; }
button.failed { white-space: normal; text-align: left; }
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
.pipvideo { position: fixed; right: 0; bottom: 0; width: 2px; height: 2px; opacity: 0;
  pointer-events: none; }
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
let retryTimer = null;
let ended = false;
let lastData = Date.now();
let lastConnect = Date.now();
const STALE_MS = 40000;
const RETRY_MS = 5000;

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
    retryTimer = setTimeout(recover, RETRY_MS);
    return;
  }
  lastData = Date.now();
  if (message.kind === "heartbeat") return;
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
  noteChanges();
  drawPip();
}

// The stream is replaced, not trusted: iOS drops sockets of a page that was in the
// background without telling EventSource, so silence is treated as a dead stream.
function connect() {
  clearTimeout(retryTimer);
  if (source) source.close();
  lastConnect = Date.now();
  const current = new EventSource("/api/events");
  source = current;
  current.onopen = () => setLive("ライブ", "on");
  current.onmessage = (event) => {
    try { apply(JSON.parse(event.data)); } catch (error) { setLive("受信エラー", "off"); }
  };
  current.onerror = () => {
    if (current !== source) return;
    if (current.readyState === EventSource.CLOSED) {
      setLive("再接続待ち…", "");
      retryTimer = setTimeout(recover, RETRY_MS);
    } else {
      setLive("再接続中…", "");
    }
  };
}

// After a refusal, tell an ended session (log in again) from a passing failure (retry).
async function recover() {
  try {
    const response = await fetch("/api/state", { credentials: "same-origin" });
    if (response.status === 401) {
      setLive("ログイン切れ（再読み込みしてください）", "off");
      ended = true;
      return;
    }
  } catch (error) {
    // Offline: connect() below fails again and the next retry is scheduled.
  }
  connect();
}

function dataAge() {
  return Date.now() - lastData;
}

setInterval(() => {
  if (!ended && Date.now() - Math.max(lastData, lastConnect) > STALE_MS) connect();
}, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !ended) connect();
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted && !ended) connect();
});

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

// Picture-in-picture: the latest job drawn on a canvas and shown as a <video> (iOS only
// allows video). Nothing is sent: it draws what the page already received over SSE.
const pipVideo = document.getElementById("pip-video");
const pipButton = document.getElementById("pip");
let pipCanvas = null;
let pipTimer = null;

function pipSupported() {
  return typeof pipVideo.requestPictureInPicture === "function" ||
    typeof pipVideo.webkitSetPresentationMode === "function";
}

function pipJob() {
  const live = (card) => card.state === "running" || card.state === "queued";
  return cards.find(live) || cards[0] || null;
}

function pipElapsed(card) {
  if (!card || !card.created_utc) return "";
  const start = new Date(card.created_utc).getTime();
  const live = card.state === "running" || card.state === "queued";
  const end = live || !card.finished_utc ? Date.now() : new Date(card.finished_utc).getTime();
  if (isNaN(start) || isNaN(end)) return "";
  const total = Math.max(0, Math.round((end - start) / 1000));
  const two = (n) => String(n).padStart(2, "0");
  return Math.floor(total / 60) + ":" + two(total % 60);
}

// State colours as RGB. The window tints toward the shown job's colour and pulses once
// when it ends, so the result reads at a glance without sound.
const TONES = { succeeded: [31, 122, 58], failed: [180, 35, 24], cancelled: [180, 35, 24],
  running: [29, 78, 216], queued: [29, 78, 216], needs_review: [161, 92, 0] };
const ENDED = new Set(["succeeded", "failed", "cancelled", "needs_review"]);
const calmMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let seenStates = null;
let flash = null;
let tint = null;
let tintAt = 0;
let pipFast = null;

const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
const rgb = (c) => "rgb(" + c.map((v) => Math.round(v)).join(",") + ")";

// Compares job states with the last ones seen; the first snapshot only records them.
function noteChanges() {
  const shown = pipJob();
  if (seenStates !== null && shown && ENDED.has(shown.state) &&
      seenStates.get(shown.job_id) !== shown.state) {
    flash = { at: Date.now() };
  }
  seenStates = new Map(cards.map((card) => [card.job_id, card.state]));
}

// Redraw every 100 ms only while a colour is moving or pulsing; otherwise once a second.
function setFast(on) {
  if (on && !pipFast) pipFast = setInterval(drawPip, 100);
  else if (!on && pipFast) { clearInterval(pipFast); pipFast = null; }
}

// A thin strip (15:2) keeps the window small: laid out in 240x32 units, drawn at 360x48
// pixels (fewer than the 320x180 it replaces). Top row: mark, kind, time; below it a ticker.
const PIP_W = 240;
const PIP_H = 32;
const PIP_SCALE = 1.5;
const KINDS = { firmware_build: "BUILD", firmware_fetch_source: "FETCH", firmware_flash: "FLASH",
  firmware_restore: "RESTORE", firmware_backup: "BACKUP", hello: "HELLO", capture_home: "CAPTURE",
  app_session: "APP", diagnostics: "DIAG", passive_observe: "OBSERVE", firmware_observe: "FW OBS",
  restart_and_observe: "RESTART", derby_smoke: "DERBY", derby_fixed_workload: "DERBY",
  firmware_workflow: "WORKFLOW" };

const TICKER_SPEED = 12;
const TICKER_HOLD_MS = 2000;
let tickerNote = "";
let tickerAt = 0;

// Only values the page already shows: job id, stage and the result or reason code.
function tickerText(card) {
  const outcome = card.failure_reason ? "理由: " + card.failure_reason
    : card.result ? "結果: " + card.result : "";
  return [card.job_id, card.stage, outcome].filter(Boolean).join("  ·  ");
}

function shortKind(card) {
  return KINDS[card.test] || String(card.test || card.job_id || "?").toUpperCase().slice(0, 8);
}

// The state as a mark, not a word: a turning arc (running), a blinking dot (queued),
// a check (succeeded), a cross (failed, cancelled) or an exclamation mark (needs review).
function drawIcon(g, state, x, y, now, size) {
  const step = Math.floor(now / 1000);
  g.save();
  g.translate(x, y);
  g.scale(size, size);
  const cx = 0;
  const cy = 0;
  g.lineWidth = 4;
  g.lineCap = "round";
  g.lineJoin = "round";
  g.strokeStyle = "#ffffff";
  g.fillStyle = "#ffffff";
  g.beginPath();
  if (state === "running") {
    g.globalAlpha = 0.35;
    g.arc(cx, cy, 15, 0, 2 * Math.PI);
    g.stroke();
    g.globalAlpha = 1;
    g.beginPath();
    const from = (step % 8) * (Math.PI / 4);
    g.arc(cx, cy, 15, from, from + Math.PI * 0.6);
    g.stroke();
  } else if (state === "queued") {
    g.arc(cx, cy, 15, 0, 2 * Math.PI);
    g.stroke();
    if (step % 2 === 0) {
      g.beginPath();
      g.arc(cx, cy, 6, 0, 2 * Math.PI);
      g.fill();
    }
  } else if (state === "succeeded") {
    g.moveTo(cx - 11, cy);
    g.lineTo(cx - 3, cy + 8);
    g.lineTo(cx + 11, cy - 9);
    g.stroke();
  } else if (state === "failed" || state === "cancelled") {
    g.moveTo(cx - 9, cy - 9);
    g.lineTo(cx + 9, cy + 9);
    g.moveTo(cx + 9, cy - 9);
    g.lineTo(cx - 9, cy + 9);
    g.stroke();
  } else if (state === "needs_review") {
    g.moveTo(cx, cy - 12);
    g.lineTo(cx, cy + 3);
    g.stroke();
    g.beginPath();
    g.arc(cx, cy + 11, 2.5, 0, 2 * Math.PI);
    g.fill();
  } else {
    g.globalAlpha = 0.5;
    g.arc(cx, cy, 15, 0, 2 * Math.PI);
    g.stroke();
    g.globalAlpha = 1;
  }
  g.restore();
}

// A slow marquee in the strip's lower row; text that fits stays still, and so does all of
// it under reduced motion. Returns whether it is moving (the caller then redraws quickly).
function drawTicker(g, note, color, now, calm) {
  if (note !== tickerNote) {
    tickerNote = note;
    tickerAt = now;
  }
  g.save();
  g.beginPath();
  g.rect(6, 21, PIP_W - 12, 11);
  g.clip();
  g.font = "9px system-ui, sans-serif";
  g.fillStyle = color;
  g.textAlign = "left";
  const width = g.measureText(note).width;
  let moving = false;
  if (width <= PIP_W - 12 || calm) {
    g.fillText(note, 6, 29);
  } else {
    const span = width + 48;
    const offset = (Math.max(0, now - tickerAt - TICKER_HOLD_MS) / 1000) * TICKER_SPEED % span;
    g.fillText(note, 6 - offset, 29);
    g.fillText(note, 6 - offset + span, 29);
    moving = true;
  }
  g.restore();
  return moving;
}

function drawPip() {
  if (!pipCanvas) return;
  const g = pipCanvas.getContext("2d");
  g.setTransform(PIP_SCALE, 0, 0, PIP_SCALE, 0, 0);
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const base = dark ? [21, 21, 20] : [247, 247, 245];
  const card = pipJob();
  const now = Date.now();
  const calm = calmMotion.matches;
  const tone = card ? TONES[card.state] : null;
  const goal = tone ? mix(base, tone, 0.9) : base;
  if (tint === null || calm) {
    tint = goal;
  } else {
    const k = 1 - Math.exp(-(now - tintAt) / 250);
    tint = tint.map((v, i) => v + (goal[i] - v) * k);
  }
  tintAt = now;
  g.fillStyle = rgb(tint);
  g.fillRect(0, 0, PIP_W, PIP_H);
  // Three slow pulses (1.5 Hz) when a job ends; none under reduced motion.
  let pulsing = false;
  if (flash && !calm) {
    const t = (now - flash.at) / 1000;
    if (t < 2) {
      pulsing = true;
      g.fillStyle = "rgba(255,255,255," + 0.5 * Math.max(0, Math.sin(2 * Math.PI * 1.5 * t)) + ")";
      g.fillRect(0, 0, PIP_W, PIP_H);
    } else {
      flash = null;
    }
  } else {
    flash = null;
  }
  // White on a state colour; the ordinary text colour when no job is shown.
  const ink = tone ? "#ffffff" : dark ? "#ececea" : "#1d1d1b";
  const sub = tone ? "rgba(255,255,255,.8)" : dark ? "rgba(236,236,234,.8)" : "rgba(29,29,27,.75)";
  let scrolling = false;
  const text = (value, x, y, size, color, bold, align) => {
    g.font = (bold ? "bold " : "") + size + "px system-ui, sans-serif";
    g.fillStyle = color;
    g.textAlign = align || "left";
    g.fillText(String(value), x, y);
  };
  if (!card) {
    text("ジョブなし", 10, 20, 14, ink, true);
  } else {
    drawIcon(g, card.state, 14, 10, now, 0.5);
    text(shortKind(card), 28, 15, 13, ink, true);
    // Elapsed time is the page's own clock: a frozen figure means the page is suspended.
    text(pipElapsed(card) || "--:--", 190, 18, 18, ink, true, "right");
    scrolling = drawTicker(g, tickerText(card), sub, now, calm);
  }
  // Seconds since the last server message: this one shows whether the stream is alive.
  const age = dataAge();
  const seconds = Math.floor(age / 1000);
  if (age > STALE_MS) {
    g.fillStyle = rgb(TONES.failed);
    g.fillRect(0, 0, PIP_W, 2);
    text("停止 " + seconds + "s", PIP_W - 6, 14, 10, "#ffffff", true, "right");
  } else {
    text(seconds + "s", PIP_W - 6, 14, 10, sub, false, "right");
  }
  setFast(scrolling || pulsing || tint.some((v, i) => Math.abs(v - goal[i]) > 1.5));
}

// The video is made and played before the tap: Safari only enters PiP from the tap itself,
// not after an awaited play(), and a stream with no data yet cannot enter at all.
function preparePip() {
  pipCanvas = document.createElement("canvas");
  pipCanvas.width = PIP_W * PIP_SCALE;
  pipCanvas.height = PIP_H * PIP_SCALE;
  drawPip();
  pipVideo.srcObject = pipCanvas.captureStream(15);
  pipTimer = setInterval(drawPip, 1000);
  pipVideo.play().catch(() => {});
}

function pipFailed(step, error) {
  pipButton.textContent = "PiPを開始できません（" + step + ": " +
    ((error && error.name) || "unknown") + "）";
  pipButton.classList.add("failed");
  setTimeout(() => {
    pipButton.textContent = "PiP";
    pipButton.classList.remove("failed");
  }, 8000);
}

function startPip() {
  try {
    if (typeof pipVideo.requestPictureInPicture === "function") {
      pipVideo.requestPictureInPicture().catch((error) => pipFailed("pip", error));
    } else {
      pipVideo.webkitSetPresentationMode("picture-in-picture");
      setTimeout(() => {
        if (pipVideo.webkitPresentationMode !== "picture-in-picture") {
          pipFailed("pip", { name: "not_started readyState=" + pipVideo.readyState });
        }
      }, 1500);
    }
  } catch (error) {
    pipFailed("pip", error);
  }
}

if (pipSupported()) {
  pipButton.hidden = false;
  pipButton.addEventListener("click", startPip);
  preparePip();
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
