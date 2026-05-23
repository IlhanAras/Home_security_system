"""
Hybrid IoT Security System - FastAPI backend.

Responsibilities:
    * Bridge the Mosquitto MQTT broker to the browser via WebSocket.
    * Log every incoming MQTT event into a local SQLite database.
    * Expose REST endpoints for command dispatch and event retrieval.
    * Serve the static dashboard (HTML/CSS/JS) from ./static.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import aiomqtt
import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_CLIENT_ID = "dashboard-backend"

TOPIC_ALERT = "security/alert"
TOPIC_STATUS = "security/status"
TOPIC_COMMAND = "security/command"
TOPIC_ACK = "security/ack"

DEVICE_ID = "esp32-cam-01"

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "events.db"
STATIC_DIR = BASE_DIR / "static"

# Motion-triggered clip recording: on every `security/alert` we pull N frames
# from the ESP32's /capture endpoint and store them as JPEG blobs in SQLite.
# Playback is a multipart/x-mixed-replace stream at the same frame rate, so
# the browser renders the clip inside a plain <img>.
#
# These are runtime-editable via POST /api/clip-config so the dashboard can
# tune recording length / framerate / JPEG quality without re-flashing.
# `quality` is additionally propagated to the firmware over MQTT so future
# /capture calls return JPEGs at the new compression level.
CLIP_DEFAULT_DURATION_S = 5
CLIP_DEFAULT_FPS        = 2
CLIP_DEFAULT_QUALITY    = 16
CLIP_DEFAULT_FRAMESIZE  = 9   # SVGA — framesize_t enum: QVGA=5, CIF=6, VGA=8, SVGA=9, XGA=10, HD=12, SXGA=13, UXGA=15

clip_config = {
    "duration_s": CLIP_DEFAULT_DURATION_S,
    "fps":        CLIP_DEFAULT_FPS,
    "quality":    CLIP_DEFAULT_QUALITY,
    "framesize":  CLIP_DEFAULT_FRAMESIZE,
}

VALID_FRAMESIZES = {5, 6, 8, 9, 10, 12, 13, 15}

# Mirror of the PIR-armed state on the device. We flip this whenever the
# dashboard issues a pir_on/pir_off command so pages reloaded mid-session
# can read the current state back via /api/health instead of resetting the
# toggle button to its default. Not 100% authoritative (firmware resets on
# boot), but close enough for the lab demo.
pir_enabled = True


# ESP32-CAM host (IP or hostname). Set either automatically by parsing the
# retained `security/status` payload (firmware publishes its own IP there),
# or manually via POST /api/config when the firmware build predates that
# feature. The dashboard no longer talks to the ESP32 directly; all camera
# traffic is proxied through this backend so a single public URL (ngrok)
# is enough.
esp32_host: str | None = None


def _auto_discover_host(payload: str) -> None:
    """Sync esp32_host + camera settings from a security/status message.

    Because the status topic is retained, a fresh backend subscription
    receives the last value immediately and can populate esp32_host
    without waiting for the next heartbeat or a manual POST /api/config.
    Also syncs quality, framesize and pir_enabled from the firmware so
    the backend stays in agreement after restarts on either side.
    """
    global esp32_host, pir_enabled
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    ip = data.get("ip")
    if isinstance(ip, str) and ip.strip():
        ip = ip.strip()
        if ip != esp32_host:
            esp32_host = ip
            print(f"[discover] esp32_host <- {ip} (from security/status)")
    # Sync camera settings so clip_config matches what firmware is actually
    # using. Especially important after a backend restart where clip_config
    # reloads from SQLite but the firmware may have been changed in between.
    synced = []
    fw_quality = data.get("quality")
    if isinstance(fw_quality, int) and 10 <= fw_quality <= 63:
        if clip_config["quality"] != fw_quality:
            clip_config["quality"] = fw_quality
            synced.append(f"quality={fw_quality}")
    fw_framesize = data.get("framesize")
    if isinstance(fw_framesize, int) and fw_framesize in VALID_FRAMESIZES:
        if clip_config["framesize"] != fw_framesize:
            clip_config["framesize"] = fw_framesize
            synced.append(f"framesize={fw_framesize}")
    if synced:
        _save_clip_config()
        print(f"[discover] clip_config synced: {', '.join(synced)}")
    fw_pir = data.get("pir_enabled")
    if isinstance(fw_pir, bool):
        pir_enabled = fw_pir


# ---------------------------------------------------------------------------
# SQLite event log
# ---------------------------------------------------------------------------
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                topic     TEXT    NOT NULL,
                payload   TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clips (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    INTEGER,
                timestamp   TEXT    NOT NULL,
                frame_count INTEGER NOT NULL DEFAULT 0,
                complete    INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clip_frames (
                clip_id INTEGER NOT NULL,
                seq     INTEGER NOT NULL,
                jpeg    BLOB    NOT NULL,
                PRIMARY KEY (clip_id, seq)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # Lightweight migration: older DBs don't have `favorite` / `fps` on
        # clips. `fps` defaults to CLIP_DEFAULT_FPS so legacy rows still play
        # back at a reasonable rate; new recordings overwrite it.
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(clips)").fetchall()
        }
        if "favorite" not in existing_cols:
            conn.execute(
                "ALTER TABLE clips ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0"
            )
        if "fps" not in existing_cols:
            conn.execute(
                f"ALTER TABLE clips ADD COLUMN fps REAL NOT NULL "
                f"DEFAULT {CLIP_DEFAULT_FPS}"
            )
        conn.commit()


def _load_clip_config() -> None:
    """Restore clip_config from SQLite so settings survive backend restarts."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'clip_config'"
        ).fetchone()
    if not row:
        return
    try:
        saved = json.loads(row[0])
    except (ValueError, TypeError):
        return
    for k in ("duration_s", "fps", "quality", "framesize"):
        if k in saved:
            clip_config[k] = saved[k]


