# ESP32-CAM Firmware

Arduino sketch for the AI-Thinker ESP32-CAM running the security node.

## 1. Arduino IDE setup

1. Install **Arduino IDE 2.x**.
2. Preferences → *Additional boards manager URLs*:
   `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
3. Tools → Board → Boards Manager → install **esp32** by Espressif (3.x).
4. Library Manager → install **PubSubClient** by Nick O'Leary.

## 2. Board + upload settings

| Setting              | Value                                    |
| -------------------- | ---------------------------------------- |
| Board                | AI Thinker ESP32-CAM                     |
| CPU frequency        | 240 MHz                                  |
| Flash frequency      | 80 MHz                                   |
| Flash mode           | QIO                                      |
| Partition scheme     | Huge APP (3MB No OTA / 1MB SPIFFS)       |
| PSRAM                | Enabled                                  |
| Upload speed         | 460800 (or 115200 if unstable)           |
| Port                 | the FTDI COM port                        |

### Wiring the FTDI for upload

| FTDI       | ESP32-CAM |
| ---------- | --------- |
| 5V (or 3V3 to 3V3) | 5V |
| GND        | GND       |
| TX         | U0R (RX)  |
| RX         | U0T (TX)  |

**To enter upload mode:** connect `IO0` -> `GND`, press RESET, start upload in the IDE. Remove the jumper after upload and press RESET again to run the sketch.

## 3. Pin assignments (per SDD)

| Pin      | Connected to        | Purpose                       |
| -------- | ------------------- | ----------------------------- |
| GPIO 13  | HC-SR501 OUT        | PIR input / deep-sleep wake   |
| GPIO 4   | Onboard flash LED   | LED control via MQTT          |
| 5V / GND | HC-SR501 VCC / GND  | Power for PIR                 |

### Wiring the HC-SR501

Three wires, no transistor, no level shifter — the HC-SR501 module has
an onboard regulator and outputs 3.3 V logic, which GPIO 13 accepts
directly.

```
HC-SR501 VCC → breadboard (+) rail  [5V from FTDI]
HC-SR501 GND → breadboard (−) rail  [shared with ESP32-CAM GND]
HC-SR501 OUT → ESP32-CAM GPIO 13    [direct, 3.3V-safe]
```

Module DIP settings:

- **Trigger jumper: `H`** (repeat-trigger). Keeps OUT high while motion
  continues.
- **Sx (sensitivity) pot**: start centered, trim if too twitchy.
- **Tx (time-delay) pot**: all the way **counter-clockwise** (~3 s). Keeps
  the per-event window short so the firmware's 30 s Sleep Guard doesn't
  overlap two consecutive triggers.
- **~60 s power-on warm-up** — false triggers during this window are
  normal per the datasheet; ignore them.

Optional debug LED (parallel to the GPIO 13 wire, not in series with it):

```
         ┌──── GPIO 13
PIR OUT ─┤
         └── LED anode → 330 Ω → GND
```

Forward current ≈ 4 mA, well within the HC-SR501's output capability, so
GPIO 13 still sees clean 3.3 V logic. Any common red/yellow/green 3 mm
LED works.

## 4. Configure credentials

Copy `secrets.h.example` to `secrets.h` and fill in **both** credential sets
(personal *and* enterprise) — only the one that matches your chosen Wi-Fi
mode is actually used, but keeping both in the file lets you swap networks
with a single flag change in `firmware.ino`.

```cpp
// WPA2-Personal
#define WIFI_SSID      "..."
#define WIFI_PASSWORD  "..."

// WPA2-Enterprise (eduroam)
#define EAP_SSID       "eduroam"
#define EAP_IDENTITY   "user@hacettepe.edu.tr"
#define EAP_USERNAME   "user@hacettepe.edu.tr"
#define EAP_PASSWORD   "..."

