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

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Authorization": `tma ${initData()}`,
      "X-Origin-Agent": originAgent(),
      ...(opts.headers || {}),
    },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${path}: ${body.slice(0, 160)}`);
  }
  // 204 или пустой body допустимы для POST
  const text = await res.text();
  return text ? JSON.parse(text) : {};
}

// ── User / founder gating ────────────────────────────────────────────────

let me = { is_founder: false, user_id: 0 };

async function loadMe() {
  try {
    me = await api("/api/me");
  } catch (_) {
    me = { is_founder: false, user_id: 0 };
  }
}

// ── Toast / popup helpers ────────────────────────────────────────────────

function toast(msg) {
  // Telegram WebApp-native, если SDK есть; иначе fallback в #err
  if (tg?.showPopup) {
    try {
      tg.showPopup({ message: String(msg).slice(0, 250), buttons: [{ type: "ok" }] });
      return;
    } catch (_) {}
  }
  const box = document.getElementById("err");
  box.classList.remove("hidden");
  box.textContent = msg;
  setTimeout(() => box.classList.add("hidden"), 3000);
}

/** Открыть меню действий для агента. Использует tg.showPopup если доступен. */
function openAgentActions(name, displayName) {
  if (tg?.showPopup) {
    tg.showPopup(
      {
        title: displayName || name,
        message: "Управление агентом",
        buttons: [
          { id: "restart", type: "default", text: "Перезапустить" },
          { id: "stop", type: "destructive", text: "Остановить" },
          { id: "cancel", type: "cancel" },
        ],
      },
      (buttonId) => {
        if (buttonId === "stop") doAction(name, "stop");
        else if (buttonId === "restart") doAction(name, "restart");
      }
    );
  } else {
    // Fallback для обычного браузера
    const choice = window.prompt(`Агент ${name}: stop | restart`, "restart");
    if (choice === "stop" || choice === "restart") doAction(name, choice);
  }
}

async function doAction(name, action) {
  try {
    await api(`/api/agents/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    if (tg?.HapticFeedback) {
      try { tg.HapticFeedback.notificationOccurred("success"); } catch (_) {}
    }
    toast(action === "stop" ? `Агент ${name} остановлен.` : `Агент ${name} перезапущен.`);
    // мгновенный рефреш — чтобы статус обновился не через 5 сек
    refresh();
  } catch (e) {
    if (tg?.HapticFeedback) {
      try { tg.HapticFeedback.notificationOccurred("error"); } catch (_) {}
    }
    toast(`Не удалось: ${e.message}`);
  }
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

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
  {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]
));