def _save_clip_config() -> None:
    """Persist current clip_config to SQLite."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            ("clip_config", json.dumps(clip_config)),
        )
        conn.commit()


def log_event(topic: str, payload: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO events (timestamp, topic, payload) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), topic, payload),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_events(limit: int = 100) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, topic, payload "
            "FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"id": r[0], "timestamp": r[1], "topic": r[2], "payload": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Clip storage helpers (SQLite BLOB-backed)
# ---------------------------------------------------------------------------
def create_clip(event_id: int | None) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO clips (event_id, timestamp) VALUES (?, ?)",
            (event_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return int(cur.lastrowid)


def save_clip_frame(clip_id: int, seq: int, jpeg: bytes) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO clip_frames (clip_id, seq, jpeg) VALUES (?, ?, ?)",
            (clip_id, seq, jpeg),
        )
        conn.commit()


def finalize_clip(clip_id: int, frame_count: int, fps: float) -> None:
    # `fps` is the *actual* recording rate: clip_config.fps for PIR clips, or
    # stored/elapsed for stream-save. Playback uses this so a clip recorded
    # at 10 fps doesn't get replayed at whatever rate clip_config happens to
    # be set to right now.
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE clips SET frame_count = ?, complete = 1, fps = ? "
            "WHERE id = ?",
            (frame_count, fps, clip_id),
        )
        conn.commit()


def list_clips(limit: int = 50, favorites_only: bool = False) -> list[dict]:
    # `frame_count > 0` hides clips that finished with zero frames — usually
    # the ESP32 was unreachable (Wi-Fi flap) during the entire 5 s window.
    # Recording them would clutter the UI with unplayable rows that 404 on
    # click. New recordings with stored=0 are deleted at record_clip end;
    # this filter also hides any legacy rows that slipped in before that.
    sql = (
        "SELECT id, event_id, timestamp, frame_count, favorite, fps "
        "FROM clips WHERE complete = 1 AND frame_count > 0"
    )
    params: list = []
    if favorites_only:
        sql += " AND favorite = 1"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id":          r[0],
            "event_id":    r[1],
            "timestamp":   r[2],
            "frame_count": r[3],
            "favorite":    bool(r[4]),
            "fps":         float(r[5]) if r[5] else float(CLIP_DEFAULT_FPS),
        }
        for r in rows
    ]


def get_clip_meta(clip_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, event_id, timestamp, frame_count, favorite, fps "
            "FROM clips WHERE id = ? AND complete = 1 AND frame_count > 0",
            (clip_id,),
        ).fetchone()
    if not row:
        return None
    fps = float(row[5]) if row[5] else float(CLIP_DEFAULT_FPS)
    frame_count = row[3]
    return {
        "id":          row[0],
        "event_id":    row[1],
        "timestamp":   row[2],
        "frame_count": frame_count,
        "favorite":    bool(row[4]),
        "fps":         fps,
        "duration_s":  frame_count / fps if fps > 0 else 0.0,
    }


def get_clip_frame(clip_id: int, seq: int) -> bytes | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT jpeg FROM clip_frames WHERE clip_id = ? AND seq = ?",
            (clip_id, seq),
        ).fetchone()
    return row[0] if row else None


def set_clip_favorite(clip_id: int, favorite: bool) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE clips SET favorite = ? WHERE id = ?",
            (1 if favorite else 0, clip_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_clip(clip_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM clip_frames WHERE clip_id = ?", (clip_id,))
        cur = conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        conn.commit()
        return cur.rowcount > 0


def delete_clips_bulk(ids: list[int]) -> int:
    if not ids:
        return 0
    with sqlite3.connect(DB_PATH) as conn:
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"DELETE FROM clip_frames WHERE clip_id IN ({placeholders})",
            ids,
        )
        cur = conn.execute(
            f"DELETE FROM clips WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        return cur.rowcount


def get_clip_frames(clip_id: int) -> list[bytes]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT jpeg FROM clip_frames WHERE clip_id = ? ORDER BY seq ASC",
            (clip_id,),
        ).fetchall()
    return [r[0] for r in rows]


# Only one recording is allowed to run at a time; the ESP32's esp_http_server
# has a limited socket pool and back-to-back PIR pulses could otherwise stack
# overlapping /capture polls.
recording_lock = asyncio.Lock()


async def record_clip(event_id: int) -> None:
    host = esp32_host
    if not host:
        print(f"[clip] skipped for event {event_id}: no esp32_host")
        return
    if recording_lock.locked():
        print(f"[clip] skipped for event {event_id}: another clip in progress")
        return

    async with recording_lock:
        clip_id = create_clip(event_id)
        duration_s = clip_config["duration_s"]
        fps = clip_config["fps"]
        total_frames = max(1, int(duration_s * fps))
        print(f"[clip {clip_id}] recording {total_frames} frames "
              f"({duration_s}s @ {fps}fps) from {host}")

        interval = 1.0 / fps
        stored = 0
        loop = asyncio.get_event_loop()

        async with httpx.AsyncClient(timeout=3.0) as client:
            for attempt in range(total_frames):
                tick = loop.time()
                try:
                    r = await client.get(f"http://{host}/capture")
                    if r.status_code == 200 and r.content:
                        save_clip_frame(clip_id, stored, r.content)
                        stored += 1
                except httpx.HTTPError as exc:
                    print(f"[clip {clip_id}] frame {attempt} failed: "
                          f"{type(exc).__name__}: {exc}")
                remaining = interval - (loop.time() - tick)
                if remaining > 0:
                    await asyncio.sleep(remaining)

        if stored == 0:
            # ESP32 was unreachable for the entire window. An empty clip is
            # unplayable, so drop the row rather than litter the UI.
            delete_clip(clip_id)
            print(f"[clip {clip_id}] abandoned: 0/{total_frames} frames "
                  f"(esp32 unreachable?)")
            return

        finalize_clip(clip_id, stored, float(fps))
        print(f"[clip {clip_id}] done: {stored}/{total_frames} frames @ {fps} fps")

        await manager.broadcast({
            "type":        "clip_ready",
            "clip_id":     clip_id,
            "event_id":    event_id,
            "frame_count": stored,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })


# ---------------------------------------------------------------------------
# WebSocket fan-out
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# MQTT client lifecycle
# ---------------------------------------------------------------------------
# Kept as a module-level handle so REST endpoints can publish without
# coordinating queues. Becomes None while the broker is unreachable.
mqtt_client: aiomqtt.Client | None = None


async def mqtt_loop() -> None:
    global mqtt_client
    while True:
        try:
            async with aiomqtt.Client(
                hostname=MQTT_HOST,
                port=MQTT_PORT,
                identifier=MQTT_CLIENT_ID,
            ) as client:
                mqtt_client = client
                await client.subscribe(TOPIC_ALERT)
                await client.subscribe(TOPIC_STATUS)
                print(f"[MQTT] connected to {MQTT_HOST}:{MQTT_PORT}")

                async for msg in client.messages:
                    topic = str(msg.topic)
                    payload = msg.payload.decode("utf-8", errors="replace")
                    event_id = log_event(topic, payload)
                    if topic == TOPIC_STATUS:
                        _auto_discover_host(payload)
                    if topic == TOPIC_ALERT:
                        # Fire-and-forget: recording runs alongside MQTT loop
                        # so it doesn't block subsequent messages.
                        asyncio.create_task(record_clip(event_id))
                    await manager.broadcast(
                        {
                            "topic": topic,
                            "payload": payload,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
        except aiomqtt.MqttError as exc:
            print(f"[MQTT] connection lost: {exc} -- retrying in 3s")
            mqtt_client = None
            await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _load_clip_config()
    task = asyncio.create_task(mqtt_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    lifespan=lifespan,
    title="Hybrid IoT Security Dashboard",
    version="0.1.0",
)


class CommandRequest(BaseModel):
    command: Literal["led_on", "led_off", "capture", "pir_on", "pir_off"]


class AckRequest(BaseModel):
    device_id: str = DEVICE_ID


class ConfigRequest(BaseModel):
    esp32_host: str


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "mqtt_connected":    mqtt_client is not None,
        "broker":            f"{MQTT_HOST}:{MQTT_PORT}",
        "pir_enabled":       pir_enabled,
        "stream_recording":  stream_recorder["active"],
        "stream_clip_id":    stream_recorder["clip_id"],
    }


@app.get("/api/events")
async def api_events(limit: int = 100) -> dict:
    return {"events": get_events(limit)}


@app.get("/api/clips")
async def api_clips(limit: int = 50, favorites: bool = False) -> dict:
    return {"clips": list_clips(limit, favorites_only=favorites)}


# ---------------------------------------------------------------------------
# Clip configuration (runtime-editable from the dashboard)
# ---------------------------------------------------------------------------
class ClipConfigRequest(BaseModel):
    duration_s: int | None = None
    fps:        int | None = None
    quality:    int | None = None
    framesize:  int | None = None


@app.get("/api/clip-config")
async def api_clip_config_get() -> dict:
    return dict(clip_config)


@app.post("/api/clip-config")
async def api_clip_config_set(req: ClipConfigRequest) -> dict:
    # Reject camera-level changes while any recording is in progress so a
    # single clip never contains mixed resolution/quality frames.
    camera_change = req.quality is not None or req.framesize is not None
    if camera_change and (stream_recorder["active"] or recording_lock.locked()):
        raise HTTPException(
            status_code=409,
            detail="Cannot change camera settings while a recording is in progress",
        )
    changed = {}
    if req.duration_s is not None:
        clip_config["duration_s"] = max(1, min(60, req.duration_s))
        changed["duration_s"] = clip_config["duration_s"]
    if req.fps is not None:
        clip_config["fps"] = max(1, min(10, req.fps))
        changed["fps"] = clip_config["fps"]
    if req.quality is not None:
        q = max(10, min(63, req.quality))
        clip_config["quality"] = q
        changed["quality"] = q
    if req.framesize is not None:
        fs = req.framesize
        if fs not in VALID_FRAMESIZES:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid framesize {fs}; valid: {sorted(VALID_FRAMESIZES)}",
            )
        clip_config["framesize"] = fs
        changed["framesize"] = fs
    if mqtt_client is not None:
        if "quality" in changed:
            await mqtt_client.publish(
                TOPIC_COMMAND,
                json.dumps({"command": "cam_quality", "value": clip_config["quality"]}),
            )
        if "framesize" in changed:
            await mqtt_client.publish(
                TOPIC_COMMAND,
                json.dumps({"command": "cam_framesize", "value": clip_config["framesize"]}),
            )
    if changed:
        _save_clip_config()
    return {"ok": True, "clip_config": dict(clip_config), "changed": changed}


# ---------------------------------------------------------------------------
# Stream recording — "Save this live stream" in the dashboard.
#
# Instead of hitting the ESP32 with a second /capture polling session (which
# contends with the user's live /stream on the ESP32's limited socket pool),
# we tap the multipart bytes already flowing through /api/stream. While a
# recording is active, every JPEG extracted from the relay is inserted as a
# clip_frames row. This keeps ESP32 load identical to plain streaming.
# ---------------------------------------------------------------------------
stream_recorder = {
    "active":     False,
    "clip_id":    None,
    "seq":        0,
    "started_at": None,  # time.monotonic() at start; used to compute real fps
}


def _ensure_not_recording() -> None:
    if stream_recorder["active"]:
        raise HTTPException(status_code=409, detail="stream recording already active")


@app.post("/api/stream-record/start")
async def api_stream_record_start() -> dict:
    _ensure_not_recording()
    clip_id = create_clip(event_id=None)
    stream_recorder["clip_id"]    = clip_id
    stream_recorder["seq"]        = 0
    stream_recorder["active"]     = True
    stream_recorder["started_at"] = time.monotonic()
    print(f"[stream-rec {clip_id}] started")
    return {"ok": True, "clip_id": clip_id}


@app.post("/api/stream-record/stop")
async def api_stream_record_stop() -> dict:
    if not stream_recorder["active"]:
        raise HTTPException(status_code=409, detail="no recording in progress")
    clip_id = stream_recorder["clip_id"]
    stored  = stream_recorder["seq"]
    # Real fps for stream-save = frames / wall-clock elapsed. Playback uses
    # this so a 5 s recording plays in 5 s, regardless of whatever rate the
    # ESP32's /stream happened to emit at.
    started_at = stream_recorder["started_at"]
    elapsed = max(0.01, time.monotonic() - started_at) if started_at else 0.01
    stream_recorder["active"]     = False
    stream_recorder["clip_id"]    = None
    stream_recorder["started_at"] = None

    if stored == 0:
        delete_clip(clip_id)
        print(f"[stream-rec {clip_id}] stopped with 0 frames — dropped")
        return {"ok": True, "clip_id": clip_id, "frame_count": 0, "discarded": True}

    fps = stored / elapsed
    finalize_clip(clip_id, stored, fps)
    print(f"[stream-rec {clip_id}] stopped: {stored} frames in {elapsed:.2f}s "
          f"(~{fps:.2f} fps)")
    await manager.broadcast({
        "type":        "clip_ready",
        "clip_id":     clip_id,
        "event_id":    None,
        "frame_count": stored,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True, "clip_id": clip_id, "frame_count": stored}


class _JpegExtractor:
    """Accumulates raw multipart bytes and yields complete JPEG frames.

    We lean on JPEG framing markers (FF D8 ... FF D9) directly so the parser
    stays oblivious to whatever boundary/headers the ESP32's esp_http_server
    chose to emit.
    """

    SOI = b"\xff\xd8\xff"
    EOI = b"\xff\xd9"

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buf.extend(chunk)
        frames: list[bytes] = []
        while True:
            start = self.buf.find(self.SOI)
            if start < 0:
                self.buf.clear()
                break
            end = self.buf.find(self.EOI, start + 3)
            if end < 0:
                # Incomplete JPEG — keep what we have for the next chunk.
                del self.buf[:start]
                break
            end += 2
            frames.append(bytes(self.buf[start:end]))
            del self.buf[:end]
        return frames


class FavoriteRequest(BaseModel):
    favorite: bool


class BulkDeleteRequest(BaseModel):
    ids: list[int]


@app.post("/api/clips/bulk-delete")
async def api_clips_bulk_delete(req: BulkDeleteRequest) -> dict:
    # Declared before the `{clip_id}` routes so "bulk-delete" never gets
    # interpreted as a clip id. FastAPI matches routes in declaration order.
    deleted = delete_clips_bulk(req.ids)
    return {"ok": True, "deleted": deleted}


@app.get("/api/clips/{clip_id}/meta")
async def api_clip_meta(clip_id: int) -> dict:
    meta = get_clip_meta(clip_id)
    if not meta:
        raise HTTPException(status_code=404, detail="clip not found")
    return meta


@app.get("/api/clips/{clip_id}/frame/{seq}")
async def api_clip_frame(clip_id: int, seq: int) -> Response:
    # Frames are immutable once a clip is finalized, so let the browser (and
    # any intermediary) cache them aggressively — this is what keeps the
    # custom scrubbing player snappy on re-seeks.
    jpeg = get_clip_frame(clip_id, seq)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="frame not found")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@app.post("/api/clips/{clip_id}/favorite")
async def api_clip_favorite(clip_id: int, req: FavoriteRequest) -> dict:
    if not set_clip_favorite(clip_id, req.favorite):
        raise HTTPException(status_code=404, detail="clip not found")
    return {"ok": True, "clip_id": clip_id, "favorite": req.favorite}


@app.delete("/api/clips/{clip_id}")
async def api_clip_delete(clip_id: int) -> dict:
    if not delete_clip(clip_id):
        raise HTTPException(status_code=404, detail="clip not found")
    return {"ok": True, "clip_id": clip_id}


@app.get("/api/clips/{clip_id}")
async def api_clip_playback(clip_id: int) -> StreamingResponse:
    frames = get_clip_frames(clip_id)
    if not frames:
        raise HTTPException(status_code=404, detail="clip not found")

    # Playback at the clip's *own* recorded fps so a 5 s clip replays in 5 s
    # regardless of what clip_config.fps is currently set to. The custom
    # player in clips.html uses /frame/{seq} instead, but this multipart
    # path is kept for direct-url consumption.
    meta = get_clip_meta(clip_id)
    fps = meta["fps"] if meta else float(CLIP_DEFAULT_FPS)
    interval = 1.0 / max(fps, 0.1)
    boundary = "clipboundary"

    async def generate():
        for jpeg in frames:
            part = (
                f"\r\n--{boundary}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpeg)}\r\n\r\n"
            ).encode("ascii") + jpeg
            yield part
            await asyncio.sleep(interval)

    return StreamingResponse(
        generate(),
        media_type=f"multipart/x-mixed-replace;boundary={boundary}",
    )


@app.post("/api/command")
async def api_command(req: CommandRequest) -> dict:
    global pir_enabled
    if mqtt_client is None:
        raise HTTPException(status_code=503, detail="MQTT broker not connected")
    payload = json.dumps({"command": req.command})
    await mqtt_client.publish(TOPIC_COMMAND, payload)
    # Remember the PIR arm state so pages reloaded later can sync their
    # toggle button without guessing.
    if req.command == "pir_on":
        pir_enabled = True
    elif req.command == "pir_off":
        pir_enabled = False
    return {"ok": True, "topic": TOPIC_COMMAND, "payload": payload}


@app.post("/api/ack")
async def api_ack(req: AckRequest) -> dict:
    if mqtt_client is None:
        raise HTTPException(status_code=503, detail="MQTT broker not connected")
    payload = json.dumps({"ack": "image_received", "device_id": req.device_id})
    await mqtt_client.publish(TOPIC_ACK, payload)
    return {"ok": True, "topic": TOPIC_ACK, "payload": payload}


# ---------------------------------------------------------------------------
# ESP32-CAM proxy endpoints
#
# The browser hits /api/capture and /api/stream on the backend; the backend
# opens an internal HTTP connection to the ESP32 and relays the response.
# This way the ESP32 stays on the local LAN while the dashboard can be
# exposed publicly (ngrok, Cloudflare Tunnel, etc.) via a single URL.
#
# NOTE: only POST /api/config exists — no GET — so the public ngrok URL
# cannot be used to read back the LAN IP. Configure the host from the
# local machine, e.g.:
#   curl -X POST http://127.0.0.1:8000/api/config \
#        -H "Content-Type: application/json" \
#        -d '{"esp32_host":"10.x.y.z"}'
# ---------------------------------------------------------------------------
@app.post("/api/config")
async def api_config_set(req: ConfigRequest) -> dict:
    global esp32_host
    esp32_host = req.esp32_host.strip() or None
    return {"ok": True, "esp32_host": esp32_host}


def _require_host() -> str:
    if not esp32_host:
        raise HTTPException(
            status_code=503,
            detail="ESP32 host not configured; POST to /api/config first.",
        )
    return esp32_host


@app.get("/api/capture")
async def api_capture() -> Response:
    host = _require_host()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"http://{host}/capture")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"ESP32 unreachable: {exc}")
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/stream")
async def api_stream() -> StreamingResponse:
    host = _require_host()
    # Stream reads must be unbounded (MJPEG is long-lived), but the
    # connect/write phases should still time out so a dead ESP32 doesn't
    # hang the worker.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0),
    )

    async def relay():
        extractor = _JpegExtractor()
        try:
            async with client.stream("GET", f"http://{host}/stream") as upstream:
                async for chunk in upstream.aiter_raw():
                    # Always forward the raw bytes to the browser first so
                    # live playback is never gated on recorder bookkeeping.
                    yield chunk
                    # If a stream recording is in progress, pluck any
                    # completed JPEG frames out of the same byte stream.
                    if stream_recorder["active"]:
                        for jpeg in extractor.feed(chunk):
                            save_clip_frame(
                                stream_recorder["clip_id"],
                                stream_recorder["seq"],
                                jpeg,
                            )
                            stream_recorder["seq"] += 1
        except (httpx.HTTPError, ConnectionError) as exc:
            # Upstream closed mid-stream or refused the connection. Normal
            # over mobile/ngrok when the client tears down aggressively; no
            # need to crash the ASGI handler with a traceback.
            print(f"[stream] upstream ended: {type(exc).__name__}: {exc}")
        finally:
            await client.aclose()

    # The ESP32's firmware pins the boundary to "frameboundary" in
    # firmware.ino (STREAM_PART_BOUNDARY). Hardcode it here rather than
    # race the upstream Content-Type header.
    return StreamingResponse(
        relay(),
        media_type="multipart/x-mixed-replace;boundary=frameboundary",
    )


@app.get("/api/device-status")
async def api_device_status() -> Response:
    host = _require_host()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://{host}/status")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"ESP32 unreachable: {exc}")
    return Response(content=r.content, media_type="application/json")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            # The dashboard has nothing to send; receive_text blocks until
            # the client disconnects, which raises WebSocketDisconnect.
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# Serve the static dashboard. Mount last so API routes take precedence.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
