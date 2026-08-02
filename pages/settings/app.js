// Eidolon plugin settings
const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

// 与后端 DEFAULT_CONFIG 对应的字段分组
const TEXT_FIELDS = ["seedream_api_key", "seedream_model", "proxy"];
const TEXTAREA_FIELDS = ["nl_keywords", "enhance_system_prompt_zh", "enhance_system_prompt_en"];
const INT_FIELDS = [
  "max_concurrency", "cooldown_seconds", "request_timeout",
  "total_limit", "per_user_limit", "nl_min_prompt_len", "enhance_timeout",
  "image_cache_max_mb",
];
const BOOL_FIELDS = [
  "enable_proxy", "watermark", "admin_ignore_limit",
  "enable_nl_trigger", "enable_prompt_enhance",
];
// 分段/可视化选择器: 字段名 -> 容器 id
const GROUP_FIELDS = {
  aspect_ratio: "aspect_group",
  image_size: "image_size_group",
  output_format: "output_format_group",
  enhance_lang: "enhance_lang_group",
  nl_trigger_mode: "nl_trigger_mode_group",
};

let groupValues = {}; // 当前分段选择器的值
let toastTimer = null;
let aboutLoaded = false;
let confirmResolve = null;
const customSelects = new Map();

function syncSegmentedIndicator(box, animate = true) {
  if (!box) return;
  let indicator = box.querySelector(":scope > .selection-indicator");
  if (!indicator) {
    indicator = document.createElement("span");
    indicator.className = "selection-indicator";
    indicator.setAttribute("aria-hidden", "true");
    box.prepend(indicator);
  }
  const active = box.querySelector("button.active");
  if (!active || box.offsetWidth === 0) {
    indicator.classList.remove("visible");
    return;
  }
  indicator.classList.toggle("no-motion", !animate);
  indicator.style.setProperty("--indicator-x", `${active.offsetLeft}px`);
  indicator.style.setProperty("--indicator-y", `${active.offsetTop}px`);
  indicator.style.setProperty("--indicator-w", `${active.offsetWidth}px`);
  indicator.style.setProperty("--indicator-h", `${active.offsetHeight}px`);
  indicator.classList.add("visible");
  if (!animate) requestAnimationFrame(() => indicator.classList.remove("no-motion"));
}

function syncAllSegmentedIndicators(animate = true) {
  for (const box of document.querySelectorAll(".segmented")) {
    syncSegmentedIndicator(box, animate);
  }
}

function closeCustomSelects(except = null) {
  for (const control of customSelects.values()) {
    if (control !== except) {
      control.wrapper.classList.remove("open");
      control.trigger.setAttribute("aria-expanded", "false");
    }
  }
}

function syncCustomSelect(select) {
  const control = customSelects.get(select);
  if (!control) return;
  const selected = select.selectedOptions[0];
  control.value.textContent = selected?.textContent || "请选择";
  control.trigger.classList.toggle("placeholder", !select.value);
  control.trigger.disabled = select.disabled;
  for (const option of control.menu.querySelectorAll("button")) {
    const active = option.dataset.value === select.value;
    option.classList.toggle("selected", active);
    option.setAttribute("aria-selected", String(active));
  }
}