function renderAgents(agents, statuses) {
  const host = document.getElementById("agents");
  if (!agents.length) {
    host.innerHTML = `<p style="color:var(--hint);padding:12px">Нет запущенных агентов.</p>`;
    return;
  }
  host.innerHTML = agents.map((a) => {
    const s = statuses[a.name] || {};
    const busy = s.busy;
    const avatarLetter = (a.display_name || a.name).slice(0, 1).toUpperCase();
    const statusText = busy
      ? `занят · ${s.active_count || 1} задач(и)`
      : "свободен";
    const actionBtn = me.is_founder
      ? `<button class="agent-menu" data-agent="${esc(a.name)}" data-display="${esc(a.display_name || a.name)}" aria-label="Действия">⋯</button>`
      : "";
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
        ${actionBtn}
      </div>
    `;
  }).join("");
}

function renderFeed(events) {
  const host = document.getElementById("feed");
  if (!events.length) {
    host.innerHTML = `<li class="feed-card feed-empty">Пока пусто.</li>`;
    return;
  }
  host.innerHTML = events.map((e) => {
    const avatarLetter = (e.agent || "?").slice(0, 1).toUpperCase();
    const roleLabel = {
      user: "пользователь",
      assistant: "ответ",
      system: "система",
    }[e.role] || e.role || "";
    return `
      <li class="feed-card">
        <div class="feed-avatar">${esc(avatarLetter)}</div>
        <div class="feed-body">
          <div class="feed-head">
            <span class="feed-who">${esc(e.agent)}</span>
            <span class="feed-verified" title="Источник: agent log.md" aria-label="verified">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="20 6 9 17 4 12"></polyline>
              </svg>
            </span>
            ${roleLabel ? `<span class="feed-role feed-role-${esc(e.role || "")}">${esc(roleLabel)}</span>` : ""}
            <span class="feed-time">${esc(fmtTs(e.ts))}</span>
          </div>
          <div class="feed-preview">${esc(e.preview || "—")}</div>
        </div>
      </li>
    `;
  }).join("");
}

// Event delegation: клик на ⋯ в списке агентов
document.getElementById("agents").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".agent-menu");
  if (!btn) return;
  ev.stopPropagation();
  openAgentActions(btn.dataset.agent, btn.dataset.display);
});

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

document.getElementById("refresh").addEventListener("click", () => {
  if (state.tab === "cockpit") refresh();
  else if (state.tab === "brain") brain.reload();
  else if (state.tab === "market") market.reload();
});

// ── Tabs ──────────────────────────────────────────────────────────────────

const state = { tab: "cockpit" };

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll(".view").forEach((v) => {
    v.classList.toggle("hidden", v.dataset.view !== name);
  });
  document.getElementById("hdr-heading").textContent =
    name === "brain" ? "Мозг" : name === "market" ? "Маркет" : "Cockpit";
  if (name === "brain") brain.ensureLoaded();
  if (name === "market") market.ensureLoaded();
}

document.querySelectorAll(".tab").forEach((b) =>
  b.addEventListener("click", () => switchTab(b.dataset.tab))
);

// ── Brain tab (wiki browser) ─────────────────────────────────────────────

const brain = {
  agents: [],          // [{name, display_name, is_master}, ...]
  agent: null,         // выбранный агент
  tree: [],            // [{path, type, size}, ...] — плоский
  cwd: "",             // текущая директория (prefix)
  selected: null,      // {path, content, size}
  loaded: false,

  async ensureLoaded() {
    if (this.loaded) return;
    try {
      const data = await api("/api/agents");
      const all = data.agents || [];
      // Non-founder: показываем только те, что есть в accessible_agents
      const allowed = me.is_founder
        ? null
        : new Set(me.accessible_agents || []);
      this.agents = allowed ? all.filter((a) => allowed.has(a.name)) : all;
      this.agent = this.agents[0]?.name || null;
      this.renderAgentSelect();
      if (this.agent) await this.loadTree();
      this.loaded = true;
    } catch (e) {
      toast(`Не получилось загрузить агентов: ${e.message}`);
    }
  },

  async reload() {
    this.loaded = false;
    this.selected = null;
    this.cwd = "";
    await this.ensureLoaded();
  },

  async loadTree() {
    const data = await api(`/api/agents/${encodeURIComponent(this.agent)}/memory/tree`);
    this.tree = data.nodes || [];
    this.render();
  },

  renderAgentSelect() {
    const sel = document.getElementById("brain-agent");
    sel.innerHTML = this.agents.map((a) =>
      `<option value="${esc(a.name)}">${esc(a.display_name || a.name)}</option>`
    ).join("");
    sel.value = this.agent || "";
    sel.onchange = async () => {
      this.agent = sel.value;
      this.cwd = "";
      this.selected = null;
      await this.loadTree();
    };
  },

  // Прямые дети cwd: папка "X" если есть node path=="cwd/X..."; файл — точный path==cwd/X
  directChildren() {
    const prefix = this.cwd ? this.cwd + "/" : "";
    const folders = new Set();
    const files = [];
    for (const n of this.tree) {
      if (!n.path.startsWith(prefix) && prefix) continue;
      const rest = n.path.slice(prefix.length);
      if (!rest || rest.includes("/")) {
        // глубже — возьмём только имя первого сегмента как папку
        const seg = rest.split("/")[0];
        if (seg) folders.add(seg);
        continue;
      }
      // прямой файл или пустой dir
      if (n.type === "dir") folders.add(rest);
      else files.push({ ...n, name: rest });
    }
    return {
      folders: [...folders].sort(),
      files: files.sort((a, b) => a.name.localeCompare(b.name)),
    };
  },

  render() {
    const path = document.getElementById("brain-path");
    const tree = document.getElementById("brain-tree");
    const content = document.getElementById("brain-content");
    const back = document.getElementById("brain-back");

    path.textContent = this.cwd ? `/${this.cwd}` : "/";

    if (this.selected) {
      tree.classList.add("hidden");
      content.classList.remove("hidden");
      back.classList.remove("hidden");
      content.innerHTML =
        `<div class="brain-filename">${esc(this.selected.path)}</div>` +
        renderMarkdown(this.selected.content);
      return;
    }

    content.classList.add("hidden");
    tree.classList.remove("hidden");
    back.classList.toggle("hidden", !this.cwd);

    const { folders, files } = this.directChildren();
    const rows = [];
    for (const f of folders) {
      rows.push(`<li class="tree-row tree-dir" data-kind="dir" data-name="${esc(f)}">
        <span class="tree-icon">📁</span>
        <span class="tree-label">${esc(f)}</span>
        <span class="tree-chev">›</span>
      </li>`);
    }
    for (const f of files) {
      const kb = f.size > 1024 ? `${Math.round(f.size / 1024)} KB` : `${f.size} B`;
      rows.push(`<li class="tree-row tree-file" data-kind="file" data-path="${esc(f.path)}">
        <span class="tree-icon">📄</span>
        <span class="tree-label">${esc(f.name)}</span>
        <span class="tree-size">${esc(kb)}</span>
      </li>`);
    }
    tree.innerHTML = rows.join("") ||
      `<li class="tree-empty">Пусто</li>`;
  },

  async openFile(path) {
    try {
      const data = await api(
        `/api/agents/${encodeURIComponent(this.agent)}/memory/file?path=${encodeURIComponent(path)}`
      );
      this.selected = data;
      this.render();
    } catch (e) {
      toast(`Не удалось открыть: ${e.message}`);
    }
  },

  goBack() {
    if (this.selected) {
      this.selected = null;
      this.render();
      return;
    }
    if (this.cwd) {
      const parts = this.cwd.split("/");
      parts.pop();
      this.cwd = parts.join("/");
      this.render();
    }
  },

  enterDir(name) {
    this.cwd = this.cwd ? `${this.cwd}/${name}` : name;
    this.render();
  },
};

document.getElementById("brain-tree").addEventListener("click", (ev) => {
  const row = ev.target.closest(".tree-row");
  if (!row) return;
  if (row.dataset.kind === "dir") brain.enterDir(row.dataset.name);
  else if (row.dataset.kind === "file") brain.openFile(row.dataset.path);
});
document.getElementById("brain-back").addEventListener("click", () => brain.goBack());

// ── Minimal Markdown renderer ─────────────────────────────────────────────

function renderMarkdown(md) {
  if (!md) return "";
  // Code blocks first (fence ```…```), иначе inline-код поймает всё.
  const blocks = [];
  const withoutFences = md.replace(/```([a-z0-9_-]*)\n([\s\S]*?)```/gi, (_, _lang, code) => {
    const i = blocks.push(code) - 1;
    return `\u0000BLOCK${i}\u0000`;
  });

  const lines = withoutFences.split("\n");
  const out = [];
  let inList = false;

  const flushList = () => {
    if (inList) { out.push("</ul>"); inList = false; }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.+)$/))) {
      flushList();
      const lvl = m[1].length;
      out.push(`<h${lvl} class="md-h${lvl}">${inlineMd(m[2])}</h${lvl}>`);
    } else if ((m = line.match(/^[-*]\s+(.+)$/))) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inlineMd(m[1])}</li>`);
    } else if (line === "") {
      flushList();
      out.push("");
    } else if (line.startsWith("\u0000BLOCK")) {
      flushList();
      out.push(line);
    } else {
      flushList();
      out.push(`<p>${inlineMd(line)}</p>`);
    }
  }
  flushList();

  let html = out.join("\n");
  html = html.replace(/\u0000BLOCK(\d+)\u0000/g, (_, i) =>
    `<pre class="md-code"><code>${esc(blocks[Number(i)])}</code></pre>`
  );
  return `<div class="md">${html}</div>`;
}

