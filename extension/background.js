/* 扩展后台：统一代理后端请求，避免页面跨域限制。 */

const DEFAULT_BACKEND = "http://127.0.0.1:8000";

chrome.runtime.onInstalled.addListener(async () => {
  const { backend } = await chrome.storage.sync.get("backend");
  if (!backend) {
    await chrome.storage.sync.set({ backend: DEFAULT_BACKEND });
  }
  const { debugLog } = await chrome.storage.sync.get("debugLog");
  if (debugLog === undefined) {
    // 调试日志默认关闭，只在排查「拉起播放器慢」时手动打开
    await chrome.storage.sync.set({ debugLog: false });
  }
});

chrome.action.onClicked.addListener(() => {
  chrome.runtime.openOptionsPage();
});

async function getBackend() {
  const { backend } = await chrome.storage.sync.get("backend");
  return (backend || DEFAULT_BACKEND).replace(/\/+$/, "");
}

// ------------------------------------------------------------------ 调试日志

const LOG_LIMIT = 60;

async function debugEnabled() {
  const { debugLog } = await chrome.storage.sync.get("debugLog");
  return !!debugLog;
}

function logLine(entry) {
  const time = new Date(entry.at).toLocaleTimeString("zh-CN", { hour12: false });
  const span =
    entry.start_at && entry.end_at
      ? `${new Date(entry.start_at).toLocaleTimeString("zh-CN", { hour12: false })}→${new Date(
          entry.end_at
        ).toLocaleTimeString("zh-CN", { hour12: false })}`
      : time;
  const t = entry.timings || {};
  if (entry.kind === "launch") {
    const bits = [
      `[bili4k] ${span} 拉起 ${entry.player || "播放器"}`,
      entry.launch_ms != null ? `点击到拉起 ${entry.launch_ms}ms` : "",
      entry.api_ms != null ? `接口 ${entry.api_ms}ms` : "",
      t.grpc_ms != null ? `gRPC ${t.grpc_ms}ms` : "",
      t.probe_ms != null ? `探测 ${t.probe_ms}ms` : ""
    ];
    console.log(bits.filter(Boolean).join(" | "), entry);
    return;
  }
  const bits = [
    `[bili4k] ${span} ${entry.method || "GET"} ${entry.path || ""}`,
    entry.ms != null ? `${entry.ms}ms` : "",
    t.grpc_ms != null ? `gRPC ${t.grpc_ms}ms/${t.grpc_calls || 0}次` : "",
    t.cache_hit ? "取流缓存命中" : "",
    t.resolve_ms != null ? `解析 ${t.resolve_ms}ms` : "",
    t.probe_ms != null ? `节点探测 ${t.probe_ms}ms` : "",
    t.total_ms != null ? `后端总计 ${t.total_ms}ms` : "",
    entry.ok ? "OK" : `失败 ${entry.error || ""}`
  ];
  console.log(bits.filter(Boolean).join(" | "));
}

async function recordLog(entry) {
  if (!(await debugEnabled())) {
    return;
  }
  const full = { at: Date.now(), ...entry };
  logLine(full);
  const { debugLogs = [] } = await chrome.storage.local.get("debugLogs");
  await chrome.storage.local.set({
    debugLogs: [full, ...debugLogs].slice(0, LOG_LIMIT)
  });
}

async function callApi(path, options = {}) {
  const base = (options.base || (await getBackend())).replace(/\/+$/, "");
  const url = base + path;
  // 后端卡住时不能让扩展的「请求中」永远不释放：默认 15 秒超时并中断
  const timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : 15000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(url, {
      method: options.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: options.body ? JSON.stringify(options.body) : undefined,
      cache: "no-store",
      signal: controller.signal
    });
  } catch (err) {
    clearTimeout(timer);
    if (err && err.name === "AbortError") {
      throw new Error(
        `请求超时（${Math.round(timeoutMs / 1000)} 秒未响应）：${options.method || "GET"} ${url}`
      );
    }
    // 连不上时 fetch 只抛 TypeError，这里补上真实 URL，避免「Failed to fetch」无法定位
    throw new Error(`无法连接后端（${url}）：${err.message || err}`);
  }

  let text;
  try {
    text = await response.text();
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new Error(
        `请求超时（${Math.round(timeoutMs / 1000)} 秒未读完响应）：${options.method || "GET"} ${url}`
      );
    }
    throw new Error(`读取响应失败（${url}）：${err.message || err}`);
  } finally {
    clearTimeout(timer);
  }
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (err) {
    throw new Error(
      `后端返回非 JSON（HTTP ${response.status}）：${options.method || "GET"} ${url}，请检查后端地址是否正确`
    );
  }
  if (!response.ok || !payload.ok) {
    // 把 error.code / error.message / 完整 URL 一起带出来：
    // 「接口不存在（not_found）」和「分片会话过期（session_not_found）」都是 404，
    // 只看状态码分不清，也会让人不知道是哪一次调用触发的。
    const error = payload.error || {};
    const code = typeof error === "string" ? error : error.code || "";
    let message = typeof error === "string" ? "" : error.message || "";
    if (!message && payload.detail) {
      message =
        typeof payload.detail === "string"
          ? payload.detail
          : JSON.stringify(payload.detail);
    }
    const head = [`HTTP ${response.status}`, code].filter(Boolean).join(" / ");
    let reason = message || text.slice(0, 160) || "无响应体";
    if (response.status === 404 && (!code || code === "not_found")) {
      reason += "（接口不存在：后端镜像可能未更新，用 /openapi.json 核对路由）";
    }
    throw new Error(
      `请求失败（${head}）：${reason}｜${options.method || "GET"} ${url}`
    );
  }
  return payload.data;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.type) {
    return false;
  }

  if (message.type === "api") {
    const startedAt = performance.now();
    const startedEpoch = Date.now();
    const method = (message.options && message.options.method) || "GET";
    callApi(message.path, message.options || {})
      .then((data) => {
        recordLog({
          kind: "api",
          method,
          path: message.path,
          start_at: startedEpoch,
          end_at: Date.now(),
          ms: Math.round(performance.now() - startedAt),
          ok: true,
          timings: (data && data.timings) || null
        }).catch(() => {});
        sendResponse({ ok: true, data });
      })
      .catch((err) => {
        recordLog({
          kind: "api",
          method,
          path: message.path,
          start_at: startedEpoch,
          end_at: Date.now(),
          ms: Math.round(performance.now() - startedAt),
          ok: false,
          error: err.message
        }).catch(() => {});
        sendResponse({ ok: false, error: err.message });
      });
    return true;
  }

  if (message.type === "log") {
    recordLog(message.entry || {})
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }

  if (message.type === "download") {
    chrome.downloads.download(
      { url: message.url, filename: message.filename, saveAs: true },
      (downloadId) => {
        if (chrome.runtime.lastError) {
          sendResponse({ ok: false, error: chrome.runtime.lastError.message });
        } else {
          sendResponse({ ok: true, downloadId });
        }
      }
    );
    return true;
  }

  if (message.type === "openOptions") {
    chrome.runtime.openOptionsPage();
    sendResponse({ ok: true });
    return false;
  }

  return false;
});
