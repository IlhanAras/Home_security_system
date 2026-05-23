// ============================================================
// Hybrid IoT Security Dashboard - frontend controller.
//
// Responsibilities:
//   - Maintain a WebSocket to the backend for real-time MQTT events.
//   - Drive the camera panel (snapshot refresh or MJPEG stream) against
//     the backend's /api/capture and /api/stream proxy endpoints. The
//     ESP32 host is configured on the backend only (POST /api/config from
//     the local machine) so the public ngrok URL never reveals LAN IPs.
//   - On each motion alert: fetch a fresh frame and POST /api/ack so the
//     firmware can safely enter deep sleep (Sleep Guard protocol).
//   - Dispatch commands and render the event log / device status.
// ============================================================

const LS_THEME = "sec.theme";

// Session-scoped state keys. sessionStorage survives same-tab navigations
// (Dashboard <-> Clips via header links, and F5 reloads) but goes away when
// the tab is closed — which is exactly the "between page" window the user
// cares about.
const SS_STREAMING   = "sec.streaming";
const SS_SAVING      = "sec.saving";
const SS_PIR_ENABLED = "sec.pirEnabled";

// ---------------------------------------------------------------
// Theme (light / dark) — Tailwind uses the `dark` class on <html>.
// The *initial* application happens inline in index.html <head> to avoid
// a FOUC; this block only handles runtime toggling + persistence.
// ---------------------------------------------------------------
function isDark() {
    return document.documentElement.classList.contains("dark");
}

function setTheme(theme) {
    const dark = theme === "dark";
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem(LS_THEME, dark ? "dark" : "light");
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = dark ? "Light" : "Dark";
}

const els = {
    wsDot:        document.getElementById("ws-dot"),
    wsLabel:      document.getElementById("ws-label"),
    mqttDot:      document.getElementById("mqtt-dot"),
    mqttLabel:    document.getElementById("mqtt-label"),

    alertPanel:   document.getElementById("alert-panel"),
    alertNone:    document.getElementById("alert-none"),
    alertContent: document.getElementById("alert-content"),
    alertEvent:   document.getElementById("alert-event"),
    alertDevice:  document.getElementById("alert-device"),
    alertTime:    document.getElementById("alert-time"),

    camImg:       document.getElementById("cam-img"),
    camPlaceholder: document.getElementById("cam-placeholder"),

    btnCamToggle: document.getElementById("btn-cam-toggle"),
    btnStreamSave: document.getElementById("btn-stream-save"),
    btnCapture:   document.getElementById("btn-capture"),
    btnLedOn:     document.getElementById("btn-led-on"),
    btnLedOff:    document.getElementById("btn-led-off"),
    btnPirToggle: document.getElementById("btn-pir-toggle"),

    clipConfigForm:   document.getElementById("clip-config-form"),
    clipFramesize:    document.getElementById("clip-framesize"),
    clipQuality:      document.getElementById("clip-quality"),
    clipDuration:     document.getElementById("clip-duration"),
    clipFps:          document.getElementById("clip-fps"),
    clipConfigStatus: document.getElementById("clip-config-status"),

    devStatus:    document.getElementById("dev-status"),
    devRssi:      document.getElementById("dev-rssi"),
    devUptime:    document.getElementById("dev-uptime"),
    devUpdated:   document.getElementById("dev-updated"),

    eventBody:    document.getElementById("event-body"),
};

// ---------------------------------------------------------------
// Camera stream — off by default.
//   - The stream is expensive on the ESP32 side (it pins one socket for
//     the lifetime of the MJPEG connection) and contends with the clip
//     recorder and /capture calls. So we start CLOSED: the user explicitly
//     opens it with the toggle button when they want to watch live, and
//     closes it again before triggering anything else.
//   - The ESP32 host stays on the backend; the frontend only ever hits
//     /api/stream and lets the backend proxy.
// ---------------------------------------------------------------
let streaming = false;
// PIR arm / disarm state. Firmware defaults to armed at boot, so this
// matches. Declared up here because startStream() auto-disarms PIR, and
// we want both variables in one place.
let pirEnabled = true;
// Remembers whether PIR was armed before the user hit Start stream so we
// can put it back exactly how they left it once they stop.
let pirWasEnabledBeforeStream = true;

