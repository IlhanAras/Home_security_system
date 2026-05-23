# Mosquitto Broker - Hybrid IoT Security System

Local MQTT broker for the BBM460 project.

## 1. Install Mosquitto (Windows)

1. Download the Windows installer from <https://mosquitto.org/download/>
   (file: `mosquitto-*-install-windows-x64.exe`)
2. Run the installer, accept defaults. Install location is typically either
   `C:\Program Files\mosquitto\` or `C:\Program Files (x86)\Mosquitto\`
   depending on the installer build. Both helper scripts in this directory
   (`run_broker.bat`, `test_broker.bat`, `pub_test_alert.bat`) probe both
   paths automatically.
3. Optional: add the chosen install directory to your `PATH` so
   `mosquitto`, `mosquitto_pub`, `mosquitto_sub` are available in any shell.

> Windows installs Mosquitto as a service by default. **Stop it** before
> running with our config so the port isn't held:
> `net stop mosquitto` (from an Administrator terminal).

### Windows Firewall (once per machine)

The ESP32 and the PC's Mosquitto broker usually land on the same LAN but
different NICs, and Windows Firewall blocks inbound 1883 by default. From
an **Administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Mosquitto MQTT 1883" -Direction Inbound -Protocol TCP -LocalPort 1883 -Action Allow
```

`Profile: Any` is the default, so the rule covers both Private (home
router / hotspot) and Public (eduroam) Windows network profiles.

## 2. Configuration

[`mosquitto.conf`](./mosquitto.conf) enables:

| Listener | Port | Protocol | Purpose |
| --- | --- | --- | --- |
| MQTT   | 1883 | `mqtt`       | ESP32-CAM firmware + FastAPI backend |
| WS     | 9001 | `websockets` | Browser (MQTT.js) — optional direct connection |

Anonymous access is **on** (lab-only). Retained messages persist in `./data/`.

## 3. Start the broker

```bat
cd broker
run_broker.bat
```

You should see lines like:
```
Opening ipv4 listen socket on port 1883.
Opening ipv4 listen socket on port 9001.
mosquitto version 2.x.x running
```

## 4. Smoke test

Open a second terminal:

```bat
cd broker
test_broker.bat
```

From a third terminal, publish a test alert using the bundled helper:

```bat
pub_test_alert.bat
```

(or call `mosquitto_pub` directly — the helper probes both common install
paths for you.)

The second terminal should print:
```
security/alert {"event":"motion","device_id":"esp32-cam-01"}
```

## 5. Topic reference

| Topic | Publisher | Subscriber | Payload |
| --- | --- | --- | --- |
| `security/alert`   | ESP32     | Dashboard | `{"event":"motion","device_id":"esp32-cam-01","uptime":42,"rssi":-41}` |
| `security/command` | Dashboard | ESP32     | `{"command":"led_on"\|"led_off"\|"capture"\|"pir_on"\|"pir_off"\|"cam_quality"\|"cam_framesize"}` (some carry `"value": N`) |
| `security/status`  | ESP32     | Dashboard | `{"status":"online","device_id":"esp32-cam-01","ip":"10.225.49.95","rssi":-41,"uptime":42,"heap":123456,"quality":16,"framesize":9,"pir_enabled":true}` (retained) |
| `security/ack`     | Dashboard | ESP32     | `{"ack":"image_received","device_id":"esp32-cam-01"}` |

`security/status` is published **retained** so a fresh dashboard sees the
last known state without waiting for the next heartbeat. The firmware also
registers a **Last Will** on `security/status` with `{"status":"offline"}`
retained, so if the ESP32 dies or loses Wi-Fi abruptly the broker flips the
dashboard's status indicator automatically.

The `ip` field on the `online` payload is consumed by the backend's
auto-discovery logic — see `src/web/README.md` for details. Clearing the
retained status (`mosquitto_pub -t security/status -r -n`) forces the next
ESP32 boot to re-publish and the backend to re-learn the IP.
