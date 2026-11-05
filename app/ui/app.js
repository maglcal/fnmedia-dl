/* 飞牛下载助手 管理页脚本（原生 JS，无构建步骤） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const API = {
  // 所有请求走相对路径（配合 <base> 前缀自适应），绝对路径会在
  // /app/fnmedia-dl/ 挂载形态下打到应用外面导致 404。
  _rel: (u) => u.replace(/^\//, ""),
  async get(url) {
    const r = await fetch(this._rel(url));
    if (!r.ok) throw new Error(await errText(r));
    return r.json();
  },
  async send(url, method, body) {
    const r = await fetch(this._rel(url), {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) throw new Error(await errText(r));
    return r.json();
  },
};

async function errText(r) {
  try {
    const d = await r.json();
    return d.detail || JSON.stringify(d);
  } catch {
    return `HTTP ${r.status}`;
  }
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.hidden = true), 2600);
}

function fmtSize(n) {
  if (!n && n !== 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function fmtSpeed(n) {
  return n ? fmtSize(n) + "/s" : "-";
}

const STATE_TEXT = {
  running: "下载中", waiting: "排队中", paused: "已暂停",
  completed: "已完成", error: "失败", gone: "已清理", removed: "已移除",
};

/* ---------------- Tabs ---------------- */
$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "tasks") loadTasks();
    if (btn.dataset.tab === "settings") loadConfig();
    if (btn.dataset.tab === "add") loadChips();
  });
});

/* ---------------- 通道状态 chips ---------------- */
async function loadChips() {
  try {
    const data = await API.get("/api/backends");
    const box = $("#backendChips");
    box.innerHTML = "";
    for (const [key, st] of Object.entries(data)) {
      const div = document.createElement("div");
      const cls = !st.configured ? "idle" : st.reachable ? "ok" : "bad";
      div.className = `chip ${cls}`;
      div.title = st.detail || "";
      div.textContent = st.label || key;
      box.appendChild(div);
    }
  } catch (e) {
    $("#backendChips").innerHTML = `<div class="chip bad" title="${e.message}">通道状态获取失败</div>`;
  }
}

/* ---------------- 解析 ---------------- */
$("#btnParse").addEventListener("click", async () => {
  const text = $("#linkInput").value.trim();
  if (!text) return toast("请先粘贴链接");
  try {
    const data = await API.send("/api/parse", "POST", { text });
    renderParse(data.links);
  } catch (e) {
    toast("解析失败：" + e.message);
  }
});

