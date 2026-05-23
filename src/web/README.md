# Web Dashboard - Backend (FastAPI)

Bridges the Mosquitto MQTT broker to the browser and exposes a small REST API
for the dashboard.

## Endpoints

| Method | Path                          | Purpose                                                                                  |
| ------ | ----------------------------- | ---------------------------------------------------------------------------------------- |
| GET    | `/api/health`                 | `{ok, mqtt_connected, broker, pir_enabled, stream_recording, stream_clip_id}`            |
| GET    | `/api/events`                 | Most recent logged events (default 100)                                                  |
| POST   | `/api/command`                | Publish to `security/command`: `led_on`, `led_off`, `capture`, `pir_on`, `pir_off`, `cam_quality`, `cam_framesize` |
| POST   | `/api/ack`                    | Publish image-received ack to `security/ack`                                             |
| POST   | `/api/config`                 | Override the in-memory ESP32 LAN IP (manual fallback — auto-discovery is default)        |
| GET    | `/api/capture`                | JPEG from `http://<esp32>/capture` (backend proxy)                                       |
| GET    | `/api/stream`                 | MJPEG from `http://<esp32>/stream` (backend proxy) **+** tees frames into stream-save    |
| GET    | `/api/device-status`          | JSON from `http://<esp32>/status` (backend proxy)                                        |
| GET    | `/api/clips`                  | JSON list; `?favorites=true` filters to starred; always filters `frame_count > 0`; each row includes its recorded `fps` |
| GET    | `/api/clips/{id}`             | Multipart playback at the clip's *recorded* fps (kept for direct-URL use)                |
| GET    | `/api/clips/{id}/meta`        | `{id, event_id, timestamp, frame_count, favorite, fps, duration_s}` for the custom JS player |
| GET    | `/api/clips/{id}/frame/{seq}` | Single JPEG for frame `seq`; `Cache-Control: public, max-age=604800, immutable`          |
| POST   | `/api/clips/{id}/favorite`    | Body `{favorite: bool}` → ★ toggle                                                       |
| DELETE | `/api/clips/{id}`             | Hard-delete clip row + all its frames (`DELETE FROM clip_frames` + `DELETE FROM clips`)  |
| POST   | `/api/clips/bulk-delete`      | Body `{ids:[...]}` → hard-delete every listed clip + its frames in one round-trip        |
| GET    | `/api/clip-config`            | Current `{duration_s, fps, quality, framesize}` for camera + clip recording              |
| POST   | `/api/clip-config`            | Update any subset (clamped). `quality` + `framesize` forwarded to firmware via MQTT. Returns 409 while recording is active (for quality/framesize changes) |
| POST   | `/api/stream-record/start`    | Begin saving whatever's flowing through `/api/stream`; returns `{clip_id}`               |
| POST   | `/api/stream-record/stop`     | Finalize the save; empty captures are dropped instead of saved                           |
| WS     | `/ws`                         | MQTT messages + `{type:"clip_ready"}` pseudo-events fanned out to browsers               |
| GET    | `/`                           | Dashboard (`static/index.html`)                                                          |
| GET    | `/clips.html`                 | Motion-clips page (`static/clips.html`)                                                  |

### Auto-discovery vs. manual config

The backend populates `esp32_host` automatically by parsing every
retained `security/status` payload for an `ip` field. A fresh backend
subscription receives the last known status on subscribe (because the
topic is retained), so the camera usually just works after a restart
with no manual step.

`POST /api/config` is kept as a manual override / fallback for when
pre-auto-discovery firmware is flashed or the retained status has been
cleared. There is **no `GET /api/config`** on purpose — the proxy
endpoints are the only way the outside world touches the ESP32, and the
LAN IP is never exposed through the dashboard:

```bat
curl -X POST http://127.0.0.1:8000/api/config ^
     -H "Content-Type: application/json" ^
     -d "{\"esp32_host\":\"10.225.49.95\"}"
```

`esp32_host` is always in-memory; re-run the curl after every backend
restart if you're on the manual path.

### Motion clips (PIR-triggered)

