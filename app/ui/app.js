/* 飞牛下载助手 管理页脚本（原生 JS，无构建步骤） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* 空安全事件绑定：HTML 元素缺失时跳过并告警，绝不让单个缺失元素把
   整个脚本剩下的初始化全部带崩（0.1.20「关于/版本/更新失踪」的回归背景：
   顶层一处 null.addEventListener 抛异常 → 其后所有绑定和 init 全灭）。 */
function on(sel, ev, fn, opts) {
  const el = $(sel);
  if (el) el.addEventListener(ev, fn, opts);
  else console.warn("[fnmedia-dl] 绑定跳过（元素不存在）：", sel);
}

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
  if (!el) return;
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
    if (btn.dataset.tab === "add") { loadChips(); loadQueue(); }
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
    // 引擎今日配额行（通道设置页「迅雷下载引擎」卡）
    const q = data.thunder_engine && data.thunder_engine.quota;
    const qEl = $("#teQuota");
    if (qEl) {
      qEl.textContent = q
        ? `📊 今日已创建 ${q.used} 个任务 · ` +
          (q.blocked
            ? "今日配额已满（派发将跳过引擎，明日自动恢复；可走迅雷远程通道）"
            : "配额状态正常")
        : "";
    }
  } catch (e) {
    $("#backendChips").innerHTML = `<div class="chip bad" title="${e.message}">通道状态获取失败</div>`;
  }
}

/* ---------------- 解析 ---------------- */
on("#btnParse", "click", async () => {
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
on("#btnDispatch", "click", async () => {
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
      ${r.duplicate
        ? `<span class="result-warn">⚠ 之前下载过：${esc(r.duplicate.name || r.duplicate.url || "")}</span>`
        : ""}
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
    // 失败/已清理：重试+删除（没有暂停语义）；其余活动任务：暂停/继续+删除
    const dead = ["error", "gone"].includes(t.state);
    const canControl = !["gone", "removed", "completed", "error"].includes(t.state);
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
        ${dead ? `
          <button class="btn" data-act="retry"
                  data-backend="${esc(t.backend)}" data-id="${esc(t.id)}">重试</button>` : ""}
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

  const ACT_TEXT = { delete: "已删除", pause: "已暂停", resume: "已继续",
                     retry: "已按原链接重新派发" };
  body.querySelectorAll("button[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const { act, backend, id } = btn.dataset;
      try {
        const url = `/api/tasks/${encodeURIComponent(backend)}/${encodeURIComponent(id)}` +
                    (act === "delete" ? "" : `/${act}`);
        const r = await API.send(url, act === "delete" ? "DELETE" : "POST");
        // 重试可能自动换了通道（原通道配额已满），优先展示后端提示
        toast(act === "retry" && r && r.message ? r.message : (ACT_TEXT[act] || "已完成"));
        loadTasks();
      } catch (e) {
        toast("操作失败：" + e.message);
      }
    });
  });
}

on("#btnRefresh", "click", loadTasks);

/* ---------------- 下载历史查重 ---------------- */
async function checkHistory() {
  const url = $("#historyInput").value.trim();
  if (!url) return toast("请先粘贴要查询的链接");
  const box = $("#historyResult");
  box.hidden = false;
  box.className = "dir-status";
  box.textContent = "查询中…";
  try {
    const r = await API.send("/api/history/check", "POST", { links: [url] });
    const res = (r.results || [])[0] || {};
    if (res.downloaded) {
      box.classList.add("warn");
      const m = res.matches[0] || {};
      const when = m.created_at
        ? new Date(m.created_at * 1000).toLocaleString() : "时间未知";
      box.textContent = `⚠ 该链接下载过 ${res.count} 次（最近：${m.name || ""}` +
        ` · ${m.dir || "默认目录"} · ${when}），注意避免重复下载`;
    } else {
      box.classList.add("ok");
      box.textContent = "✓ 未找到下载记录，可以派发";
    }
  } catch (e) {
    box.classList.add("bad");
    box.textContent = "查询失败：" + e.message;
  }
}

on("#btnHistory", "click", checkHistory);
on("#historyInput", "keydown", (e) => {
  if (e.key === "Enter") checkHistory();
});