function inlineMd(s) {
  // Порядок: code → bold → italic → link (экранируем сразу, потом делаем markup)
  // Работаем через placeholder-sentinels, чтобы избежать повторных замен внутри кода
  const codeSpans = [];
  let t = esc(s).replace(/`([^`]+)`/g, (_, c) => {
    const i = codeSpans.push(c) - 1;
    return `\u0001CODE${i}\u0001`;
  });
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, text, url) => {
    const safe = /^(https?:|mailto:|\/)/i.test(url) ? url : "#";
    return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  });
  t = t.replace(/\[\[([^\]]+)\]\]/g, (_, slug) =>
    `<a class="wikilink" data-slug="${slug}">${slug}</a>`
  );
  t = t.replace(/\u0001CODE(\d+)\u0001/g, (_, i) =>
    `<code>${codeSpans[Number(i)]}</code>`
  );
  return t;
}

// wikilinks: клик → открыть файл в памяти (если есть по slug)
document.getElementById("brain-content").addEventListener("click", (ev) => {
  const a = ev.target.closest(".wikilink");
  if (!a) return;
  ev.preventDefault();
  const slug = a.dataset.slug;
  // попробовать найти файл по хвосту имени
  const candidates = brain.tree.filter(
    (n) => n.type === "file" && n.path.endsWith(`/${slug}.md`) || n.path === `${slug}.md`
  );
  if (candidates.length) {
    brain.openFile(candidates[0].path);
  } else {
    toast(`Не нашёл ${slug}.md`);
  }
});

// ── Market tab (skill marketplace) ───────────────────────────────────────

const market = {
  loaded: false,
  pool: null,           // {available, reason, skills}
  agents: [],           // целевые агенты для установки
  installed: {},        // agent → Set of installed skill names
  filter: "",

  async ensureLoaded() {
    if (this.loaded) return;
    await this.loadAgents();
    await this.loadPool();
    await this.loadInstalled();
    this.render();
    this.loaded = true;
  },

  async reload() {
    this.loaded = false;
    await this.ensureLoaded();
  },

  async loadAgents() {
    try {
      const data = await api("/api/agents");
      const all = data.agents || [];
      const allowed = me.is_founder ? null : new Set(me.accessible_agents || []);
      this.agents = allowed ? all.filter((a) => allowed.has(a.name)) : all;
    } catch (_) {
      this.agents = [];
    }
  },

  async loadPool() {
    try {
      this.pool = await api("/api/skills/pool");
    } catch (e) {
      this.pool = { available: false, reason: e.message, skills: [] };
    }
  },

  async loadInstalled() {
    const map = {};
    await Promise.all(
      this.agents.map(async (a) => {
        try {
          const data = await api(`/api/agents/${encodeURIComponent(a.name)}/skills`);
          map[a.name] = new Set((data.skills || []).map((s) => s.name));
        } catch (_) {
          map[a.name] = new Set();
        }
      })
    );
    this.installed = map;
  },

  render() {
    const statusEl = document.getElementById("market-status");
    const listEl = document.getElementById("market-list");

    if (!this.pool?.available) {
      statusEl.classList.remove("hidden");
      statusEl.textContent = this.pool?.reason
        ? `Пул недоступен: ${this.pool.reason}`
        : "Пул недоступен";
      listEl.innerHTML = "";
      return;
    }
    statusEl.classList.add("hidden");

    const q = this.filter.trim().toLowerCase();
    const skills = (this.pool.skills || []).filter((s) => {
      if (!q) return true;
      return (
        (s.name || "").toLowerCase().includes(q) ||
        (s.description || "").toLowerCase().includes(q) ||
        (s.tags || []).some((t) => t.toLowerCase().includes(q))
      );
    });

    if (!skills.length) {
      listEl.innerHTML = `<li class="market-empty">Ничего не найдено.</li>`;
      return;
    }

    listEl.innerHTML = skills.map((s) => {
      // где уже установлен?
      const installedIn = this.agents
        .filter((a) => this.installed[a.name]?.has(s.name))
        .map((a) => a.display_name || a.name);
      const tags = (s.tags || []).slice(0, 3)
        .map((t) => `<span class="market-tag">${esc(t)}</span>`).join("");
      return `
        <li class="market-item">
          <div class="market-head">
            <span class="market-name">${esc(s.name)}</span>
            ${s.version ? `<span class="market-version">v${esc(s.version)}</span>` : ""}
            <button class="btn-mini market-install" data-skill="${esc(s.name)}">
              Установить
            </button>
          </div>
          ${s.description ? `<div class="market-desc">${esc(s.description)}</div>` : ""}
          <div class="market-meta">
            ${tags}
            ${installedIn.length ? `<span class="market-installed">✓ у ${esc(installedIn.join(", "))}</span>` : ""}
          </div>
        </li>
      `;
    }).join("");
  },

  async install(skill) {
    if (!this.agents.length) {
      toast("Нет доступных агентов.");
      return;
    }
    const target = await this.pickAgent();
    if (!target) return;
    try {
      const res = await api(
        `/api/agents/${encodeURIComponent(target)}/skills/install`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill }),
        }
      );
      if (tg?.HapticFeedback) {
        try { tg.HapticFeedback.notificationOccurred("success"); } catch (_) {}
      }
      const warn = res.missing_memory?.length
        ? ` (нет в памяти: ${res.missing_memory.join(", ")})`
        : "";
      toast(`'${skill}' установлен для ${target}${warn}`);
      // локально пометим как установленный без полного reload
      if (!this.installed[target]) this.installed[target] = new Set();
      this.installed[target].add(skill);
      this.render();
    } catch (e) {
      if (tg?.HapticFeedback) {
        try { tg.HapticFeedback.notificationOccurred("error"); } catch (_) {}
      }
      toast(`Не получилось: ${e.message}`);
    }
  },

  async pickAgent() {
    if (this.agents.length === 1) return this.agents[0].name;
    // Используем tg.showPopup если 2-3 агента; иначе fallback prompt
    if (tg?.showPopup && this.agents.length <= 3) {
      return new Promise((resolve) => {
        tg.showPopup(
          {
            title: "Куда установить?",
            message: "Выбери агента для установки скилла",
            buttons: this.agents.map((a) => ({
              id: a.name,
              type: "default",
              text: a.display_name || a.name,
            })).concat([{ id: "__cancel", type: "cancel" }]),
          },
          (btn) => resolve(btn && btn !== "__cancel" ? btn : null)
        );
      });
    }
    const names = this.agents.map((a) => a.name).join(" | ");
    const pick = window.prompt(`Установить в агента:\n${names}`, this.agents[0].name);
    return this.agents.find((a) => a.name === pick)?.name || null;
  },

  async refresh() {
    try {
      const res = await api("/api/skills/pool/refresh", { method: "POST" });
      toast(`Пул обновлён: ${res.skills_count} скиллов`);
      await this.reload();
    } catch (e) {
      toast(`Refresh failed: ${e.message}`);
    }
  },
};

document.getElementById("market-list").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".market-install");
  if (btn) market.install(btn.dataset.skill);
});
document.getElementById("market-refresh").addEventListener("click", () => market.refresh());
document.getElementById("market-search").addEventListener("input", (ev) => {
  market.filter = ev.target.value || "";
  market.render();
});

// ── Initial paint ─────────────────────────────────────────────────────────

document.querySelectorAll(".stats-value").forEach((el) => {
  el.classList.add("skeleton");
  el.textContent = "000";
});

loadMe().then(refresh).then(() => {
  document.querySelectorAll(".stats-value").forEach((el) => el.classList.remove("skeleton"));
});
setInterval(() => {
  if (state.tab === "cockpit") refresh();
}, POLL_MS);
