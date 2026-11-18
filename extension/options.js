/* 扩展设置页：后端地址、连接测试、账号状态、调试日志。 */

const DEFAULT_BACKEND = "http://127.0.0.1:8000";
const LOG_KEY = "debugLogs";

const $ = (id) => document.getElementById(id);

function setMsg(text, kind) {
  const el = $("msg");
  el.textContent = text || "";
  el.className = "msg" + (kind ? " " + kind : "");
}

function normalizeBackend(value) {
  let url = (value || "").trim();
  if (!url) {
    return "";
  }
  if (!/^https?:\/\//i.test(url)) {
    url = "http://" + url;
  }
  return url.replace(/\/+$/, "");
}

function send(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (resp) => {
      const err = chrome.runtime.lastError;
      if (err) {
        reject(new Error(err.message));
        return;
      }
      resolve(resp || { ok: false, error: "扩展后台无响应" });
    });
  });
}

async function api(path, options) {
  const resp = await send({ type: "api", path, options });
  if (!resp.ok) {
    throw new Error(resp.error || "请求失败");
  }
  return resp.data;
}

async function loadBackend() {
  const { backend } = await chrome.storage.sync.get("backend");
  $("backend").value = backend || DEFAULT_BACKEND;
}

async function refreshAccount() {
  const chip = $("account-state");
  try {
    const account = await api("/api/auth/status");
    chip.textContent = account.logged_in ? "已登录" : "未登录";
    chip.classList.toggle("on", !!account.logged_in);
    $("account-mid").textContent = account.mid || "-";
    $("account-name").textContent = account.uname || "-";
  } catch (err) {
    chip.textContent = "后端未连接";
    chip.classList.remove("on");
    $("account-mid").textContent = "-";
    $("account-name").textContent = "-";
  }
}

// ------------------------------------------------------------------ 调试日志

function fmtTime(at) {
  return new Date(at || Date.now()).toLocaleTimeString("zh-CN", { hour12: false });
}

function addChip(parent, text, kind) {
  const chip = document.createElement("span");
  chip.className = "chip" + (kind ? " " + kind : "");
  chip.textContent = text;
  parent.appendChild(chip);
}

function renderEntry(entry) {
  const item = document.createElement("div");
  item.className = "log-item" + (entry.ok === false ? " error" : "");

  const top = document.createElement("div");
  top.className = "log-top";

  const time = document.createElement("span");
  time.className = "log-time";
  time.textContent =
    entry.start_at && entry.end_at
      ? `${fmtTime(entry.start_at)} → ${fmtTime(entry.end_at)}`
      : fmtTime(entry.at);

  const kind = document.createElement("span");
  kind.className = "log-kind";
  if (entry.kind === "launch") {
    kind.textContent = `拉起 ${entry.player || "播放器"}` + (entry.qn ? ` · qn=${entry.qn}` : "");
  } else {
    kind.textContent = `${entry.method || "GET"} ${entry.path || ""}`;
  }

  const ms = document.createElement("span");
  ms.className = "log-ms";
  if (entry.kind === "launch") {
    ms.textContent = entry.launch_ms != null ? `${entry.launch_ms}ms` : "";
  } else {
    ms.textContent = entry.ms != null ? `${entry.ms}ms` : "";
  }

  top.append(time, kind, ms);
  item.appendChild(top);

  const meta = document.createElement("div");
  meta.className = "log-meta";
  const t = entry.timings || {};
  if (t.grpc_ms != null) {
    addChip(
      meta,
      `gRPC ${t.grpc_ms}ms` + (t.grpc_calls ? ` / ${t.grpc_calls} 次` : ""),
      "hot"
    );
  }
  if (t.cache_hit) {
    addChip(meta, "取流缓存命中");
  }
  if (t.resolve_ms != null) {
    addChip(meta, `解析 ${t.resolve_ms}ms`);
  }
  if (t.probe_ms != null) {
    addChip(meta, `节点探测 ${t.probe_ms}ms`);
  }
  if (t.total_ms != null) {
    addChip(meta, `后端总计 ${t.total_ms}ms`);
  }
  if (entry.api_ms != null) {
    addChip(meta, `接口 ${entry.api_ms}ms`);
  }
  if (entry.kind !== "launch") {
    if (entry.ok === false) {
      addChip(meta, entry.error || "失败", "bad");
    } else {
      addChip(meta, "OK");
    }
  }
  if (meta.childNodes.length) {
    item.appendChild(meta);
  }
  return item;
}

async function renderLogs() {
  const { [LOG_KEY]: logs = [] } = await chrome.storage.local.get(LOG_KEY);
  const list = $("log-list");
  list.textContent = "";
  $("log-count").textContent = `${logs.length} 条`;
  if (!logs.length) {
    const empty = document.createElement("p");
    empty.className = "log-empty";
    empty.textContent = "暂无日志。开启开关后，在 B 站页面点一次播放即可记录。";
    list.appendChild(empty);
    return;
  }
  const frag = document.createDocumentFragment();
  logs.forEach((entry) => frag.appendChild(renderEntry(entry)));
  list.appendChild(frag);
}

async function loadDebug() {
  const { debugLog } = await chrome.storage.sync.get("debugLog");
  $("debug-log").checked = !!debugLog;
  await renderLogs();
}

$("save").addEventListener("click", async () => {
  const backend = normalizeBackend($("backend").value);
  if (!backend) {
    setMsg("请填写后端服务地址", "error");
    return;
  }
  await chrome.storage.sync.set({ backend });
  $("backend").value = backend;
  setMsg("已保存：" + backend, "ok");
  refreshAccount();
});

$("test").addEventListener("click", async () => {
  const backend = normalizeBackend($("backend").value);
  if (!backend) {
    setMsg("请填写后端服务地址", "error");
    return;
  }
  setMsg("正在连接…");
  try {
    const health = await api("/api/health", { base: backend });
    setMsg(
      `连接成功 · 版本 ${health.version} · ${health.logged_in ? "已登录" : "未登录"}`,
      "ok"
    );
  } catch (err) {
    setMsg("连接失败：" + err.message, "error");
  }
});

$("logout").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
    setMsg("已清除服务端凭证", "ok");
    refreshAccount();
  } catch (err) {
    setMsg("退出失败：" + err.message, "error");
  }
});

$("refresh").addEventListener("click", () => {
  refreshAccount();
  setMsg("");
});

$("debug-log").addEventListener("change", async (event) => {
  const enabled = event.target.checked;
  await chrome.storage.sync.set({ debugLog: enabled });
  setMsg(enabled ? "调试日志已开启，触发一次播放后回到这里查看" : "调试日志已关闭", "ok");
  if (!enabled) {
    await renderLogs();
  }
});

$("log-refresh").addEventListener("click", renderLogs);

$("log-clear").addEventListener("click", async () => {
  await chrome.storage.local.set({ [LOG_KEY]: [] });
  await renderLogs();
  setMsg("调试日志已清空", "ok");
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes[LOG_KEY]) {
    renderLogs();
  }
});

loadBackend();
refreshAccount();
loadDebug();