#define MQTT_HOST      "192.168.1.100"   // your PC's LAN IP (not 127.0.0.1)
#define MQTT_PORT      1883
```

Find your PC's IP with `ipconfig` (Windows) — the "IPv4 Address" of the Wi-Fi adapter that's on the same network as the ESP32. **The IP changes when you switch networks**, so update `MQTT_HOST` whenever you flip the Wi-Fi mode.

> The broker config allows anonymous access from any LAN IP, so no extra
> firewall work is needed beyond letting `mosquitto.exe` through the
> Windows Firewall prompt the first time.

## 5. Operating modes

Two compile-time flags at the top of `firmware.ino`:

```cpp
#define ALWAYS_ON           1   // 1 = stay awake, 0 = deep sleep between events
#define USE_ENTERPRISE_WIFI 0   // 0 = WPA2-Personal, 1 = WPA2-Enterprise (eduroam)
```

### `ALWAYS_ON`

- **`1`** (default) — radio stays on, commands and HTTP are always reachable.
  Use for the lab demo and wiring checks.
- **`0`** — device enters deep sleep after each alert is ack'd (or after a
  30 s fallback window). Wakes on PIR HIGH on GPIO 13. Demonstrates the
  Sleep Guard protocol from the SDD.

### `USE_ENTERPRISE_WIFI`

Currently shipped as **`0`** in `firmware.ino` (WPA2-Personal / phone
hotspot mode).

- **`0`** — WPA2-Personal with `WIFI_SSID` + `WIFI_PASSWORD`.
  Used with home routers and phone hotspots. Fastest path to get online.
- **`1`** — WPA2-Enterprise (eduroam) via PEAP + MSCHAPv2. Uses
  `EAP_SSID` / `EAP_IDENTITY` / `EAP_USERNAME` / `EAP_PASSWORD`. No CA cert
  is required for Hacettepe. Handshake can take up to ~20 s; firmware
  timeout is extended accordingly. Requires that the campus network does
  **not** enforce client isolation (otherwise the ESP32 can't reach the
  PC's Mosquitto broker).

## 6. After flashing

Open Serial Monitor at **115200 baud**. You should see:

```
[BOOT] count=1 wake_reason=0 always_on=1
[CAM] initialized
[WiFi] connected: 192.168.1.42  RSSI -52 dBm
[HTTP] listening on http://192.168.1.42/
[MQTT] connecting to 192.168.1.100:1883 ... ok
```

The firmware's `publishStatus()` also embeds the LAN IP in the retained
`security/status` payload — the backend parses this field on subscribe
and auto-populates its `esp32_host`. So in the normal flow you **do not
need** to push the IP manually.

If you need to override (pre-auto-discovery build, cleared retained
status, etc.), the manual fallback curl is still available; see
`src/web/README.md` for details.

Then in the dashboard:

1. Open `http://127.0.0.1:8000/` — the camera stream is **off by
   default**. Click **Start stream** to open the MJPEG feed. The
   ESP32's IP is auto-discovered from the retained `security/status`
   heartbeat (usually instant).
2. Wave your hand in front of the PIR (when armed) → the alert panel
   flashes, a `security/alert` row appears in the event log, and the
   backend records a clip in the background. Open **/clips.html** to
   play it back once recording completes (`clip_ready` push over `/ws`).
3. Tune resolution, quality, duration, and FPS from the **Camera &
   Clip Settings** form on the dashboard — no reflashing needed.

## 7. HTTP endpoints

| Path      | Response                           |
| --------- | ---------------------------------- |
| `/capture`| Single JPEG frame (`image/jpeg`)   |
| `/stream` | MJPEG multipart stream             |
| `/status` | JSON: device_id, rssi, uptime, heap, boot_count, quality, framesize, pir_enabled |

## 8. MQTT commands the firmware accepts

Published to `security/command` by the backend (via `/api/command`).
Firmware parses with `strstr` + `sscanf` for numeric values.

| Command payload                               | Effect                                                 |
| --------------------------------------------- | ------------------------------------------------------ |
| `{"command":"led_on"}`                        | GPIO 4 HIGH (onboard flash LED on)                     |
| `{"command":"led_off"}`                       | GPIO 4 LOW                                             |
| `{"command":"capture"}`                       | `publishAlert("manual")` — same pipeline as a real PIR |
| `{"command":"pir_on"}`                        | Arm PIR. Also clears any motionFlag buffered while disarmed, so re-enabling doesn't double-fire |
| `{"command":"pir_off"}`                       | Disarm PIR. Main loop stops publishing alerts while false; ISR still fires, flag is just ignored |
| `{"command":"cam_quality","value":<10..63>}`  | `sensor_t::set_quality()` at runtime. Lower = higher quality, larger frames |
| `{"command":"cam_framesize","value":<N>}`     | `sensor_t::set_framesize()` at runtime. N per `framesize_t` enum (QVGA=5, VGA=8, SVGA=9, HD=12, UXGA=15) |

Every command is logged to serial with `[MQTT] security/command => ...`
and its side-effect echoed (`[PIR] armed`, `[CAM] quality -> 16`, etc.).