function refreshCustomSelect(select) {
  const control = customSelects.get(select);
  if (!control) return;
  control.menu.replaceChildren(...Array.from(select.options, (nativeOption) => {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "select-option";
    option.dataset.value = nativeOption.value;
    option.textContent = nativeOption.textContent;
    option.disabled = nativeOption.disabled;
    option.setAttribute("role", "option");
    option.addEventListener("click", () => {
      if (select.value !== nativeOption.value) {
        select.value = nativeOption.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      syncCustomSelect(select);
      control.wrapper.classList.remove("open");
      control.trigger.setAttribute("aria-expanded", "false");
      control.trigger.focus();
    });
    return option;
  }));
  syncCustomSelect(select);
}

function initCustomSelect(select) {
  if (customSelects.has(select)) return;
  const wrapper = document.createElement("div");
  wrapper.className = "custom-select";
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "select-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  const value = document.createElement("span");
  value.className = "select-value";
  const arrow = document.createElement("span");
  arrow.className = "select-arrow";
  arrow.setAttribute("aria-hidden", "true");
  const menu = document.createElement("div");
  menu.className = "select-menu";
  menu.setAttribute("role", "listbox");
  trigger.append(value, arrow);
  wrapper.append(trigger, menu);
  select.after(wrapper);
  select.classList.add("native-select-hidden");
  customSelects.set(select, { wrapper, trigger, value, menu });
  trigger.addEventListener("click", () => {
    const opening = !wrapper.classList.contains("open");
    closeCustomSelects(opening ? customSelects.get(select) : null);
    wrapper.classList.toggle("open", opening);
    trigger.setAttribute("aria-expanded", String(opening));
    if (opening) menu.querySelector(".selected")?.scrollIntoView({ block: "nearest" });
  });
  trigger.addEventListener("keydown", (e) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End", "Escape"].includes(e.key)) return;
    e.preventDefault();
    if (e.key === "Escape") {
      wrapper.classList.remove("open");
      trigger.setAttribute("aria-expanded", "false");
      return;
    }
    const options = [...select.options].filter((option) => !option.disabled);
    let index = Math.max(0, options.findIndex((option) => option.value === select.value));
    if (e.key === "ArrowDown") index = Math.min(options.length - 1, index + 1);
    if (e.key === "ArrowUp") index = Math.max(0, index - 1);
    if (e.key === "Home") index = 0;
    if (e.key === "End") index = options.length - 1;
    const next = options[index];
    if (next && select.value !== next.value) {
      select.value = next.value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      syncCustomSelect(select);
    }
  });
  select.addEventListener("change", () => syncCustomSelect(select));
  refreshCustomSelect(select);
}

function initCustomSelects() {
  for (const select of document.querySelectorAll("select")) initCustomSelect(select);
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".custom-select")) closeCustomSelects();
  });
}

function initSmoothWheelScroll() {
  const content = document.querySelector(".content");
  let target = content.scrollTop;
  let frame = 0;
  const tick = () => {
    const distance = target - content.scrollTop;
    if (Math.abs(distance) < .5) {
      content.scrollTop = target;
      frame = 0;
      return;
    }
    content.scrollTop += distance * .16;
    frame = requestAnimationFrame(tick);
  };
  content.addEventListener("scroll", () => {
    if (!frame) target = content.scrollTop;
  }, { passive: true });
  content.addEventListener("wheel", (e) => {
    if (e.ctrlKey || e.target.closest(".select-menu, textarea")) return;
    const max = content.scrollHeight - content.clientHeight;
    if (max <= 0) return;
    const delta = e.deltaMode === 1 ? e.deltaY * 32 : e.deltaMode === 2 ? e.deltaY * content.clientHeight : e.deltaY;
    target = Math.max(0, Math.min(max, target + delta));
    e.preventDefault();
    if (!frame) frame = requestAnimationFrame(tick);
  }, { passive: false });
}

// ---------- 主题 ----------
function applyTheme(ctx) {
  const dark = ctx?.isDark ?? false;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

// ---------- Toast ----------
function toast(msg, isErr = false) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), 2600);
}

function askConfirm({ title, message, confirmText = "确认", danger = false }) {
  const modal = $("confirm-modal");
  $("confirm-title").textContent = title;
  $("confirm-message").textContent = message;
  $("confirm-ok").textContent = confirmText;
  $("confirm-ok").className = "btn " + (danger ? "danger solid" : "primary");
  $("confirm-mark").classList.toggle("danger", danger);
  modal.classList.add("show");
  modal.setAttribute("aria-hidden", "false");
  $("confirm-cancel").focus();
  return new Promise((resolve) => { confirmResolve = resolve; });
}