function renderParse(links) {
  const card = $("#parseCard");
  card.hidden = false;
  $("#parseCount").textContent = `${links.length} 条`;
  const ul = $("#parseList");
  ul.innerHTML = "";
  for (const li of links) {
    const el = document.createElement("li");
    el.innerHTML = `
      <span class="kind ${li.kind}">${li.kind}</span>
      <span class="link-name">${esc(li.display || li.url)}</span>
      ${li.dispatchable ? "" : `<span class="result-fail">${esc(li.reason || "不支持派发")}</span>`}
      ${li.meta && li.meta.infohash ? `<span class="link-reason">信息哈希：${esc(li.meta.infohash)}</span>` : ""}
    `;
    ul.appendChild(el);
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------- 派发 ---------------- */
$("#btnDispatch").addEventListener("click", async () => {
  const text = $("#linkInput").value.trim();
  if (!text) return toast("请先粘贴链接");
  const btn = $("#btnDispatch");
  btn.disabled = true;
  btn.textContent = "派发中…";
  try {
    const data = await API.send("/api/tasks", "POST", {
      text,
      backend: $("#backendSelect").value,
      directory: $("#dirInput").value.trim() || null,
    });
    renderDispatch(data.results);
    toast(`派发完成：成功 ${data.results.filter((r) => r.ok).length} / ${data.results.length}`);
  } catch (e) {
    toast("派发失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "开始下载";
  }
});

function renderDispatch(results) {
  const card = $("#dispatchCard");
  card.hidden = false;
  const ul = $("#dispatchList");
  ul.innerHTML = "";
  for (const r of results) {
    const el = document.createElement("li");
    el.innerHTML = `
      <span class="link-name">${esc(r.url)}</span>
      ${r.ok
        ? `<span class="result-ok">✓ 已提交 · ${esc(r.label || r.backend)} · 任务 ${esc((r.id || "").slice(0, 12))}</span>`
        : `<span class="result-fail">✗ ${esc(r.error)}</span>`}
    `;
    ul.appendChild(el);
  }
}

/* ---------------- 任务列表 ---------------- */
let taskTimer = null;

async function loadTasks() {
  try {
    const data = await API.get("/api/tasks");
    renderTasks(data.tasks, data.backend_errors || {});
  } catch (e) {
    $("#taskError").hidden = false;
    $("#taskError").textContent = "任务列表加载失败：" + e.message;
  }
}

function renderTasks(tasks, backendErrors) {
  const errBox = $("#taskError");
  const errs = Object.entries(backendErrors);
  if (errs.length) {
    errBox.hidden = false;
    errBox.textContent = errs.map(([k, v]) => `${k}: ${v}`).join("；");
  } else {
    errBox.hidden = true;
  }

  const body = $("#taskBody");
  if (!tasks.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">暂无任务</td></tr>`;
    return;
  }
  body.innerHTML = "";
  for (const t of tasks) {
    const tr = document.createElement("tr");
    const canControl = !["gone", "removed", "completed"].includes(t.state);
    tr.innerHTML = `
      <td>
        <div class="task-name">${esc(t.name)}</div>
        ${t.url ? `<div class="task-url">${esc(t.url)}</div>` : ""}
        ${t.message ? `<div class="task-url">${esc(t.message)}</div>` : ""}
      </td>
      <td>${esc(t.backend)}</td>
      <td><span class="state ${t.state}">${STATE_TEXT[t.state] || t.state}</span></td>
      <td>
        <div class="progress"><div class="state-${t.state}" style="width:${Math.min(100, t.progress || 0)}%"></div></div>
        <div class="progress-text"><span>${(t.progress || 0).toFixed(1)}%</span><span>${fmtSize(t.size)}</span></div>
      </td>
      <td>${fmtSpeed(t.speed)}</td>
      <td>
        ${canControl ? `
          <button class="btn" data-act="${t.state === "paused" ? "resume" : "pause"}"
                  data-backend="${esc(t.backend)}" data-id="${esc(t.id)}">
            ${t.state === "paused" ? "继续" : "暂停"}
          </button>` : ""}
        <button class="btn danger" data-act="delete"
                data-backend="${esc(t.backend)}" data-id="${esc(t.id)}">删除</button>
      </td>
    `;
    body.appendChild(tr);
  }

  body.querySelectorAll("button[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const { act, backend, id } = btn.dataset;
      try {
        const url = `/api/tasks/${encodeURIComponent(backend)}/${encodeURIComponent(id)}` +
                    (act === "delete" ? "" : `/${act}`);
        await API.send(url, act === "delete" ? "DELETE" : "POST");
        toast(act === "delete" ? "已删除" : act === "pause" ? "已暂停" : "已继续");
        loadTasks();
      } catch (e) {
        toast("操作失败：" + e.message);
      }
    });
  });
}

$("#btnRefresh").addEventListener("click", loadTasks);

function scheduleAutoRefresh() {
  clearInterval(taskTimer);
  taskTimer = setInterval(() => {
    if ($("#autoRefresh").checked && $("#tab-tasks").classList.contains("active")) {
      loadTasks();
    }
  }, 5000);
}

/* ---------------- 设置 ---------------- */
const ORDER_KEYS = ["thunder_cloud", "thunder_engine", "aria2"];
const ORDER_LABELS = {
  thunder_cloud: "迅雷远程通道",
  thunder_engine: "迅雷下载引擎",
  aria2: "aria2",
};
let currentOrder = [...ORDER_KEYS];

function renderOrder() {
  const box = $("#orderBox");
  box.innerHTML = "";
  currentOrder.forEach((key, i) => {
    const div = document.createElement("div");
    div.className = "order-item";
    div.innerHTML = `
      <span class="idx">${i + 1}</span>
      <span class="name">${ORDER_LABELS[key] || key}</span>
      <button class="btn ghost" data-move="up" data-key="${key}" ${i === 0 ? "disabled" : ""}>上移</button>
      <button class="btn ghost" data-move="down" data-key="${key}" ${i === currentOrder.length - 1 ? "disabled" : ""}>下移</button>
    `;
    box.appendChild(div);
  });
  box.querySelectorAll("button[data-move]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = currentOrder.indexOf(btn.dataset.key);
      const to = btn.dataset.move === "up" ? idx - 1 : idx + 1;
      if (to < 0 || to >= currentOrder.length) return;
      [currentOrder[idx], currentOrder[to]] = [currentOrder[to], currentOrder[idx]];
      renderOrder();
    });
  });
}

