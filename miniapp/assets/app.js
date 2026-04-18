// Cockpit v1 — vanilla JS, без сборки.
// Опрашивает /api/stats, /api/agents, /api/agents/{name}/status, /api/activity.
// Телеграм WebApp SDK — через глобальный window.Telegram.WebApp.

const POLL_MS = 5000;

const tg = window.Telegram?.WebApp;
if (tg) {
  try { tg.ready(); tg.expand(); } catch (_) {}
}

/** Источник `origin_agent`: query-параметр, выставленный /dashboard командой. */
function originAgent() {
  const url = new URL(location.href);
  return url.searchParams.get("origin_agent") || "";
}

/** initData из Telegram или dev-fallback. */
function initData() {
  if (tg?.initData) return tg.initData;
  // Dev-режим: разрешаем задать в localStorage для локального прогона.
  return localStorage.getItem("tma_init_data") || "";
}

async function api(path) {
  const res = await fetch(path, {
    headers: {
      "Authorization": `tma ${initData()}`,
      "X-Origin-Agent": originAgent(),
    },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${path}: ${body.slice(0, 160)}`);
  }
  return res.json();
}

// ── Rendering ─────────────────────────────────────────────────────────────

function fmtTs(iso) {
  // "2026-04-18T14:05" → локальное HH:MM если сегодня, иначе DD.MM HH:MM
  if (!iso) return "";
  const [date, time] = iso.split("T");
  const today = new Date().toISOString().slice(0, 10);
  return date === today ? time : `${date.slice(8,10)}.${date.slice(5,7)} ${time}`;
}

function renderStats(stats) {
  const t = stats?.totals || {};
  setStat("total_calls", t.total_calls ?? 0);
  setStat("errors", t.errors ?? 0);
  setStat("tool_calls", t.tool_calls ?? 0);
  setStat("avg_latency", t.avg_latency ? `${t.avg_latency}s` : "—");
}

function setStat(name, value) {
  const el = document.querySelector(`[data-stat="${name}"]`);
  if (el) el.textContent = String(value);
}

function renderAgents(agents, statuses) {
  const host = document.getElementById("agents");
  if (!agents.length) {
    host.innerHTML = `<p style="color:var(--hint);padding:12px">Нет запущенных агентов.</p>`;
    return;
  }
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
    {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]
  ));
  host.innerHTML = agents.map((a) => {
    const s = statuses[a.name] || {};
    const busy = s.busy;
    const avatarLetter = (a.display_name || a.name).slice(0, 1).toUpperCase();
    const statusText = busy
      ? `занят · ${s.active_count || 1} задач(и)`
      : "свободен";
    return `
      <div class="agent">
        <div class="agent-avatar">${esc(avatarLetter)}</div>
        <div class="agent-body">
          <div class="agent-name">
            <span class="dot ${busy ? "warn" : "ok"}"></span>
            <span>${esc(a.display_name || a.name)}</span>
            ${a.is_master ? '<span class="badge master">master</span>' : ""}
          </div>
          <div class="agent-sub">${esc(statusText)}</div>
        </div>
      </div>
    `;
  }).join("");
}

function renderFeed(events) {
  const host = document.getElementById("feed");
  if (!events.length) {
    host.innerHTML = `<li class="feed-item" style="color:var(--hint)">Пока пусто.</li>`;
    return;
  }
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
    {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]
  ));
  host.innerHTML = events.map((e) => `
    <li class="feed-item">
      <span class="feed-meta">${esc(fmtTs(e.ts))}</span>
      <span class="feed-agent">${esc(e.agent)}</span>
      <span class="feed-text ${e.role === "assistant" ? "assistant" : ""}">${esc(e.preview || "—")}</span>
    </li>
  `).join("");
}

// ── Refresh cycle ─────────────────────────────────────────────────────────

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  const btn = document.getElementById("refresh");
  const dot = document.getElementById("conn-dot");
  const errBox = document.getElementById("err");
  btn.classList.add("spinning");
  try {
    const [agents, stats, activity] = await Promise.all([
      api("/api/agents"),
      api("/api/stats?period=today"),
      api("/api/activity?limit=15"),
    ]);
    // Статусы по каждому агенту
    const statusList = await Promise.all(
      agents.agents.map((a) => api(`/api/agents/${encodeURIComponent(a.name)}/status`).catch(() => ({})))
    );
    const statuses = {};
    agents.agents.forEach((a, i) => { statuses[a.name] = statusList[i] || {}; });

    renderStats(stats);
    renderAgents(agents.agents, statuses);
    renderFeed(activity.events);

    dot.className = "dot ok";
    errBox.classList.add("hidden");
    document.getElementById("last-updated").textContent =
      "обновлено " + new Date().toLocaleTimeString();

    if (tg?.HapticFeedback) {
      try { tg.HapticFeedback.impactOccurred("light"); } catch (_) {}
    }
  } catch (e) {
    dot.className = "dot err";
    errBox.classList.remove("hidden");
    errBox.textContent = `Ошибка загрузки: ${e.message}`;
  } finally {
    btn.classList.remove("spinning");
    refreshing = false;
  }
}

document.getElementById("refresh").addEventListener("click", refresh);

// Initial paint with skeletons
document.querySelectorAll(".stats-value").forEach((el) => {
  el.classList.add("skeleton");
  el.textContent = "000";
});

refresh().then(() => {
  document.querySelectorAll(".stats-value").forEach((el) => el.classList.remove("skeleton"));
});
setInterval(refresh, POLL_MS);