function closeConfirm(result) {
  if (!confirmResolve) return;
  const resolve = confirmResolve;
  confirmResolve = null;
  $("confirm-modal").classList.remove("show");
  $("confirm-modal").setAttribute("aria-hidden", "true");
  resolve(result);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// ---------- 视图切换 ----------
function switchView(name) {
  for (const btn of document.querySelectorAll(".nav-item")) {
    btn.classList.toggle("active", btn.dataset.view === name);
  }
  for (const v of document.querySelectorAll(".view")) {
    v.classList.toggle("active", v.id === "view-" + name);
  }
  requestAnimationFrame(() => syncAllSegmentedIndicators(false));
  if (name === "quota") loadQuota();
  if (name === "about" && !aboutLoaded) {
    aboutLoaded = true;
    loadAbout();
  }
}

// ---------- 分段选择器 ----------
function initGroups() {
  for (const [field, groupId] of Object.entries(GROUP_FIELDS)) {
    const box = $(groupId);
    box.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-val]");
      if (!btn) return;
      setGroupValue(field, btn.dataset.val);
    });
  }
}
function setGroupValue(field, val) {
  groupValues[field] = val;
  const box = $(GROUP_FIELDS[field]);
  for (const btn of box.querySelectorAll("[data-val]")) {
    btn.classList.toggle("active", btn.dataset.val === val);
  }
  syncSegmentedIndicator(box);
  if (field === "nl_trigger_mode") updateNlModeUI();
}

// 根据生图意图判断方式切换相关字段的显示
function updateNlModeUI() {
  const llm = groupValues.nl_trigger_mode === "llm";
  $("nl_keywords_field").classList.toggle("hidden", llm);
  $("nl_judge_field").classList.toggle("hidden", !llm);
}

// ---------- 加载 / 渲染 ----------
async function loadConfig() {
  const cfg = await bridge.apiGet("config");
  for (const k of TEXT_FIELDS) if ($(k)) $(k).value = cfg[k] ?? "";
  for (const k of TEXTAREA_FIELDS) if ($(k)) $(k).value = cfg[k] ?? "";
  for (const k of INT_FIELDS) if ($(k)) $(k).value = cfg[k] ?? 0;
  for (const k of BOOL_FIELDS) if ($(k)) $(k).checked = Boolean(cfg[k]);
  for (const k of Object.keys(GROUP_FIELDS)) setGroupValue(k, cfg[k]);
  await loadProviders(cfg);
}

// ---------- 润色 / 判断模型下拉(AstrBot 已配置的模型) ----------
async function loadProviders(cfg) {
  let list = [];
  let loadError = false;
  try {
    const data = await bridge.apiGet("providers");
    list = data.providers || [];
  } catch (e) {
    loadError = true;
    console.error("load providers:", e);
  }
  for (const id of ["enhance_provider_id", "nl_judge_provider_id"]) {
    const sel = $(id);
    sel.innerHTML = "";
    if (loadError) {
      sel.add(new Option("加载模型列表失败", ""));
    } else if (list.length === 0) {
      sel.add(new Option("暂无可用模型，请先在 AstrBot 中配置", ""));
    } else {
      sel.add(new Option("未选择", ""));
      for (const p of list) {
        const label = `${p.name || p.id}${p.model_name ? " · " + p.model_name : ""}`;
        sel.add(new Option(label, p.id));
      }
    }
    sel.value = (cfg && cfg[id]) || "";
    refreshCustomSelect(sel);
  }
}

// ---------- 恢复润色提示词默认值 ----------
async function restorePromptDefault(lang) {
  const target = lang === "en" ? "enhance_system_prompt_en" : "enhance_system_prompt_zh";
  try {
    const defaults = await bridge.apiGet("prompt-defaults");
    $(target).value = defaults[lang] || "";
    toast(`已恢复${lang === "en" ? "英文" : "中文"}默认提示词，请保存配置以应用`);
  } catch (e) {
    toast("恢复默认失败：" + (e.message || e), true);
  }
}

// ---------- 恢复触发关键词默认值 ----------
async function restoreKeywords() {
  try {
    const defaults = await bridge.apiGet("prompt-defaults");
    $("nl_keywords").value = defaults.nl_keywords || "";
    toast("已恢复默认触发关键词，请保存配置以应用");
  } catch (e) {
    toast("恢复默认失败：" + (e.message || e), true);
  }
}

async function loadStats() {
  try {
    const s = await bridge.apiGet("stats");
    $("stat-used").textContent = s.total_used;
    $("stat-total").textContent = s.total_limit > 0 ? s.total_limit : "不限";
    if (s.total_limit > 0 && s.total_used >= s.total_limit) {
      $("stat-dot").classList.add("full");
    }
  } catch { /* 状态不可用时不影响配置 */ }
}