async function loadConfig() {
  try {
    const cfg = await API.get("/api/config");
    currentOrder = (cfg.engine_order || ORDER_KEYS).slice();
    renderOrder();
    $("#defaultDir").value = cfg.default_dir || "";
    const tc = cfg.thunder_cloud || {};
    $("#tcLogin").value = tc.login_name || "";
    $("#tcPassword").value = tc.password || "";
    $("#tcCreditKey").value = tc.creditkey || "";
    $("#tcTarget").value = tc.target || "";
    $("#tcDir").value = tc.dir || "";
    const te = cfg.thunder_engine || {};
    $("#teUrl").value = te.base_url || "";
    $("#teDir").value = te.dir || "";
    const a2 = cfg.aria2 || {};
    $("#a2Url").value = a2.rpc_url || "";
    $("#a2Secret").value = a2.secret || "";
    $("#a2Dir").value = a2.dir || "";
  } catch (e) {
    toast("配置加载失败：" + e.message);
  }
  loadFs();
  loadTcReview();
}

/* 目录与授权：共享目录、已授权目录、目录检测 */
let shareDirCached = "";

async function loadFs() {
  try {
    const p = await API.get("/api/fs/paths");
    shareDirCached = p.share_dir || "";
    $("#authPaths").textContent = p.accessible.length
      ? p.accessible.join("、")
      : "（无。仅应用共享目录免授权可用）";
    $("#btnFillShare").disabled = !shareDirCached;
  } catch (e) {
    $("#authPaths").textContent = "获取失败：" + e.message;
  }
}

$("#btnFillShare").addEventListener("click", () => {
  if (!shareDirCached) return;
  $("#defaultDir").value = shareDirCached;
  toast("已填入应用共享目录，记得保存设置");
});

$("#btnCheckDir").addEventListener("click", async () => {
  const dir = $("#defaultDir").value.trim();
  const box = $("#dirStatus");
  if (!dir) { toast("先在上方填写要检测的目录"); return; }
  box.hidden = false;
  box.className = "dir-status";
  box.textContent = "检测中…";
  try {
    const r = await API.get("/api/fs/check?path=" + encodeURIComponent(dir));
    if (r.ok) {
      box.classList.add("ok");
      box.textContent = "✓ 可用：目录存在且本应用可写";
    } else if (!r.exists) {
      box.classList.add("bad");
      box.textContent = "✗ 目录不存在或本应用不可见。" + r.hint;
    } else {
      box.classList.add("bad");
      box.textContent = "✗ 目录存在但本应用不可写。" + r.hint;
    }
  } catch (e) {
    box.classList.add("bad");
    box.textContent = "检测失败：" + e.message;
  }
});

async function saveConfig() {
  const cfg = {
    engine_order: currentOrder,
    default_dir: $("#defaultDir").value.trim(),
    thunder_cloud: {
      login_name: $("#tcLogin").value.trim(),
      password: $("#tcPassword").value,
      creditkey: $("#tcCreditKey").value.trim(),
      target: $("#tcTarget").value.trim(),
      dir: $("#tcDir").value.trim(),
    },
    thunder_engine: {
      base_url: $("#teUrl").value.trim(),
      dir: $("#teDir").value.trim(),
    },
    aria2: {
      rpc_url: $("#a2Url").value.trim(),
      secret: $("#a2Secret").value,
      dir: $("#a2Dir").value.trim(),
    },
  };
  await API.send("/api/config", "PUT", { config: cfg });
}

$("#btnSaveCfg").addEventListener("click", async () => {
  try {
    await saveConfig();
    $("#cfgMsg").textContent = "已保存";
    setTimeout(() => ($("#cfgMsg").textContent = ""), 2000);
    loadChips();
    loadTcReview();   // 保存可能改动信任密钥/验证状态，同步验证框
  } catch (e) {
    toast("保存失败：" + e.message);
  }
});

/* 测试/登录结果在卡片内持久展示（风控提示较长，toast 会被截断） */
function cardStatus(btn, text, ok) {
  const card = btn.closest(".card");
  if (!card) return;
  let el = card.querySelector(".card-status");
  if (!el) {
    el = document.createElement("div");
    el.className = "card-status";
    card.appendChild(el);
  }
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.textContent = text;
  el.hidden = false;
}