On every `security/alert` the backend spawns a fire-and-forget
`record_clip(event_id)` task that polls `http://<esp32_host>/capture`
at the currently configured fps for the currently configured duration,
and stores each JPEG as a BLOB row in `clip_frames`.

Defaults: 5 s @ 2 fps = 10 frames. All four settings (duration, fps,
quality, framesize) are **runtime-editable** through `GET/POST
/api/clip-config`. Changes apply to the *next* alert. `quality` and
`framesize` are forwarded to the firmware over MQTT (`cam_quality` /
`cam_framesize` commands) so all subsequent captures — not just clip
recording — use the new camera settings. Quality/framesize changes are
rejected (409) while any recording is in progress to prevent
mixed-resolution frames.

An `asyncio.Lock` prevents overlapping recordings from back-to-back
PIR pulses. Empty captures (Wi-Fi flap, all frame GETs failed) delete
the clip row instead of finalizing; `list_clips` also filters
`frame_count > 0` so only playable clips reach the UI.

When recording succeeds, the backend broadcasts
`{"type":"clip_ready", clip_id, event_id, frame_count, timestamp}` on
`/ws`. `clips.html` listens for it and refreshes its list live.

Each recording persists its **own** fps into the `clips` row at finalize
time — PIR clips store `clip_config.fps` as captured, so tuning
`clip_config` afterwards doesn't warp existing clips. `GET
/api/clips/{id}/meta` returns that per-clip rate; `GET
/api/clips/{id}/frame/{seq}` serves individual frames with
`Cache-Control: immutable`, which is what the custom in-browser player
uses to implement play/pause + scrubbing. The older multipart
`/api/clips/{id}` endpoint is still wired up and now also paces at the
clip's recorded fps, but the main UI no longer uses it.

### Stream-save (user-triggered)

Separate pipeline for "save what I'm watching". Triggered by the
dashboard's Save-stream button:

- `POST /api/stream-record/start` sets `stream_recorder["active"] = true`
  and creates a new clip row.
- The `/api/stream` relay, while forwarding upstream chunks to the
  browser, additionally feeds each chunk into `_JpegExtractor`. That
  class scans for JPEG SOI/EOI markers (`FF D8 FF ... FF D9`) across
  chunk boundaries and emits complete frames as BLOBs to `clip_frames`.
- `POST /api/stream-record/stop` flips the flag off, finalizes the
  clip, and broadcasts `clip_ready`. The finalize writes a measured fps
  (`frames_extracted / monotonic_elapsed`) into the row so the custom
  player can replay the capture in real time — a 10 s session plays in
  10 s even though nothing paces the ESP32's `/stream` emitter.

Key properties:
- **No extra ESP32 load** — the recorder taps bytes that were already
  flowing through the proxy. There's no second `/capture` polling loop.
- **Unbounded duration** — streams can be minutes long; the user stops
  when they want.
- **Navigation-safe** — `stream_recorder` is a backend module dict, so
  nav-away + nav-back reopens `/api/stream` and keeps feeding the same
  `clip_id` with an incrementing `seq` counter. The gap while the page
  was elsewhere has no frames, which is semantically correct.

### PIR arm/disarm and cross-navigation state

`/api/command` with `pir_on` / `pir_off` publishes to the firmware **and**
updates the backend's own `pir_enabled` mirror. That mirror is exposed in
`/api/health` so tabs reloaded later (e.g. after a Clips navigation) can
sync their PIR toggle button without guessing.

The backend does **not** persist session state to disk. Frontend uses
`sessionStorage` to remember "is this tab streaming / saving / what
filter on the clips page" across same-tab navigation. Backend-owned
state (`pir_enabled`, `stream_recorder["active"]`) is authoritative on
reload — the frontend queries `/api/health` and reconciles.

### SQLite schema

Four tables back the backend:

| Table | Columns | Notes |
| ----- | ------- | ----- |
| `events`      | `id, timestamp, topic, payload` | every MQTT message the backend sees |
| `clips`       | `id, event_id, timestamp, frame_count, complete, favorite, fps` | one row per PIR-triggered clip or stream-save. `fps` is the *recorded* rate (PIR: `clip_config.fps` at capture; stream-save: `stored/elapsed`) and drives playback |
| `clip_frames` | `clip_id, seq, jpeg BLOB` | per-frame JPEG payloads |
| `config`      | `key TEXT PK, value TEXT` | persists `clip_config` as JSON so settings survive backend restarts |

`init_db()` auto-adds missing columns with `ALTER TABLE` when an older
DB is opened: `favorite` (default 0) and `fps` (default
`CLIP_DEFAULT_FPS`). No manual migration needed.

Deletions (`DELETE /api/clips/{id}` and `POST /api/clips/bulk-delete`)
are **hard deletes** — rows in `clips` and every matching BLOB in
`clip_frames` are removed in the same transaction. There's no soft-
delete flag or trash/restore path. SQLite doesn't shrink the DB file
automatically; freed pages get reused by subsequent inserts. If a lot
of stream-save clips have been wiped and you want the file size back,
run `sqlite3 events.db "VACUUM;"` manually.

## Frontend

Two pages under `static/`, both styled with Tailwind + Inter (flat-2.0):

- `index.html` — main dashboard (alerts, camera, device status, event log).
  Script: `static/app.js`.
- `clips.html` — standalone motion-clips page. Self-contained: its
  `<script>` block is inline, does not depend on `app.js`.

Shared across both pages: `static/style.css` (minimal overrides) and
`localStorage["sec.theme"]` for theme persistence. Navigation between
them: header **Clips** link on the dashboard, header **Dashboard** link
on the clips page.

- **Tailwind CSS** is loaded from `cdn.tailwindcss.com` with
  `darkMode: "class"` and Inter set as the default `font-sans`.
- The **theme toggle** (top-right of each page) flips the `dark` class on
  `<html>` and persists the choice in `localStorage.sec.theme`. An inline
  script in `<head>` applies the saved theme before Tailwind paints,
  preventing a flash of wrong colors.
- `style.css` is intentionally small — only rules Tailwind utilities
  can't express directly: `.event-scroll` (20-row scroller for event log),
  `.clip-scroll` (same for clip list, slightly taller rows for ★/✕
  buttons), `.dot-on` / `.dot-off` backgrounds (with `!important` to
  beat Tailwind's late injection), the `#alert-panel.active` flash
  ring, and the `#cam-img` visibility rule.