async function loadCache() {
  try {
    const cache = await bridge.apiGet("cache");
    $("cache-status").textContent =
      `当前 ${cache.total_mb ?? 0} MB / ${cache.max_mb ?? 0} MB，共 ${cache.file_count ?? 0} 张图片`;
  } catch (e) {
    $("cache-status").textContent = "缓存状态加载失败";
    console.error("load cache:", e);
  }
}

async function clearCache() {
  const confirmed = await askConfirm({
    title: "清空图片缓存",
    message: "所有已生成并保存在本地的缓存图片将被删除，此操作无法恢复。配置和用量记录不会受影响。",
    confirmText: "清空缓存",
    danger: true,
  });
  if (!confirmed) return;
  const btn = $("btn-clear-cache");
  btn.disabled = true;
  try {
    const result = await bridge.apiPost("cache/clear", {});
    toast(`已清空 ${result.deleted_count ?? 0} 张缓存图片`);
    await loadCache();
  } catch (e) {
    toast("清空缓存失败：" + (e.message || e), true);
  } finally {
    btn.disabled = false;
  }
}

// ---------- 收集 / 保存 ----------
function collectConfig() {
  const payload = {};
  for (const k of TEXT_FIELDS) payload[k] = $(k).value.trim();
  for (const k of TEXTAREA_FIELDS) payload[k] = $(k).value;
  for (const k of INT_FIELDS) {
    const input = $(k);
    if (!input) continue;
    const v = parseInt(input.value, 10);
    payload[k] = Number.isFinite(v) ? v : 0;
  }
  for (const k of BOOL_FIELDS) payload[k] = $(k).checked;
  Object.assign(payload, groupValues);
  for (const id of ["enhance_provider_id", "nl_judge_provider_id"]) {
    const sel = $(id);
    payload[id] = (sel ? sel.value : "").trim();
  }
  return payload;
}

async function save() {
  const btn = $("btn-save");
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    await bridge.apiPost("config/save", collectConfig());
    toast("配置已保存并生效");
    $("save-tip").textContent = "配置已保存";
    await loadCache();
  } catch (e) {
    toast("保存失败：" + (e.message || e), true);
  } finally {
    btn.disabled = false;
    btn.textContent = "保存配置";
  }
}