/* 测试按钮 */
$$("button[data-test]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "测试中…";
    try {
      // 先保存当前表单，保证测试的是最新配置（await，不靠定时器赌时序）
      await saveConfig();
      const st = await API.send("/api/backends/test", "POST", { backend: btn.dataset.test });
      cardStatus(btn, (st.reachable ? "✓ " : "✗ ") + st.detail, st.reachable);
    } catch (e) {
      cardStatus(btn, "✗ 测试失败：" + e.message, false);
    } finally {
      btn.disabled = false;
      btn.textContent = "测试连接";
      if (btn.dataset.test === "thunder_cloud") loadTcReview();
    }
  });
});

/* 迅雷重新登录 */
$("#tcLoginBtn").addEventListener("click", async () => {
  $("#tcLoginBtn").disabled = true;
  try {
    $("#btnSaveCfg").click();
    await new Promise((r) => setTimeout(r, 300));
    const st = await API.send("/api/backends/test", "POST", { backend: "thunder_cloud" });
    cardStatus($("#tcLoginBtn"), (st.reachable ? "✓ " : "✗ ") + st.detail, st.reachable);
    loadChips();
  } catch (e) {
    cardStatus($("#tcLoginBtn"), "✗ 登录失败：" + e.message, false);
  } finally {
    $("#tcLoginBtn").disabled = false;
    loadTcReview();
  }
});

/* ---------------- 迅雷风控短信验证（review_panel） ----------------
   验证链接是迅雷 App 内嵌页，独立浏览器打开白屏；这里直调
   sendsms/checksms 完成验证。 */
async function loadTcReview() {
  try {
    const st = await API.get("/api/thunder_cloud/review");
    $("#tcReviewBox").hidden = !st.active;
    $("#tcReviewMobile").textContent = st.mobile ? `接收号码 ${st.mobile}` : "";
    if (!st.active) $("#tcReviewStatus").hidden = true;
    // 风控期间信任密钥以后端为准（自动填入；正在输入时不打断）
    const keyEl = $("#tcCreditKey");
    if (st.active && st.creditkey && document.activeElement !== keyEl) {
      keyEl.value = st.creditkey;
    }
  } catch (e) {
    /* 静默：仅影响验证框显示 */
  }
}

function tcReviewMsg(text, ok) {
  const el = $("#tcReviewStatus");
  el.hidden = false;
  el.className = "dir-status " + (ok ? "ok" : "bad");
  el.textContent = text;
}