const BTN_START_CLASS = "px-5 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium transition";
const BTN_STOP_CLASS  = "px-5 py-2 rounded-lg bg-red-600 hover:bg-red-700 text-white text-sm font-medium transition";

async function startStream() {
    streaming = true;
    sessionStorage.setItem(SS_STREAMING, "true");
    // Auto-disarm PIR: while streaming, motion alerts would otherwise add
    // noisy rows to the event log / clip list and waste backend recording
    // cycles. Remember the previous PIR state so we can restore it on stop.
    pirWasEnabledBeforeStream = pirEnabled;
    sessionStorage.setItem("sec.pirBeforeStream", String(pirEnabled));
    if (pirEnabled) {
        pirEnabled = false;
        sessionStorage.setItem(SS_PIR_ENABLED, "false");
        renderPirButton();
        await sendCommand("pir_off");
    }
    // Cache-bust so pressing Start always opens a fresh upstream socket.
    els.camImg.src = `/api/stream?t=${Date.now()}`;
    els.camImg.classList.add("visible");
    els.btnCamToggle.textContent = "Stop stream";
    els.btnCamToggle.className = BTN_STOP_CLASS;
    // Expose the Save button only while the stream is actually running.
    els.btnStreamSave.classList.remove("hidden");
}

async function stopStream() {
    streaming = false;
    sessionStorage.removeItem(SS_STREAMING);
    // If a save was in progress, stop it too so we don't leave a dangling
    // recording on the backend.
    if (saving) await stopStreamSave();
    // removeAttribute tears down the MJPEG connection cleanly; setting to
    // "" would sometimes keep the last frame painted.
    els.camImg.removeAttribute("src");
    els.camImg.classList.remove("visible");
    els.btnCamToggle.textContent = "Start stream";
    els.btnCamToggle.className = BTN_START_CLASS;
    els.btnStreamSave.classList.add("hidden");
    // Restore PIR to whatever the user had before streaming.
    if (pirWasEnabledBeforeStream && !pirEnabled) {
        pirEnabled = true;
        sessionStorage.setItem(SS_PIR_ENABLED, "true");
        renderPirButton();
        await sendCommand("pir_on");
    }
    sessionStorage.removeItem("sec.pirBeforeStream");
}

// Restore stream/save state after a same-tab navigation (e.g. coming back
// from /clips.html). sessionStorage holds the intent; the backend's
// /api/health holds the authoritative recorder state, so we cross-check.
async function restoreStreamState() {
    const wasStreaming = sessionStorage.getItem(SS_STREAMING) === "true";
    const beforeStream = sessionStorage.getItem("sec.pirBeforeStream");
    if (beforeStream !== null) {
        pirWasEnabledBeforeStream = beforeStream === "true";
    }
    if (!wasStreaming) return;

    // Re-open the stream without re-sending the pir_off command (firmware
    // already has the right state). This is essentially startStream minus
    // the side-effects.
    streaming = true;
    els.camImg.src = `/api/stream?t=${Date.now()}`;
    els.camImg.classList.add("visible");
    els.btnCamToggle.textContent = "Stop stream";
    els.btnCamToggle.className = BTN_STOP_CLASS;
    els.btnStreamSave.classList.remove("hidden");

    // If backend says a stream recording is still in progress, sync the
    // save button. The /api/stream relay will keep extracting JPEGs into
    // that same clip now that the stream is live again.
    try {
        const res = await fetch("/api/health");
        if (res.ok) {
            const data = await res.json();
            if (data.stream_recording) {
                saving = true;
                sessionStorage.setItem(SS_SAVING, "true");
                els.btnStreamSave.textContent = "Stop saving";
                els.btnStreamSave.className = BTN_SAVE_REC_CLASS;
            }
        }
    } catch { /* ignore — pollHealth will retry */ }
}

