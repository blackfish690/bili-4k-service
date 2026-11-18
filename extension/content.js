/* B 站视频页入口：右下角 4K 悬浮按钮 + 面板（扫码登录 / 清晰度 / 拉起 PotPlayer）。 */
(() => {
  "use strict";

  if (window.__bili4kLoaded) {
    return;
  }
  window.__bili4kLoaded = true;

  const BV_RE = /\/video\/(BV[0-9A-Za-z]+)/;
  const AV_RE = /\/video\/av(\d+)/i;

  const state = {
    target: null,
    page: 1,
    data: null,
    qn: null,
    account: null,
    busy: false,
    loading: false,
    loadSeq: 0,
    retried: false,
    retryTimer: null,
    qrSession: null,
    pollTimer: null,
    launchUrl: "",
    debugLog: false,
    lastApi: null
  };

  // 调试开关默认关闭；设置页切换后即时生效，无需刷新视频页
  try {
    chrome.storage.sync.get("debugLog", (value) => {
      state.debugLog = !!(value && value.debugLog);
    });
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === "sync" && changes.debugLog) {
        state.debugLog = !!changes.debugLog.newValue;
      }
    });
  } catch (err) {
    state.debugLog = false;
  }

  // ------------------------------------------------------------------ utils

  function parseTarget() {
    const match = location.pathname.match(BV_RE);
    if (match) {
      return { bvid: match[1] };
    }
    const av = location.pathname.match(AV_RE);
    if (av) {
      return { aid: Number(av[1]) };
    }
    return null;
  }

  function currentPage() {
    const value = Number(new URLSearchParams(location.search).get("p") || 1);
    return Number.isFinite(value) && value > 0 ? Math.floor(value) : 1;
  }

  function send(message) {
    return new Promise((resolve, reject) => {
      try {
        chrome.runtime.sendMessage(message, (resp) => {
          const err = chrome.runtime.lastError;
          if (err) {
            reject(new Error(err.message));
            return;
          }
          resolve(resp || { ok: false, error: "扩展后台无响应" });
        });
      } catch (err) {
        reject(err);
      }
    });
  }

  async function api(path, options) {
    const started = performance.now();
    const resp = await send({ type: "api", path, options });
    const ms = Math.round(performance.now() - started);
    const timings = (resp && resp.data && resp.data.timings) || null;
    state.lastApi = { path, ms, timings };
    if (state.debugLog) {
      console.log(`[bili4k] API ${path} ${ms}ms`, timings || "");
    }
    if (!resp.ok) {
      throw new Error(resp.error || "请求失败");
    }
    return resp.data;
  }

  // 调试日志默认关闭；开启后记录接口往返时间与后端 gRPC/探测耗时
  function logLaunch(entry) {
    if (!state.debugLog) {
      return;
    }
    const payload = { kind: "launch", ...entry };
    console.log("[bili4k]", payload);
    send({ type: "log", entry: payload }).catch(() => {});
  }

  function timingSuffix(timings) {
    if (!state.debugLog || !timings) {
      return "";
    }
    const parts = [];
    if (timings.grpc_ms != null) {
      parts.push(`gRPC ${timings.grpc_ms}ms`);
    }
    if (timings.cache_hit) {
      parts.push("取流缓存");
    }
    if (timings.probe_ms != null) {
      parts.push(`探测 ${timings.probe_ms}ms`);
    }
    if (timings.total_ms != null) {
      parts.push(`后端 ${timings.total_ms}ms`);
    }
    return parts.length ? `（${parts.join(" · ")}）` : "";
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (e) {
        ok = false;
      }
      area.remove();
      return ok;
    }
  }

  // --------------------------------------------------------------------- UI

  const host = document.createElement("div");
  host.id = "bili4k-host";
  const shadow = host.attachShadow({ mode: "open" });

  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      * { box-sizing: border-box; }
      .hidden { display: none !important; }

      /* 收起态：常驻胶囊，「打开 mpv 播放」一步直达 */
      .dock {
        position: fixed; right: 24px; bottom: 24px; z-index: 2147483000;
        display: flex; align-items: stretch; overflow: hidden; border-radius: 999px;
        background: rgba(24, 26, 31, .93); border: 1px solid rgba(251, 114, 153, .5);
        box-shadow: 0 10px 28px rgba(0, 0, 0, .45);
        backdrop-filter: blur(14px) saturate(130%);
        font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
      }
      .dock-main {
        display: flex; align-items: center; gap: 6px; height: 40px; padding: 0 14px;
        border: 0; background: transparent; color: #f7f8fa; cursor: pointer;
        font-size: 12.5px; font-weight: 600; line-height: 1; white-space: nowrap;
      }
      .dock-main:hover { background: rgba(251, 114, 153, .2); }
      .dock-main svg { width: 13px; height: 13px; flex: none; }
      /* 请求进行中：禁用两个入口，给出「请求中…」反馈 */
      .dock-main:disabled { opacity: .65; cursor: default; }
      .dock-main:disabled:hover { background: transparent; }
      .dock-main.busy svg { animation: bili4k-pulse .9s ease-in-out infinite; }
      @keyframes bili4k-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
      .primary:disabled { opacity: .7; cursor: default; }
      .dock-more {
        height: 40px; padding: 0 13px; border: 0;
        border-left: 1px solid rgba(255, 255, 255, .14);
        background: transparent; color: #ff9db5; cursor: pointer;
        font-size: 12px; font-weight: 700; letter-spacing: .5px;
        /* 画质文案动态变化（4K / 1080P60 / 杜比视界），限制宽度避免撑坏胶囊 */
        min-width: 46px; max-width: 88px; text-align: center;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .dock-more:hover { background: rgba(251, 114, 153, .2); color: #fff; }

      /* 收起状态下的即时反馈 */
      .toast {
        position: fixed; right: 24px; bottom: 80px; z-index: 2147483002;
        max-width: 300px; max-height: 180px; overflow: auto;
        padding: 9px 12px; border-radius: 10px;
        background: rgba(25, 27, 32, .96); border: 1px solid rgba(255, 255, 255, .1);
        box-shadow: 0 12px 30px rgba(0, 0, 0, .5); color: #e9ebee;
        font: 12px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
        opacity: 0; transform: translateY(6px); pointer-events: none;
        transition: opacity .18s ease, transform .18s ease;
      }
      .toast.show { opacity: 1; transform: none; }
      .toast.error { border-color: rgba(255, 125, 132, .5); color: #ffb3b7; }

      .panel {
        --fg: #f2f3f5; --muted: #a6abb5; --line: rgba(255, 255, 255, .1);
        --chip: rgba(255, 255, 255, .06); --accent: #fb7299;
        --accent-soft: rgba(251, 114, 153, .14); --amber: #f0b53a;
        position: fixed; right: 24px; bottom: 92px; z-index: 2147483001;
        width: 360px; max-height: 74vh; overflow: auto;
        color: var(--fg); border: 1px solid rgba(255, 255, 255, .09);
        border-radius: 16px;
        background:
          radial-gradient(130% 70% at 50% -10%, rgba(251, 114, 153, .17), transparent 62%),
          rgba(25, 27, 32, .95);
        backdrop-filter: blur(18px) saturate(140%);
        box-shadow: 0 18px 48px rgba(0, 0, 0, .55);
        padding: 14px;
        font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
      }
      .panel::-webkit-scrollbar { width: 6px; }
      .panel::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, .14); border-radius: 3px; }

      header { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; }
      .badge {
        background: var(--accent); color: #fff; font-weight: 700; font-size: 11px;
        padding: 2px 7px; border-radius: 5px; letter-spacing: .5px;
      }
      .head-title { font-weight: 600; flex: 1; }
      .icon {
        border: 0; background: transparent; color: var(--muted); font-size: 15px;
        cursor: pointer; line-height: 1; padding: 3px 5px; border-radius: 6px;
      }
      .icon:hover { color: var(--fg); background: var(--chip); }

      .account {
        display: flex; align-items: center; gap: 8px; background: var(--chip);
        border: 1px solid var(--line); border-radius: 10px;
        padding: 8px 10px; margin-bottom: 10px;
      }
      .avatar {
        width: 28px; height: 28px; border-radius: 50%; flex: none;
        display: flex; align-items: center; justify-content: center;
        background: linear-gradient(135deg, #fb7299, #7b61ff);
        background-size: cover; background-position: center;
        color: #fff; font-weight: 700; font-size: 13px;
      }
      .account-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
      .account-name { display: flex; align-items: center; gap: 5px; min-width: 0; }
      .account-text {
        font-size: 12px; font-weight: 600; color: var(--fg);
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      }
      .uid {
        font: 10px/1.4 ui-monospace, SFMono-Regular, Consolas, "Courier New", monospace;
        color: var(--muted); letter-spacing: .2px;
      }
      .vip {
        flex: none; font-size: 9px; font-weight: 700; line-height: 1; padding: 2px 4px;
        border-radius: 4px; color: #402b00;
        background: linear-gradient(135deg, #ffe08a, #f0b53a);
      }
      .dot { width: 6px; height: 6px; border-radius: 50%; background: #6b7280; flex: none; }
      .dot.on { background: #34d399; box-shadow: 0 0 0 3px rgba(52, 211, 153, .16); }
      .mini {
        border: 1px solid var(--line); background: transparent; color: var(--fg);
        border-radius: 6px; font-size: 11px; padding: 4px 8px; cursor: pointer;
        white-space: nowrap;
      }
      .mini:hover { border-color: var(--accent); color: #ffc0d2; }

      .help {
        background: var(--chip); border: 1px solid var(--line); border-radius: 9px;
        padding: 8px 10px; margin-bottom: 10px; font-size: 11px; line-height: 1.55;
        color: var(--muted);
      }
      .help p { margin: 0 0 4px; }
      .help p:last-child { margin-bottom: 0; }
      .help b { color: var(--fg); font-weight: 600; }

      .video-title {
        font-size: 12px; color: var(--muted); margin-bottom: 8px;
        display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
        overflow: hidden;
      }
      .section-label { font-size: 11px; color: var(--muted); margin-bottom: 6px; }
      .login-hint { font-size: 11px; color: #ffb27a; margin-bottom: 6px; }

      /* 3 列 × 2 行，减少面板高度 */
      .qualities {
        display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 7px; margin-bottom: 10px;
      }
      .q {
        position: relative; display: flex; flex-direction: column; gap: 2px;
        min-width: 0; padding: 8px 8px 7px; border: 1px solid var(--line);
        border-radius: 9px; background: rgba(255, 255, 255, .03); cursor: pointer;
        user-select: none;
        transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
      }
      .q:hover { border-color: rgba(251, 114, 153, .5); }
      .q input { display: none; }
      .q.on {
        border-color: var(--accent); background: var(--accent-soft);
        box-shadow: 0 0 0 1px rgba(251, 114, 153, .5), 0 0 16px rgba(251, 114, 153, .3);
      }
      .q.on::after {
        content: "✓"; position: absolute; top: 3px; right: 5px;
        font-size: 10px; font-weight: 700; line-height: 1; color: var(--accent);
      }
      .q.off { opacity: .4; cursor: not-allowed; }
      .qmain {
        font-size: 12px; font-weight: 600; padding-right: 10px;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .qmeta {
        font-size: 9.5px; line-height: 1.35; color: var(--muted);
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }

      /* 试看提示：细边框 + 浅琥珀文字，不再用大色块抢主按钮 */
      .notice {
        display: none; align-items: flex-start; gap: 6px; margin-bottom: 10px;
        padding: 6px 8px; border: 1px solid rgba(240, 181, 58, .34);
        border-radius: 8px; background: rgba(240, 181, 58, .07);
        color: #f2c078; font-size: 11px; line-height: 1.45;
      }
      .notice.show { display: flex; }
      .notice::before {
        content: "!"; flex: none; width: 13px; height: 13px; margin-top: 1px;
        border: 1px solid rgba(240, 181, 58, .65); border-radius: 50%;
        color: var(--amber); font: 700 9px/13px ui-monospace, monospace;
        text-align: center;
      }

      .actions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }
      .actions button {
        display: inline-flex; align-items: center; justify-content: center; gap: 4px;
        height: 32px; padding: 0 6px; border: 1px solid var(--line); border-radius: 8px;
        background: rgba(255, 255, 255, .04); color: var(--fg);
        font-size: 11.5px; cursor: pointer; white-space: nowrap; overflow: hidden;
      }
      .actions button:hover { border-color: rgba(251, 114, 153, .6); color: #ffc0d2; }
      .actions button.primary {
        grid-column: 1 / -1; height: 38px; border: 0;
        background: linear-gradient(135deg, #e2507c, #cf3f6e); color: #fff;
        font-size: 13px; font-weight: 700;
        box-shadow: 0 8px 20px rgba(207, 63, 110, .38);
      }
      .actions button.primary:hover {
        background: linear-gradient(135deg, #ea5f89, #d94a78); color: #fff;
      }
      .actions button:disabled { opacity: .45; cursor: not-allowed; }
      .actions svg {
        width: 12px; height: 12px; flex: none; fill: none; stroke: currentColor;
        stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round;
      }

      .status {
        margin-top: 9px; min-height: 14px; font-size: 11px; color: #9aa1ad;
        word-break: break-all;
      }
      .status.error { color: #ff8a90; }
      .fallback { display: none; align-items: center; gap: 8px; flex-wrap: wrap;
                  margin-top: 8px; }
      .fallback.show { display: flex; }
      .fallback a { color: #ff9db5; font-size: 11.5px; text-decoration: underline;
                    cursor: pointer; }

      .qr {
        position: absolute; inset: 0; z-index: 3; border-radius: 16px;
        display: flex; flex-direction: column; align-items: center; justify-content: center;
        gap: 8px; padding: 16px; background: rgba(23, 25, 29, .98);
      }
      .qr-title { font-weight: 600; }
      .qr-img { width: 190px; height: 190px; image-rendering: pixelated;
                border: 1px solid var(--line); border-radius: 8px; background: #fff; }
      .qr-status { font-size: 12px; color: var(--muted); text-align: center; }
      .qr-actions { display: flex; gap: 8px; }
    </style>

    <div class="dock">
      <button class="dock-main" data-act="quick-direct" title="用当前页面视频直连播放（mpv）">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l10-6.5z" fill="currentColor"/></svg>
        <span class="dock-label">打开 mpv 播放</span>
      </button>
      <button class="dock-more" data-act="toggle" title="清晰度 / 登录 / 设置">画质</button>
    </div>

    <div class="toast"></div>

    <section class="panel hidden">
      <header>
        <span class="badge">4K</span>
        <span class="head-title">Bili 4K 拉流</span>
        <button class="icon" data-act="help" title="直连与代理的区别">?</button>
        <button class="icon" data-act="settings" title="扩展设置">⚙</button>
        <button class="icon" data-act="close" title="收起">×</button>
      </header>

      <div class="account">
        <span class="avatar"><span class="avatar-text">?</span></span>
        <span class="account-main">
          <span class="account-name">
            <span class="dot"></span>
            <span class="account-text">检查登录状态…</span>
            <span class="vip hidden">大会员</span>
          </span>
          <span class="uid hidden"></span>
        </span>
        <button class="mini" data-act="login">扫码登录</button>
      </div>

      <div class="help hidden">
        <p><b>直连</b>：mpv 直接播 B 站 CDN 直链（视频 + 音频），不经过 NAS，带宽最省。</p>
        <p><b>代理</b>：<code>pili://</code> 拉起 PotPlayer，需先注册协议；未注册时用「复制链接」。</p>
        <p>灰色档位表示服务端未下发，换相邻档位或稍后重试即可。</p>
      </div>

      <div class="video-title"></div>
      <div class="section-label">清晰度</div>
      <div class="login-hint hidden">未登录：4K / 1080P60 需要大会员账号</div>
      <div class="qualities"></div>
      <div class="notice"></div>

      <div class="actions">
        <button class="primary" data-act="direct">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l10-6.5z" fill="currentColor" stroke="none"/></svg>
          直连播放（mpv）
        </button>
        <button data-act="play" title="通过 pili:// 拉起 PotPlayer">
          <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="13" rx="2"/><path d="M10 10v4l3.5-2z" fill="currentColor" stroke="none"/></svg>
          PotPlayer
        </button>
        <button data-act="copy" title="复制代理播放地址">
          <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M6 15H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v1"/></svg>
          复制链接
        </button>
        <button data-act="download" title="下载分片 MPD">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10"/><path d="M8 10.5l4 4 4-4"/><path d="M5 19h14"/></svg>
          下载 MPD
        </button>
      </div>

      <div class="status"></div>
      <div class="fallback">
        <a data-role="launch-link" href="#">播放器没启动？点这里手动打开</a>
        <button class="mini" data-act="copy-launch">复制 pili:// 链接</button>
      </div>

      <div class="qr hidden">
        <div class="qr-title">B 站 App 扫码登录</div>
        <img class="qr-img" alt="登录二维码">
        <div class="qr-status">等待扫码…</div>
        <div class="qr-actions">
          <button class="mini" data-act="qr-refresh">刷新二维码</button>
          <button class="mini" data-act="qr-cancel">取消</button>
        </div>
      </div>
    </section>
  `;

  const ui = {
    panel: shadow.querySelector(".panel"),
    toast: shadow.querySelector(".toast"),
    dockMain: shadow.querySelector(".dock-main"),
    dockLabel: shadow.querySelector(".dock-label"),
    dockMore: shadow.querySelector(".dock-more"),
    dot: shadow.querySelector(".dot"),
    avatar: shadow.querySelector(".avatar"),
    avatarText: shadow.querySelector(".avatar-text"),
    accountText: shadow.querySelector(".account-text"),
    uid: shadow.querySelector(".uid"),
    vip: shadow.querySelector(".vip"),
    help: shadow.querySelector(".help"),
    loginHint: shadow.querySelector(".login-hint"),
    loginBtn: shadow.querySelector('[data-act="login"]'),
    videoTitle: shadow.querySelector(".video-title"),
    qualities: shadow.querySelector(".qualities"),
    notice: shadow.querySelector(".notice"),
    playBtn: shadow.querySelector('[data-act="play"]'),
    directBtn: shadow.querySelector('[data-act="direct"]'),
    status: shadow.querySelector(".status"),
    fallback: shadow.querySelector(".fallback"),
    launchLink: shadow.querySelector('[data-role="launch-link"]'),
    qr: shadow.querySelector(".qr"),
    qrImg: shadow.querySelector(".qr-img"),
    qrStatus: shadow.querySelector(".qr-status")
  };

  (document.documentElement || document.body).appendChild(host);

  // ------------------------------------------------------------------ render

  let toastTimer = null;

  function flash(text, isError) {
    if (!text) {
      return;
    }
    ui.toast.textContent = text;
    ui.toast.classList.toggle("error", Boolean(isError));
    ui.toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(
      () => ui.toast.classList.remove("show"),
      isError ? 9000 : 5000
    );
  }

  function setStatus(text, isError) {
    ui.status.textContent = text || "";
    ui.status.classList.toggle("error", Boolean(isError));
    // 面板收起时用浮层反馈，保证「打开 mpv 播放」这一步也有结果可看
    if (text && ui.panel.classList.contains("hidden")) {
      flash(text, isError);
    }
  }

  function loggedIn() {
    return Boolean(state.account && state.account.logged_in);
  }

  function qualityText(item) {
    const parts = [];
    if (item.width && item.height) {
      parts.push(`${item.width}x${item.height}`);
    }
    if (item.need_vip) {
      parts.push("大会员");
    }
    // 已登录时不再逐卡重复「需登录」，改由顶部账号栏统一表达
    if (item.need_login && !loggedIn()) {
      parts.push("需登录");
    }
    if (!item.available) {
      parts.push("未下发");
    }
    return parts.join(" · ") || item.format || "";
  }

  function renderQualities() {
    ui.qualities.textContent = "";
    const items = (state.data && state.data.qualities) || [];
    const selected = state.qn;

    items.forEach((item) => {
      const label = document.createElement("label");
      label.className = "q" + (item.available ? "" : " off") + (item.qn === selected ? " on" : "");

      const input = document.createElement("input");
      input.type = "radio";
      input.name = "b4k-qn";
      input.value = String(item.qn);
      input.checked = item.qn === selected;
      input.disabled = !item.available;

      const main = document.createElement("span");
      main.className = "qmain";
      main.textContent = item.label || `qn${item.qn}`;

      const metaText = qualityText(item);
      const meta = document.createElement("span");
      meta.className = "qmeta";
      meta.textContent = metaText;

      // 3 列布局会截断文字，悬停时给出完整信息
      label.title = [item.label || `qn${item.qn}`, metaText].filter(Boolean).join(" · ");

      label.append(input, main, meta);
      ui.qualities.appendChild(label);
    });
  }

  function anyAvailable() {
    return Boolean(state.data && (state.data.qualities || []).some((item) => item.available));
  }

  // 当前视频下发了视频流的最高档；不依赖后端排序，按 qn 取最大值
  function bestQuality() {
    let best = null;
    ((state.data && state.data.qualities) || []).forEach((item) => {
      if (!item.available) {
        return;
      }
      if (!best || Number(item.qn) > Number(best.qn)) {
        best = item;
      }
    });
    return best;
  }

  function dockQualityLabel(item) {
    if (!item) {
      return "";
    }
    return String(item.label || item.display_desc || `qn${item.qn}`);
  }

  // 收起态胶囊上的画质与 quick-direct 实际请求的 qn 同源，避免「显示 1080P60 却请求 4K」
  function renderDockQuality() {
    const best = bestQuality();
    ui.dockMore.textContent = best ? dockQualityLabel(best) : "画质";
    ui.dockMore.title = best
      ? `当前最高可用：${dockQualityLabel(best)}；点击展开清晰度 / 登录 / 设置`
      : "清晰度 / 登录 / 设置";
  }

  // 收起态胶囊与展开态主按钮共用 state.busy 这一把锁：
  // 请求期间两者都禁用并显示「请求中…」，避免用户以为没生效又去点
  function renderBusyChrome() {
    const busy = state.busy;
    ui.dockMain.disabled = busy;
    ui.dockMain.classList.toggle("busy", busy);
    ui.dockLabel.textContent = busy ? "请求中…" : "打开 mpv 播放";
    ui.directBtn.disabled = busy || !anyAvailable();
    ui.directBtn.textContent = busy ? "正在获取播放地址…" : "▶ 直连播放（mpv）";
  }

  function setBusy(value) {
    state.busy = value;
    renderBusyChrome();
    if (state.data) {
      renderAll();
    }
  }

  function renderAll() {
    if (!state.data) {
      return;
    }
    const video = state.data.video || {};
    const part = state.page > 1 && video.page_title ? ` · P${state.page} ${video.page_title}` : "";
    ui.videoTitle.textContent = (video.title || "未知视频") + part;

    renderQualities();
    renderDockQuality();
    // 未登录时统一提示一次，登录后各卡片不再重复「需登录」
    ui.loginHint.classList.toggle("hidden", loggedIn());
    const notice = state.data.notice || "";
    ui.notice.textContent = notice;
    ui.notice.classList.toggle("show", Boolean(notice));

    ui.playBtn.disabled = !anyAvailable() || state.busy;
    renderBusyChrome();
  }

  // ----------------------------------------------------------------- actions

  async function refreshAccount() {
    try {
      const account = await api("/api/auth/status");
      state.account = account;
      const logged = Boolean(account.logged_in);
      ui.dot.classList.toggle("on", logged);
      ui.accountText.textContent = logged
        ? account.uname || "已登录"
        : "未登录（4K 需要大会员账号）";
      ui.uid.textContent = logged && account.mid ? `UID ${account.mid}` : "";
      ui.uid.classList.toggle("hidden", !(logged && account.mid));
      const vip = Boolean(account.vip || account.vip_status || account.is_vip);
      ui.vip.classList.toggle("hidden", !(logged && vip));

      // /api/auth/status 目前不返回头像地址：有 face 用图，否则用首字母占位
      if (logged && account.face) {
        ui.avatar.style.backgroundImage = `url("${account.face}")`;
        ui.avatarText.style.display = "none";
      } else {
        ui.avatar.style.backgroundImage = "";
        ui.avatarText.style.display = "";
        ui.avatarText.textContent = logged
          ? (String(account.uname || "B").trim().charAt(0) || "B").toUpperCase()
          : "?";
      }

      ui.loginBtn.textContent = logged ? "退出" : "扫码登录";
      ui.loginBtn.dataset.act = logged ? "logout" : "login";
      ui.loginHint.classList.toggle("hidden", logged);
      // 登录态影响「需登录」标注，刷新一次卡片
      renderQualities();
    } catch (err) {
      state.account = null;
      ui.accountText.textContent = "后端未连接，请打开扩展设置";
      ui.dot.classList.remove("on");
      ui.uid.classList.add("hidden");
      ui.vip.classList.add("hidden");
      ui.avatar.style.backgroundImage = "";
      ui.avatarText.style.display = "";
      ui.avatarText.textContent = "?";
      ui.loginBtn.textContent = "扫码登录";
      ui.loginBtn.dataset.act = "login";
      ui.loginHint.classList.add("hidden");
    }
  }

  // 进入视频页时静默预取一次档位：不弹面板、不拉播放器、失败不刷红色提示。
  // 面板展开时直接复用这次结果；silent 失败只补一次，之后由展开或点按重试。
  async function loadVideo(options) {
    const opts = options || {};
    const force = Boolean(opts.force);
    const silent = Boolean(opts.silent) && !force;
    if (!state.target) {
      return null;
    }
    if (!force && state.data) {
      // 已有档位数据，面板展开/重复调用都不再打接口
      renderAll();
      return state.data;
    }
    if (!force && state.loading) {
      // 预取或展开触发的请求还在路上，复用同一个结果
      return null;
    }
    const token = ++state.loadSeq;
    state.loading = true;
    if (!silent) {
      setStatus("正在向服务端取流…");
      ui.playBtn.disabled = true;
    }
    try {
      const data = await api("/api/play/resolve", {
        method: "POST",
        body: { ...state.target, page: state.page, qn: 120 }
      });
      if (token !== state.loadSeq) {
        // 期间切换了视频 / 分 P，丢弃上一个视频的过期响应
        return null;
      }
      state.data = data;
      state.qn = data.selected ? data.selected.qn : null;
      state.retried = false;
      renderAll();
      if (!silent) {
        setStatus("");
      }
      return data;
    } catch (err) {
      if (token !== state.loadSeq) {
        return null;
      }
      state.data = null;
      renderQualities();
      renderDockQuality();
      ui.playBtn.disabled = true;
      if (silent) {
        scheduleSilentRetry();
      } else {
        setStatus(err.message, true);
      }
      return null;
    } finally {
      if (token === state.loadSeq) {
        state.loading = false;
      }
    }
  }

  // 静默预取失败（含 15 秒超时）后只补一次，避免后端持续不可用时反复请求
  function scheduleSilentRetry() {
    if (state.retried) {
      return;
    }
    state.retried = true;
    clearTimeout(state.retryTimer);
    state.retryTimer = setTimeout(() => {
      state.retryTimer = null;
      if (!state.data && !state.loading && state.target) {
        loadVideo({ silent: true });
      }
    }, 8000);
  }

  async function resolveSelected() {
    const qn = state.qn || (state.data && state.data.selected && state.data.selected.qn);
    const data = await api("/api/play/resolve", {
      method: "POST",
      body: { ...state.target, page: state.page, qn }
    });
    state.data = data;
    if (data.selected) {
      state.qn = data.selected.qn;
    }
    renderAll();
    return data;
  }

  // 拉起外部播放器前先暂停页面里的视频：只暂停、不关闭，用户回来自己点继续。
  // B 站部分播放器把 <video> 包在 bwp-video 等自定义元素的 shadow root 里，
  // 页面也可能同时存在多个 video，因此统一收集去重后再暂停。
  function collectVideos(root, videos, seenRoots) {
    if (!root || seenRoots.has(root) || !root.querySelectorAll) {
      return;
    }
    seenRoots.add(root);
    root.querySelectorAll("video").forEach((video) => videos.add(video));
    root.querySelectorAll("bwp-video").forEach((player) => {
      if (player.shadowRoot) {
        collectVideos(player.shadowRoot, videos, seenRoots);
      }
    });
    root.querySelectorAll("iframe").forEach((frame) => {
      try {
        const doc = frame.contentDocument;
        if (doc) {
          collectVideos(doc, videos, seenRoots);
        }
      } catch (err) {
        // 跨域 iframe 无法访问，忽略
      }
    });
  }

  function pausePageVideos() {
    const videos = new Set();
    collectVideos(document, videos, new Set());
    let paused = 0;
    videos.forEach((video) => {
      if (!video.paused) {
        try {
          video.pause();
          paused += 1;
        } catch (err) {
          // 个别播放器覆盖了 pause，失败不影响拉起外部播放器
        }
      }
    });
    return { total: videos.size, paused };
  }

  function pauseHint(paused) {
    return paused ? `（已暂停页面视频 ${paused} 个）` : "";
  }

  function launchProtocol(url) {
    const frame = document.createElement("iframe");
    frame.style.cssText = "display:none;width:0;height:0;border:0";
    frame.src = url;
    (document.documentElement || document.body).appendChild(frame);
    setTimeout(() => frame.remove(), 5000);
  }

  // 浏览器可能静默拦截 iframe 里的 pili:// 跳转（无弹窗、无报错），
  // 因此每次都给出可点击的真实链接 + 可复制的 URI，让失败可见、可自查。
  function armFallback(url, hint) {
    state.launchUrl = url;
    ui.launchLink.href = url;
    ui.fallback.classList.add("show");
    setStatus(hint);
  }

  function clearFallback() {
    state.launchUrl = "";
    ui.fallback.classList.remove("show");
    ui.launchLink.removeAttribute("href");
  }

  // Windows ShellExecute 传递 pili:// 的整串上限约 2048 字符，留出余量
  const MAX_PROTOCOL_URL = 1900;

  // 标题取自后端 resolve/direct 的 video.title，分 P 追加 page_title
  function mediaTitle(video, page) {
    if (!video) {
      return "";
    }
    const base = String(video.title || "").trim();
    if (!base) {
      return "";
    }
    if (page > 1) {
      const part = String(video.page_title || "").trim();
      if (part) {
        return `${base} - P${page} ${part}`;
      }
    }
    return base;
  }

  // 把标题插到 api 参数之前（启动脚本按「api 取到末尾」解析），超长时按字符截断
  function withMediaTitle(launchUrl, title) {
    const text = String(title || "").trim();
    const marker = "?mode=mpv&api=";
    const idx = launchUrl ? launchUrl.indexOf(marker) : -1;
    if (!text || idx < 0) {
      return launchUrl;
    }
    const prefix = launchUrl.slice(0, idx) + "?mode=mpv&title=";
    const suffix = "&api=" + launchUrl.slice(idx + marker.length);
    const budget = MAX_PROTOCOL_URL - prefix.length - suffix.length;
    if (budget <= 0) {
      // 连参数名都放不下：保持原链接，mpv 回退显示 URL
      return launchUrl;
    }
    let encoded = encodeURIComponent(text);
    if (encoded.length > budget) {
      // 先按字符截断再编码，避免把 %XX 切断；仍放不下就放弃标题
      const chars = Array.from(text);
      while (chars.length && encodeURIComponent(chars.join("") + "…").length > budget) {
        chars.pop();
      }
      encoded = chars.length ? encodeURIComponent(chars.join("") + "…") : "";
    }
    return encoded ? prefix + encoded + suffix : launchUrl;
  }

  async function doDirect(preferBest) {
    if (state.busy) {
      return;
    }
    const clickStarted = performance.now();
    setBusy(true);
    clearFallback();
    setStatus("正在获取 CDN 直链…");
    try {
      // 胶囊入口用「最高可用档」，面板入口沿用用户选中档，二者都与各自显示一致
      const best = preferBest ? bestQuality() : null;
      const qn =
        (best && best.qn) ||
        state.qn ||
        (state.data && state.data.selected && state.data.selected.qn) ||
        120;
      const params = new URLSearchParams({
        page: String(state.page),
        qn: String(qn)
      });
      if (state.target.bvid) {
        params.set("bvid", state.target.bvid);
      }
      if (state.target.aid) {
        params.set("aid", String(state.target.aid));
      }
      const data = await api("/api/play/direct?" + params.toString());
      if (!data.selected || !data.selected.available) {
        setStatus(data.notice || "该清晰度未下发视频流", true);
        return;
      }
      if (!data.direct || data.direct.ok === false || !data.launch_url) {
        setStatus(data.notice || "该档位的 CDN 节点不可用，请换档位或改用代理模式", true);
        return;
      }
      // 标题由后端提供，带进 pili:// 让 mpv 的窗口标题/OSC 顶部显示视频名
      const launchUrl = withMediaTitle(
        data.launch_url,
        mediaTitle(data.video || (state.data && state.data.video), state.page)
      );
      // 先暂停页面里正在播放的视频，再拉起外部播放器；只暂停，不关闭
      const paused = pausePageVideos().paused;
      logLaunch({
        player: "mpv",
        qn,
        api_ms: state.lastApi ? state.lastApi.ms : null,
        launch_ms: Math.round(performance.now() - clickStarted),
        timings: data.timings || (state.lastApi && state.lastApi.timings) || null
      });
      launchProtocol(launchUrl);
      armFallback(
        launchUrl,
        "✓ 已请求 mpv 播放；没反应就展开面板手动打开" +
          pauseHint(paused) +
          timingSuffix(data.timings)
      );
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function doPlay() {
    if (state.busy) {
      return;
    }
    const clickStarted = performance.now();
    setBusy(true);
    clearFallback();
    setStatus("正在获取播放地址…");
    try {
      const data = await resolveSelected();
      if (!data.selected || !data.selected.available) {
        setStatus(data.notice || "该清晰度未下发视频流", true);
        return;
      }
      const url = "pili://play?url=" + encodeURIComponent(data.potplayer_url || data.mpd_url);
      // 先暂停页面里正在播放的视频，再拉起 PotPlayer；只暂停，不关闭
      const paused = pausePageVideos().paused;
      logLaunch({
        player: "PotPlayer",
        qn: state.qn,
        api_ms: state.lastApi ? state.lastApi.ms : null,
        launch_ms: Math.round(performance.now() - clickStarted),
        timings: data.timings || (state.lastApi && state.lastApi.timings) || null
      });
      launchProtocol(url);
      armFallback(
        url,
        "✓ 已请求 PotPlayer；没启动就展开面板手动打开" +
          pauseHint(paused) +
          timingSuffix(data.timings)
      );
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function doCopy() {
    setStatus("正在获取播放地址…");
    try {
      const data = await resolveSelected();
      if (!data.selected || !data.selected.available) {
        setStatus(data.notice || "该清晰度未下发视频流", true);
        return;
      }
      const ok = await copyText(data.potplayer_url || data.mpd_url);
      setStatus(ok ? "✓ 播放地址已复制" : data.mpd_url, !ok);
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  async function doDownload() {
    setStatus("正在获取播放清单…");
    try {
      const data = await resolveSelected();
      if (!data.selected || !data.selected.available) {
        setStatus(data.notice || "该清晰度未下发视频流", true);
        return;
      }
      const name = `bili-${data.video.bvid || data.video.aid}-qn${data.selected.qn}.mpd`;
      const resp = await send({ type: "download", url: data.potplayer_url || data.mpd_url, filename: name });
      if (!resp.ok) {
        throw new Error(resp.error || "下载失败");
      }
      setStatus("✓ 已开始下载 .mpd");
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  // ---------------------------------------------------------------- QR login

  function stopPolling() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  function hideQr() {
    state.qrSession = null;
    stopPolling();
    ui.qr.classList.add("hidden");
  }

  async function startQr() {
    stopPolling();
    ui.qr.classList.remove("hidden");
    ui.qrImg.removeAttribute("src");
    ui.qrStatus.textContent = "正在申请二维码…";
    try {
      const data = await api("/api/auth/qr", { method: "POST" });
      state.qrSession = data.session_id;
      ui.qrImg.src = data.qr_image;
      ui.qrStatus.textContent = "请用 B 站 App 扫码并在手机上确认";
      pollQr();
    } catch (err) {
      ui.qrStatus.textContent = err.message;
    }
  }

  function pollQr() {
    stopPolling();
    if (!state.qrSession) {
      return;
    }
    state.pollTimer = setTimeout(async () => {
      if (!state.qrSession) {
        return;
      }
      try {
        const result = await api("/api/auth/qr/" + state.qrSession);
        ui.qrStatus.textContent = result.message || result.status;
        if (result.status === "confirmed") {
          hideQr();
          await refreshAccount();
          await loadVideo({ force: true });
          return;
        }
        if (result.status === "expired" || result.status === "error") {
          state.qrSession = null;
          stopPolling();
          ui.qrStatus.textContent = (result.message || "二维码已失效") + "，请刷新";
          return;
        }
      } catch (err) {
        ui.qrStatus.textContent = err.message;
      }
      pollQr();
    }, 2000);
  }

  async function doLogout() {
    try {
      await api("/api/auth/logout", { method: "POST" });
      await refreshAccount();
      await loadVideo({ force: true });
      setStatus("已清除服务端凭证。");
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  // --------------------------------------------------------------- lifecycle

  function togglePanel(show) {
    const visible = typeof show === "boolean" ? show : ui.panel.classList.contains("hidden");
    ui.panel.classList.toggle("hidden", !visible);
    if (visible) {
      // 面板里能看到完整状态，撤掉收起态的浮层反馈
      ui.toast.classList.remove("show");
      refreshAccount();
      loadVideo();
    } else {
      hideQr();
    }
  }

  shadow.addEventListener("click", (event) => {
    const el = event.target.closest("[data-act]");
    if (!el) {
      return;
    }
    const act = el.dataset.act;
    if (act === "toggle") {
      togglePanel();
    } else if (act === "close") {
      togglePanel(false);
    } else if (act === "settings") {
      send({ type: "openOptions" });
    } else if (act === "help") {
      ui.help.classList.toggle("hidden");
    } else if (act === "quick-direct") {
      // 收起态直接播放：无需先展开面板，按胶囊上显示的最高可用档请求
      doDirect(true);
    } else if (act === "login") {
      startQr();
    } else if (act === "logout") {
      doLogout();
    } else if (act === "play") {
      doPlay();
    } else if (act === "direct") {
      doDirect();
    } else if (act === "copy") {
      doCopy();
    } else if (act === "copy-launch") {
      if (!state.launchUrl) {
        setStatus("还没有可复制的 pili:// 链接，请先点播放。", true);
        return;
      }
      copyText(state.launchUrl).then((ok) => {
        setStatus(ok ? "pili:// 链接已复制，可粘贴到 Win+R 或地址栏执行。" : state.launchUrl, !ok);
      });
    } else if (act === "download") {
      doDownload();
    } else if (act === "qr-refresh") {
      startQr();
    } else if (act === "qr-cancel") {
      hideQr();
    }
  });

  ui.qualities.addEventListener("change", (event) => {
    if (event.target.name !== "b4k-qn") {
      return;
    }
    state.qn = Number(event.target.value);
    renderQualities();
  });

  // 切换视频 / 分 P：丢弃上一个视频的数据、在途响应与重试计时，避免残留旧档位
  function resetVideoState() {
    state.data = null;
    state.qn = null;
    state.loading = false;
    state.loadSeq += 1;
    state.retried = false;
    clearTimeout(state.retryTimer);
    state.retryTimer = null;
    renderQualities();
    renderDockQuality();
    renderBusyChrome();
    ui.videoTitle.textContent = "正在加载视频信息…";
    ui.notice.textContent = "";
    ui.notice.classList.remove("show");
    ui.playBtn.disabled = true;
    setStatus("");
  }

  function syncTarget() {
    const target = parseTarget();
    if (!target) {
      host.style.display = "none";
      return;
    }
    host.style.display = "";
    const page = currentPage();
    const changed =
      !state.target ||
      target.bvid !== state.target.bvid ||
      target.aid !== state.target.aid ||
      page !== state.page;
    if (changed) {
      state.target = target;
      state.page = page;
      resetVideoState();
      // 进入视频页就静默取一次档位，收起态胶囊无需展开面板即可显示最高可用档
      loadVideo({ silent: true });
    }
  }

  syncTarget();
  setInterval(syncTarget, 1200);
})();
