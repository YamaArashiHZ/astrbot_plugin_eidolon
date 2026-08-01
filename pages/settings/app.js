// 异画师 Eidolon · 插件设置页逻辑(侧边导航 + 三视图)
const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

// 与后端 DEFAULT_CONFIG 对应的字段分组
const TEXT_FIELDS = ["seedream_api_key", "seedream_model", "proxy"];
const TEXTAREA_FIELDS = ["enhance_system_prompt_zh", "enhance_system_prompt_en"];
const INT_FIELDS = [
  "max_num", "cooldown_seconds", "request_timeout",
  "total_limit", "per_user_limit", "nl_min_prompt_len",
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
};

let groupValues = {}; // 当前分段选择器的值
let toastTimer = null;
let aboutLoaded = false;

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

// ---------- 润色模型下拉(AstrBot 已配置的模型) ----------
async function loadProviders(cfg) {
  const sel = $("enhance_provider_id");
  sel.innerHTML = "";
  try {
    const data = await bridge.apiGet("providers");
    const list = data.providers || [];
    if (list.length === 0) {
      sel.add(new Option("未配置模型(请先在 AstrBot 接入模型)", ""));
    } else {
      sel.add(new Option("未选择", ""));
      for (const p of list) {
        const label = `${p.name || p.id}${p.model_name ? " · " + p.model_name : ""}`;
        sel.add(new Option(label, p.id));
      }
    }
    sel.value = (cfg && cfg.enhance_provider_id) || "";
  } catch (e) {
    sel.add(new Option("加载模型列表失败", ""));
    console.error("load providers:", e);
  }
}