els.btnCamToggle.addEventListener("click", () => {
    if (streaming) stopStream();
    else           startStream();
});

// If the backend returns 502/503 (ESP32 unreachable / host not configured),
// the <img> fires an error event. Reset the toggle so the user can retry
// without being stuck in a "streaming" state that isn't actually streaming.
els.camImg.addEventListener("error", () => {
    if (streaming) stopStream();
});

// ---------------------------------------------------------------
// Stream save — tees frames already flowing through /api/stream into
// a new clip. Active only while `streaming` is true.
// ---------------------------------------------------------------
let saving = false;

const BTN_SAVE_IDLE_CLASS = "px-5 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-medium transition";
const BTN_SAVE_REC_CLASS  = "px-5 py-2 rounded-lg bg-red-600 hover:bg-red-700 text-white text-sm font-medium transition animate-pulse";

async function startStreamSave() {
    try {
        const res = await fetch("/api/stream-record/start", { method: "POST" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        saving = true;
        sessionStorage.setItem(SS_SAVING, "true");
        els.btnStreamSave.textContent = "Stop saving";
        els.btnStreamSave.className = BTN_SAVE_REC_CLASS;
    } catch (err) {
        console.warn("startStreamSave failed:", err);
    }
}

async function stopStreamSave() {
    try {
        const res = await fetch("/api/stream-record/stop", { method: "POST" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
        console.warn("stopStreamSave failed:", err);
    }
    saving = false;
    sessionStorage.removeItem(SS_SAVING);
    els.btnStreamSave.textContent = "Save stream";
    els.btnStreamSave.className = BTN_SAVE_IDLE_CLASS;
}

els.btnStreamSave.addEventListener("click", () => {
    if (saving) stopStreamSave();
    else        startStreamSave();
});

// ---------------------------------------------------------------
// Commands
// ---------------------------------------------------------------
async function sendCommand(command) {
    try {
        const res = await fetch("/api/command", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ command }),
        });
        if (!res.ok) {
            const body = await res.text();
            console.warn("command failed:", res.status, body);
        }
    } catch (err) {
        console.warn("command error:", err);
    }
}

els.btnCapture.addEventListener("click", () => sendCommand("capture"));
els.btnLedOn.addEventListener("click",   () => sendCommand("led_on"));
els.btnLedOff.addEventListener("click",  () => sendCommand("led_off"));

// PIR arm / disarm toggle. The firmware defaults to armed at every boot, so
// the dashboard boot state (green "PIR: ON") matches that. If the command
// fails we leave the local state as-is — the dashboard and device will
// re-sync on the next user click.
const PIR_ON_CLASS  = "px-3.5 py-1.5 rounded-lg bg-green-600 hover:bg-green-700 text-white text-xs font-semibold transition";
const PIR_OFF_CLASS = "px-3.5 py-1.5 rounded-lg bg-red-600 hover:bg-red-700 text-white text-xs font-semibold transition";

function renderPirButton() {
    els.btnPirToggle.textContent = pirEnabled ? "PIR: ON" : "PIR: OFF";
    els.btnPirToggle.className   = pirEnabled ? PIR_ON_CLASS : PIR_OFF_CLASS;
}

els.btnPirToggle.addEventListener("click", async () => {
    pirEnabled = !pirEnabled;
    sessionStorage.setItem(SS_PIR_ENABLED, String(pirEnabled));
    renderPirButton();
    await sendCommand(pirEnabled ? "pir_on" : "pir_off");
});

// Restore PIR state on page load. Backend /api/health is authoritative
// (it reflects the last MQTT command), fall back to sessionStorage.
async function restorePirState() {
    try {
        const res = await fetch("/api/health");
        if (res.ok) {
            const data = await res.json();
            if (data.pir_enabled !== undefined) {
                pirEnabled = !!data.pir_enabled;
                renderPirButton();
                return;
            }
        }
    } catch { /* fall through */ }
    const stored = sessionStorage.getItem(SS_PIR_ENABLED);
    if (stored !== null) {
        pirEnabled = stored === "true";
        renderPirButton();
    }
}

// ---------------------------------------------------------------
// Motion clip settings (duration, fps, quality)
//   Pull current values from the backend at startup so the form shows the
//   truth, then POST any edits back. Quality is forwarded to the firmware
//   over MQTT by the backend; duration and fps stay server-side and are
//   consumed by record_clip on the next alert.
// ---------------------------------------------------------------
async function loadClipConfig() {
    try {
        const res = await fetch("/api/clip-config");
        if (!res.ok) return;
        const cfg = await res.json();
        if (cfg.framesize  !== undefined) els.clipFramesize.value = String(cfg.framesize);
        if (cfg.quality    !== undefined) els.clipQuality.value   = cfg.quality;
        if (cfg.duration_s !== undefined) els.clipDuration.value  = cfg.duration_s;
        if (cfg.fps        !== undefined) els.clipFps.value       = cfg.fps;
    } catch (err) {
        console.warn("loadClipConfig failed:", err);
    }
}

function clipConfigFlashStatus(msg, ok = true) {
    els.clipConfigStatus.textContent = msg;
    els.clipConfigStatus.classList.remove("text-red-500", "text-emerald-500");
    els.clipConfigStatus.classList.add(ok ? "text-emerald-500" : "text-red-500");
    setTimeout(() => {
        els.clipConfigStatus.innerHTML = "&nbsp;";
        els.clipConfigStatus.classList.remove("text-red-500", "text-emerald-500");
    }, 3000);
}

els.clipConfigForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
        framesize:  Number(els.clipFramesize.value) || undefined,
        quality:    Number(els.clipQuality.value)    || undefined,
        duration_s: Number(els.clipDuration.value)   || undefined,
        fps:        Number(els.clipFps.value)        || undefined,
    };
    try {
        const res = await fetch("/api/clip-config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (res.status === 409) {
            clipConfigFlashStatus("Recording active — stop it first.", false);
            return;
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        els.clipFramesize.value = String(data.clip_config.framesize);
        els.clipQuality.value   = data.clip_config.quality;
        els.clipDuration.value  = data.clip_config.duration_s;
        els.clipFps.value       = data.clip_config.fps;
        clipConfigFlashStatus("Saved.");
        if (streaming) {
            els.camImg.removeAttribute("src");
            await new Promise(r => setTimeout(r, 300));
            els.camImg.src = `/api/stream?t=${Date.now()}`;
        }
    } catch (err) {
        clipConfigFlashStatus("Failed.", false);
        console.warn("clip-config POST failed:", err);
    }
});

