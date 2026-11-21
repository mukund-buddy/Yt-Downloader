/* TuneGrab front-end: talks to the local bridge. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const STORE_KEY = "tg.settings.v1";

  const defaults = {
    mode: "audio",
    qualityAudio: "320",
    qualityVideo: "1080",
    prefix: "",
    suffix: "",
    template: "",
    out: "",
    maxItems: 200,
    cookies: "",
    clean: true,
    sponsorblock: true,
    fixedMtime: true,
    bridge: "",
    token: "",
  };

  let settings = { ...defaults, ...readStore() };
  if (!settings.bridge) settings.bridge = defaultBridge();

  let mode = settings.mode === "video" ? "video" : "audio";
  let jobId = null;
  let job = null;
  let source = null;
  let pollTimer = null;
  let engineReady = false;

  function readStore() {
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {};
    } catch {
      return {};
    }
  }

  function saveStore() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(settings));
    } catch {
      /* private mode */
    }
  }

  function defaultBridge() {
    const { protocol, hostname, origin } = location;
    if (protocol === "http:" || protocol === "https:") {
      if (hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]") {
        return origin;
      }
    }
    return "http://127.0.0.1:8787";
  }

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));

  function toast(message) {
    const el = $("toast");
    el.textContent = message;
    el.classList.add("show");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  function setBadge(state, text) {
    const cls = state === "ok" ? "ok" : state === "warn" ? "warn" : "";
    for (const [badge, label] of [["engineBadge", "engineText"], ["cardBadge", "cardEngine"]]) {
      $(badge).className = "engine-badge " + cls;
      $(label).textContent = text;
    }
  }

  async function fetchJson(url, options = {}, timeout = 4000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const headers = { ...(options.headers || {}) };
      if (settings.token) headers["X-Tunegrab-Token"] = settings.token;
      const response = await fetch(url, { ...options, headers, signal: controller.signal });
      const data = await response.json().catch(() => ({}));
      return { ok: response.ok, status: response.status, data };
    } finally {
      clearTimeout(timer);
    }
  }

  function qualityOptions(which) {
    const audio = [
      ["320", "320 kbps — best MP3"],
      ["256", "256 kbps"],
      ["192", "192 kbps"],
      ["128", "128 kbps"],
    ];
    const video = [
      ["2160", "2160p — 4K"],
      ["1440", "1440p — 2K"],
      ["1080", "1080p — Full HD"],
      ["720", "720p — HD"],
      ["480", "480p"],
      ["360", "360p"],
    ];
    return which === "video" ? video : audio;
  }

  function renderQuality() {
    const select = $("quality");
    const current = mode === "video" ? settings.qualityVideo : settings.qualityAudio;
    const list = qualityOptions(mode);
    select.innerHTML = list.map(([value, label]) =>
      `<option value="${value}">${esc(label)}</option>`).join("");
    select.value = list.some(([value]) => value === current) ? current : list[0][0];
    settings[mode === "video" ? "qualityVideo" : "qualityAudio"] = select.value;
  }

  function setMode(next) {
    mode = next === "video" ? "video" : "audio";
    settings.mode = mode;
    document.querySelectorAll("#modeSeg button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    });
    renderQuality();
    updatePreview();
    saveStore();
  }

  function updatePreview() {
    const prefix = $("prefix").value.trim();
    const suffix = $("suffix").value.trim();
    const ext = mode === "audio" ? ".mp3" : ".mp4";
    const head = prefix ? esc(prefix) + " " : "";
    const tail = suffix ? " " + esc(suffix) : "";
    $("previewName").innerHTML =
      `${head}<b>Artist</b> - <b>Song title</b>${tail}${esc(ext)}`;
  }

  function urls() {
    return $("urls").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  }

  function updateCount() {
    const list = urls();
    $("urlCount").textContent = list.length
      ? `${list.length} link${list.length > 1 ? "s" : ""} · playlists expand`
      : "one per line";
  }

  function collect() {
    return {
      urls: urls(),
      mode,
      quality: $("quality").value,
      prefix: $("prefix").value.trim(),
      suffix: $("suffix").value.trim(),
      template: $("template").value.trim(),
      out: $("out").value.trim(),
      max_items: Number($("maxItems").value) || 200,
      cookies: $("cookies").value.trim(),
      clean: $("clean").checked,
      sponsorblock: $("sponsorblock").checked,
      fixed_mtime: $("fixedMtime").checked,
    };
  }

  async function detectEngine() {
    const saved = settings.bridge;
    if (!saved) {
      settings.bridge = defaultBridge();
      saveStore();
    }
    if ($("bridgeUrl") && !$("bridgeUrl").value) $("bridgeUrl").value = settings.bridge;
    setBadge("", "Checking");
    try {
      const { ok, data } = await fetchJson(`${settings.bridge}/api/health`, {}, 2500);
      if (ok && data && data.ok) {
        engineReady = true;
        setBadge("ok", "Engine ready");
        $("offlineNotice").hidden = true;
        if (data.out && !$("out").value) {
          $("out").value = data.out;
          $("out").placeholder = data.out;
        }
        return true;
      }
    } catch {
      /* offline */
    }
    engineReady = false;
    setBadge("warn", "Engine offline");
    $("offlineNotice").hidden = false;
    return false;
  }

  function terminal(state) {
    return state === "done" || state === "failed" || state === "cancelled";
  }

  function stopLive() {
    if (source) {
      source.close();
      source = null;
    }
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function subscribe(id) {
    stopLive();
    const suffix = settings.token ? `?token=${encodeURIComponent(settings.token)}` : "";
    try {
      source = new EventSource(`${settings.bridge}/api/jobs/${id}/events${suffix}`);
      source.addEventListener("progress", (event) => {
        try {
          render(JSON.parse(event.data));
        } catch {
          /* ignore malformed frame */
        }
      });
      source.onerror = () => {
        if (source) source.close();
        source = null;
        startPolling(id);
      };
    } catch {
      startPolling(id);
    }
  }

  function startPolling(id) {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      try {
        const { ok, data } = await fetchJson(`${settings.bridge}/api/jobs/${id}`, {}, 4000);
        if (ok && data && data.job) {
          render(data.job);
          if (terminal(data.job.state)) stopLive();
        }
      } catch {
        /* keep trying */
      }
    }, 1200);
  }

  function render(next) {
    if (!next) return;
    job = next;
    $("progressPanel").hidden = false;

    const done = Number(job.completed) || 0;
    const failed = Number(job.failed) || 0;
    const total = Number(job.total) || 0;
    const current = job.current || {};
    const state = job.state || "queued";

    $("stateText").textContent = state;
    $("summaryText").textContent = job.summary || "";
    $("statDone").textContent = done;
    $("statFailed").textContent = failed;
    $("statLeft").textContent = total ? Math.max(total - done - failed, 0) : "—";

    const bar = $("bar");
    if (total > 0) {
      bar.classList.remove("indeterminate");
      const partial = Number(current.percent) || 0;
      const percent = Math.min(100, ((done + failed + partial / 100) / total) * 100);
      $("barFill").style.width = `${percent}%`;
    } else if (state === "queued" || state === "resolving") {
      bar.classList.add("indeterminate");
      $("barFill").style.width = "";
    } else {
      bar.classList.remove("indeterminate");
      $("barFill").style.width = "100%";
    }

    renderItems(job.items || []);
    renderLog(job.log || []);

    const active = state === "queued" || state === "resolving" || state === "running";
    $("startBtn").disabled = active;
    $("cancelBtn").hidden = !active;
    if (!active) {
      stopLive();
      if (state === "done") toast(job.summary || "Finished");
      else if (state === "failed") toast(job.error || "Job failed");
    }
  }

  function renderItems(items) {
    const box = $("items");
    const shown = items.slice(0, 120);
    const rows = shown.map((item, index) => {
      const status = item.status || "pending";
      const title = item.title || item.url || "";
      const tip = item.file || item.error || "";
      return `<div class="item-row" title="${esc(tip)}">
        <span class="idx">${String(index + 1).padStart(2, "0")}</span>
        <span class="name">${esc(title)}</span>
        <span class="pill ${esc(status)}">${esc(status)}</span>
      </div>`;
    });
    if (items.length > shown.length) {
      rows.push(`<div class="item-row"><span class="idx">…</span>
        <span class="name">+${items.length - shown.length} more items</span></div>`);
    }
    box.innerHTML = rows.join("") || `<div class="item-row"><span class="name">Waiting for links…</span></div>`;
  }

  function renderLog(lines) {
    const log = $("log");
    const tail = lines.slice(-80);
    log.textContent = tail.join("\n");
    if (!$("log").hidden) log.scrollTop = log.scrollHeight;
  }

  async function start() {
    const list = urls();
    if (!list.length) {
      toast("Paste at least one link");
      $("urls").focus();
      return;
    }
    if (!engineReady && !(await detectEngine())) {
      toast("Start start.bat first, then try again");
      return;
    }

    $("startBtn").disabled = true;
    $("cancelBtn").hidden = true;
    $("progressPanel").hidden = false;
    $("stateText").textContent = "queued";
    $("summaryText").textContent = "Sending job…";
    $("bar").classList.add("indeterminate");
    $("items").innerHTML = "";
    $("statDone").textContent = "0";
    $("statFailed").textContent = "0";
    $("statLeft").textContent = "—";

    try {
      const { ok, data } = await fetchJson(`${settings.bridge}/api/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collect()),
      }, 20000);
      if (!ok || !data || !data.ok) {
        throw new Error((data && data.error) || "job was refused");
      }
      jobId = data.job.id;
      render(data.job);
      subscribe(jobId);
    } catch (error) {
      toast(error.message || "Could not start the job");
      $("startBtn").disabled = false;
      $("bar").classList.remove("indeterminate");
      $("summaryText").textContent = "Could not start. Is the engine running?";
    }
  }

  async function cancel() {
    if (!jobId) return;
    try {
      await fetchJson(`${settings.bridge}/api/jobs/${jobId}/cancel`, { method: "POST" }, 6000);
      toast("Cancelling…");
    } catch {
      toast("Could not cancel");
    }
  }

  function agentCommand() {
    const data = collect();
    const lines = [
      "python bridge/tunegrab_bridge.py add",
      ...data.urls.map((url) => `"${url}"`),
      "--mode", data.mode,
      "--quality", data.quality,
      "--out", `"${data.out || "%USERPROFILE%\\Music\\TuneGrab"}"`,
    ];
    if (data.prefix) lines.push("--prefix", `"${data.prefix}"`);
    if (data.suffix) lines.push("--suffix", `"${data.suffix}"`);
    if (data.template) lines.push("--template", `"${data.template}"`);
    if (!data.clean) lines.push("--no-clean");
    if (!data.sponsorblock) lines.push("--no-sponsorblock");
    if (!data.fixed_mtime) lines.push("--no-fixed-mtime");
    if (data.max_items !== 200) lines.push("--max-items", String(data.max_items));
    if (data.cookies) lines.push("--cookies", `"${data.cookies}"`);
    lines.push("--json");
    return lines.join(" ");
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied");
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
        toast("Copied");
      } catch {
        toast("Copy failed");
      }
      area.remove();
    }
  }

  function wire() {
    document.querySelectorAll("#modeSeg button").forEach((button) => {
      button.addEventListener("click", () => setMode(button.dataset.mode));
    });

    $("quality").addEventListener("change", () => {
      settings[mode === "video" ? "qualityVideo" : "qualityAudio"] = $("quality").value;
      saveStore();
    });

    for (const id of ["prefix", "suffix"]) {
      $(id).addEventListener("input", () => {
        settings[id] = $(id).value;
        updatePreview();
        saveStore();
      });
    }

    $("urls").addEventListener("input", updateCount);

    for (const id of ["template", "out", "cookies", "maxItems", "bridgeUrl", "token"]) {
      $(id).addEventListener("input", () => {
        settings[id] = $(id).value;
        if (id === "bridgeUrl") settings.bridge = $(id).value.trim() || defaultBridge();
        if (id === "token") settings.token = $(id).value;
        saveStore();
      });
    }
    $("bridgeUrl").addEventListener("change", detectEngine);

    for (const id of ["clean", "sponsorblock", "fixedMtime"]) {
      $(id).addEventListener("change", () => {
        settings[id] = $(id).checked;
        saveStore();
      });
    }

    $("startBtn").addEventListener("click", start);
    $("cancelBtn").addEventListener("click", cancel);
    $("copyAgentBtn").addEventListener("click", () => {
      if (!urls().length) {
        toast("Paste a link first");
        return;
      }
      copyText(agentCommand());
    });

    $("logToggle").addEventListener("click", () => {
      const log = $("log");
      log.hidden = !log.hidden;
      $("logToggle").textContent = log.hidden ? "Show engine log" : "Hide engine log";
      if (!log.hidden) log.scrollTop = log.scrollHeight;
    });

    document.querySelectorAll("[data-copy]").forEach((button) => {
      button.addEventListener("click", () => {
        const target = document.getElementById(button.dataset.copy);
        if (target) copyText(target.innerText);
      });
    });
  }

  function applySettings() {
    $("prefix").value = settings.prefix || "";
    $("suffix").value = settings.suffix || "";
    $("template").value = settings.template || "";
    $("out").value = settings.out || "";
    $("maxItems").value = settings.maxItems || 200;
    $("cookies").value = settings.cookies || "";
    $("clean").checked = settings.clean !== false;
    $("sponsorblock").checked = settings.sponsorblock !== false;
    $("fixedMtime").checked = settings.fixedMtime !== false;
    $("bridgeUrl").value = settings.bridge;
    $("token").value = settings.token || "";
    setMode(settings.mode || "audio");
    updatePreview();
    updateCount();
  }

  function init() {
    wire();
    applySettings();
    detectEngine();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