function scheduleAutoRefresh() {
  clearInterval(taskTimer);
  taskTimer = setInterval(() => {
    const auto = $("#autoRefresh");
    const tasks = $("#tab-tasks");
    if (auto && auto.checked && tasks && tasks.classList.contains("active")) {
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
  loadXdStatus();
  loadChips();   // 同步刷新引擎配额行（#teQuota 在本页）
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

on("#btnFillShare", "click", () => {
  if (!shareDirCached) return;
  $("#defaultDir").value = shareDirCached;
  toast("已填入应用共享目录，记得保存设置");
});

on("#btnCheckDir", "click", async () => {
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

on("#btnSaveCfg", "click", async () => {
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
on("#tcLoginBtn", "click", async () => {
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

/* ---------------- Docker 版迅雷一键部署（cnk3x/xunlei） ----------------
   安装向导选「帮我自动部署」或这里点按钮：自动拉镜像、起容器、配面板地址。
   部署由独立后台进程执行（服务重启不影响），这里只做状态轮询与结果回填。 */
const XDP = { timer: null };

function xdRender(s) {
  const box = $("#xdDeployStatus");
  const btn = $("#btnDeployXd");
  const d = (s && s.deploy) || {};
  const active = !!s && (s.running || (!d.done && d.stage !== "idle" && d.stage !== "error"));
  if (box) box.hidden = false;
  if (btn) btn.disabled = active;
  if (!box) return active;
  if (active) {
    box.className = "dir-status";
    box.textContent = `部署中：${d.detail || d.stage || "…"}（${d.percent || 0}%）`;
  } else if (d.stage === "error") {
    box.className = "dir-status bad";
    box.textContent = "✗ " + (d.detail || "部署失败") + "，可点上方按钮重试";
  } else if (s && s.container === "running") {
    box.className = "dir-status ok";
    box.textContent = "✓ Docker 版迅雷容器运行中，点「测试连接」确认连通后即可使用";
  } else if (d.done) {
    box.className = "dir-status ok";
    box.textContent = "✓ " + (d.detail || "已部署");
  } else if (s && !s.docker) {
    box.className = "dir-status bad";
    box.textContent = "未检测到 Docker：请先到 应用中心 → Docker 安装并启动，再点「一键部署」";
  } else {
    box.className = "dir-status";
    box.textContent = "还没部署 Docker 版迅雷：点上方按钮自动安装配置（需 NAS 已装 Docker）";
  }
  return active;
}

/* 目录选项（默认 / 自己设置）：radio 联动输入框可用状态 */
function xdPick(name) {
  const el = $(`input[name="${name}"]:checked`);
  return el ? el.value : "default";
}

$$('input[name="xdDataMode"]').forEach((r) => r.addEventListener("change", () => {
  const t = $("#xdDataDir");
  if (t) t.disabled = xdPick("xdDataMode") !== "custom";
}));
$$('input[name="xdDlMode"]').forEach((r) => r.addEventListener("change", () => {
  const t = $("#xdDlDir");
  if (t) t.disabled = xdPick("xdDlMode") !== "custom";
}));

function xdDirBody() {
  const body = {};
  if (xdPick("xdDataMode") === "custom") body.data_dir = ($("#xdDataDir").value || "").trim();
  if (xdPick("xdDlMode") === "custom") body.dl_dir = ($("#xdDlDir").value || "").trim();
  return body;
}

async function loadXdStatus() {
  try {
    const s = await API.get("/api/xunlei_docker/status");
    const dd = $("#xdDataDefault");
    const dl = $("#xdDlDefault");
    if (dd) dd.textContent = s.default_data_dir || "应用数据共享目录";
    if (dl) dl.textContent = s.default_dl_dir || "应用下载共享目录";
    xdRender(s);
  } catch (e) {
    const box = $("#xdDeployStatus");
    if (!box) return;
    box.hidden = false;
    box.className = "dir-status bad";
    box.textContent = "部署状态获取失败：" + e.message;
  }
}

function xdStartPoll() {
  if (XDP.timer) return;
  XDP.timer = setInterval(async () => {
    let s = null;
    try {
      s = await API.get("/api/xunlei_docker/status");
    } catch {
      return;   // 服务暂不可达，等下一轮
    }
    const active = xdRender(s);
    if (active) return;
    clearInterval(XDP.timer);
    XDP.timer = null;
    const d = s.deploy || {};
    if (d.done && d.stage !== "error") {
      // 成功后自动回填面板地址并保存，用户零操作
      const teUrl = $("#teUrl");
      if (teUrl && !teUrl.value.trim()) teUrl.value = s.panel_url;
      try { await saveConfig(); } catch { /* 保存失败不影响部署结果展示 */ }
      toast("✓ Docker 版迅雷已就绪，打开面板登录迅雷账号，激活内测码填「迅雷牛通」即可使用");
      loadChips();
    }
  }, 2500);
}

on("#btnDeployXd", "click", async () => {
  const opts = xdDirBody();
  const dirNote =
    (opts.data_dir ? `配置文件存放位置：${opts.data_dir}` : "配置文件存放位置：默认（应用数据共享）") + "\n" +
    (opts.dl_dir ? `下载文件存放位置：${opts.dl_dir}` : "下载文件存放位置：默认（应用下载共享）");
  if (!confirm("将自动拉取 cnk3x/xunlei 镜像并启动容器，继续？\n" +
    dirNote + "\n需要 NAS 已安装并启用 Docker（应用中心 → Docker）。")) return;
  const btn = $("#btnDeployXd");
  const box = $("#xdDeployStatus");
  if (btn) btn.disabled = true;
  if (box) {
    box.hidden = false;
    box.className = "dir-status";
    box.textContent = "部署中：准备…";
  }
  try {
    const r = await API.send("/api/xunlei_docker/deploy", "POST", opts);
    toast(r.detail || "已开始部署…");
    xdStartPoll();
  } catch (e) {
    if (box) {
      box.className = "dir-status bad";
      box.textContent = "✗ " + e.message;
    }
    if (btn) btn.disabled = false;
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

on("#tcSmsSend", "click", async () => {
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

on("#tcSmsCheck", "click", async () => {
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
    const top = $("#appVersion");
    const about = $("#aboutVersion");
    const aboutText = $("#aboutVersionText");
    if (top) top.textContent = v;
    if (about) about.textContent = v;
    if (aboutText) aboutText.textContent = `${h.name || "fnmedia-dl"} ${v}`;
  } catch {
    /* 版本显示失败不影响主功能 */
  }
}

on("#updateCheckBtn", "click", async () => {
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

on("#updateRunBtn", "click", async () => {
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

/* ---------------- 任务队列（每日定时派发） ---------------- */
async function loadQueue() {
  try {
    renderQueue(await API.get("/api/queue"));
  } catch (e) {
    const box = $("#queueStatus");
    if (box) {
      box.hidden = false;
      box.className = "dir-status bad";
      box.textContent = "队列加载失败：" + e.message;
    }
  }
}

const QSTATE_BADGE = {
  queued: `<span class="result-warn">待派发</span>`,
  done: `<span class="result-ok">✓ 已派发</span>`,
  failed: `<span class="result-fail">✗ 派发失败</span>`,
};

function renderQueue(s) {
  const items = (s && s.items) || [];
  const st = (s && s.settings) || {};
  const auto = $("#queueAuto");
  const tm = $("#queueTime");
  if (auto) auto.checked = !!st.enabled;
  if (tm && st.time) tm.value = st.time;

  const ul = $("#queueList");
  if (ul) {
    ul.innerHTML = "";
    if (!items.length) {
      ul.innerHTML = `<li class="empty">队列为空：在上方粘贴链接后点「加入队列」</li>`;
    }
    for (const it of items) {
      const li = document.createElement("li");
      li.innerHTML = `
        <span class="link-name">${esc(it.name || it.url)}</span>
        <span class="link-reason">${esc(it.url)}</span>
        ${QSTATE_BADGE[it.status] || esc(it.status)}
        ${it.status === "done" && it.backend ? `<span class="link-reason">${esc(it.backend)}</span>` : ""}
        ${it.status === "failed" && it.error ? `<span class="result-fail">${esc(it.error)}</span>` : ""}
        <button class="btn danger" data-qdel="${esc(it.id)}">移除</button>`;
      ul.appendChild(li);
    }
    ul.querySelectorAll("button[data-qdel]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          renderQueue(await API.send(`/api/queue/${encodeURIComponent(btn.dataset.qdel)}`, "DELETE"));
        } catch (e) {
          toast("移除失败：" + e.message);
        }
      });
    });
  }

  const box = $("#queueStatus");
  if (box) {
    box.hidden = false;
    box.className = "dir-status";
    box.textContent = st.enabled
      ? `✓ 每日 ${st.time || "00:05"} 自动派发待发项` +
        (st.last_run ? ` · 上次派发：${st.last_run}` : "")
      : "定时派发未开启：勾选「每日定时派发」并保存，每天到点自动放行队列";
  }
}

on("#btnEnqueue", "click", async () => {
  const text = $("#linkInput").value.trim();
  if (!text) return toast("请先在上方粘贴链接");
  try {
    const r = await API.send("/api/queue", "POST", {
      text,
      directory: $("#dirInput").value.trim() || null,
    });
    toast(`✓ 已加入队列 ${(r.added || []).length} 条`);
    renderQueue(r);
  } catch (e) {
    toast("入队失败：" + e.message);
  }
});

on("#btnQueueRun", "click", async () => {
  const btn = $("#btnQueueRun");
  if (btn) btn.disabled = true;
  try {
    const r = await API.send("/api/queue/run", "POST", {});
    const results = r.results || [];
    toast(`队列派发完成：成功 ${results.filter((x) => x.ok).length} / ${results.length}`);
    renderQueue(r);
  } catch (e) {
    toast("派发失败：" + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
});

on("#btnQueueSave", "click", async () => {
  try {
    const r = await API.send("/api/queue/settings", "POST", {
      enabled: $("#queueAuto").checked,
      time: $("#queueTime").value || "00:05",
    });
    toast($("#queueAuto").checked ? "✓ 已开启每日定时派发" : "已关闭每日定时派发");
    renderQueue(r);
  } catch (e) {
    toast("保存失败：" + e.message);
  }
});

/* ---------------- init ----------------
   顶层执行到这说明脚本未被中途异常带崩；给页面看门狗一个就绪信号。
   看门狗见 index.html 尾部（脚本没跑起来时给用户明确提示而不是静默残废）。 */
try {
  loadChips();
  loadVersion();
  loadQueue();
  scheduleAutoRefresh();
  window.__fnmediaReady = true;
} catch (e) {
  console.error("[fnmedia-dl] 初始化异常：", e);
}