async function sendAck() {
    try {
        await fetch("/api/ack", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
        });
    } catch (err) {
        console.warn("ack error:", err);
    }
}

// ---------------------------------------------------------------
// Event log (REST) + device status
// ---------------------------------------------------------------
function formatTime(iso) {
    if (!iso) return "—";
    try {
        return new Date(iso).toLocaleString();
    } catch { return iso; }
}

const EVENT_ROW_CLASS = "hover:bg-slate-50 dark:hover:bg-slate-800/50";
const EVENT_TD_CLASS  = "px-4 py-2 align-top";

function renderEventRow(evt) {
    const tr = document.createElement("tr");
    tr.className = EVENT_ROW_CLASS;
    tr.innerHTML = `
        <td class="${EVENT_TD_CLASS} text-slate-400 dark:text-slate-500">${evt.id ?? ""}</td>
        <td class="${EVENT_TD_CLASS} text-slate-600 dark:text-slate-300 whitespace-nowrap"></td>
        <td class="${EVENT_TD_CLASS} text-slate-600 dark:text-slate-300 whitespace-nowrap"></td>
        <td class="${EVENT_TD_CLASS} font-mono text-xs text-slate-600 dark:text-slate-300 break-all"></td>`;
    const cells = tr.querySelectorAll("td");
    cells[1].textContent = formatTime(evt.timestamp);
    cells[2].textContent = evt.topic;
    cells[3].textContent = evt.payload;
    return tr;
}