### Dashboard (`index.html` + `app.js`)

- **Camera section** is stream-only now. A single **Start stream /
  Stop stream** toggle — no mode dropdown, no snapshot polling, no
  manual refresh button. The stream is **off by default**; pressing
  Start opens `/api/stream`.
- **Auto-disarm PIR on Start stream**: `startStream()` remembers the
  current `pirEnabled`, issues `pir_off` over MQTT, and flips the
  header pill to red. `stopStream()` restores the prior PIR state.
- **Save stream button** appears only while streaming. Toggles
  `POST /api/stream-record/start` / `/stop`. Backend taps the bytes
  already flowing through `/api/stream`, so saving is free on the
  ESP32 side. Button pulses red while recording.
- **PIR: ON / OFF** pill in the header issues `pir_on` / `pir_off`
  manually. Its initial state is synced with backend `/api/health.pir_enabled`.
- **Camera & Clip Settings** form (Resolution / Quality / Duration /
  FPS) POSTs to `/api/clip-config` on Apply. The backend clamps into
  safe ranges and returns the canonical values; the form re-syncs.
  Resolution and quality are forwarded to the firmware over MQTT;
  duration and FPS only affect PIR clip recording. If the stream is
  open, Apply auto-restarts it so the new resolution is visible
  instantly.
- **Device Status panel** colors values: online → green, offline →
  red. RSSI ≥ -60 green, -60..-75 amber, < -75 red. Applied by
  `applyStatusColor()` / `applyRssiColor()` helpers — old tier classes
  are stripped before each update so utilities don't accumulate.
- **Last alert + Device Status rehydration**: `loadEvents()` walks the
  returned events descending and populates both panels from the most
  recent `security/alert` and `security/status` payloads. Fixes the
  "I navigated to Clips and back — panels are empty" problem.
