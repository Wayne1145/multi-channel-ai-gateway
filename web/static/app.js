/* Tsukuyomi AI Gateway · 管理后台 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const TOKEN_KEY = "sessionToken";
  const AUTH_KEY = "sessionPrincipal";
  let token = sessionStorage.getItem(TOKEN_KEY) || "";
  let principal = JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  let registering = false;

  /* ---------- 基础请求 ---------- */
  async function api(path, opts = {}) {
    const headers = Object.assign(
      token ? { "Authorization": `Bearer ${token}` } : {},
      opts.headers || {},
    );
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    if (res.status === 401) {
      logout();
      throw new Error("登录已失效，请重新登录");
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
    media: "媒体审计", messages: "消息", tasks: "死信任务", settings: "平台设置",
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
    if (name === "settings") loadSettings();
  }

  /* ---------- 登录 / 退出 ---------- */
  function enterApp() {
    document.querySelectorAll(".admin-only").forEach((el) =>
      el.classList.toggle("hidden", principal?.role !== "admin"));
    $("login").classList.add("hidden");
    $("app").classList.remove("hidden");
    switchView("dashboard");
  }
  function logout() {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(AUTH_KEY);
    token = "";
    principal = null;
    $("app").classList.add("hidden");
    $("login").classList.remove("hidden");
    $("password-input").value = "";
    $("login-error").classList.add("hidden");
  }

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("login-btn");
    const err = $("login-error");
    const username = $("username-input").value.trim();
    const password = $("password-input").value;
    if (!username || !password) return;
    btn.disabled = true;
    err.classList.add("hidden");
    try {
      const path = registering ? "/api/auth/register" : "/api/auth/login";
      const payload = { username, password };
      if (registering) payload.display_name = $("display-name-input").value.trim() || undefined;
      const auth = await api(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      token = auth.token;
      principal = auth;
      sessionStorage.setItem(TOKEN_KEY, token);
      sessionStorage.setItem(AUTH_KEY, JSON.stringify(principal));
      enterApp();
    } catch (ex) {
      err.textContent = ex.message;
      err.classList.remove("hidden");
      token = "";
    } finally {
      btn.disabled = false;
    }
  });
  $("register-toggle").addEventListener("click", () => {
    registering = !registering;
    $("display-name-input").classList.toggle("hidden", !registering);
    $("login-btn").textContent = registering ? "创建并登录" : "登录";
    $("register-toggle").textContent = registering ? "返回登录" : "注册账户";
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

  /* ---------- 数字滚动 ---------- */
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function animateCount(el, value) {
    const target = Number(value) || 0;
    if (reduceMotion || target === 0) {
      el.textContent = target.toLocaleString();
      return;
    }
    const dur = 620;
    const t0 = performance.now();
    const tick = (t) => {
      const p = Math.min(1, (t - t0) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(target * eased).toLocaleString();
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  /* ---------- 仪表盘 ---------- */
  async function loadDashboard() {
    try {
      if (principal?.role === "user") {
        const summary = await api("/api/me/summary");
        animateCount($("stat-users"), 1);
        animateCount($("stat-messages"), summary.conversations);
        animateCount($("stat-failed"), summary.memories);
        animateCount($("stat-tokens"), summary.tokens_total);
        $("trend").innerHTML = `
          <div class="detail-item"><b>${summary.cards}</b><span>角色卡</span></div>
          <div class="detail-item"><b>${summary.presets}</b><span>预设</span></div>
          <div class="detail-item"><b>${summary.providers}</b><span>BYOK 供应商</span></div>
          <div class="detail-item"><b>${summary.media_assets}</b><span>媒体记录</span></div>`;
        if (summary.quota) {
          const q = summary.quota;
          const pct = q.quota > 0 ? Math.min(100, Math.round((q.used / q.quota) * 100)) : 0;
          const warn = q.enabled && pct >= q.alert_threshold;
          $("quota-box").innerHTML = q.enabled
            ? `<div class="quota-head"><b>今日用量</b><span class="${warn ? "text-danger" : "muted"}">${q.used.toLocaleString()} / ${q.quota.toLocaleString()} tokens${warn ? " · 接近限额" : ""}</span></div>
               <div class="quota-bar"><div class="quota-fill ${warn ? "warn" : ""}" style="width:${pct}%"></div></div>`
            : `<div class="quota-head"><b>每日配额</b><span class="muted">未启用</span></div>`;
        }
        $("last-updated").innerHTML =
          '<span class="pulse"></span>用户自足模式 · ' + esc(summary.display_name || principal.username);
        return;
      }
      const [stats, trend] = await Promise.all([
        api("/api/admin/stats"),
        api("/api/admin/usage/trend?days=7"),
      ]);
      animateCount($("stat-users"), stats.users);
      animateCount($("stat-messages"), stats.messages);
      animateCount($("stat-failed"), stats.failed);
      animateCount($("stat-tokens"), stats.tokens);
      renderTrend(trend);
      const modeText = stats.single_user_mode
        ? "单用户模式"
        : stats.mode === "managed" ? "统一管理模式" : "用户自足模式";
      $("last-updated").innerHTML =
        '<span class="pulse"></span>' + modeText + " · 更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
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
            <td><button class="btn btn-sm" data-detail="${u.id}" data-name="${esc(u.display_name || u.id)}" data-username="${esc(u.account_username || "")}">详情</button></td>
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
        b.addEventListener("click", () => openUserDetail(
          b.dataset.detail, b.dataset.name, b.dataset.username)));
    } catch (ex) {
      toast(ex.message, true);
    }
  }
  $("user-search").addEventListener("input", () => loadUsers());

  /* ---------- 用户详情（管理端不可读内容） ---------- */
  async function openUserDetail(userId, name, accountUsername) {
    try {
      const [cards, presets, providers, policies, detail] = await Promise.all([
        api(`/api/admin/users/${userId}/cards`),
        api(`/api/admin/users/${userId}/presets`),
        api(`/api/admin/users/${userId}/providers`),
        api(`/api/admin/users/${userId}/policies`),
        api(`/api/admin/users/${userId}/detail`),
      ]);
      $("user-modal-title").textContent = `用户详情 · ${name}`;
      $("user-modal").dataset.userId = userId;
      $("account-username").value = accountUsername || "";
      $("account-password").value = "";
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
  $("account-save").addEventListener("click", async () => {
    const userId = $("user-modal").dataset.userId;
    const username = $("account-username").value.trim();
    const password = $("account-password").value;
    if (!userId || !username || !password) {
      toast("请填写登录账号和新密码", true);
      return;
    }
    try {
      await api(`/api/admin/users/${userId}/account`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      $("account-password").value = "";
      toast("登录账号已分配或重置");
      loadUsers();
    } catch (ex) { toast(ex.message, true); }
  });
  $("user-modal-close").addEventListener("click", () => $("user-modal").classList.add("hidden"));
  $("user-modal").addEventListener("click", (e) => {
    if (e.target === $("user-modal")) $("user-modal").classList.add("hidden");
  });

  /* ---------- 渠道实例 ---------- */
  const instanceBase = () => principal?.role === "admin"
    ? "/api/admin/channel-instances" : "/api/me/channel-instances";

  async function openQrcode(instanceId) {
    $("qrcode-modal").classList.remove("hidden");
    $("qrcode-loading").textContent = "正在生成安全二维码…";
    $("qrcode-loading").classList.remove("hidden");
    $("qrcode-image").classList.add("hidden");
    try {
      const res = await fetch(`${instanceBase()}/${instanceId}/qrcode`, {
        headers: { "Authorization": `Bearer ${token}` },
        cache: "no-store",
      });
      if (!res.ok) throw new Error("二维码已过期，请重新生成");
      const url = URL.createObjectURL(await res.blob());
      $("qrcode-image").src = url;
      $("qrcode-image").classList.remove("hidden");
      $("qrcode-loading").classList.add("hidden");
    } catch (ex) {
      $("qrcode-loading").textContent = ex.message;
    }
  }

  async function loadInstances() {
    try {
      const rows = await api(instanceBase());
      $("inst-tbody").innerHTML = rows.length
        ? rows.map((i) => `
          <tr>
            <td>${esc(i.instance_name)}</td>
            <td class="mono">${esc(i.channel)}</td>
            <td>
              <span class="badge badge-${i.status === "online" ? "ok" : i.status === "error" ? "dead" : "ignored"}">${esc(i.status)}</span>
              ${i.login?.qrcode_available ? `<button class="btn btn-sm" data-qrcode="${i.id}">扫码登录</button>` : ""}
            </td>
            <td class="mono small">${esc((i.owner_user_id || "—").slice(0, 12))}</td>
            <td class="mono small">${esc((i.created_at || "").replace("T", " ").slice(0, 19))}</td>
            <td>
              <button class="btn btn-sm" data-start="${i.id}" ${["online", "logging_in"].includes(i.status) ? "disabled" : ""}>启动</button>
              <button class="btn btn-sm" data-stop="${i.id}" ${!["online", "logging_in"].includes(i.status) ? "disabled" : ""}>停止</button>
            </td>
          </tr>`).join("")
        : '<tr><td colspan="6" class="muted">暂无渠道实例</td></tr>';
      document.querySelectorAll("[data-start]").forEach((b) =>
        b.addEventListener("click", async () => {
          try {
            const instance = await api(`${instanceBase()}/${b.dataset.start}/start`, { method: "POST" });
            if (instance.login?.qrcode_available) {
              await openQrcode(b.dataset.start);
              toast("二维码已生成，请用隔离微信小号扫码");
            } else {
              toast("启动指令已下发");
            }
            loadInstances();
          } catch (ex) { toast(ex.message, true); }
        }));
      document.querySelectorAll("[data-stop]").forEach((b) =>
        b.addEventListener("click", async () => {
          try {
            await api(`${instanceBase()}/${b.dataset.stop}/stop`, { method: "POST" });
            toast("已停止");
            loadInstances();
          } catch (ex) { toast(ex.message, true); }
        }));
      document.querySelectorAll("[data-qrcode]").forEach((b) =>
        b.addEventListener("click", () => openQrcode(b.dataset.qrcode)));
      if (rows.some((i) => i.status === "logging_in")) {
        setTimeout(loadInstances, 3000);
      }
    } catch (ex) {
      toast(ex.message, true);
    }
  }
  $("inst-create").addEventListener("click", async () => {
    const name = $("inst-name").value.trim();
    const owner = $("inst-owner").value.trim();
    if (!name) { toast("实例名称必填", true); return; }
    try {
      const body = principal?.role === "admin"
        ? { channel: $("inst-channel").value, instance_name: name }
        : { instance_name: name };
      if (principal?.role === "admin" && owner) body.owner_user_id = owner;
      await api(instanceBase(), {
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
  $("qrcode-close").addEventListener("click", () => $("qrcode-modal").classList.add("hidden"));
  $("qrcode-modal").addEventListener("click", (e) => {
    if (e.target === $("qrcode-modal")) $("qrcode-modal").classList.add("hidden");
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

  /* ---------- 平台设置 ---------- */
  const SETTING_GROUPS = {
    general: "基础与公告", model: "模型与供应商", account: "用户与账号",
    quota: "用量与限额", content: "消息与内容", media: "媒体",
    task: "任务与可靠性", channel: "渠道与 ClawBot", retention: "数据保留",
  };
  const groupOrder = ["general", "model", "account", "quota", "content", "media", "task", "channel", "retention"];
  let settingsState = [];

  function settingInputHtml(s) {
    if (s.secret) {
      return `<span class="badge ${s.value.configured ? "badge-ok" : ""}">${s.value.configured ? "已配置（内容仅存于环境变量）" : "未配置"}</span>`;
    }
    if (!s.editable) {
      const val = s.type === "bool" ? (s.value ? "开启" : "关闭") : esc(String(s.value ?? ""));
      return `<span class="settings-static">${val}</span><span class="badge">仅展示</span>`;
    }
    const name = `setting-${s.key}`;
    if (s.type === "bool") {
      return `<label class="switch"><input type="checkbox" data-setting="${s.key}" id="${name}" ${s.value ? "checked" : ""}><span class="switch-slider"></span></label>`;
    }
    if (s.type === "select") {
      return `<select class="field settings-input" data-setting="${s.key}" id="${name}">${(s.options || [])
        .map((o) => `<option value="${esc(o)}" ${String(s.value) === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    }
    const isInt = s.type === "int";
    const attrs = `type="number" ${isInt ? 'step="1"' : 'step="any"'} min="${s.min ?? ""}" max="${s.max ?? ""}"`;
    const displayVal = s.key === "media_max_size_bytes" && s.value
      ? Math.round(s.value / (1024 * 1024) * 100) / 100
      : s.value;
    const displayUnit = s.key === "media_max_size_bytes" ? "MB" : (s.unit || "");
    return `<input class="field settings-input" data-setting="${s.key}" id="${name}" ${attrs} value="${esc(String(displayVal))}"><span class="muted small">${esc(displayUnit)}</span>`;
  }

  function settingValueFromDom(s) {
    const el = document.querySelector(`[data-setting="${s.key}"]`);
    if (!el) return undefined;
    if (s.type === "bool") return el.checked;
    if (s.type === "int") {
      if (el.value === "") return undefined;
      const n = parseInt(el.value, 10);
      // 媒体大小上限在界面以 MB 显示，存储为字节
      return s.key === "media_max_size_bytes" ? n * 1024 * 1024 : n;
    }
    if (s.type === "float") return el.value === "" ? undefined : parseFloat(el.value);
    if (s.type === "select") return el.value;
    return el.value;
  }

  function renderSettings() {
    const box = $("settings-groups");
    box.innerHTML = groupOrder.map((group) => {
      const items = settingsState.filter((s) => s.group === group);
      if (!items.length) return "";
      return `
        <div class="settings-group">
          <h3>${SETTING_GROUPS[group] || group}</h3>
          ${items.map((s) => `
            <div class="settings-item">
              <div class="settings-info">
                <div class="settings-label">${esc(s.label)}</div>
                <div class="settings-desc">${esc(s.description || "")}</div>
              </div>
              <div class="settings-control">${settingInputHtml(s)}</div>
            </div>`).join("")}
        </div>`;
    }).join("");
  }

  async function loadSettings() {
    try {
      const data = await api("/api/admin/settings");
      settingsState = data.settings;
      renderSettings();
    } catch (ex) {
      toast(ex.message, true);
    }
  }

  async function saveSettings() {
    const values = {};
    settingsState.forEach((s) => {
      if (!s.editable || s.secret) return;
      const v = settingValueFromDom(s);
      if (v !== undefined) values[s.key] = v;
    });
    if (!Object.keys(values).length) {
      toast("没有可保存的设置", true);
      return;
    }
    try {
      await api("/api/admin/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values }),
      });
      toast("设置已保存并生效");
      loadSettings();
    } catch (ex) {
      toast(ex.message, true);
    }
  }

  /* ---------- 启动 ---------- */
  fetch("/api/auth/config")
    .then((res) => res.json())
    .then((cfg) => {
      $("register-toggle").classList.toggle("hidden", !cfg.registration_enabled);
      if (cfg.announcement) {
        const el = $("login-announcement");
        el.textContent = "公告：" + cfg.announcement;
        el.classList.remove("hidden");
      }
    })
    .catch(() => {});
  $("settings-save").addEventListener("click", saveSettings);
  if (token) {
    api("/api/auth/me")
      .then((auth) => {
        principal = auth;
        sessionStorage.setItem(AUTH_KEY, JSON.stringify(auth));
        enterApp();
      })
      .catch(() => logout());
  }
})();