async function loadEvents() {
    try {
        const res = await fetch("/api/events?limit=100");
        const data = await res.json();
        els.eventBody.innerHTML = "";
        if (!data.events || data.events.length === 0) {
            els.eventBody.innerHTML =
                '<tr><td colspan="4" class="px-4 py-6 text-center text-slate-500 dark:text-slate-400">No events yet.</td></tr>';
            return;
        }
        for (const evt of data.events) {
            els.eventBody.appendChild(renderEventRow(evt));
        }
        // Refill the Last alert and Device status panels from history so a
        // fresh page load (e.g. after navigating from /clips.html) shows the
        // same state the live WS handler would have set.
        hydratePanelsFromEvents(data.events);
    } catch (err) {
        console.warn("loadEvents failed:", err);
    }
}

function hydratePanelsFromEvents(events) {
    // `events` is descending by id, so the first match per topic is newest.
    const lastAlert  = events.find(e => e.topic === "security/alert");
    const lastStatus = events.find(e => e.topic === "security/status");

    if (lastAlert) {
        let parsed = null;
        try { parsed = JSON.parse(lastAlert.payload); } catch { /* ignore */ }
        els.alertNone.classList.add("hidden");
        els.alertContent.classList.remove("hidden");
        els.alertEvent.textContent  = (parsed && parsed.event) || "alert";
        els.alertDevice.textContent = (parsed && parsed.device_id) || "";
        els.alertTime.textContent   = formatTime(lastAlert.timestamp);
    }

    if (lastStatus) {
        let parsed = null;
        try { parsed = JSON.parse(lastStatus.payload); } catch { /* ignore */ }
        if (parsed) {
            if (parsed.status !== undefined) {
                els.devStatus.textContent = parsed.status;
                applyStatusColor(els.devStatus, parsed.status);
            }
            if (parsed.rssi !== undefined) {
                els.devRssi.textContent = `${parsed.rssi} dBm`;
                applyRssiColor(els.devRssi, parsed.rssi);
            }
            if (parsed.uptime !== undefined) els.devUptime.textContent = `${parsed.uptime} s`;
            els.devUpdated.textContent = formatTime(lastStatus.timestamp);
        }
    }
}

function setDot(elem, on) {
    elem.classList.toggle("dot-on", on);
    elem.classList.toggle("dot-off", !on);
}

// Device status coloring — strips previous tier classes before applying the
// new one so the element doesn't accumulate conflicting text-* utilities.
const STATUS_COLOR_CLASSES = ["text-green-500", "text-red-500", "text-amber-500"];

function applyStatusColor(elem, value) {
    elem.classList.remove(...STATUS_COLOR_CLASSES);
    if (!value) return;
    elem.classList.add(value === "online" ? "text-green-500" : "text-red-500");
}

function applyRssiColor(elem, rssi) {
    elem.classList.remove(...STATUS_COLOR_CLASSES);
    if (rssi === undefined || rssi === null) return;
    // Wi-Fi signal tiers:
    //   >= -60 dBm : good       (green)
    //   -60 .. -75 : workable   (amber)
    //   <  -75 dBm : poor       (red)
    if (rssi >= -60)      elem.classList.add("text-green-500");
    else if (rssi >= -75) elem.classList.add("text-amber-500");
    else                  elem.classList.add("text-red-500");
}