- **Cross-navigation state** is kept in `sessionStorage`:
  `sec.streaming`, `sec.saving`, `sec.pirEnabled`,
  `sec.pirBeforeStream`. On bootstrap, `restoreStreamState()` and
  `restorePirState()` reconcile with backend `/api/health` (which is
  authoritative for `pir_enabled` and `stream_recording`).

### Clips page (`clips.html`)

- Self-contained: inline `<script>` block, no `app.js` dependency.
- **Header is identical to the dashboard** (true shared layout): same
  `<h1>` ("Hybrid IoT Security"), Dashboard / Clips nav on the left,
  **PIR: ON / OFF** pill + WebSocket + Broker dots + theme toggle on the
  right. The PIR pill on this page mirrors the dashboard's
  implementation — `sessionStorage["sec.pirEnabled"]` for the initial
  paint, reconciled against `/api/health.pir_enabled` on each poll, and
  click publishes `pir_on` / `pir_off` via `POST /api/command`.
- Layout: **custom player** on the left (`<img id="clip-player">` +
  placeholder + play/pause button + range-input scrubber + `cur / total s`
  time label + now-playing caption), filter buttons + clip list on the
  right.
- **All / ★ Favorites filter** above the table. Selection persists in
  `sessionStorage["sec.clipFilter"]`.
- Table uses `.clip-scroll` (15 rows visible, sticky header, scroll
  for more). Each row has:
  - leftmost **select checkbox** (for bulk delete — see below)
  - click anywhere outside buttons → `playClip(clip)` which fetches
    `/api/clips/{id}/meta` and drives the custom player
  - **★ / ☆** toggle → `POST /api/clips/{id}/favorite`
  - **✕** delete → Turkish `confirm()` → `DELETE /api/clips/{id}`;
    resets player if the deleted clip was playing.
- **Bulk delete:** a tri-state select-all checkbox lives in the table
  header (supports the `indeterminate` middle state); ticking one or
  more rows reveals a red **"Delete selected (N)"** button next to the
  filter buttons. Confirming POSTs the full id list to
  `/api/clips/bulk-delete`, after which the selection clears and the
  list reloads. Filter changes (All ↔ Favorites) also clear the
  selection to avoid deleting rows the user can't currently see.
- **Custom video-style player:** JS state machine instead of the
  browser's `<video>` element because frames live as JPEG BLOBs in
  SQLite. Play/Pause toggles a `setInterval(tick, 1000 / fps)` that
  advances `playhead`; the scrubber `oninput` sets `playhead` directly;
  the `<img>` `src` follows `/api/clips/{id}/frame/{seq}`. Per-clip fps
  means 5 s recorded at 10 fps plays for 5 s, regardless of where
  `clip_config.fps` currently sits.
- Opens its own `/ws` and listens **only** for `{type:"clip_ready"}`
  messages — everything else is ignored. The dashboard does the
  inverse.

### Shared conventions

- `app.js` on the dashboard opens `/ws` and ignores `type:"clip_ready"`
  frames; `clips.html` opens its own `/ws` and ignores everything
  except `clip_ready`. Each page consumes exactly what it needs.
- No build step. Any edit to the static files is picked up on a plain
  browser refresh (the backend is just serving them).

## First-time setup

```bat
cd web
setup.bat
```

Creates `.venv` and installs `fastapi`, `uvicorn`, `aiomqtt`, `pydantic`.

## Run

```bat
cd web
run.bat
```

Then open <http://127.0.0.1:8000/api/health> — expect `{"ok":true,"mqtt_connected":true,...}`.

## Manual sanity check

With the broker running (`broker/run_broker.bat`) and this backend running:

```bat
REM Trigger a motion alert -- the backend logs it and fans it out to /ws
broker\pub_test_alert.bat

REM See it in the event log
curl http://127.0.0.1:8000/api/events

REM Send a command (publishes to security/command)
curl -X POST http://127.0.0.1:8000/api/command -H "Content-Type: application/json" -d "{\"command\":\"led_on\"}"
```
