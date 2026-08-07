/* Tsukuyomi AI Gateway · 管理后台 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const TOKEN_KEY = "adminToken";
  let token = sessionStorage.getItem(TOKEN_KEY) || "";

  /* ---------- 基础请求 ---------- */
  async function api(path, opts = {}) {
    const headers = Object.assign({ "X-Admin-Token": token }, opts.headers || {});
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    if (res.status === 401) {
      logout();
      throw new Error("登录已失效，请重新输入管理员令牌");
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `请求失败 (${res.status})`);
    }
    return res.json();
  }

  /* ---------- Toast ---------- */
  let toastTimer = null;
  function toast(msg, isError = false) {
    const el = $("toast");
    el.textContent = msg;
    el.style.background = isError ? "var(--danger)" : "var(--ink)";
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
  }

  /* ---------- 视图切换 ---------- */
  const TITLES = {
    dashboard: "仪表盘", users: "用户", instances: "渠道实例",
    media: "媒体审计", messages: "消息", tasks: "死信任务",
  };
  function switchView(name) {
    document.querySelectorAll(".nav-item").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === name));
    document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
    $("view-" + name).classList.remove("hidden");
    $("page-title").textContent = TITLES[name];
    if (name === "dashboard") loadDashboard();
    if (name === "users") loadUsers();
    if (name === "instances") loadInstances();
    if (name === "media") loadMedia();
    if (name === "messages") loadMessages();
    if (name === "tasks") loadTasks();
  }

  /* ---------- 登录 / 退出 ---------- */
  function enterApp() {
    $("login").classList.add("hidden");
    $("app").classList.remove("hidden");
    switchView("dashboard");
  }
  function logout() {
    sessionStorage.removeItem(TOKEN_KEY);
    token = "";
    $("app").classList.add("hidden");
    $("login").classList.remove("hidden");
    $("token-input").value = "";
    $("login-error").classList.add("hidden");
  }

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("login-btn");
    const err = $("login-error");
    token = $("token-input").value.trim();
    if (!token) return;
    btn.disabled = true;
    err.classList.add("hidden");
    try {
      await api("/api/admin/stats");
      sessionStorage.setItem(TOKEN_KEY, token);
      enterApp();
    } catch (ex) {
      err.textContent = ex.message;
      err.classList.remove("hidden");
      token = "";
    } finally {
      btn.disabled = false;
    }
  });
  $("logout").addEventListener("click", logout);

  /* ---------- 导航 ---------- */
  document.querySelectorAll(".nav-item").forEach((b) =>
    b.addEventListener("click", () => switchView(b.dataset.view)));
  $("refresh").addEventListener("click", () => {
    const active = document.querySelector(".nav-item.active").dataset.view;
    switchView(active);
    toast("已刷新");
  });

  /* ---------- 仪表盘 ---------- */
  async function loadDashboard() {
    try {
      const [stats, trend] = await Promise.all([
        api("/api/admin/stats"),
        api("/api/admin/usage/trend?days=7"),
      ]);
      $("stat-users").textContent = stats.users;
      $("stat-messages").textContent = stats.messages;
      $("stat-failed").textContent = stats.failed;
      $("stat-tokens").textContent = Number(stats.tokens).toLocaleString();
      renderTrend(trend);
      const modeText = stats.single_user_mode
        ? "单用户模式"
        : stats.mode === "managed" ? "统一管理模式" : "用户自足模式";
      $("last-updated").textContent = modeText + " · 更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
    } catch (ex) {
      toast(ex.message, true);
    }
  }

  function renderTrend(trend) {
    const box = $("trend");
    box.innerHTML = "";
    const max = Math.max(1, ...trend.map((d) => d.tokens));
    trend.forEach((d) => {
      const col = document.createElement("div");
      col.className = "trend-col";
      const val = document.createElement("div");
      val.className = "trend-val";
      val.textContent = d.tokens ? (d.tokens >= 1000 ? (d.tokens / 1000).toFixed(1) + "k" : d.tokens) : "";
      const bar = document.createElement("div");
      bar.className = "trend-bar" + (d.tokens ? "" : " zero");
      bar.style.height = Math.max(2, Math.round((d.tokens / max) * 100)) + "%";
      const label = document.createElement("div");
      label.className = "trend-label";
      label.textContent = d.date.slice(5);
      col.append(val, bar, label);
      box.appendChild(col);
    });
  }

  /* ---------- 用户 ---------- */
  async function loadUsers() {
    try {
      const rows = await api("/api/admin/users?limit=200");
      const q = ($("user-search").value || "").trim().toLowerCase();
      const list = q ? rows.filter((u) => (u.id + (u.display_name || "")).toLowerCase().includes(q)) : rows;
      $("user-tbody").innerHTML = list.length
        ? list.map((u) => {
          const modeLabel = u.mode === "managed" ? "统一管理" : u.mode === "self_service" ? "自足" : "跟随平台";
          const toggleTo = u.mode === "managed" ? "self_service" : "managed";
          return `
          <tr>
            <td class="mono">${esc(u.id.slice(0, 8))}</td>
            <td>${esc(u.display_name || "—")}</td>
            <td class="mono">${esc(u.model || "默认")}</td>
            <td>
              <span class="badge">${esc(modeLabel)}</span>
              <button class="btn btn-sm" data-mode="${u.id}" data-mode-val="${toggleTo}" title="切换用户模式">${u.mode ? "切回" : "设为统一管理"}</button>
            </td>
            <td><button class="btn btn-sm" data-detail="${u.id}" data-name="${esc(u.display_name || u.id)}">详情</button></td>
            <td>${u.blocked ? '<span class="badge badge-dead">已封禁</span>' : '<span class="badge badge-ok">正常</span>'}</td>
            <td class="mono small">${esc((u.created_at || "").replace("T", " ").slice(0, 19))}</td>
            <td><button class="btn btn-sm ${u.blocked ? "" : "btn-danger"}" data-block="${u.id}" data-state="${u.blocked ? "0" : "1"}">${u.blocked ? "解封" : "封禁"}</button></td>
          </tr>`; }).join("")
        : '<tr><td colspan="8" class="muted">暂无用户</td></tr>';
      document.querySelectorAll("[data-block]").forEach((b) =>
        b.addEventListener("click", async () => {
          const id = b.dataset.block;
          const blocked = b.dataset.state === "1";
          if (blocked && !confirm("确认封禁该用户？")) return;
          try {
            await api(`/api/admin/users/${id}/block?blocked=${blocked}`, { method: "POST" });
            toast(blocked ? "已封禁" : "已解封");
            loadUsers();
          } catch (ex) { toast(ex.message, true); }
        }));
      document.querySelectorAll("[data-mode]").forEach((b) =>
        b.addEventListener("click", async () => {
          try {
            await api(`/api/admin/users/${b.dataset.mode}/mode`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ mode: b.dataset.modeVal }),
            });
            toast("用户模式已更新");
            loadUsers();
          } catch (ex) { toast(ex.message, true); }
        }));
      document.querySelectorAll("[data-detail]").forEach((b) =>
        b.addEventListener("click", () => openUserDetail(b.dataset.detail, b.dataset.name)));
    } catch (ex) {
      toast(ex.message, true);
    }
  }
  $("user-search").addEventListener("input", () => loadUsers());

  /* ---------- 用户详情（管理端不可读内容） ---------- */
  async function openUserDetail(userId, name) {
    try {
      const [cards, presets, providers, policies, detail] = await Promise.all([
        api(`/api/admin/users/${userId}/cards`),
        api(`/api/admin/users/${userId}/presets`),
        api(`/api/admin/users/${userId}/providers`),
        api(`/api/admin/users/${userId}/policies`),
        api(`/api/admin/users/${userId}/detail`),
      ]);
      $("user-modal-title").textContent = `用户详情 · ${name}`;
      $("user-detail-grid").innerHTML = [
        ["会话数", detail.conversations],
        ["记忆条数", detail.memories],
        ["媒体记录", detail.media_assets],
        ["今日 Tokens", Number(detail.tokens_today).toLocaleString()],
      ].map(([k, v]) => `<div class="detail-item"><b>${v}</b><span>${k}</span></div>`).join("");

      $("card-tbody").innerHTML = cards.length
        ? cards.map((c) => `
          <tr>
            <td>${esc(c.name)}</td>
            <td class="mono">${esc(c.format)}</td>
            <td>${c.active ? '<span class="badge badge-ok">生效</span>' : '<span class="badge">未生效</span>'}</td>
            <td class="mono small">${esc((c.updated_at || "").replace("T", " ").slice(0, 19))}</td>
          </tr>`).join("")
        : '<tr><td colspan="4" class="muted">该用户还没有角色卡</td></tr>';

      $("preset-tbody").innerHTML = presets.length
        ? presets.map((p) => `
          <tr>
            <td>${esc(p.name)}</td>
            <td class="mono small">${esc((p.updated_at || "").replace("T", " ").slice(0, 19))}</td>
          </tr>`).join("")
        : '<tr><td colspan="2" class="muted">暂无预设</td></tr>';

      $("provider-tbody").innerHTML = providers.length
        ? providers.map((p) => `
          <tr>
            <td class="mono">${esc(p.provider_key)}</td>
            <td class="text-clip" title="${esc(p.base_url || "")}">${esc(p.base_url || "—")}</td>
            <td class="mono small">${esc((p.models || []).join(", ") || "—")}</td>
            <td>${p.is_default ? '<span class="badge badge-ok">默认</span>' : "—"}</td>
          </tr>`).join("")
        : '<tr><td colspan="4" class="muted">未配置自带供应商（使用平台默认）</td></tr>';

      $("policy-tbody").innerHTML = policies.length
        ? policies.map((p) => `
          <tr>
            <td class="mono">/${esc(p.command)}</td>
            <td>${p.user_id ? "用户" : (p.channel ? esc(p.channel) : "平台")}</td>
            <td>${p.allowed ? '<span class="badge badge-ok">允许</span>' : '<span class="badge badge-dead">禁止</span>'}</td>
            <td>${p.silent_block ? "静默" : "—"}</td>
            <td class="mono small">${esc(p.blocked_strategy)}</td>
          </tr>`).join("")
        : '<tr><td colspan="5" class="muted">无自定义策略（按模式默认执行）</td></tr>';

      $("user-modal").classList.remove("hidden");
    } catch (ex) {
      toast(ex.message, true);
    }
  }
  $("user-modal-close").addEventListener("click", () => $("user-modal").classList.add("hidden"));
  $("user-modal").addEventListener("click", (e) => {
    if (e.target === $("user-modal")) $("user-modal").classList.add("hidden");
  });

  /* ---------- 渠道实例 ---------- */
  async function loadInstances() {
    try {
      const rows = await api("/api/admin/channel-instances");
      $("inst-tbody").innerHTML = rows.length
        ? rows.map((i) => `
          <tr>
            <td>${esc(i.instance_name)}</td>
            <td class="mono">${esc(i.channel)}</td>
            <td><span class="badge badge-${i.status === "online" ? "ok" : i.status === "error" ? "dead" : "ignored"}">${esc(i.status)}</span></td>
            <td class="mono small">${esc((i.owner_user_id || "—").slice(0, 12))}</td>
            <td class="mono small">${esc((i.created_at || "").replace("T", " ").slice(0, 19))}</td>
            <td>
              <button class="btn btn-sm" data-start="${i.id}" ${i.status === "online" ? "disabled" : ""}>启动</button>
              <button class="btn btn-sm" data-stop="${i.id}" ${i.status !== "online" ? "disabled" : ""}>停止</button>
            </td>
          </tr>`).join("")
        : '<tr><td colspan="6" class="muted">暂无渠道实例</td></tr>';
      document.querySelectorAll("[data-start]").forEach((b) =>
        b.addEventListener("click", async () => {
          try {
            await api(`/api/admin/channel-instances/${b.dataset.start}/start`, { method: "POST" });
            toast("启动指令已下发");
            loadInstances();
          } catch (ex) { toast(ex.message, true); }
        }));
      document.querySelectorAll("[data-stop]").forEach((b) =>
        b.addEventListener("click", async () => {
          try {
            await api(`/api/admin/channel-instances/${b.dataset.stop}/stop`, { method: "POST" });
            toast("已停止");
            loadInstances();
          } catch (ex) { toast(ex.message, true); }
        }));
    } catch (ex) {
      toast(ex.message, true);
    }
  }
  $("inst-create").addEventListener("click", async () => {
    const name = $("inst-name").value.trim();
    const owner = $("inst-owner").value.trim();
    if (!name) { toast("实例名称必填", true); return; }
    try {
      const body = { channel: $("inst-channel").value, instance_name: name };
      if (owner) body.owner_user_id = owner;
      await api("/api/admin/channel-instances", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      $("inst-name").value = "";
      $("inst-owner").value = "";
      toast("实例已创建");
      loadInstances();
    } catch (ex) { toast(ex.message, true); }
  });

  /* ---------- 媒体审计 ---------- */
  async function loadMedia() {
    try {
      const rows = await api("/api/admin/media?limit=200");
      $("media-tbody").innerHTML = rows.length
        ? rows.slice(0, 150).map((m) => `
          <tr>
            <td class="mono small">${esc((m.created_at || "").replace("T", " ").slice(5, 19))}</td>
            <td class="mono">${esc(m.channel)}</td>
            <td>${esc(m.media_type)}</td>
            <td class="mono small">${esc(m.mime || "—")}</td>
            <td class="mono small">${m.size_bytes ? (m.size_bytes / 1024).toFixed(1) + " KB" : "—"}</td>
            <td><span class="badge badge-${m.status === "stored" ? "ok" : m.status === "rejected" ? "dead" : "ignored"}">${esc(m.status)}</span></td>
            <td class="small">${esc(m.rejected_reason || "—")}</td>
          </tr>`).join("")
        : '<tr><td colspan="7" class="muted">暂无媒体记录</td></tr>';
    } catch (ex) {
      toast(ex.message, true);
    }
  }

  /* ---------- 消息 ---------- */
  async function loadMessages() {
    try {
      const dir = $("msg-filter").value;
      const status = $("msg-status").value;
      let rows = await api("/api/admin/messages?limit=300");
      if (dir) rows = rows.filter((m) => m.direction === dir);
      if (status) rows = rows.filter((m) => m.status === status);
      $("msg-tbody").innerHTML = rows.length
        ? rows.slice(0, 200).map((m) => `
          <tr>
            <td class="mono small">${esc((m.created_at || "").replace("T", " ").slice(5, 19))}</td>
            <td><span class="badge badge-${m.direction}">${m.direction === "inbound" ? "入站" : "出站"}</span></td>
            <td><span class="badge badge-${m.status}">${esc(m.status)}</span></td>
            <td class="text-clip" title="${esc(m.content || m.error || "")}">${esc((m.content || m.error || "—").slice(0, 120))}</td>
          </tr>`).join("")
        : '<tr><td colspan="4" class="muted">暂无消息</td></tr>';
    } catch (ex) {
      toast(ex.message, true);
    }
  }
  $("msg-filter").addEventListener("change", loadMessages);
  $("msg-status").addEventListener("change", loadMessages);

  /* ---------- 死信任务 ---------- */
  async function loadTasks() {
    try {
      const rows = await api("/api/admin/tasks/dead?limit=200");
      $("task-tbody").innerHTML = rows.length
        ? rows.map((t) => `
          <tr>
            <td class="mono">${esc(t.type)}</td>
            <td>${t.attempts}</td>
            <td class="text-clip" title="${esc(t.error || "")}">${esc((t.error || "—").slice(0, 100))}</td>
            <td class="mono small">${esc((t.updated_at || "").replace("T", " ").slice(0, 19))}</td>
            <td><button class="btn btn-sm" data-replay="${t.id}">重放</button></td>
          </tr>`).join("")
        : '<tr><td colspan="5" class="muted">暂无死信任务</td></tr>';
      document.querySelectorAll("[data-replay]").forEach((b) =>
        b.addEventListener("click", async () => {
          try {
            await api(`/api/admin/tasks/${b.dataset.replay}/replay`, { method: "POST" });
            toast("已重放，任务重新进入队列");
            loadTasks();
          } catch (ex) { toast(ex.message, true); }
        }));
    } catch (ex) {
      toast(ex.message, true);
    }
  }

  /* ---------- 启动 ---------- */
  if (token) {
    api("/api/admin/stats")
      .then(enterApp)
      .catch(() => logout());
  }
})();