async function pollHealth() {
    try {
        const res = await fetch("/api/health");
        const data = await res.json();
        if (data.mqtt_connected) {
            setDot(els.mqttDot, true);
            els.mqttLabel.textContent = `Broker: ${data.broker}`;
        } else {
            setDot(els.mqttDot, false);
            els.mqttLabel.textContent = "Broker: disconnected";
        }
    } catch {
        setDot(els.mqttDot, false);
        els.mqttLabel.textContent = "Broker: unknown";
    }
}

// ---------------------------------------------------------------
// Live MQTT stream via WebSocket
// ---------------------------------------------------------------
function handleMqttMessage(topic, payload, timestamp) {
    let parsed = null;
    try { parsed = JSON.parse(payload); } catch { parsed = null; }

    if (topic === "security/alert") {
        els.alertPanel.classList.add("active");
        setTimeout(() => els.alertPanel.classList.remove("active"), 1500);

        els.alertNone.classList.add("hidden");
        els.alertContent.classList.remove("hidden");
        els.alertEvent.textContent  = (parsed && parsed.event) || "alert";
        els.alertDevice.textContent = (parsed && parsed.device_id) || "";
        els.alertTime.textContent   = formatTime(timestamp);

        // Sleep Guard: still ack so the firmware can release its 30 s
        // active window (and eventually deep-sleep if ALWAYS_ON=0). The
        // camera panel is not touched here on purpose — the user-driven
        // stream toggle owns that element, so PIR alerts never interfere
        // with whatever the user has chosen to display.
        sendAck();
    }

    if (topic === "security/status" && parsed) {
        if (parsed.status !== undefined) {
            els.devStatus.textContent = parsed.status;
            applyStatusColor(els.devStatus, parsed.status);
        }
        if (parsed.rssi !== undefined) {
            els.devRssi.textContent = `${parsed.rssi} dBm`;
            applyRssiColor(els.devRssi, parsed.rssi);
        }
        if (parsed.uptime !== undefined) els.devUptime.textContent = `${parsed.uptime} s`;
        els.devUpdated.textContent = formatTime(timestamp);
    }

    // Prepend to log table (matches descending id order of /api/events).
    const row = renderEventRow({
        id: "",
        timestamp,
        topic,
        payload,
    });
    els.eventBody.prepend(row);
    // Trim the table so it doesn't grow without bound.
    while (els.eventBody.children.length > 100) {
        els.eventBody.lastElementChild.remove();
    }
}

function openSocket() {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/ws`;
    const ws = new WebSocket(url);

    ws.addEventListener("open", () => {
        setDot(els.wsDot, true);
        els.wsLabel.textContent = "WebSocket: connected";
    });

    ws.addEventListener("message", (e) => {
        try {
            const data = JSON.parse(e.data);
            // Clip readiness messages are consumed by the dedicated /clips.html
            // page; ignore them here rather than render them in the event log.
            if (data.type === "clip_ready") return;
            handleMqttMessage(data.topic, data.payload, data.timestamp);
        } catch (err) {
            console.warn("bad ws frame:", err);
        }
    });

    ws.addEventListener("close", () => {
        setDot(els.wsDot, false);
        els.wsLabel.textContent = "WebSocket: reconnecting...";
        setTimeout(openSocket, 2000);
    });

    ws.addEventListener("error", () => {
        ws.close();
    });
}

// ---------------------------------------------------------------
// Theme toggle wiring
// ---------------------------------------------------------------
(function wireThemeToggle() {
    const btn = document.getElementById("theme-toggle");
    if (!btn) return;
    // Label reflects the theme the user will switch TO.
    btn.textContent = isDark() ? "Light" : "Dark";
    btn.addEventListener("click", () => {
        setTheme(isDark() ? "light" : "dark");
    });
})();

// ---------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------
loadEvents();
loadClipConfig();
// Restore cross-navigation state (stream running, save recording, PIR
// armed) before first paint so the user sees the same page they left.
restorePirState();
restoreStreamState();
pollHealth();
setInterval(pollHealth, 5000);
openSocket();
