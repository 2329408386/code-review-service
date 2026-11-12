/* XKL 代码检视平台 Web 控制台（原生 JS，无构建依赖）。
 *
 * 结构：
 *   1. 工具函数（DOM、请求、格式化）
 *   2. 管理员登录态
 *   3. 页面路由（#reviews / #stats / #repos / #models / #scope）
 *   4. 检视记录列表 + 详情抽屉（概览 / 意见 / 推理过程，SSE 实时推送）
 *   5. 数据统计（SVG 图表：趋势、环形图、表内比例条）
 *   6. 代码仓管理（隐藏、分组）
 *   7. 模型路由配置
 *   8. 检视范围配置
 */
(() => {
  "use strict";

  // ==================== 1. 工具函数 ====================

  const $ = (id) => document.getElementById(id);
  const ADMIN_TOKEN_KEY = "admin_token";
  // 检视记录的筛选条件（代码仓、提交人、状态、排序、展示方式、每页数量）保存在浏览器本地，刷新后恢复
  const FILTERS_KEY = "review_filters";
  const FILTER_FIELDS = {
    repository: "filter-repository",
    author: "filter-author",
    sort: "filter-sort",
    view: "filter-view",
    pageSize: "filter-page-size",
  };
  const STATUS_LABELS = { pending: "排队中", running: "检视中", success: "已完成", failed: "失败", ignored: "已忽略" };
  const SEVERITY_LABELS = { major: "严重", minor: "一般", suggestion: "建议" };
  const COMMENT_LABELS = { pending: "待回写", sent: "已回写", failed: "回写失败", skipped: "已跳过", local: "仅平台展示" };
  const EVALUATION_LABELS = { pending: "待发送", sent: "已发送", failed: "发送失败", local: "仅平台展示" };
  const REPLY_SEGMENTS = [
    { key: "resolved", label: "已解决", color: "var(--success)" },
    { key: "no_problem", label: "没问题", color: "var(--info)" },
    { key: "no_risk", label: "无风险", color: "var(--warning)" },
    { key: "other", label: "其他", color: "var(--text-3)" },
  ];
  const SHARD_PATTERN = /^codex\.(discovery|verify|direct)\.(\d+)\.reasoning$/;
  const SHARD_LABELS = { discovery: "代码检视", verify: "证据复核", direct: "超大新增文件直检" };

  /** 转义 HTML，所有动态内容插入前都必须经过它。 */
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString("zh-CN");
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function formatDuration(task) {
    if (!task.started_at || !task.finished_at) return "—";
    const seconds = Math.max(0, (new Date(task.finished_at) - new Date(task.started_at)) / 1000);
    return seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
  }

  let toastTimer = null;
  function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 2600);
  }

  /** 统一请求：自动带上管理员 Token，非 2xx 时抛出带 status 的错误。 */
  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const token = localStorage.getItem(ADMIN_TOKEN_KEY);
    if (token) headers.Authorization = `Bearer ${token}`;
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      ...options,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join("；") : data.detail;
      const error = new Error(detail || `请求失败（${response.status}）`);
      error.status = response.status;
      if (response.status === 401 && token) setAdmin(null);
      throw error;
    }
    return data;
  }

  /** 任务编号：MR 显示 !iid，本地提交显示短提交号。 */
  function taskRef(task) {
    return task.origin === "local" ? task.source_sha.slice(0, 8) : `!${task.mr_iid}`;
  }

  /** 分支信息：MR 显示 源 → 目标，本地提交显示所在分支。 */
  function branchText(task) {
    return task.origin === "local"
      ? `${escapeHtml(task.source_branch)} · 本地提交`
      : `${escapeHtml(task.source_branch)} → ${escapeHtml(task.target_branch)}`;
  }

  function statusBadge(status) {
    return `<span class="badge status-${escapeHtml(status)}">${STATUS_LABELS[status] || escapeHtml(status)}</span>`;
  }

  const ICONS = {
    play: '<svg viewBox="0 0 24 24"><path d="M7 4l13 8-13 8z" /></svg>',
    rerun: '<svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" /></svg>',
    trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" /></svg>',
    download: '<svg viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14" /></svg>',
  };

  // ==================== 2. 管理员登录态 ====================

  const auth = { admin: false, username: "" };

  function setAdmin(info) {
    auth.admin = Boolean(info);
    auth.username = info ? info.username || "" : "";
    if (!info) localStorage.removeItem(ADMIN_TOKEN_KEY);
    $("admin-badge").hidden = !auth.admin;
    $("admin-badge").textContent = auth.admin ? `管理员 ${auth.username}` : "";
    $("admin-button").textContent = auth.admin ? "退出登录" : "管理员登录";
    document.body.classList.toggle("is-admin", auth.admin);
    renderRecords();
    renderModels();
    renderScope();
    renderRepos();
  }

  async function refreshAuth() {
    try {
      const data = await api("/api/auth/status");
      setAdmin(data.admin ? data : null);
    } catch {
      setAdmin(null);
    }
  }

  function openLogin() {
    $("login-error").hidden = true;
    $("login-password").value = "";
    $("login-mask").hidden = false;
    $("login-username").focus();
  }

  function closeLogin() {
    $("login-mask").hidden = true;
  }

  $("admin-button").addEventListener("click", async () => {
    if (!auth.admin) return openLogin();
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    setAdmin(null);
    toast("已退出登录");
  });
  $("login-cancel").addEventListener("click", closeLogin);
  $("login-mask").addEventListener("click", (event) => { if (event.target === $("login-mask")) closeLogin(); });
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const data = await api("/api/auth/login", {
        method: "POST",
        body: { username: $("login-username").value.trim(), password: $("login-password").value },
      });
      localStorage.setItem(ADMIN_TOKEN_KEY, data.token);
      closeLogin();
      setAdmin(data);
      loadModels();
      toast("登录成功");
    } catch (error) {
      $("login-error").textContent = error.message;
      $("login-error").hidden = false;
    }
  });

  /** 需要管理员权限的操作入口：未登录时弹出登录框。 */
  function requireAdmin() {
    if (auth.admin) return true;
    openLogin();
    return false;
  }

  // ==================== 3. 页面路由 ====================

  const PAGES = { reviews: "检视记录", stats: "数据统计", repos: "代码仓", models: "模型路由", scope: "检视范围" };
  let currentPage = "reviews";

  function route() {
    const page = location.hash.slice(1);
    currentPage = PAGES[page] ? page : "reviews";
    for (const name of Object.keys(PAGES)) $(`page-${name}`).hidden = name !== currentPage;
    document.querySelectorAll(".nav a").forEach((link) => link.classList.toggle("active", link.dataset.page === currentPage));
    $("page-title").textContent = PAGES[currentPage];
    document.title = `${PAGES[currentPage]} · XKL 代码检视平台`;
    if (currentPage === "reviews") loadReviews();
    if (currentPage === "stats") loadStats();
    if (currentPage === "repos") loadRepos();
    if (currentPage === "models") loadModels();
    if (currentPage === "scope") loadScope();
  }
  window.addEventListener("hashchange", route);

  // ==================== 4. 检视记录 ====================

  const list = {
    items: [],
    summary: null,
    page: 1,
    totalPages: 1,
    statuses: new Set(),
  };

  /** 从浏览器本地恢复筛选条件；下拉框只接受仍然存在的选项。 */
  function restoreFilters() {
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(FILTERS_KEY) || "{}") || {};
    } catch {
      /* 本地存储不可用或内容损坏时使用默认值 */
    }
    for (const [key, id] of Object.entries(FILTER_FIELDS)) {
      const value = saved[key];
      if (typeof value !== "string") continue;
      const node = $(id);
      if (node.tagName !== "SELECT" || [...node.options].some((option) => option.value === value)) node.value = value;
    }
    if (Array.isArray(saved.statuses)) list.statuses = new Set(saved.statuses.filter((status) => STATUS_LABELS[status]));
  }

  function saveFilters() {
    const data = { statuses: [...list.statuses] };
    for (const [key, id] of Object.entries(FILTER_FIELDS)) data[key] = $(id).value;
    try {
      localStorage.setItem(FILTERS_KEY, JSON.stringify(data));
    } catch {
      /* 隐私模式等场景无法保存时忽略 */
    }
  }

  function renderStatusChips() {
    const chips = [`<button type="button" class="chip${list.statuses.size ? "" : " on"}" data-status="">全部</button>`];
    for (const [status, label] of Object.entries(STATUS_LABELS)) {
      chips.push(`<button type="button" class="chip${list.statuses.has(status) ? " on" : ""}" data-status="${status}">${label}</button>`);
    }
    $("status-chips").innerHTML = chips.join("");
  }

  $("status-chips").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    const status = chip.dataset.status;
    if (!status) list.statuses.clear();
    else if (list.statuses.has(status)) list.statuses.delete(status);
    else list.statuses.add(status);
    renderStatusChips();
    searchNow();
  });

  function reviewQuery() {
    const params = new URLSearchParams();
    const repository = $("filter-repository").value.trim();
    const author = $("filter-author").value.trim();
    if (repository) params.set("repository", repository);
    if (author) params.set("author", author);
    for (const status of list.statuses) params.append("status", status);
    params.set("sort", $("filter-sort").value);
    params.set("page", list.page);
    params.set("page_size", $("filter-page-size").value);
    return params;
  }

  let reviewsRequest = 0;
  async function loadReviews() {
    const requestId = ++reviewsRequest;
    try {
      const data = await api(`/api/reviews?${reviewQuery()}`);
      if (requestId !== reviewsRequest) return; // 丢弃过期响应
      list.items = data.items;
      list.summary = data.summary;
      list.page = data.page;
      list.totalPages = data.total_pages;
      renderRecords();
      renderKpis();
      $("kpi-refresh").textContent = `最近刷新 ${formatTime(new Date())}`;
    } catch (error) {
      toast(`加载检视记录失败：${error.message}`);
    }
  }

  function renderKpis() {
    const summary = list.summary;
    if (!summary) return;
    $("kpi-reviews").textContent = formatNumber(summary.review_count);
    $("kpi-findings").textContent = formatNumber(summary.finding_count);
    $("kpi-repos").textContent = formatNumber(summary.repository_count);
  }

  /** 延后到晚上执行的大型检视单独成组，其余按时间或代码仓分组。 */
  function groupRecords(items) {
    const delayed = items.filter((task) => task.status === "pending" && task.scheduled_start_at);
    const rest = items.filter((task) => !delayed.includes(task));
    const groups = [];
    if (delayed.length) groups.push({ title: "晚上8点后检视", items: delayed, delayed: true });
    if ($("filter-view").value === "repository") {
      const byRepo = new Map();
      for (const task of rest) {
        if (!byRepo.has(task.repository_name)) byRepo.set(task.repository_name, []);
        byRepo.get(task.repository_name).push(task);
      }
      for (const [name, tasks] of byRepo) groups.push({ title: name, items: tasks });
    } else if (rest.length) {
      groups.push({ title: "", items: rest });
    }
    return groups;
  }

  /** 卡片上的简短时间：月-日 时:分。 */
  function shortTime(value) {
    const text = formatTime(value);
    return text === "—" ? text : text.slice(5, 16);
  }

  /** 一条检视记录的小卡片；完整标题、分支等信息放在悬停提示里。 */
  function recordCard(task) {
    const selected = drawer.taskId === task.id ? " selected" : "";
    const busy = task.status === "pending" || task.status === "running";
    const actions = auth.admin
      ? `<div class="card-actions">
          ${task.scheduled_start_at && task.status === "pending" ? `<button class="icon-btn" type="button" data-action="run-now" title="立即检视" aria-label="立即检视">${ICONS.play}</button>` : ""}
          <button class="icon-btn" type="button" data-action="rerun" title="重新检视" aria-label="重新检视"${busy ? " disabled" : ""}>${ICONS.rerun}</button>
          <button class="icon-btn danger" type="button" data-action="delete" title="删除记录" aria-label="删除记录"${task.status === "running" ? " disabled" : ""}>${ICONS.trash}</button>
        </div>`
      : "";
    const hint = [
      task.title || "（无标题）",
      `${task.repository_name} ${taskRef(task)}`,
      task.origin === "local" ? `${task.source_branch} · 本地提交` : `${task.source_branch} → ${task.target_branch}`,
      task.ignored_reason ? `已忽略：${task.ignored_reason}` : "",
    ].filter(Boolean).join("\n");
    const when = task.scheduled_start_at && task.status === "pending"
      ? `计划 ${shortTime(task.scheduled_start_at)}`
      : shortTime(task.created_at);
    return `<div class="review-card card-${escapeHtml(task.status)}${selected}" data-id="${escapeHtml(task.id)}" tabindex="0" role="button" title="${escapeHtml(hint)}">
      <div class="card-top">
        ${statusBadge(task.status)}
        <span class="finding-pill${task.finding_count ? " has" : ""}">${task.finding_count} 条</span>
      </div>
      <div class="card-title">${escapeHtml(task.title || "（无标题）")}</div>
      <div class="card-repo"><span class="repo">${escapeHtml(task.repository_name)}</span><span class="mono">${taskRef(task)}</span></div>
      <div class="card-meta"><span class="author">${escapeHtml(task.author || "未知")}</span><span>${when}</span></div>
      ${actions}
    </div>`;
  }

  function renderRecords() {
    const groups = groupRecords(list.items);
    $("records").innerHTML = groups
      .map((group) => `<section class="group${group.delayed ? " delayed" : ""}">
        ${group.title ? `<div class="group-head"><h2>${escapeHtml(group.title)}</h2><span>${group.items.length} 条</span></div>` : ""}
        <div class="review-grid">${group.items.map(recordCard).join("")}</div>
      </section>`)
      .join("");
    $("empty").hidden = list.items.length > 0;
    $("pagination").hidden = list.totalPages <= 1;
    $("page-info").textContent = `第 ${list.page} / ${list.totalPages} 页`;
    $("page-prev").disabled = list.page <= 1;
    $("page-next").disabled = list.page >= list.totalPages;
  }

  async function taskAction(action, taskId) {
    if (!requireAdmin()) return;
    try {
      if (action === "delete") {
        if (!confirm("确定删除这条检视记录及其意见吗？")) return;
        await api(`/api/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
        if (drawer.taskId === taskId) closeDrawer();
        toast("已删除");
      } else if (action === "rerun") {
        const data = await api(`/api/tasks/${encodeURIComponent(taskId)}/rerun`, { method: "POST" });
        toast("已加入检视队列");
        openDrawer(data.task_id);
      } else if (action === "run-now") {
        await api(`/api/tasks/${encodeURIComponent(taskId)}/run-now`, { method: "POST" });
        toast("已取消定时，立即执行");
      }
      loadReviews();
    } catch (error) {
      toast(error.message);
    }
  }

  $("records").addEventListener("click", (event) => {
    const row = event.target.closest(".review-card");
    if (!row) return;
    const button = event.target.closest("[data-action]");
    if (button) {
      event.stopPropagation();
      taskAction(button.dataset.action, row.dataset.id);
    } else {
      openDrawer(row.dataset.id);
    }
  });
  $("records").addEventListener("keydown", (event) => {
    const row = event.target.closest(".review-card");
    if (row && event.target === row && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      openDrawer(row.dataset.id);
    }
  });

  // 筛选：输入时防抖自动搜索；回车或从下拉提示中选择时立即搜索
  let filterTimer = null;
  function searchNow() {
    clearTimeout(filterTimer);
    saveFilters();
    list.page = 1;
    loadReviews();
  }
  for (const id of ["filter-repository", "filter-author"]) {
    $(id).addEventListener("input", () => {
      clearTimeout(filterTimer);
      filterTimer = setTimeout(searchNow, 300);
    });
    $(id).addEventListener("change", searchNow);
    $(id).addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        searchNow();
      }
    });
  }
  for (const id of ["filter-sort", "filter-page-size"]) $(id).addEventListener("change", searchNow);
  $("filter-view").addEventListener("change", () => { saveFilters(); renderRecords(); });
  $("refresh").addEventListener("click", () => { loadReviews(); loadReplyStats(); loadHealth(); });
  $("page-prev").addEventListener("click", () => { list.page -= 1; loadReviews(); });
  $("page-next").addEventListener("click", () => { list.page += 1; loadReviews(); });

  async function loadFilterOptions() {
    try {
      const [repos, authors] = await Promise.all([api("/api/repositories"), api("/api/authors")]);
      scope.repositories = repos.map((repo) => repo.name);
      $("repository-list").innerHTML = repos.map((repo) => `<option value="${escapeHtml(repo.name)}"></option>`).join("");
      $("author-list").innerHTML = authors.map((name) => `<option value="${escapeHtml(name)}"></option>`).join("");
    } catch {
      /* 下拉提示加载失败不影响主流程 */
    }
  }

  /** 检视意见 KPI：开发者回复分布的迷你环形图。 */
  async function loadReplyStats() {
    try {
      const replies = await api("/api/stats/review-replies");
      const segments = REPLY_SEGMENTS.map((seg) => ({ ...seg, value: replies[seg.key] || 0 }));
      const total = replies.classification_total || 0;
      $("kpi-reply-sub").textContent = `已解决 ${percent(replies.resolved, total)}`;
      $("reply-donut").innerHTML = `${donutSvg(segments, { size: 68, stroke: 10 })}${legendHtml(segments, { compact: true })}`;
    } catch {
      /* 统计失败时保持上次结果 */
    }
  }

  async function loadHealth() {
    try {
      const health = await api("/health");
      $("health").className = `health ${health.status === "ok" ? "ok" : "bad"}`;
      $("health-text").textContent = health.status === "ok" ? `服务正常 · ${health.codex_version || "Codex"}` : "服务降级：数据库不可用";
      $("kpi-queue").textContent = `队列 ${health.queue_size} · 执行中 ${health.active_reviews}`;
    } catch {
      $("health").className = "health bad";
      $("health-text").textContent = "无法连接服务";
    }
  }

  // ---------- 详情抽屉 ----------

  const drawer = {
    taskId: null,
    task: null,
    findings: [],
    tab: "overview",
    events: [],
    eventsLoaded: false,
    lastEventId: 0,
    hasMore: false,
    source: null,
  };

  async function openDrawer(taskId) {
    closeStream();
    Object.assign(drawer, { taskId, task: null, findings: [], events: [], eventsLoaded: false, lastEventId: 0, hasMore: false });
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
    $("drawer-mask").hidden = false;
    $("drawer-title").innerHTML = '<span class="loading">加载中…</span>';
    $("drawer-body").innerHTML = "";
    renderRecords();
    await loadDetail();
  }

  function closeDrawer() {
    closeStream();
    drawer.taskId = null;
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    $("drawer-mask").hidden = true;
    renderRecords();
  }

  async function loadDetail() {
    const taskId = drawer.taskId;
    try {
      const data = await api(`/api/reviews/${encodeURIComponent(taskId)}`);
      if (drawer.taskId !== taskId) return;
      drawer.task = data.task;
      drawer.findings = data.findings;
      renderDrawer();
      // 任务未结束时订阅实时事件，事件到达后刷新详情
      if (["pending", "running"].includes(data.task.status)) openStream(Math.max(drawer.lastEventId, data.last_event_id));
    } catch (error) {
      $("drawer-title").innerHTML = "";
      $("drawer-body").innerHTML = `<div class="error-box">${escapeHtml(error.message)}</div>`;
    }
  }

  function renderDrawer() {
    const task = drawer.task;
    if (!task) return;
    const link = task.mr_url ? `<a href="${escapeHtml(task.mr_url)}" target="_blank" rel="noopener">在 CodeHub 中打开 ↗</a>` : "";
    $("drawer-title").innerHTML = `<h2>${escapeHtml(task.repository_name)} ${taskRef(task)} · ${escapeHtml(task.title || "（无标题）")}</h2>
      <div class="sub">${statusBadge(task.status)}<span class="mono">${branchText(task)}</span><span>${escapeHtml(task.author)}</span>${link}</div>`;
    $("tab-findings-count").textContent = drawer.findings.length;
    document.querySelectorAll(".tabs button").forEach((button) => button.classList.toggle("active", button.dataset.tab === drawer.tab));
    if (drawer.tab === "overview") renderOverview();
    if (drawer.tab === "findings") renderFindings();
    if (drawer.tab === "timeline") renderTimeline();
  }

  function renderOverview() {
    const task = drawer.task;
    const evaluation = task.evaluation_status
      ? `${task.evaluation_level ? `等级 ${task.evaluation_level} · ` : ""}${EVALUATION_LABELS[task.evaluation_status] || task.evaluation_status}`
      : "—";
    const items = [
      ["任务 ID", `<span class="mono">${escapeHtml(task.id)}</span>`],
      ["触发动作", escapeHtml(task.action)],
      ["检视模式", escapeHtml(task.review_mode || "—")],
      ["创建时间", formatTime(task.created_at)],
      ["开始时间", formatTime(task.started_at)],
      ["耗时", formatDuration(task)],
      ["计划执行", formatTime(task.scheduled_start_at)],
      ["检视分片", task.review_chunk_count || "—"],
      ["文件 / 新增行", `${task.reviewed_file_count} / ${task.reviewed_added_line_count}`],
      ["有效 / 被过滤意见", `${task.finding_count} / ${task.rejected_finding_count}`],
      ["使用模型", escapeHtml(task.models_used.join("、") || "—")],
      ["Token 输入 / 输出", `${formatNumber(task.token_input)} / ${formatNumber(task.token_output)}`],
      ["源提交", `<span class="mono">${escapeHtml((task.source_sha || "").slice(0, 12))}</span>`],
      ["目标提交", `<span class="mono">${escapeHtml((task.target_sha || "—").slice(0, 12))}</span>`],
      ["整体评价", escapeHtml(evaluation)],
    ];
    const sections = [];
    if (task.error_message) sections.push(`<div class="error-box">${escapeHtml(task.error_message)}</div>`);
    if (task.ignored_reason) sections.push(`<p class="notice">已忽略：${escapeHtml(task.ignored_reason)}</p>`);
    sections.push(`<dl class="info-grid">${items.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>`);
    if (task.review_summary) sections.push(`<div class="section"><h3>检视总结</h3><p class="prose">${escapeHtml(task.review_summary)}</p></div>`);
    if (task.evaluation_content) sections.push(`<div class="section"><h3>整体评价</h3><p class="prose">${escapeHtml(task.evaluation_content)}</p></div>`);
    if (task.evaluation_error) sections.push(`<div class="error-box">评价发送失败：${escapeHtml(task.evaluation_error)}</div>`);
    if (task.description) sections.push(`<div class="section"><h3>MR 描述</h3><p class="prose">${escapeHtml(task.description)}</p></div>`);
    $("drawer-body").innerHTML = sections.join("");
  }

  function renderFindings() {
    const findings = drawer.findings;
    const counts = { major: 0, minor: 0, suggestion: 0 };
    findings.forEach((finding) => { counts[finding.severity] = (counts[finding.severity] || 0) + 1; });
    const summary = Object.entries(SEVERITY_LABELS)
      .map(([key, label]) => `<span class="sev sev-tag-${key}">${label} ${counts[key]}</span>`)
      .join("");
    const exportLink = `/api/reviews/${encodeURIComponent(drawer.taskId)}/findings/export`;
    const header = `<div class="findings-bar"><div class="sev-summary">${summary}</div>
      <a class="btn btn-sm" href="${exportLink}" download>${ICONS.download}导出 CSV</a></div>`;
    if (!findings.length) {
      $("drawer-body").innerHTML = `${header}<div class="empty">本次检视没有发现问题</div>`;
      return;
    }
    $("drawer-body").innerHTML = header + findings.map((finding) => {
      const path = finding.new_path === "/dev/null" ? finding.old_path : finding.new_path;
      const failed = finding.status === "failed";
      const foot = `${COMMENT_LABELS[finding.status] || finding.status}${finding.comment_sent_at ? ` · ${formatTime(finding.comment_sent_at)}` : ""}${finding.comment_error ? ` · ${escapeHtml(finding.comment_error)}` : ""}`;
      return `<article class="finding sev-${finding.severity}">
        <div class="finding-head"><span class="sev sev-tag-${finding.severity}">${SEVERITY_LABELS[finding.severity] || finding.severity}</span><strong>${escapeHtml(finding.title)}</strong></div>
        <div class="finding-loc">${escapeHtml(path)}:${finding.new_line}</div>
        <p class="finding-body">${escapeHtml(finding.body)}</p>
        <div class="finding-foot${failed ? " error" : ""}">${foot}</div>
      </article>`;
    }).join("");
  }

  // ---------- 推理过程（按分片分组，懒加载 + SSE 增量） ----------

  async function loadEvents(more = false) {
    const taskId = drawer.taskId;
    const after = more ? drawer.lastEventId : 0;
    try {
      const data = await api(`/api/reviews/${encodeURIComponent(taskId)}/events?reasoning_only=true&limit=500&after_id=${after}`);
      if (drawer.taskId !== taskId) return;
      drawer.events = more ? drawer.events.concat(data.items) : data.items;
      drawer.lastEventId = Math.max(drawer.lastEventId, data.last_id);
      drawer.hasMore = data.has_more;
      drawer.eventsLoaded = true;
      if (drawer.tab === "timeline") renderTimeline();
    } catch (error) {
      toast(`加载推理过程失败：${error.message}`);
    }
  }

  /** 推理事件所属分片的标题。后端分片编号从 1 开始；只有 1 个分片时不显示编号。 */
  function shardOf(event) {
    const match = SHARD_PATTERN.exec(event.stage);
    if (!match) return "检视流程";
    const total = drawer.task?.review_chunk_count || 0;
    return total > 1 ? `${SHARD_LABELS[match[1]]} · 分片 ${match[2]}/${total}` : SHARD_LABELS[match[1]];
  }

  function renderTimeline() {
    if (!drawer.eventsLoaded) {
      $("drawer-body").innerHTML = '<div class="loading">加载推理过程…</div>';
      loadEvents();
      return;
    }
    if (!drawer.events.length) {
      $("drawer-body").innerHTML = '<div class="empty">暂无推理过程记录</div>';
      return;
    }
    const shards = new Map();
    for (const event of drawer.events) {
      const key = shardOf(event);
      if (!shards.has(key)) shards.set(key, []);
      shards.get(key).push(event);
    }
    const html = [...shards].map(([title, events]) => `<section class="shard"><h4 class="shard-title">${escapeHtml(title)}</h4>
      ${events.map((event) => `<div class="step"><details>
        <summary><span class="step-label">${escapeHtml(event.message)}</span><span class="step-preview">${escapeHtml((event.detail || "").slice(0, 120))}</span><time>${formatTime(event.created_at).slice(11)}</time></summary>
        ${event.detail ? `<pre>${escapeHtml(event.detail)}</pre>` : ""}
      </details></div>`).join("")}
    </section>`).join("");
    const more = drawer.hasMore ? '<div class="timeline-more"><button class="btn btn-sm" type="button" id="timeline-more">加载更多</button></div>' : "";
    const tools = '<div class="timeline-tools"><button class="btn btn-sm" type="button" data-expand="1">全部展开</button><button class="btn btn-sm" type="button" data-expand="0">全部收起</button></div>';
    $("drawer-body").innerHTML = tools + html + more;
  }

  $("drawer-body").addEventListener("click", (event) => {
    const expand = event.target.closest("[data-expand]");
    if (expand) {
      $("drawer-body").querySelectorAll("details").forEach((node) => { node.open = expand.dataset.expand === "1"; });
    }
    if (event.target.id === "timeline-more") loadEvents(true);
  });

  /** 订阅任务事件流；任何事件到达都刷新详情，推理事件直接追加到时间线。 */
  function openStream(afterId) {
    closeStream();
    const taskId = drawer.taskId;
    const source = new EventSource(`/api/review-events/stream?task_id=${encodeURIComponent(taskId)}&after_id=${afterId}`);
    drawer.source = source;
    $("live-state").textContent = "● 实时";
    let refreshTimer = null;
    source.addEventListener("review_event", (message) => {
      const event = JSON.parse(message.data);
      if (drawer.taskId !== taskId) return;
      if (drawer.eventsLoaded && SHARD_PATTERN.test(event.stage) && event.id > drawer.lastEventId) {
        drawer.events.push(event);
        drawer.lastEventId = event.id;
        if (drawer.tab === "timeline") renderTimeline();
      }
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(refreshLiveTask, 800);
    });
    source.onerror = () => { $("live-state").textContent = "重连中…"; };
    source.onopen = () => { $("live-state").textContent = "● 实时"; };
  }

  async function refreshLiveTask() {
    const taskId = drawer.taskId;
    if (!taskId) return;
    try {
      const data = await api(`/api/reviews/${encodeURIComponent(taskId)}`);
      if (drawer.taskId !== taskId) return;
      drawer.task = data.task;
      drawer.findings = data.findings;
      if (drawer.tab !== "timeline") renderDrawer();
      else $("tab-findings-count").textContent = drawer.findings.length;
      if (!["pending", "running"].includes(data.task.status)) {
        closeStream();
        loadReviews();
      }
    } catch {
      /* 下次事件再刷新 */
    }
  }

  function closeStream() {
    if (drawer.source) drawer.source.close();
    drawer.source = null;
    $("live-state").textContent = "";
  }

  document.querySelector(".tabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-tab]");
    if (!button || !drawer.task) return;
    drawer.tab = button.dataset.tab;
    renderDrawer();
  });
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-mask").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("login-mask").hidden) closeLogin();
    else if (drawer.taskId) closeDrawer();
  });

  // ==================== 5. 数据统计 ====================

  // ---------- 图表工具（纯 SVG，无第三方依赖；悬停提示统一读取 data-tip）
  const SERIES_COLORS = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)", "var(--c7)", "var(--c8)"];
  const SEVERITY_SEGMENTS = [
    { key: "major", label: "严重", color: "var(--danger)" },
    { key: "minor", label: "一般", color: "var(--warning)" },
    { key: "suggestion", label: "建议", color: "var(--info)" },
  ];

  /** 大数字的紧凑写法：1.2万、3.45亿。 */
  function formatCompact(value) {
    const n = Number(value || 0);
    if (n >= 1e8) return `${(n / 1e8).toFixed(2)}亿`;
    if (n >= 1e4) return `${(n / 1e4).toFixed(1)}万`;
    return formatNumber(n);
  }

  function percent(part, total) {
    return total ? `${((part / total) * 100).toFixed(1)}%` : "—";  // 保留 1 位小数
  }

  /** 环形图：segments = [{ label, value, color }]；相邻扇区之间留 2px 间隙。 */
  function donutSvg(segments, { size = 132, stroke = 16, center = "", caption = "" } = {}) {
    const half = size / 2;
    const r = (size - stroke) / 2;
    const circumference = 2 * Math.PI * r;
    const total = segments.reduce((sum, seg) => sum + seg.value, 0);
    const visible = segments.filter((seg) => seg.value > 0);
    const gap = visible.length > 1 ? 2 : 0;
    let offset = 0;
    const arcs = visible.map((seg) => {
      const length = (seg.value / total) * circumference;
      const tip = `${seg.label}\n${formatNumber(seg.value)} · ${percent(seg.value, total)}`;
      const arc = `<circle class="arc" cx="${half}" cy="${half}" r="${r}" fill="none" stroke="${seg.color}" stroke-width="${stroke}"
        stroke-dasharray="${Math.max(0.5, length - gap)} ${circumference}" stroke-dashoffset="${-offset}" data-tip="${escapeHtml(tip)}" />`;
      offset += length;
      return arc;
    }).join("");
    return `<svg class="viz donut" viewBox="0 0 ${size} ${size}" style="width:${size}px;height:${size}px" role="img">
      <circle cx="${half}" cy="${half}" r="${r}" fill="none" stroke="var(--track)" stroke-width="${stroke}" />
      <g transform="rotate(-90 ${half} ${half})">${arcs}</g>
      ${center ? `<text x="${half}" y="${half - (caption ? 2 : -6)}" text-anchor="middle" class="donut-value">${escapeHtml(center)}</text>` : ""}
      ${caption ? `<text x="${half}" y="${half + 16}" text-anchor="middle" class="donut-caption">${escapeHtml(caption)}</text>` : ""}
    </svg>`;
  }

  function legendHtml(segments, { compact = false } = {}) {
    const total = segments.reduce((sum, seg) => sum + seg.value, 0);
    return `<ul class="legend-list${compact ? " compact" : ""}">${segments.map((seg) => `<li>
      <i style="background:${seg.color}"></i><span class="legend-label">${escapeHtml(seg.label)}</span>
      <b title="${formatNumber(seg.value)}">${formatCompact(seg.value)}</b>${compact ? "" : `<em>${percent(seg.value, total)}</em>`}
    </li>`).join("")}</ul>`;
  }

  function donutBlock(title, segments, options = {}) {
    const total = segments.reduce((sum, seg) => sum + seg.value, 0);
    return `<h3>${escapeHtml(title)}</h3>
      ${total ? `<div class="donut-body">${donutSvg(segments, { center: formatCompact(total), caption: options.caption || "合计" })}${legendHtml(segments)}</div>`
        : '<div class="empty small">暂无数据</div>'}`;
  }

  /** 表格单元格内的比例条。 */
  function cellBar(value, max, color = "var(--c1)", text = formatNumber(value)) {
    const width = max ? Math.max(value ? 2 : 0, (value / max) * 100) : 0;
    return `<div class="cell-bar"><span class="cell-track"><span style="width:${width}%;background:${color}"></span></span><b>${text}</b></div>`;
  }

  /** 严重 / 一般 / 建议 的迷你堆叠条。 */
  function severityStack(severity) {
    const total = SEVERITY_SEGMENTS.reduce((sum, seg) => sum + (severity[seg.key] || 0), 0);
    const tip = SEVERITY_SEGMENTS.map((seg) => `${seg.label} ${severity[seg.key] || 0}`).join("\n");
    const parts = total
      ? SEVERITY_SEGMENTS.filter((seg) => severity[seg.key]).map((seg) => `<span style="flex:${severity[seg.key]};background:${seg.color}"></span>`).join("")
      : "";
    return `<div class="sev-stack" data-tip="${escapeHtml(tip)}"><span class="sev-track">${parts}</span>
      <span class="sev-nums">${SEVERITY_SEGMENTS.map((seg) => severity[seg.key] || 0).join(" / ")}</span></div>`;
  }

  /** 刻度：把最大值向上取整到 1/2/5×10^n 的 4 等分。 */
  function niceMax(value) {
    if (value <= 4) return 4;
    const step = value / 4;
    const power = 10 ** Math.floor(Math.log10(step));
    const nice = [1, 2, 5, 10].find((m) => m * power >= step) * power;
    return nice * 4;
  }

  /** 每日趋势：检视次数（面积 + 折线）与意见数（折线），共用一个纵轴。 */
  function trendSvg(days, width, height = 240) {
    const pad = { top: 12, right: 12, bottom: 26, left: 40 };
    const plotW = Math.max(10, width - pad.left - pad.right);
    const plotH = height - pad.top - pad.bottom;
    const max = niceMax(Math.max(0, ...days.map((d) => Math.max(d.reviews, d.findings))));
    const step = days.length > 1 ? plotW / (days.length - 1) : 0;
    const x = (i) => pad.left + (days.length > 1 ? i * step : plotW / 2);
    const y = (v) => pad.top + plotH - (v / max) * plotH;
    const grid = [0, 1, 2, 3, 4].map((i) => {
      const value = (max / 4) * i;
      return `<line class="grid" x1="${pad.left}" x2="${width - pad.right}" y1="${y(value)}" y2="${y(value)}" />
        <text class="axis" x="${pad.left - 8}" y="${y(value) + 4}" text-anchor="end">${formatCompact(value)}</text>`;
    }).join("");
    const labelEvery = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(plotW / 64))));
    const labels = days.map((d, i) => (i % labelEvery === 0 || i === days.length - 1) && (i === days.length - 1 || days.length - 1 - i >= labelEvery / 2)
      ? `<text class="axis" x="${x(i)}" y="${height - 6}" text-anchor="middle">${d.day.slice(5)}</text>` : "").join("");
    const path = (key) => days.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`).join("");
    const area = days.length > 1 ? `${path("reviews")}L${x(days.length - 1)},${y(0)}L${x(0)},${y(0)}Z` : "";
    const dots = days.length <= 31
      ? days.map((d, i) => `<circle class="dot" cx="${x(i)}" cy="${y(d.reviews)}" r="3" fill="var(--c1)" />
          <circle class="dot" cx="${x(i)}" cy="${y(d.findings)}" r="3" fill="var(--c2)" />`).join("")
      : "";
    const hitW = days.length > 1 ? step : plotW;
    const hits = days.map((d, i) => `<rect class="hit" x="${x(i) - hitW / 2}" y="${pad.top}" width="${hitW}" height="${plotH}"
      data-tip="${escapeHtml(`${d.day}\n检视 ${formatNumber(d.reviews)} 次\n意见 ${formatNumber(d.findings)} 条\nToken ${formatCompact(d.tokens)}`)}" />`).join("");
    return `<svg class="viz" viewBox="0 0 ${width} ${height}" style="width:${width}px;height:${height}px" role="img" aria-label="检视趋势">
      ${grid}${labels}
      ${area ? `<path d="${area}" fill="var(--c1)" fill-opacity="0.10" />` : ""}
      <path d="${path("reviews")}" fill="none" stroke="var(--c1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
      <path d="${path("findings")}" fill="none" stroke="var(--c2)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
      ${dots}${hits}
    </svg>`;
  }

  // 悬停提示：任何带 data-tip 的图表元素
  const tip = $("chart-tip");
  document.addEventListener("mousemove", (event) => {
    const target = event.target.closest ? event.target.closest("[data-tip]") : null;
    if (!target) {
      tip.hidden = true;
      return;
    }
    tip.innerHTML = escapeHtml(target.getAttribute("data-tip")).replace(/\n/g, "<br>");
    tip.hidden = false;
    const { innerWidth, innerHeight } = window;
    const left = Math.min(event.clientX + 14, innerWidth - tip.offsetWidth - 8);
    const top = event.clientY + 14 + tip.offsetHeight > innerHeight ? event.clientY - tip.offsetHeight - 10 : event.clientY + 14;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  });

  // ---------- 统计页
  const STATS_KEY = "stats_filters";
  const stats = { range: "30d", scope: "", data: null, catalog: null, sort: { key: "finding_count", dir: -1 } };
  try {
    const saved = JSON.parse(localStorage.getItem(STATS_KEY) || "{}");
    if (["7d", "30d", "90d", "all"].includes(saved.range)) stats.range = saved.range;
    if (typeof saved.scope === "string") stats.scope = saved.scope;
    if (saved.sort && typeof saved.sort.key === "string") stats.sort = { key: saved.sort.key, dir: saved.sort.dir > 0 ? 1 : -1 };
  } catch {
    /* 本地存储不可用时使用默认值 */
  }

  function saveStatsFilters() {
    try {
      localStorage.setItem(STATS_KEY, JSON.stringify({ range: stats.range, scope: stats.scope, sort: stats.sort }));
    } catch {
      /* 忽略 */
    }
  }

  /** 统计维度：""=全部，"g:分组名"，"r:代码仓"。 */
  function statsQuery() {
    const params = new URLSearchParams({ range: stats.range });
    if (stats.scope.startsWith("g:")) params.set("group", stats.scope.slice(2));
    if (stats.scope.startsWith("r:")) params.set("repository", stats.scope.slice(2));
    return params;
  }

  function renderScopeSelect() {
    const catalog = stats.catalog;
    const visible = catalog.repositories.filter((repo) => !repo.hidden).map((repo) => repo.name).sort((a, b) => a.localeCompare(b));
    const option = (value, label) => `<option value="${escapeHtml(value)}"${value === stats.scope ? " selected" : ""}>${escapeHtml(label)}</option>`;
    const groups = catalog.groups.map((group) => option(`g:${group.name}`, `${group.name}（${group.repositories.length}）`));
    if (catalog.groups.length) groups.push(option("g:__ungrouped__", "未分组"));
    $("stats-scope").innerHTML = option("", "全部代码仓")
      + (groups.length ? `<optgroup label="分组">${groups.join("")}</optgroup>` : "")
      + (visible.length ? `<optgroup label="代码仓">${visible.map((name) => option(`r:${name}`, name)).join("")}</optgroup>` : "");
  }

  let statsRequest = 0;
  async function loadStats() {
    const requestId = ++statsRequest;
    try {
      stats.catalog = await api("/api/settings/repositories");
      const valid = stats.scope === "" || stats.scope === "g:__ungrouped__"
        || stats.catalog.groups.some((group) => stats.scope === `g:${group.name}`)
        || stats.catalog.repositories.some((repo) => !repo.hidden && stats.scope === `r:${repo.name}`);
      if (!valid) stats.scope = "";
      renderScopeSelect();
      document.querySelectorAll("#stats-range button").forEach((button) => button.classList.toggle("on", button.dataset.range === stats.range));
      const data = await api(`/api/stats/overview?${statsQuery()}`);
      if (requestId !== statsRequest) return;
      stats.data = data;
      renderStats();
    } catch (error) {
      toast(`加载统计失败：${error.message}`);
    }
  }

  function renderStats() {
    const data = stats.data;
    if (!data) return;
    const t = data.totals;
    $("stats-period").textContent = `${data.range.since_day} 至 ${data.range.today}`;
    $("stat-reviews").textContent = formatNumber(t.review_count);
    $("stat-reviews-sub").textContent = `${formatNumber(t.mr_count)} 个 MR · 已忽略 ${formatNumber(t.ignored_count)}`;
    $("stat-findings").textContent = formatNumber(t.finding_count);
    $("stat-findings-sub").textContent = t.success_count ? `平均每次 ${(t.finding_count / t.success_count).toFixed(1)} 条` : "—";
    const finished = t.success_count + t.failed_count;
    $("stat-success").textContent = percent(t.success_count, finished);
    $("stat-success-sub").textContent = `成功 ${formatNumber(t.success_count)} · 失败 ${formatNumber(t.failed_count)}`;
    $("stat-repos").textContent = formatNumber(t.repository_count);
    $("stat-repos-sub").textContent = stats.catalog.groups.length ? `共 ${stats.catalog.groups.length} 个分组` : "未设置分组";
    $("stat-tokens").textContent = formatCompact(t.total_tokens);
    $("stat-tokens").title = formatNumber(t.total_tokens);
    $("stat-tokens-sub").textContent = `入 ${formatCompact(t.input_tokens)} · 出 ${formatCompact(t.output_tokens)}`;
    $("stat-tokens-sub").title = `输入 ${formatNumber(t.input_tokens)} · 输出 ${formatNumber(t.output_tokens)} · 缓存输入 ${formatNumber(t.cached_input_tokens)}`;
    $("stat-avg-tokens").textContent = formatCompact(t.avg_tokens);
    $("stat-avg-sub").textContent = t.avg_duration ? `平均耗时 ${t.avg_duration}s` : "—";

    $("trend-legend").innerHTML = '<span><i style="background:var(--c1)"></i>检视次数</span><span><i style="background:var(--c2)"></i>检视意见</span>';
    $("severity-chart").innerHTML = donutBlock("严重级别", SEVERITY_SEGMENTS.map((seg) => ({ ...seg, value: data.severity[seg.key] || 0 })), { caption: "条意见" });
    $("reply-chart").innerHTML = donutBlock("开发者回复", REPLY_SEGMENTS.map((seg) => ({ ...seg, value: data.replies[seg.key] || 0 })), { caption: "条分类" });
    renderTrend();  // 在环形图之后绘制：趋势图高度跟随同一行卡片
    renderGroupTable();
    renderRepoTable();
    renderModelStats();
  }

  /** 补齐区间内没有检视的日期。 */
  function filledDays() {
    const byDay = new Map(stats.data.trend.map((d) => [d.day, d]));
    const days = [];
    const end = new Date(`${stats.data.range.today}T00:00:00Z`);
    for (let d = new Date(`${stats.data.range.since_day}T00:00:00Z`); d <= end && days.length < 3660; d.setUTCDate(d.getUTCDate() + 1)) {
      const day = d.toISOString().slice(0, 10);
      days.push(byDay.get(day) || { day, reviews: 0, findings: 0, tokens: 0 });
    }
    return days;
  }

  function renderTrend() {
    if (!stats.data) return;
    const box = $("trend-chart");
    box.innerHTML = stats.data.totals.review_count
      ? trendSvg(filledDays(), box.clientWidth || 720, Math.max(240, box.clientHeight))
      : '<div class="empty">暂无数据</div>';
  }

  function resolvedRate(replies) {
    return replies.classification_total ? replies.resolved / replies.classification_total : -1;
  }

  function renderGroupTable() {
    const groups = stats.data.groups;
    $("group-card").hidden = stats.scope !== "" || !groups.length;
    const maxFindings = Math.max(0, ...groups.map((g) => g.finding_count));
    const maxTokens = Math.max(0, ...groups.map((g) => g.total_tokens));
    $("group-rows").innerHTML = groups.map((g, i) => `<tr>
      <td><button class="link-btn" type="button" data-scope="g:${escapeHtml(g.key || g.name)}"><i class="swatch" style="background:${SERIES_COLORS[i % 8]}"></i>${escapeHtml(g.name)}</button></td>
      <td class="num">${formatNumber(g.repository_count)}</td>
      <td class="num">${formatNumber(g.review_count)}</td>
      <td>${cellBar(g.finding_count, maxFindings, "var(--c1)")}</td>
      <td class="num">${g.replies.classification_total ? percent(g.replies.resolved, g.replies.classification_total) : "—"}</td>
      <td class="muted-cell">${formatCompact(g.input_tokens)} / ${formatCompact(g.output_tokens)}</td>
      <td>${cellBar(g.total_tokens, maxTokens, "var(--c7)", formatCompact(g.total_tokens))}</td>
    </tr>`).join("");
  }

  function renderRepoTable() {
    const rows = stats.data.repositories.map((row) => ({ ...row, resolved_rate: resolvedRate(row.replies), group: row.group || "" }));
    const { key, dir } = stats.sort;
    rows.sort((a, b) => {
      const va = a[key];
      const vb = b[key];
      const cmp = typeof va === "string" ? va.localeCompare(vb) : va - vb;
      return cmp * dir || a.name.localeCompare(b.name);
    });
    document.querySelectorAll("#repo-head th[data-sort]").forEach((th) => {
      th.classList.toggle("sorted", th.dataset.sort === key);
      th.dataset.dir = th.dataset.sort === key ? (dir > 0 ? "asc" : "desc") : "";
    });
    const maxFindings = Math.max(0, ...rows.map((r) => r.finding_count));
    const maxTokens = Math.max(0, ...rows.map((r) => r.total_tokens));
    $("repo-table-total").textContent = `${rows.length} 个代码仓`;
    $("repo-rows").innerHTML = rows.length
      ? rows.map((r) => `<tr>
          <td><button class="link-btn" type="button" data-scope="r:${escapeHtml(r.name)}" title="查看 ${escapeHtml(r.name)} 的统计">${escapeHtml(r.name)}</button></td>
          <td>${r.group ? `<span class="group-pill">${escapeHtml(r.group)}</span>` : '<span class="muted-cell">—</span>'}</td>
          <td class="num">${formatNumber(r.review_count)}</td>
          <td>${cellBar(r.finding_count, maxFindings, "var(--c1)")}</td>
          <td>${severityStack(r.severity)}</td>
          <td class="num">${r.resolved_rate < 0 ? "—" : percent(r.replies.resolved, r.replies.classification_total)}</td>
          <td class="num" title="缓存输入 ${formatNumber(r.cached_input_tokens)}">${formatCompact(r.input_tokens)}</td>
          <td class="num">${formatCompact(r.output_tokens)}</td>
          <td>${cellBar(r.total_tokens, maxTokens, "var(--c7)", formatCompact(r.total_tokens))}</td>
        </tr>`).join("")
      : '<tr><td colspan="9" class="empty">暂无数据</td></tr>';
  }

  function renderModelStats() {
    const models = stats.data.models;
    // 颜色跟随模型名称（按名称固定排序），不随排名变化
    const names = [...models.map((m) => m.name)].sort((a, b) => a.localeCompare(b));
    const colorOf = (name) => (names.indexOf(name) < SERIES_COLORS.length ? SERIES_COLORS[names.indexOf(name)] : "var(--text-3)");
    const ranked = [...models].sort((a, b) => b.total_tokens - a.total_tokens);
    const segments = ranked.slice(0, 7).map((m) => ({ label: m.name, value: m.total_tokens, color: colorOf(m.name) }));
    const rest = ranked.slice(7).reduce((sum, m) => sum + m.total_tokens, 0);
    if (rest) segments.push({ label: "其他", value: rest, color: "var(--text-3)" });
    $("model-chart").innerHTML = donutBlock("Token 占比", segments, { caption: "Token" });
    $("model-rows").innerHTML = models.length
      ? models.map((m) => {
          const rate = m.call_count ? m.success_count / m.call_count : 0;
          const level = rate >= 0.95 ? "success" : rate >= 0.8 ? "warning" : "danger";
          return `<tr>
            <td><i class="swatch" style="background:${colorOf(m.name)}"></i>${escapeHtml(m.name)}</td>
            <td class="num">${formatNumber(m.call_count)}</td>
            <td class="num"><span class="rate rate-${level}" title="失败 ${formatNumber(m.failed_count)} 次">${percent(m.success_count, m.call_count)}</span></td>
            <td class="num">${formatNumber(m.review_count)}</td>
            <td class="num">${formatNumber(m.finding_count)}</td>
            <td class="num" title="缓存输入 ${formatNumber(m.cached_input_tokens)}">${formatCompact(m.input_tokens)}</td>
            <td class="num">${formatCompact(m.output_tokens)}</td>
            <td class="num">${m.avg_duration ? `${m.avg_duration}s` : "—"}</td>
          </tr>`;
        }).join("")
      : '<tr><td colspan="8" class="empty">暂无模型调用记录</td></tr>';
  }

  $("stats-range").addEventListener("click", (event) => {
    const button = event.target.closest("[data-range]");
    if (!button) return;
    stats.range = button.dataset.range;
    saveStatsFilters();
    loadStats();
  });
  $("stats-scope").addEventListener("change", () => {
    stats.scope = $("stats-scope").value;
    saveStatsFilters();
    loadStats();
  });
  $("page-stats").addEventListener("click", (event) => {
    const link = event.target.closest("[data-scope]");
    if (link) {
      stats.scope = link.dataset.scope;
      saveStatsFilters();
      window.scrollTo({ top: 0, behavior: "smooth" });
      return loadStats();
    }
    const th = event.target.closest("#repo-head th[data-sort]");
    if (th) {
      const key = th.dataset.sort;
      stats.sort = { key, dir: stats.sort.key === key ? -stats.sort.dir : (key === "name" || key === "group" ? 1 : -1) };
      saveStatsFilters();
      renderRepoTable();
    }
  });
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (currentPage === "stats") renderTrend(); }, 150);
  });

  // ==================== 6. 代码仓管理 ====================

  const repos = { items: [], hidden: new Set(), groups: [], loaded: false, dirty: false };

  async function loadRepos() {
    if (repos.dirty) return renderRepos();
    try {
      const data = await api("/api/settings/repositories");
      applyRepoCatalog(data);
      $("repos-status").textContent = `共 ${repos.items.length} 个代码仓 · ${repos.hidden.size} 个已隐藏`;
    } catch (error) {
      $("repos-status").textContent = `加载失败：${error.message}`;
    }
  }

  function applyRepoCatalog(data) {
    repos.items = data.repositories;
    repos.hidden = new Set(data.hidden);
    repos.groups = data.groups.map((group) => ({ name: group.name, repositories: [...group.repositories] }));
    repos.loaded = true;
    repos.dirty = false;
    renderRepos();
  }

  function repoGroup(name) {
    return repos.groups.find((group) => group.repositories.includes(name))?.name || "";
  }

  function renderRepos() {
    if (!repos.loaded) return;
    const editable = auth.admin;
    $("repos-readonly").hidden = editable;
    $("group-actions").hidden = !editable;
    $("repos-save").hidden = !editable;
    $("group-list").innerHTML = repos.groups.map((group, index) => `<span class="tag group-tag" data-index="${index}">
        <i class="swatch" style="background:${SERIES_COLORS[index % 8]}"></i>${escapeHtml(group.name)}<em>${group.repositories.length}</em>
        ${editable ? `<button type="button" data-group-action="rename" title="重命名" aria-label="重命名">✎</button><button type="button" data-group-action="remove" title="删除分组" aria-label="删除分组">×</button>` : ""}
      </span>`).join("") || '<span class="muted-cell">还没有分组</span>';

    const keyword = $("repos-search").value.trim().toLowerCase();
    const filter = $("repos-filter").value;
    const rows = repos.items.filter((repo) => {
      if (keyword && !repo.name.toLowerCase().includes(keyword)) return false;
      const hidden = repos.hidden.has(repo.name);
      if (filter === "visible") return !hidden;
      if (filter === "hidden") return hidden;
      if (filter === "ungrouped") return !repoGroup(repo.name);
      return true;
    });
    $("repos-count").textContent = `${rows.length} / ${repos.items.length}`;
    const groupOptions = (current) => `<option value="">未分组</option>${repos.groups.map((group) => `<option value="${escapeHtml(group.name)}"${group.name === current ? " selected" : ""}>${escapeHtml(group.name)}</option>`).join("")}`;
    $("repos-rows").innerHTML = rows.length
      ? rows.map((repo) => {
          const hidden = repos.hidden.has(repo.name);
          const group = repoGroup(repo.name);
          return `<tr class="${hidden ? "is-hidden" : ""}" data-repo="${escapeHtml(repo.name)}">
            <td class="repo-name">${escapeHtml(repo.name)}</td>
            <td class="num">${formatNumber(repo.review_count)}</td>
            <td>${formatTime(repo.latest_at)}</td>
            <td>${editable ? `<select class="select-sm" data-repo-action="group" aria-label="分组">${groupOptions(group)}</select>` : (group ? `<span class="group-pill">${escapeHtml(group)}</span>` : "—")}</td>
            <td>${editable
              ? `<button class="toggle${hidden ? "" : " on"}" type="button" data-repo-action="toggle" role="switch" aria-checked="${!hidden}"><span></span>${hidden ? "已隐藏" : "显示中"}</button>`
              : (hidden ? '<span class="badge status-ignored">已隐藏</span>' : '<span class="badge status-success">显示中</span>')}</td>
          </tr>`;
        }).join("")
      : '<tr><td colspan="5" class="empty">没有符合条件的代码仓</td></tr>';
  }

  function markReposDirty() {
    repos.dirty = true;
    $("repos-status").textContent = "有未保存的修改";
    renderRepos();
  }

  function addGroup() {
    if (!requireAdmin()) return;
    const name = $("group-input").value.trim();
    if (!name) return toast("请输入分组名称");
    if (repos.groups.some((group) => group.name === name)) return toast("分组名称已存在");
    repos.groups.push({ name, repositories: [] });
    $("group-input").value = "";
    markReposDirty();
  }

  $("group-add").addEventListener("click", addGroup);
  $("group-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addGroup();
    }
  });
  $("group-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-group-action]");
    if (!button || !requireAdmin()) return;
    const group = repos.groups[Number(button.closest(".group-tag").dataset.index)];
    if (button.dataset.groupAction === "remove") {
      if (!confirm(`删除分组“${group.name}”？其中的代码仓将变为未分组。`)) return;
      repos.groups.splice(repos.groups.indexOf(group), 1);
    } else {
      const name = (prompt("新的分组名称", group.name) || "").trim();
      if (!name || name === group.name) return;
      if (repos.groups.some((other) => other.name === name)) return toast("分组名称已存在");
      group.name = name;
    }
    markReposDirty();
  });
  $("repos-rows").addEventListener("click", (event) => {
    const button = event.target.closest('[data-repo-action="toggle"]');
    if (!button || !requireAdmin()) return;
    const name = button.closest("tr").dataset.repo;
    if (repos.hidden.has(name)) repos.hidden.delete(name);
    else repos.hidden.add(name);
    markReposDirty();
  });
  $("repos-rows").addEventListener("change", (event) => {
    const select = event.target.closest('[data-repo-action="group"]');
    if (!select || !requireAdmin()) return;
    const name = select.closest("tr").dataset.repo;
    for (const group of repos.groups) group.repositories = group.repositories.filter((repo) => repo !== name);
    repos.groups.find((group) => group.name === select.value)?.repositories.push(name);
    markReposDirty();
  });
  $("repos-search").addEventListener("input", renderRepos);
  $("repos-filter").addEventListener("change", renderRepos);
  $("repos-save").addEventListener("click", async () => {
    if (!requireAdmin()) return;
    try {
      const data = await api("/api/settings/repositories", {
        method: "PUT",
        body: { hidden: [...repos.hidden], groups: repos.groups },
      });
      applyRepoCatalog(data);
      $("repos-status").textContent = `已保存 · ${repos.hidden.size} 个已隐藏 · ${repos.groups.length} 个分组`;
      loadFilterOptions();
      loadReplyStats();
      toast("代码仓配置已保存并生效");
    } catch (error) {
      $("repos-status").textContent = `保存失败：${error.message}`;
    }
  });

  // ==================== 7. 模型路由 ====================

  const models = { routes: [], defaultKey: null, efforts: [], envGateway: false, health: {}, loaded: false, dirty: false };
  let routeSeq = 0;

  /** 路由在前端的唯一键：已保存的用 id，新增的用临时键。 */
  function routeKey(route) {
    return route.id || route.tempKey;
  }

  async function loadModels() {
    if (models.dirty) return renderModels(); // 有未保存修改时不覆盖
    try {
      const data = await api("/api/settings/models");
      models.routes = data.models.map((route) => ({ ...route, api_key: "" }));
      models.defaultKey = data.default_model;
      models.efforts = data.reasoning_efforts;
      models.envGateway = data.env_gateway_configured;
      models.health = data.health || {};
      models.loaded = true;
      renderModels();
    } catch (error) {
      $("model-status").textContent = `加载失败：${error.message}`;
    }
  }

  /** 路由健康状态：异常（冷却中，其它任务自动改用其它路由）或最近有失败。 */
  function routeHealthBadge(route) {
    const health = route.id && models.health[route.id];
    if (!health) return "";
    const tip = escapeHtml(health.last_error || "");
    if (!health.available) {
      const minutes = Math.max(1, Math.ceil(health.retry_in_seconds / 60));
      return ` <span class="route-health is-down" title="${tip}">异常 · 已切换到其它模型，约 ${minutes} 分钟后重试</span>`;
    }
    return ` <span class="route-health is-warn" title="${tip}">最近失败 ${health.failures} 次</span>`;
  }

  function renderModels() {
    if (!models.loaded) return;
    const editable = auth.admin;
    $("model-readonly").hidden = editable;
    $("model-actions").hidden = !editable;
    if (!models.dirty) $("model-status").textContent = models.envGateway ? "默认网关：已配置" : "默认网关：未配置";
    const assigned = new Map();
    models.routes.forEach((route) => route.repositories.forEach((repo) => assigned.set(repo, routeKey(route))));
    const disabled = editable ? "" : " disabled";
    $("model-list").innerHTML = models.routes.map((route) => {
      const key = routeKey(route);
      const isDefault = models.defaultKey === key;
      const repoNames = [...new Set([...scope.repositories, ...route.repositories])];
      const repoOptions = repoNames.map((repo) => {
        const owner = assigned.get(repo);
        const on = owner === key;
        const other = owner && !on ? models.routes.find((r) => routeKey(r) === owner) : null;
        return `<label class="repo-option${on ? " on" : ""}"><input type="checkbox" data-repo="${escapeHtml(repo)}"${on ? " checked" : ""}${disabled} />${escapeHtml(repo)}${other ? `<em>（当前：${escapeHtml(other.name)}）</em>` : ""}</label>`;
      }).join("");
      const efforts = models.efforts.map((effort) => `<option value="${effort}"${route.reasoning_effort === effort ? " selected" : ""}>${effort}</option>`).join("");
      return `<div class="route${isDefault ? " is-default" : ""}" data-key="${escapeHtml(key)}">
        <div class="route-head">
          <span class="route-name">${escapeHtml(route.name)}${routeHealthBadge(route)}</span>
          <label class="default-toggle"><input type="radio" name="default-route" data-field="default"${isDefault ? " checked" : ""}${disabled} />兜底路由</label>
          <label class="inline">并发<input type="number" min="1" max="100" value="${route.concurrency}" data-field="concurrency"${disabled} /></label>
          ${editable ? `<button class="icon-btn danger" type="button" data-route-action="remove" title="删除路由" aria-label="删除路由">${ICONS.trash}</button>` : ""}
        </div>
        <div class="route-grid">
          <label>Base URL<input type="url" value="${escapeHtml(route.base_url)}" placeholder="留空使用默认网关" data-field="base_url"${disabled} /></label>
          <label>API Key<input type="password" value="${escapeHtml(route.api_key)}" placeholder="${route.api_key_configured ? "已配置，留空保持不变" : "留空使用默认网关"}" data-field="api_key" autocomplete="new-password"${disabled} /></label>
          <label>思考强度<select data-field="reasoning_effort"${disabled}>${efforts}</select></label>
          <label>&nbsp;<button class="btn" type="button" data-route-action="discover"${disabled}>拉取模型列表</button></label>
        </div>
        <details class="repo-picker">
          <summary>绑定代码仓（${route.repositories.length}）${route.repositories.length ? `：${escapeHtml(route.repositories.slice(0, 4).join("、"))}${route.repositories.length > 4 ? " …" : ""}` : ""}</summary>
          <div class="repo-options">${repoOptions || '<span class="muted">暂无代码仓，可在下方手动添加</span>'}</div>
          ${editable ? '<div class="repo-add"><input type="text" placeholder="手动输入代码仓名称" list="repository-list" data-field="repo-input" /><button class="btn btn-sm" type="button" data-route-action="add-repo">绑定</button></div>' : ""}
        </details>
      </div>`;
    }).join("") || '<div class="empty">尚未配置模型，请添加</div>';
  }

  function routeFromNode(node) {
    const key = node.closest(".route")?.dataset.key;
    return models.routes.find((route) => routeKey(route) === key);
  }

  function markModelsDirty() {
    models.dirty = true;
    $("model-status").textContent = "有未保存的修改";
  }

  /** 把仓库绑定到指定路由（一个仓库只能属于一条路由）。 */
  function bindRepository(target, repo, bind) {
    for (const route of models.routes) route.repositories = route.repositories.filter((name) => name !== repo);
    if (bind) target.repositories.push(repo);
  }

  $("model-list").addEventListener("input", (event) => {
    const route = routeFromNode(event.target);
    const field = event.target.dataset.field;
    if (!route || !field || field === "repo-input") return;
    if (field === "concurrency") route.concurrency = Number(event.target.value) || 1;
    else if (field === "default") models.defaultKey = routeKey(route);
    else route[field] = event.target.value;
    markModelsDirty();
    if (field === "default") renderModels();
  });

  $("model-list").addEventListener("change", (event) => {
    const repo = event.target.dataset.repo;
    if (!repo) return;
    bindRepository(routeFromNode(event.target), repo, event.target.checked);
    markModelsDirty();
    renderModelsKeepOpen(event.target);
  });

  /** 重新渲染但保持当前路由的仓库面板展开。 */
  function renderModelsKeepOpen(node) {
    const key = node.closest(".route")?.dataset.key;
    renderModels();
    const picker = document.querySelector(`.route[data-key="${CSS.escape(key)}"] .repo-picker`);
    if (picker) picker.open = true;
  }

  $("model-list").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-route-action]");
    if (!button || !requireAdmin()) return;
    const route = routeFromNode(button);
    const action = button.dataset.routeAction;
    if (action === "remove") {
      if (models.routes.length <= 1) return toast("至少保留一条模型路由");
      models.routes = models.routes.filter((item) => item !== route);
      if (models.defaultKey === routeKey(route)) models.defaultKey = routeKey(models.routes[0]);
      markModelsDirty();
      renderModels();
    } else if (action === "add-repo") {
      const input = button.parentElement.querySelector("input");
      const repo = input.value.trim();
      if (!repo) return;
      bindRepository(route, repo, true);
      markModelsDirty();
      renderModelsKeepOpen(button);
    } else if (action === "discover") {
      await discoverModels({ route_id: route.id || null, base_url: route.base_url, api_key: route.api_key });
    }
  });

  async function discoverModels(body) {
    $("model-status").textContent = "正在拉取模型列表…";
    try {
      const data = await api("/api/settings/llm/models", { method: "POST", body });
      $("model-options").innerHTML = data.models.map((name) => `<option value="${escapeHtml(name)}"></option>`).join("");
      $("model-status").textContent = `已拉取 ${data.models.length} 个模型，可在“输入模型名称”中选择`;
      $("model-input").focus();
    } catch (error) {
      $("model-status").textContent = error.message;
    }
  }

  $("model-add").addEventListener("click", () => {
    if (!requireAdmin()) return;
    const name = $("model-input").value.trim();
    if (!name) return toast("请输入模型名称");
    const route = { tempKey: `new-${++routeSeq}`, id: null, name, concurrency: 1, repositories: [], base_url: "", api_key: "", api_key_configured: false, reasoning_effort: "medium" };
    models.routes.push(route);
    if (!models.defaultKey) models.defaultKey = routeKey(route);
    $("model-input").value = "";
    markModelsDirty();
    renderModels();
  });

  $("model-save").addEventListener("click", async () => {
    if (!requireAdmin()) return;
    if (!models.routes.length) return toast("至少配置一个模型");
    const defaultRoute = models.routes.find((route) => routeKey(route) === models.defaultKey) || models.routes[0];
    const body = {
      // 新增路由还没有 id，兜底路由按名称指定
      default_model: defaultRoute.id || defaultRoute.name,
      models: models.routes.map((route) => ({
        id: route.id || null,
        name: route.name,
        concurrency: route.concurrency,
        repositories: route.repositories,
        base_url: route.base_url.trim(),
        api_key: route.api_key.trim(),
        reasoning_effort: route.reasoning_effort,
      })),
    };
    try {
      await api("/api/settings/models", { method: "PUT", body });
      models.dirty = false;
      await loadModels();
      toast("模型路由已保存并生效");
    } catch (error) {
      $("model-status").textContent = `保存失败：${error.message}`;
    }
  });

  // ==================== 8. 检视范围 ====================

  const scope = { rules: [], repositories: [], loaded: false, dirty: false };

  async function loadScope() {
    if (scope.dirty) return renderScope();
    try {
      const data = await api("/api/settings/review-scope");
      scope.rules = data.rules.map((rule) => ({ repository: rule.repository, target_branches: [...rule.target_branches] }));
      scope.loaded = true;
      $("scope-status").textContent = `已配置 ${scope.rules.length} 个代码仓`;
      renderScope();
    } catch (error) {
      $("scope-status").textContent = `加载失败：${error.message}`;
    }
  }

  function renderScope() {
    if (!scope.loaded) return;
    const editable = auth.admin;
    $("scope-readonly").hidden = editable;
    $("scope-actions").hidden = !editable;
    $("scope-list").innerHTML = scope.rules.map((rule, index) => `<div class="scope" data-index="${index}">
      <div class="scope-head"><span>${escapeHtml(rule.repository)}</span>
        ${editable ? `<button class="icon-btn danger" type="button" data-scope-action="remove" title="移除代码仓" aria-label="移除代码仓">${ICONS.trash}</button>` : ""}</div>
      <div class="tags">${rule.target_branches.map((branch, i) => `<span class="tag">${escapeHtml(branch)}${editable ? `<button type="button" data-scope-action="remove-branch" data-branch="${i}" aria-label="移除分支">×</button>` : ""}</span>`).join("") || '<span class="tag-empty">至少配置一个目标分支</span>'}</div>
      ${editable ? '<div class="scope-add"><input type="text" maxlength="200" placeholder="输入允许的目标分支，回车添加" /><button class="btn btn-sm" type="button" data-scope-action="add-branch">添加分支</button></div>' : ""}
    </div>`).join("") || '<div class="empty">未配置任何规则，所有代码仓的 MR 都会被检视</div>';
  }

  function markScopeDirty() {
    scope.dirty = true;
    $("scope-status").textContent = "有未保存的修改";
    renderScope();
  }

  function addBranch(node) {
    const rule = scope.rules[Number(node.closest(".scope").dataset.index)];
    const input = node.closest(".scope").querySelector(".scope-add input");
    const branch = input.value.trim();
    if (!branch) return;
    if (!rule.target_branches.includes(branch)) rule.target_branches.push(branch);
    markScopeDirty();
    document.querySelector(`.scope[data-index="${scope.rules.indexOf(rule)}"] .scope-add input`)?.focus();
  }

  $("scope-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-scope-action]");
    if (!button || !requireAdmin()) return;
    const index = Number(button.closest(".scope").dataset.index);
    const action = button.dataset.scopeAction;
    if (action === "remove") scope.rules.splice(index, 1);
    if (action === "remove-branch") scope.rules[index].target_branches.splice(Number(button.dataset.branch), 1);
    if (action === "add-branch") return addBranch(button);
    markScopeDirty();
  });
  $("scope-list").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.closest(".scope-add")) {
      event.preventDefault();
      addBranch(event.target);
    }
  });

  $("scope-add").addEventListener("click", () => {
    if (!requireAdmin()) return;
    const repository = $("scope-input").value.trim();
    if (!repository) return toast("请输入代码仓名称");
    if (scope.rules.some((rule) => rule.repository === repository)) return toast("该代码仓已存在");
    scope.rules.push({ repository, target_branches: [] });
    $("scope-input").value = "";
    markScopeDirty();
  });

  $("scope-save").addEventListener("click", async () => {
    if (!requireAdmin()) return;
    const empty = scope.rules.find((rule) => !rule.target_branches.length);
    if (empty) return toast(`代码仓 ${empty.repository} 至少需要一个目标分支`);
    try {
      const data = await api("/api/settings/review-scope", { method: "PUT", body: { rules: scope.rules } });
      scope.rules = data.rules;
      scope.dirty = false;
      $("scope-status").textContent = `已保存，共 ${scope.rules.length} 个代码仓`;
      renderScope();
      toast("检视范围已保存并生效");
    } catch (error) {
      $("scope-status").textContent = `保存失败：${error.message}`;
    }
  });

  // ==================== 启动 ====================

  restoreFilters();
  renderStatusChips();
  refreshAuth();
  loadFilterOptions();
  loadReplyStats();
  loadHealth();
  route();

  // 定时刷新：记录列表 15 秒、回复统计 5 秒、健康状态 10 秒（页面不可见时跳过）
  const every = (ms, fn) => setInterval(() => { if (!document.hidden) fn(); }, ms);
  every(15000, () => { if (currentPage === "reviews") loadReviews(); });
  every(5000, loadReplyStats);
  every(10000, loadHealth);
})();