// ---------- 测试连接 ----------
async function testKey() {
  const btn = $("btn-test");
  const out = $("test-result");
  btn.disabled = true;
  out.className = "test-result";
    out.textContent = "正在测试连接…";
  try {
    const r = await bridge.apiPost("test", { key: $("seedream_api_key").value.trim() });
    out.className = "test-result ok";
    out.textContent = "连接成功：" + (r.message || "API Key 有效");
  } catch (e) {
    out.className = "test-result err";
    out.textContent = "连接失败：" + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

// ---------- 测试生成(实际调用方舟,消耗 1 张配额) ----------
async function testGen() {
  const btn = $("btn-test-gen");
  const out = $("test-result");
  btn.disabled = true;
  out.className = "test-result";
    out.textContent = "正在生成测试图片，预计需要 1 至 3 分钟…";
  try {
    const r = await bridge.apiPost("test-gen", {
      key: $("seedream_api_key").value.trim(),
      model: $("seedream_model").value.trim(),
    });
    out.className = "test-result ok";
    out.textContent = r.message || "测试图片生成成功";
  } catch (e) {
    out.className = "test-result err";
    out.textContent = "生成失败：" + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

// ---------- 配额视图 ----------
let quotaSort = "today";

async function loadQuota() {
  const btn = $("btn-quota-refresh");
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "刷新中…";
  try {
    const d = await bridge.apiGet("quota-detail");
    $("sum-total-limit").textContent = `${d.total_used} / ${d.total_limit > 0 ? d.total_limit : "不限"}`;
    $("sum-total-used").textContent = d.total_generated ?? 0;
    $("sum-users").textContent = (d.users || []).length;
    $("sum-per-limit").textContent = d.per_user_limit > 0 ? d.per_user_limit : "不限";
    renderQuotaList(d);
    toast("用量数据已刷新");
  } catch (e) {
    $("quota-list").innerHTML = '<div class="empty">加载失败,请稍后重试</div>';
    toast("用量刷新失败：" + (e.message || e), true);
    console.error("load quota:", e);
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

function renderQuotaList(d) {
  const list = $("quota-list");
  const users = d.users || [];
  if (users.length === 0) {
    list.innerHTML = '<div class="empty">今日暂无生成记录</div>';
    return;
  }
  // 进度条基准:优先每人限额,其次今日总限额
  const perLimit = d.per_user_limit > 0 ? d.per_user_limit : (d.total_limit > 0 ? d.total_limit : 0);
  const sorted = [...users].sort((a, b) => (
    quotaSort === "total"
      ? b.used_total - a.used_total
      : b.used_today - a.used_today
  ));
  list.innerHTML = sorted.map((u) => {
    const name = u.name ? escapeHtml(u.name) : "未知昵称";
    const qq = escapeHtml(String(u.qq));
    const admin = u.is_admin
      ? '<span class="admin-badge">管理员</span>'
      : "";
    const pct = perLimit > 0 ? Math.min(100, Math.round((u.used_today / perLimit) * 100)) : 0;
    const hideProgress = u.is_admin && d.admin_ignore_limit;
    return `
      <div class="quota-row">
        <img class="avatar" src="https://q1.qlogo.cn/g?b=qq&nk=${qq}&s=100"
             alt="" loading="lazy" onerror="this.style.visibility='hidden'" />
        <div class="u-info">
          <b>${name}${admin}</b>
          <span>QQ: ${qq}</span>
        </div>
        <div class="quota-progress${hideProgress ? " hidden" : ""}" title="${hideProgress ? "管理员不受配额限制" : `今日使用 ${u.used_today}${perLimit > 0 ? ` / ${perLimit}` : ""}`}">
          <i style="width:${pct}%"></i>
        </div>
        <div class="u-nums">
          <span class="u-today">今日 <b>${u.used_today}</b></span>
          <span>总计 <b>${u.used_total}</b></span>
        </div>
        <div class="u-actions">
          <button class="btn ghost small btn-reset-user-today" data-qq="${qq}">重置今日</button>
          <button class="btn danger small btn-reset-user-all" data-qq="${qq}">重置全部</button>
        </div>
      </div>`;
  }).join("");
  for (const btn of list.querySelectorAll(".btn-reset-user-today")) {
    btn.addEventListener("click", () => resetUserQuota(btn.dataset.qq, false));
  }
  for (const btn of list.querySelectorAll(".btn-reset-user-all")) {
    btn.addEventListener("click", () => resetUserQuota(btn.dataset.qq, true));
  }
}

async function resetUserQuota(qq, resetAll) {
  const confirmed = await askConfirm({
    title: resetAll ? "完全重置用户记录" : "重置用户今日配额",
    message: resetAll
      ? `QQ ${qq} 的今日使用量和历史总使用量都将清零，此操作无法恢复。`
      : `QQ ${qq} 的今日使用量和今日限额计数将清零，历史总使用量会保留。`,
    confirmText: resetAll ? "重置全部" : "重置今日",
    danger: resetAll,
  });
  if (!confirmed) return;
  try {
    await bridge.apiPost(resetAll ? "quota/reset-user" : "quota/reset-user-today", { qq });
    toast(resetAll ? "该用户的全部用量记录已重置" : "该用户的今日用量已重置");
    loadQuota();
    loadStats();
  } catch (e) {
    toast("重置失败：" + (e.message || e), true);
  }
}

async function resetTodayQuota() {
  const confirmed = await askConfirm({
    title: "重置今日配额",
    message: "所有人的今日使用量和今日限额计数将清零，历史总使用量记录会保留。",
    confirmText: "重置今日配额",
  });
  if (!confirmed) return;
  const btn = $("btn-quota-reset-today");
  btn.disabled = true;
  try {
    await bridge.apiPost("quota/reset-today", {});
    toast("今日用量已重置");
    loadQuota();
    loadStats();
  } catch (e) {
    toast("重置失败：" + (e.message || e), true);
  } finally {
    btn.disabled = false;
  }
}

async function resetAllQuota() {
  const confirmed = await askConfirm({
    title: "完全重置所有记录",
    message: "今日使用量和所有人的历史总使用量将全部清零，此操作无法恢复。",
    confirmText: "完全重置",
    danger: true,
  });
  if (!confirmed) return;
  const btn = $("btn-quota-reset-all");
  btn.disabled = true;
  try {
    await bridge.apiPost("quota/reset-all", {});
    toast("全部用量记录已重置");
    loadQuota();
    loadStats();
  } catch (e) {
    toast("重置失败：" + (e.message || e), true);
  } finally {
    btn.disabled = false;
  }
}

// ---------- 复制链接(iframe 沙箱禁 window.open,改为复制) ----------
async function copyText(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fallback below */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* ignore */ }
  document.body.removeChild(ta);
  return ok;
}

// ---------- 详情视图 ----------
async function loadAbout() {
  try {
    const info = await bridge.apiGet("about");
    $("about-name").textContent = info.display_name || info.name || "Eidolon";
    $("about-version").textContent = info.version || "–";
    $("about-author").textContent = info.author || "–";
    if (info.repo) {
      $("about-repo-url").textContent = info.repo;
      $("about-repo-btn").addEventListener("click", async () => {
        if (await copyText(info.repo)) {
          toast("仓库链接已复制，请在浏览器中打开");
        } else {
          toast("复制失败,请手动复制上方链接", true);
        }
      });
    } else {
      $("about-repo-btn").style.display = "none";
      $("about-repo-url").textContent = "暂未设置仓库地址";
    }
    const ARK_URL = "https://console.volcengine.com/ark";
    $("about-ark-btn").addEventListener("click", async () => {
      if (await copyText(ARK_URL)) {
        toast("控制台链接已复制，请在浏览器中打开");
      } else {
        toast("复制失败,请手动复制上方链接", true);
      }
    });
  } catch (e) {
    console.error("load about:", e);
  }
}

// ---------- 事件绑定 ----------
function bindEvents() {
  $("btn-save").addEventListener("click", save);
  $("btn-test").addEventListener("click", testKey);
  $("btn-test-gen").addEventListener("click", testGen);
  $("btn-clear-cache").addEventListener("click", clearCache);
  $("toggle-key").addEventListener("click", () => {
    const input = $("seedream_api_key");
    input.type = input.type === "password" ? "text" : "password";
  });
  for (const btn of document.querySelectorAll(".restore-prompt")) {
    btn.addEventListener("click", () => restorePromptDefault(btn.dataset.lang));
  }
  for (const btn of document.querySelectorAll(".restore-keywords")) {
    btn.addEventListener("click", restoreKeywords);
  }
  for (const btn of document.querySelectorAll(".nav-item")) {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  }
  $("btn-collapse").addEventListener("click", () => {
    $("sidebar").classList.toggle("collapsed");
  });
  $("btn-quota-refresh").addEventListener("click", loadQuota);
  $("btn-quota-reset-today").addEventListener("click", resetTodayQuota);
  $("btn-quota-reset-all").addEventListener("click", resetAllQuota);
  $("confirm-cancel").addEventListener("click", () => closeConfirm(false));
  $("confirm-ok").addEventListener("click", () => closeConfirm(true));
  $("confirm-modal").addEventListener("click", (e) => {
    if (e.target === $("confirm-modal")) closeConfirm(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeConfirm(false);
  });
  $("quota_sort_group").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-val]");
    if (!btn) return;
    quotaSort = btn.dataset.val;
    for (const b of $("quota_sort_group").querySelectorAll("[data-val]")) {
      b.classList.toggle("active", b.dataset.val === quotaSort);
    }
    syncSegmentedIndicator($("quota_sort_group"));
    loadQuota();
  });
  window.addEventListener("resize", () => syncAllSegmentedIndicators(false));
}

// ---------- 启动 ----------
const ctx = await bridge.ready();
applyTheme(ctx);
bridge.onContext(applyTheme);
initGroups();
initCustomSelects();
initSmoothWheelScroll();
bindEvents();
await loadConfig();
syncAllSegmentedIndicators(false);
await loadStats();
await loadCache();