$("#tcSmsSend").addEventListener("click", async () => {
  const btn = $("#tcSmsSend");
  btn.disabled = true;
  btn.textContent = "发送中…";
  try {
    const r = await API.send("/api/thunder_cloud/review/send", "POST", {});
    if (r.ok) tcReviewMsg("✓ 验证码已发送，请查收短信", true);
    else tcReviewMsg("✗ " + r.detail, false);
  } catch (e) {
    tcReviewMsg("✗ " + e.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = "获取验证码";
    loadTcReview();   // 会话失效自动刷新后，同步新密钥/号码进面板
  }
});

$("#tcSmsCheck").addEventListener("click", async () => {
  const btn = $("#tcSmsCheck");
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    const r = await API.send("/api/thunder_cloud/review/check", "POST", {
      code: $("#tcSmsCode").value.trim(),
    });
    if (r.ok) {
      tcReviewMsg("✓ " + (r.detail || "验证通过"), true);
      // 先把续期后的信任密钥同步进表单，再触发重新登录
      // （登录链会先保存表单，不同步会把续期值冲掉）
      if (r.creditkey) $("#tcCreditKey").value = r.creditkey;
      loadTcReview();
      $("#tcLoginBtn").click();   // 验证通过自动重新登录
    } else {
      tcReviewMsg("✗ " + r.detail, false);
    }
  } catch (e) {
    tcReviewMsg("✗ " + e.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = "提交验证";
    loadTcReview();
  }
});

/* ---------------- 版本与在线更新 ---------------- */
async function loadVersion() {
  try {
    const h = await API.get("/api/health");
    const v = "v" + (h.version || "?");
    $("#appVersion").textContent = v;
    $("#aboutVersion").textContent = v;
    $("#aboutVersionText").textContent = `${h.name || "fnmedia-dl"} ${v}`;
  } catch {
    /* 版本显示失败不影响主功能 */
  }
}

$("#updateCheckBtn").addEventListener("click", async () => {
  const btn = $("#updateCheckBtn");
  const st = $("#updateStatus");
  btn.disabled = true;
  st.hidden = false;
  st.className = "dir-status";
  st.textContent = "正在检查更新…";
  try {
    const r = await API.get("/api/update/check");
    if (r.has_update) {
      // 记住 fpk 资产地址与目标版本，亮出「一键更新」
      const fpk = (r.assets || []).find((a) => (a.name || "").endsWith(".fpk"));
      if (fpk && fpk.url) {
        UPD.url = fpk.url;
        UPD.target = r.latest;
        $("#updateRunBtn").hidden = false;
      }
      st.innerHTML =
        `发现新版本 <b>v${r.latest}</b>（当前 v${r.current}）` +
        (fpk ? "，可点「一键更新」在线升级" :
          (r.url ? ` —— <a href="${r.url}" target="_blank" rel="noreferrer">前往下载</a>` : ""));
    } else {
      $("#updateRunBtn").hidden = true;
      st.textContent = `✓ 已是最新版本（v${r.current}）`;
    }
  } catch (e) {
    st.textContent = "✗ 检查更新失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
});

/* ---------------- 一键在线更新（下载 fpk → 替换 → 重启，带进度） ---------------- */
const UPD = { url: "", target: "", timer: null };

$("#updateRunBtn").addEventListener("click", async () => {
  if (!UPD.url) return;
  const st = $("#updateStatus");
  const box = $("#updateProgressBox");
  const bar = $("#updateProgressBar");
  const stageEl = $("#updateProgressStage");
  const pctEl = $("#updateProgressPct");
  $("#updateRunBtn").disabled = true;
  $("#updateCheckBtn").disabled = true;
  st.hidden = false;
  st.className = "dir-status";
  st.textContent = `正在在线更新到 v${UPD.target}，期间服务会短暂重启，请勿关闭页面…`;
  box.hidden = false;
  bar.style.width = "2%";
  stageEl.textContent = "准备下载…";
  pctEl.textContent = "2%";
  try {
    await API.send("/api/update/download", "POST",
      { url: UPD.url, version: UPD.target });
  } catch (e) {
    st.className = "dir-status bad";
    st.textContent = "✗ 启动更新失败：" + e.message;
    box.hidden = true;
    $("#updateRunBtn").disabled = false;
    $("#updateCheckBtn").disabled = false;
    return;
  }
  const render = (p) => {
    const pct = Math.max(0, Math.min(100, p.percent || 0));
    bar.style.width = pct + "%";
    stageEl.textContent = p.detail || p.stage || "";
    pctEl.textContent = pct + "%";
    return p;
  };
  clearInterval(UPD.timer);
  UPD.timer = setInterval(async () => {
    let p = null;
    try {
      p = render(await API.get("/api/update/progress"));
    } catch {
      /* 服务重启中接口暂不可达，等下一轮 */
      stageEl.textContent = "服务重启中…";
    }
    if (p && p.done) {
      clearInterval(UPD.timer);
      if (p.stage === "error") {
        st.className = "dir-status bad";
        st.textContent = "✗ " + (p.detail || "更新失败");
        box.hidden = true;
        $("#updateRunBtn").disabled = false;
        $("#updateCheckBtn").disabled = false;
        return;
      }
      // 替换完成、服务重启中：轮询 health 等新版本起来
      stageEl.textContent = "服务重启中，等待新版本生效…";
      bar.style.width = "100%";
      pctEl.textContent = "100%";
      const oldVer = (p.from_version || "").replace(/^v/, "");
      const newVer = (p.to_version || UPD.target || "").replace(/^v/, "");
      const deadline = Date.now() + 90000;
      const healthTimer = setInterval(async () => {
        try {
          const h = await API.get("/api/health");
          const v = (h.version || "").replace(/^v/, "");
          if (v && v !== oldVer) {
            clearInterval(healthTimer);
            st.className = "dir-status ok";
            st.textContent = `✓ 更新完成：v${oldVer} → v${v}，页面即将自动刷新…`;
            toast(`✓ 已更新到 v${v}`);
            $("#updateRunBtn").hidden = true;
            $("#updateCheckBtn").disabled = false;
            loadVersion();
            // 重载整页让新 UI/署名等页面改动生效（版本号新、页面旧的问题）
            setTimeout(() => location.reload(), 1800);
            return;
          }
        } catch { /* 还没起来 */ }
        if (Date.now() > deadline) {
          clearInterval(healthTimer);
          st.className = "dir-status bad";
          st.textContent = `⚠ 文件已替换为 v${newVer}，但服务未在预期时间内恢复，` +
            `请到应用中心重启「飞牛下载助手」`;
          box.hidden = true;
          $("#updateRunBtn").disabled = false;
          $("#updateCheckBtn").disabled = false;
        }
      }, 2000);
    }
  }, 1000);
});

/* ---------------- init ---------------- */
loadChips();
loadVersion();
scheduleAutoRefresh();
