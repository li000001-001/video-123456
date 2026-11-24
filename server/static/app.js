/* 视频分类助手 · 前端逻辑（原生 JS，无框架，单核设备也流畅） */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const state = {
    tasks: [],
    cards: new Map(),
    pollTimer: null,
    voicesLoaded: false,
  };

  /* ---------------- 通用 ---------------- */
  function toast(msg, isErr) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.toggle("err", !!isErr);
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 2400);
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    if (!res.ok) {
      let msg = "请求失败 " + res.status;
      try {
        const j = await res.json();
        if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch (e) { /* ignore */ }
      throw new Error(msg);
    }
    return res.json();
  }

  function fmtDuration(sec) {
    sec = Math.round(sec || 0);
    if (!sec) return "";
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m + ":" + String(s).padStart(2, "0");
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  /* ---------------- 视图切换 ---------------- */
  function switchView(name) {
    $$(".view").forEach((v) => v.classList.add("hidden"));
    const view = $("#view-" + name);
    if (view) view.classList.remove("hidden");
    $$(".tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    if (name === "tasks") loadTasks();
    if (name === "settings") loadSettings();
  }

  $$(".tabbar button").forEach((b) => {
    b.addEventListener("click", () => switchView(b.dataset.view));
  });

  /* ---------------- 输入解析 ---------------- */
  const inputUrl = $("#inputUrl");
  const platformTag = $("#platformTag");
  let parseTimer = null;

  function guessPlatform(text) {
    const t = (text || "").toLowerCase();
    if (t.includes("douyin.com")) return "抖音";
    if (t.includes("toutiao.com") || t.includes("ixigua.com")) return "今日头条";
    if (t.includes("kuaishou.com") || t.includes("gifshow.com")) return "快手";
    if (t.includes("bilibili.com") || t.includes("b23.tv")) return "哔哩哔哩";
    if (t.includes("tiktok.com")) return "TikTok";
    if (t.includes("twitter.com") || t.includes("x.com") || t.includes("t.co")) return "X";
    return "";
  }

  inputUrl.addEventListener("input", () => {
    const name = guessPlatform(inputUrl.value);
    platformTag.textContent = name || (inputUrl.value.trim() ? "未识别平台" : "等待输入");
    platformTag.classList.toggle("muted", !name);
    clearTimeout(parseTimer);
    if (!inputUrl.value.trim()) return;
    parseTimer = setTimeout(() => {
      api("/api/parse", { method: "POST", body: JSON.stringify({ text: inputUrl.value }) })
        .then((r) => {
          if (r.urls && r.urls.length) {
            platformTag.textContent = r.urls[0].label;
            platformTag.classList.remove("muted");
          }
        })
        .catch(() => {});
    }, 600);
  });

  $("#pasteBtn").addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (!text) return toast("剪贴板是空的");
      inputUrl.value = text;
      inputUrl.dispatchEvent(new Event("input"));
    } catch (e) {
      toast("浏览器不允许读取剪贴板，请长按输入框粘贴", true);
    }
  });

  /* ---------------- 选项联动 ---------------- */
  const optSubtitle = $("#optSubtitle");
  const optBurn = $("#optBurn");
  const optDubbing = $("#optDubbing");

  optSubtitle.addEventListener("change", () => {
    if (!optSubtitle.checked) optBurn.checked = false;
    else optBurn.checked = true;
  });
  optDubbing.addEventListener("change", () => {
    $("#dubbingBox").classList.toggle("hidden", !optDubbing.checked);
    if (optDubbing.checked && !state.voicesLoaded) loadVoices();
  });

  async function loadVoices() {
    try {
      const r = await api("/api/voices");
      const sel = $("#voiceSel");
      sel.innerHTML = r.voices
        .map((v) => `<option value="${esc(v.id)}"${v.id === r.default ? " selected" : ""}>${esc(v.name)}</option>`)
        .join("");
      state.voicesLoaded = true;
    } catch (e) {
      toast("音色列表加载失败", true);
    }
  }

  /* ---------------- 提交任务 ---------------- */
  $("#submitBtn").addEventListener("click", async () => {
    const text = inputUrl.value.trim();
    if (!text) return toast("请先粘贴视频链接");
    const btn = $("#submitBtn");
    btn.disabled = true;
    btn.textContent = "提交中…";
    try {
      const payload = {
        text: text,
        download: $("#optDownload").checked,
        subtitle: $("#optSubtitle").checked,
        burn: $("#optBurn").checked,
        dubbing: $("#optDubbing").checked,
        dubbing_text: $("#dubbingText").value.trim(),
        dubbing_voice: $("#voiceSel").value || "",
        dubbing_mode: $("#dubbingMode").value,
      };
      const task = await api("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
      inputUrl.value = "";
      platformTag.textContent = "等待输入";
      platformTag.classList.add("muted");
      toast("已加入队列：" + task.platform_label);
      switchView("tasks");
      loadTasks();
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "开始处理";
    }
  });

  /* ---------------- 文件上传 ---------------- */
  $("#fileInput").addEventListener("change", async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    fd.append("subtitle", $("#optSubtitle").checked ? "true" : "false");
    fd.append("burn", $("#optBurn").checked ? "true" : "false");
    fd.append("dubbing", $("#optDubbing").checked ? "true" : "false");
    fd.append("dubbing_text", $("#dubbingText").value.trim());
    fd.append("dubbing_voice", $("#voiceSel").value || "");
    fd.append("dubbing_mode", $("#dubbingMode").value);
    toast("上传中…");
    try {
      const res = await fetch("/api/tasks/upload", { method: "POST", body: fd });
      if (!res.ok) throw new Error("上传失败");
      toast("已加入队列");
      switchView("tasks");
      loadTasks();
    } catch (e) {
      toast(e.message, true);
    } finally {
      ev.target.value = "";
    }
  });

  /* ---------------- 任务列表 ---------------- */
  const STATUS_TEXT = {
    pending: "排队中",
    running: "处理中",
    done: "已完成",
    failed: "失败",
  };

  function signature(t) {
    return [t.status, Object.keys(t.files || {}).length, (t.notes || []).length, t.category].join("|");
  }

  function buildCard(t) {
    const el = document.createElement("div");
    el.className = "task";
    el.dataset.id = t.id;
    paintCard(el, t);
    return el;
  }

  function paintCard(el, t) {
    const files = t.files || {};
    const statusCls = t.status === "done" ? "ok" : t.status === "failed" ? "err" : "run";
    const pct = Math.round(t.progress || 0);
    const catHtml = t.category
      ? `<span class="cat">${esc(t.emoji || "")} ${esc(t.category)}${t.confidence ? ` · ${Math.round(t.confidence * 100)}%` : ""}</span>`
      : `<span class="cat plain">识别中</span>`;

    const meta = [];
    if (t.platform_label) meta.push(esc(t.platform_label));
    if (t.duration) meta.push(fmtDuration(t.duration));
    if (t.author) meta.push(esc(t.author));

    let actions = "";
    if (files.subtitled) actions += `<a class="accent" href="${esc(files.subtitled)}" download>带字幕视频</a>`;
    if (files.dubbed) actions += `<a class="accent" href="${esc(files.dubbed)}" download>带配音视频</a>`;
    if (files.video && !files.subtitled) actions += `<a href="${esc(files.video)}" download>原片</a>`;
    if (files.srt) actions += `<a href="/api/tasks/${t.id}/file/srt" download>字幕 SRT</a>`;
    if (files.dubbing) actions += `<a href="${esc(files.dubbing)}" download>配音音频</a>`;
    actions += `<button class="danger" data-del="${t.id}">删除</button>`;

    const notes = (t.notes || []).map((n) => `<div>${esc(n)}</div>`).join("");
    const preview = files.subtitled || files.dubbed || files.video;

    el.innerHTML = `
      <div class="task-head">
        ${catHtml}
        <span class="status ${statusCls}">${esc(STATUS_TEXT[t.status] || t.status)}</span>
      </div>
      <h3>${esc(t.title || "（未取到标题）")}</h3>
      <div class="meta">${meta.join("<span>·</span>")}</div>
      ${t.status === "done" ? "" : `<div class="progress"><i style="width:${pct}%"></i></div>`}
      <div class="stage"><span>${esc(t.stage || "")}${t.error ? "：" + esc(t.error) : ""}</span><span class="pct">${pct}%</span></div>
      ${notes ? `<div class="notes">${notes}</div>` : ""}
      <div class="actions">${actions}</div>
      ${preview && t.status === "done" ? `<div class="video-box"><video controls preload="none" ${t.thumbnail ? `poster="${esc(t.thumbnail)}"` : ""} src="${esc(preview)}"></video></div>` : ""}
    `;
  }

  function updateProgress(el, t) {
    const bar = el.querySelector(".progress i");
    if (bar) bar.style.width = Math.round(t.progress || 0) + "%";
    const pct = el.querySelector(".pct");
    if (pct) pct.textContent = Math.round(t.progress || 0) + "%";
    const stage = el.querySelector(".stage span");
    if (stage) stage.textContent = (t.stage || "") + (t.error ? "：" + t.error : "");
  }

  function renderTasks(tasks) {
    const list = $("#taskList");
    const seen = new Set();

    tasks.forEach((t) => {
      seen.add(t.id);
      let entry = state.cards.get(t.id);
      const sig = signature(t);
      if (!entry) {
        const el = buildCard(t);
        state.cards.set(t.id, { el, sig });
        list.appendChild(el);
      } else if (entry.sig !== sig) {
        paintCard(entry.el, t);
        entry.sig = sig;
      } else {
        updateProgress(entry.el, t);
      }
    });

    state.cards.forEach((entry, id) => {
      if (!seen.has(id)) {
        entry.el.remove();
        state.cards.delete(id);
      }
    });

    $("#emptyState").classList.toggle("hidden", tasks.length > 0);

    const running = tasks.filter((t) => t.status === "running" || t.status === "pending").length;
    const badge = $("#badge");
    badge.textContent = running;
    badge.classList.toggle("hidden", running === 0);
  }

  async function loadTasks() {
    try {
      const r = await api("/api/tasks?limit=60");
      state.tasks = r.tasks || [];
      renderTasks(state.tasks);
      schedulePoll();
    } catch (e) {
      /* 静默失败，避免打扰 */
    }
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    const hasActive = state.tasks.some((t) => t.status === "running" || t.status === "pending");
    state.pollTimer = setTimeout(loadTasks, hasActive ? 1500 : 6000);
  }

  document.addEventListener("click", async (ev) => {
    const delBtn = ev.target.closest("[data-del]");
    if (delBtn) {
      const id = delBtn.dataset.del;
      try {
        await api("/api/tasks/" + id, { method: "DELETE" });
        state.cards.get(id)?.el.remove();
        state.cards.delete(id);
        loadTasks();
        toast("已删除");
      } catch (e) {
        toast("删除失败", true);
      }
    }
  });

  /* ---------------- 设置页 ---------------- */
  async function loadSettings() {
    try {
      const [health, cfg, cats] = await Promise.all([
        api("/api/health"),
        api("/api/config"),
        api("/api/categories"),
      ]);

      $("#healthBox").innerHTML = [
        ["服务", health.ok ? "正常运行" : "异常"],
        ["Python", esc(health.python)],
        ["存储目录", esc(health.storage)],
        ["本地字幕", cfg.subtitle_local_ready ? "已安装" : "未安装（可配置云端端点）"],
      ]
        .map(([k, v]) => `<span>${k}</span><span>${v}</span>`)
        .join("");

      $("#platformBox").innerHTML = (cfg.platforms || [])
        .map((p) => `<span class="chip">${esc(p.label)}</span>`)
        .join("");

      $("#categoryBox").innerHTML = (cats.categories || [])
        .map((c) => `<span class="chip">${esc(c)}</span>`)
        .join("");
    } catch (e) {
      toast("设置加载失败", true);
    }
  }

  $("#refreshBtn").addEventListener("click", () => {
    loadTasks();
    toast("已刷新");
  });

  /* ---------------- 分享接入 & 启动 ---------------- */
  function handleShareParams() {
    const params = new URLSearchParams(location.search);
    const shared = params.get("text") || params.get("url") || "";
    if (shared) {
      inputUrl.value = shared;
      inputUrl.dispatchEvent(new Event("input"));
      switchView("new");
      history.replaceState({}, "", "/");
    }
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }

  loadTasks();
  handleShareParams();
})();