// ---------- 恢复润色提示词默认值 ----------
async function restorePromptDefault(lang) {
  const target = lang === "en" ? "enhance_system_prompt_en" : "enhance_system_prompt_zh";
  try {
    const defaults = await bridge.apiGet("prompt-defaults");
    $(target).value = defaults[lang] || "";
    toast(`已恢复${lang === "en" ? "英文" : "中文"}默认提示词,点击保存生效`);
  } catch (e) {
    toast("恢复默认失败: " + (e.message || e), true);
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

// ---------- 收集 / 保存 ----------
function collectConfig() {
  const payload = {};
  for (const k of TEXT_FIELDS) payload[k] = $(k).value.trim();
  for (const k of TEXTAREA_FIELDS) payload[k] = $(k).value;
  for (const k of INT_FIELDS) {
    const v = parseInt($(k).value, 10);
    payload[k] = Number.isFinite(v) ? v : 0;
  }
  for (const k of BOOL_FIELDS) payload[k] = $(k).checked;
  Object.assign(payload, groupValues);
  const sel = $("enhance_provider_id");
  payload.enhance_provider_id = (sel ? sel.value : "").trim();
  return payload;
}

async function save() {
  const btn = $("btn-save");
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    await bridge.apiPost("config/save", collectConfig());
    toast("已保存,立即生效 ✓");
    $("save-tip").textContent = "配置已同步到插件";
  } catch (e) {
    toast("保存失败: " + (e.message || e), true);
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
  out.textContent = "测试中…";
  try {
    const r = await bridge.apiPost("test", { key: $("seedream_api_key").value.trim() });
    out.className = "test-result ok";
    out.textContent = "✓ " + (r.message || "连接成功");
  } catch (e) {
    out.className = "test-result err";
    out.textContent = "✗ " + (e.message || e);
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
  out.textContent = "生成中,可能需要 30~90 秒…";
  try {
    const r = await bridge.apiPost("test-gen", {
      key: $("seedream_api_key").value.trim(),
      model: $("seedream_model").value.trim(),
    });
    out.className = "test-result ok";
    out.textContent = "✓ " + (r.message || "生成成功");
  } catch (e) {
    out.className = "test-result err";
    out.textContent = "✗ " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

// ---------- 配额视图 ----------
let quotaSort = "today";

async function loadQuota() {
  try {
    const d = await bridge.apiGet("quota-detail");
    $("sum-total-limit").textContent = d.total_limit > 0 ? d.total_limit : "不限";
    $("sum-total-used").textContent = d.total_used;
    $("sum-users").textContent = (d.users || []).length;
    $("sum-per-limit").textContent = d.per_user_limit > 0 ? d.per_user_limit : "不限";
    renderQuotaList(d);
  } catch (e) {
    $("quota-list").innerHTML = '<div class="empty">加载失败,请稍后重试</div>';
    console.error("load quota:", e);
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
    const used = quotaSort === "total" ? u.used_total : u.used_today;
    const pct = perLimit > 0 ? Math.min(100, Math.round((used / perLimit) * 100)) : 0;
    return `
      <div class="quota-row">
        <div class="quota-bar"><i style="height:${pct}%"></i></div>
        <img class="avatar" src="https://q1.qlogo.cn/g?b=qq&nk=${qq}&s=100"
             alt="" loading="lazy" onerror="this.style.visibility='hidden'" />
        <div class="u-info">
          <b>${name}${admin}</b>
          <span>QQ: ${qq}</span>
        </div>
        <div class="u-nums">
          <span class="u-today">今日 <b>${u.used_today}</b></span>
          <span>总计 <b>${u.used_total}</b></span>
        </div>
        <button class="btn ghost small btn-reset-user" data-qq="${qq}">重置</button>
      </div>`;
  }).join("");
  for (const btn of list.querySelectorAll(".btn-reset-user")) {
    btn.addEventListener("click", () => resetUserQuota(btn.dataset.qq));
  }
}

async function resetUserQuota(qq) {
  if (!confirm(`确定重置 QQ ${qq} 的今日与总使用量吗?`)) return;
  try {
    await bridge.apiPost("quota/reset-user", { qq });
    toast("已重置该用户配额 ✓");
    loadQuota();
  } catch (e) {
    toast("重置失败: " + (e.message || e), true);
  }
}

async function resetTodayQuota() {
  if (!confirm("仅重置今日配额使用?\n\n所有人的「今日使用量」将清零(今日限额计数同步重置),历史「总使用量」记录保留。")) return;
  const btn = $("btn-quota-reset-today");
  btn.disabled = true;
  try {
    await bridge.apiPost("quota/reset-today", {});
    toast("今日配额已重置 ✓");
    loadQuota();
    loadStats();
  } catch (e) {
    toast("重置失败: " + (e.message || e), true);
  } finally {
    btn.disabled = false;
  }
}

async function resetAllQuota() {
  if (!confirm("完全重置?\n\n今日使用量与所有人的历史总使用量记录将全部清零,且无法恢复。确定继续吗?")) return;
  const btn = $("btn-quota-reset-all");
  btn.disabled = true;
  try {
    await bridge.apiPost("quota/reset-all", {});
    toast("已完全重置全部记录 ✓");
    loadQuota();
    loadStats();
  } catch (e) {
    toast("重置失败: " + (e.message || e), true);
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
    $("about-name").textContent = info.display_name || info.name || "异画师";
    $("about-version").textContent = info.version || "–";
    $("about-author").textContent = info.author || "–";
    if (info.repo) {
      $("about-repo-url").textContent = info.repo;
      $("about-repo-btn").addEventListener("click", async () => {
        if (await copyText(info.repo)) {
          toast("仓库链接已复制,请到浏览器地址栏打开 ✓");
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
        toast("控制台链接已复制,请到浏览器地址栏打开 ✓");
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
  $("toggle-key").addEventListener("click", () => {
    const input = $("seedream_api_key");
    input.type = input.type === "password" ? "text" : "password";
  });
  for (const btn of document.querySelectorAll(".restore-prompt")) {
    btn.addEventListener("click", () => restorePromptDefault(btn.dataset.lang));
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
  $("quota_sort_group").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-val]");
    if (!btn) return;
    quotaSort = btn.dataset.val;
    for (const b of $("quota_sort_group").querySelectorAll("[data-val]")) {
      b.classList.toggle("active", b.dataset.val === quotaSort);
    }
    loadQuota();
  });
}

// ---------- 启动 ----------
const ctx = await bridge.ready();
applyTheme(ctx);
bridge.onContext(applyTheme);
initGroups();
bindEvents();
await loadConfig();
await loadStats();
