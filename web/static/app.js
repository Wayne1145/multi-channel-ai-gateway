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
  const TITLES = { dashboard: "仪表盘", users: "用户", messages: "消息", tasks: "死信任务" };
  function switchView(name) {
    document.querySelectorAll(".nav-item").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === name));
    document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
    $("view-" + name).classList.remove("hidden");
    $("page-title").textContent = TITLES[name];
    if (name === "dashboard") loadDashboard();
    if (name === "users") loadUsers();
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
      $("last-updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
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
        ? list.map((u) => `
          <tr>
            <td class="mono">${esc(u.id.slice(0, 8))}</td>
            <td>${esc(u.display_name || "—")}</td>
            <td class="mono">${esc(u.model || "默认")}</td>
            <td>${u.blocked ? '<span class="badge badge-dead">已封禁</span>' : '<span class="badge badge-ok">正常</span>'}</td>
            <td class="mono small">${esc((u.created_at || "").replace("T", " ").slice(0, 19))}</td>
            <td><button class="btn btn-sm ${u.blocked ? "" : "btn-danger"}" data-block="${u.id}" data-state="${u.blocked ? "0" : "1"}">${u.blocked ? "解封" : "封禁"}</button></td>
          </tr>`).join("")
        : '<tr><td colspan="6" class="muted">暂无用户</td></tr>';
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
    } catch (ex) {
      toast(ex.message, true);
    }
  }
  $("user-search").addEventListener("input", () => loadUsers());

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
