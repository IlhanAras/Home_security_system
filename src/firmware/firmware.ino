// Feature flags (must be defined before includes that depend on them)
// 1 = stay awake (dev/demo mode); 0 = deep sleep between motion events
#define ALWAYS_ON 1

// 0 = WPA2-Personal (home / phone hotspot); 1 = WPA2-Enterprise (eduroam)
#define USE_ENTERPRISE_WIFI 0

#include <WiFi.h>
#include <PubSubClient.h>
#include "esp_camera.h"
#include "esp_http_server.h"
#include "esp_sleep.h"
#include "esp_wifi.h"
#include "driver/rtc_io.h"

#include "secrets.h"   // defines WIFI/EAP credentials + MQTT_HOST, MQTT_PORT

#if USE_ENTERPRISE_WIFI
  // New API in arduino-esp32 3.x (replaces the deprecated esp_wpa2.h).
  #include "esp_eap_client.h"
#endif

// Identity + topics + pins
#define DEVICE_ID        "esp32-cam-01"

#define TOPIC_ALERT      "security/alert"
#define TOPIC_STATUS     "security/status"
#define TOPIC_COMMAND    "security/command"
#define TOPIC_ACK        "security/ack"

#define PIR_PIN          GPIO_NUM_13
#define LED_PIN          4

static const uint32_t ACTIVE_WINDOW_MS = 30000UL;  // sleep guard window
static const uint32_t STATUS_PERIOD_MS = 30000UL;  // heartbeat cadence
static const uint32_t MQTT_RETRY_MS    = 3000UL;

// Camera tuning (lower resolution / higher quality-number = lower latency)
//   Resolutions: FRAMESIZE_QVGA (320x240), FRAMESIZE_VGA (640x480),
//                FRAMESIZE_SVGA (800x600), FRAMESIZE_HD (1280x720), ...
//   JPEG quality: 10..63, lower = better quality but larger frames.
#if USE_ENTERPRISE_WIFI
  #define CAM_FRAME_SIZE   FRAMESIZE_SVGA
  #define CAM_JPEG_QUALITY 16
#else
  #define CAM_FRAME_SIZE   FRAMESIZE_QVGA
  #define CAM_JPEG_QUALITY 18
#endif

// AI-Thinker ESP32-CAM pin map (OV2640)
#define PWDN_GPIO_NUM    32
#define RESET_GPIO_NUM   -1
#define XCLK_GPIO_NUM     0
#define SIOD_GPIO_NUM    26
#define SIOC_GPIO_NUM    27
#define Y9_GPIO_NUM      35
#define Y8_GPIO_NUM      34
#define Y7_GPIO_NUM      39
#define Y6_GPIO_NUM      36
#define Y5_GPIO_NUM      21
#define Y4_GPIO_NUM      19
#define Y3_GPIO_NUM      18
#define Y2_GPIO_NUM       5
#define VSYNC_GPIO_NUM   25
#define HREF_GPIO_NUM    23
#define PCLK_GPIO_NUM    22

// Globals
WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);

static volatile bool     motionFlag      = false;
// PIR can be remotely armed/disarmed via `pir_on` / `pir_off` MQTT commands.
// Default armed on every boot — the dashboard decides the session policy
// and just pushes `pir_off` at startup if it wants to silence motion.
static bool              pirEnabled      = true;
static bool              awaitingAck     = false;
static uint32_t          activeWindowEnd = 0;
static uint32_t          lastStatusMs    = 0;
static uint32_t          lastMqttRetryMs = 0;
static httpd_handle_t    httpServer      = NULL;

RTC_DATA_ATTR int bootCount = 0;

// Forward declarations
static void IRAM_ATTR onPir();
static bool initCamera();
static void connectWifi();
static void ensureMqtt();
static void mqttCallback(char* topic, byte* payload, unsigned int length);
static void publishAlert(const char* event);
static void publishStatus();
static bool startHttpServer();
static esp_err_t captureHandler(httpd_req_t* req);
static esp_err_t streamHandler(httpd_req_t* req);
static esp_err_t statusHandler(httpd_req_t* req);
static void enterDeepSleep();

// Setup / Loop
void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println();
    Serial.println("========================================");
    Serial.println(" Hybrid IoT Security System - ESP32-CAM ");
    Serial.println("========================================");

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    esp_sleep_wakeup_cause_t reason = esp_sleep_get_wakeup_cause();
    bootCount++;
    Serial.printf("[BOOT] count=%d wake_reason=%d always_on=%d\n",
                  bootCount, (int)reason, ALWAYS_ON);

    if (!initCamera()) {
        Serial.println("[CAM] init failed; restarting in 5s");
        delay(5000);
        ESP.restart();
    }

    // Attach PIR interrupt AFTER camera init — esp_camera_init() reinstalls
    // the GPIO ISR service which can wipe handlers registered earlier.
    // INPUT_PULLDOWN needed because camera init leaves GPIO 13 floating HIGH.
    pinMode(PIR_PIN, INPUT_PULLDOWN);
    attachInterrupt(digitalPinToInterrupt(PIR_PIN), onPir, RISING);

    connectWifi();

    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setCallback(mqttCallback);
    mqtt.setBufferSize(1024);
    // Keep-alive + socket timeout tuned for campus networks (eduroam):
    // shorter keep-alive -> more frequent PINGREQ -> NAT entry stays warm;
    // shorter socket timeout -> dead connection detected faster.
    mqtt.setKeepAlive(20);
    mqtt.setSocketTimeout(8);

    if (!startHttpServer()) {
        Serial.println("[HTTP] server failed to start");
    } else {
        Serial.printf("[HTTP] listening on http://%s/\n",
                      WiFi.localIP().toString().c_str());
    }

    // If PIR woke us up, treat the boot itself as a motion event.
    if (reason == ESP_SLEEP_WAKEUP_EXT0) {
        motionFlag = true;
    }
}

void loop() {
    ensureMqtt();
    mqtt.loop();

    if (motionFlag) {
        motionFlag = false;
        if (pirEnabled) {
            Serial.println("[PIR] motion detected -> publishing alert");
            publishAlert("motion");
        } else {
            Serial.println("[PIR] motion detected but PIR is disabled, ignoring");
        }
    }

    if (millis() - lastStatusMs > STATUS_PERIOD_MS) {
        lastStatusMs = millis();
        publishStatus();
    }

#if !ALWAYS_ON
    // Sleep Guard: go back to deep sleep once ack arrives (awaitingAck
    // becomes false inside mqttCallback) or when the active window expires.
    static bool everPending = false;
    if (awaitingAck) everPending = true;
    if (everPending) {
        const bool ackReceived = !awaitingAck;
        const bool windowOver  = (int32_t)(millis() - activeWindowEnd) > 0;
        if (ackReceived || windowOver) {
            Serial.printf("[PWR] sleep guard done (ack=%d, timeout=%d)\n",
                          (int)ackReceived, (int)windowOver);
            enterDeepSleep();
        }
    }
#endif

    delay(10);
}

// FW-PIR
static void IRAM_ATTR onPir() {
    motionFlag = true;
}

// FW-CAM
static bool initCamera() {
    camera_config_t config = {};
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer   = LEDC_TIMER_0;
    config.pin_d0       = Y2_GPIO_NUM;
    config.pin_d1       = Y3_GPIO_NUM;
    config.pin_d2       = Y4_GPIO_NUM;
    config.pin_d3       = Y5_GPIO_NUM;
    config.pin_d4       = Y6_GPIO_NUM;
    config.pin_d5       = Y7_GPIO_NUM;
    config.pin_d6       = Y8_GPIO_NUM;
    config.pin_d7       = Y9_GPIO_NUM;
    config.pin_xclk     = XCLK_GPIO_NUM;
    config.pin_pclk     = PCLK_GPIO_NUM;
    config.pin_vsync    = VSYNC_GPIO_NUM;
    config.pin_href     = HREF_GPIO_NUM;
    config.pin_sccb_sda = SIOD_GPIO_NUM;
    config.pin_sccb_scl = SIOC_GPIO_NUM;
    config.pin_pwdn     = PWDN_GPIO_NUM;
    config.pin_reset    = RESET_GPIO_NUM;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;

    const bool hasPsram = psramFound();
    Serial.printf("[CAM] PSRAM %s\n", hasPsram ? "found" : "NOT found");

    if (hasPsram) {
        // Allocate buffers at UXGA (max) so runtime set_framesize() to any
        // resolution always fits. The actual default is applied after init.
        config.frame_size   = FRAMESIZE_UXGA;
        config.jpeg_quality = CAM_JPEG_QUALITY;
        config.fb_count     = 2;
        config.fb_location  = CAMERA_FB_IN_PSRAM;
        config.grab_mode    = CAMERA_GRAB_LATEST;
    } else {
        config.frame_size   = FRAMESIZE_QVGA;
        config.jpeg_quality = 15;
        config.fb_count     = 1;
        config.fb_location  = CAMERA_FB_IN_DRAM;
        config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
    }

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("[CAM] esp_camera_init failed: 0x%x\n", err);
        return false;
    }

    // Set the actual default resolution (buffers were allocated at UXGA).
    if (hasPsram) {
        sensor_t* s = esp_camera_sensor_get();
        if (s) s->set_framesize(s, CAM_FRAME_SIZE);
    }

    Serial.println("[CAM] initialized");
    return true;
}

// Wi-Fi
static void connectWifi() {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);  // keep radio responsive for HTTP / MQTT
    WiFi.disconnect(true, true);  // clear any previous NVS credentials

#if USE_ENTERPRISE_WIFI
    Serial.printf("[WiFi] connecting to eduroam '%s' as '%s' ...",
                  EAP_SSID, EAP_IDENTITY);
    // PEAP + MSCHAPv2, no CA cert (Hacettepe docs say it's not required).
    esp_eap_client_set_identity((uint8_t*)EAP_IDENTITY, strlen(EAP_IDENTITY));
    esp_eap_client_set_username((uint8_t*)EAP_USERNAME, strlen(EAP_USERNAME));
    esp_eap_client_set_password((uint8_t*)EAP_PASSWORD, strlen(EAP_PASSWORD));
    esp_wifi_sta_enterprise_enable();
    WiFi.begin(EAP_SSID);
#else
    Serial.printf("[WiFi] connecting to SSID '%s' ...", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
#endif

    uint32_t start = millis();
    // Enterprise handshake can be slow -- allow more time.
    const uint32_t timeoutMs = USE_ENTERPRISE_WIFI ? 30000 : 15000;
    while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
        delay(250);
        Serial.print('.');
    }
    Serial.println();
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WiFi] failed; restarting");
        delay(2000);
        ESP.restart();
    }
    Serial.printf("[WiFi] connected: %s  RSSI %d dBm\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

// FW-MQTT
static void ensureMqtt() {
    if (mqtt.connected()) return;
    if (millis() - lastMqttRetryMs < MQTT_RETRY_MS) return;
    lastMqttRetryMs = millis();

    Serial.printf("[MQTT] connecting to %s:%d ...", MQTT_HOST, MQTT_PORT);
    // Publish an LWT so the dashboard can detect abrupt disconnects.
    const char* lwtPayload = "{\"status\":\"offline\",\"device_id\":\"" DEVICE_ID "\"}";
    bool ok = mqtt.connect(
        DEVICE_ID,
        /*user*/ nullptr, /*pass*/ nullptr,
        TOPIC_STATUS, 0, /*retained*/ true, lwtPayload
    );
    if (!ok) {
        Serial.printf(" failed rc=%d\n", mqtt.state());
        return;
    }
    Serial.println(" ok");
    mqtt.subscribe(TOPIC_COMMAND);
    mqtt.subscribe(TOPIC_ACK);
    publishStatus();
}

static void mqttCallback(char* topic, byte* payload, unsigned int length) {
    char buf[256];
    size_t n = length < sizeof(buf) - 1 ? length : sizeof(buf) - 1;
    memcpy(buf, payload, n);
    buf[n] = '\0';

    Serial.printf("[MQTT] %s => %s\n", topic, buf);

    if (strcmp(topic, TOPIC_COMMAND) == 0) {
        // Order matters: `pir_on` must be matched before `led_on` etc. would
        // never false-positive anyway (different prefixes), but check the
        // pir_* variants first for clarity.
        if (strstr(buf, "pir_on")) {
            pirEnabled = true;
            motionFlag = false;  // drop any pulse that fired while disabled
            Serial.println("[PIR] armed");
        } else if (strstr(buf, "pir_off")) {
            pirEnabled = false;
            Serial.println("[PIR] disarmed");
        } else if (strstr(buf, "led_on")) {
            digitalWrite(LED_PIN, HIGH);
        } else if (strstr(buf, "led_off")) {
            digitalWrite(LED_PIN, LOW);
        } else if (strstr(buf, "capture")) {
            publishAlert("manual");
        } else if (strstr(buf, "cam_quality")) {
            // Expected payload: {"command":"cam_quality","value":<10..63>}
            const char* p = strstr(buf, "\"value\":");
            int q = 0;
            if (p && sscanf(p + 8, "%d", &q) == 1) {
                if (q < 10) q = 10;
                if (q > 63) q = 63;
                sensor_t* s = esp_camera_sensor_get();
                if (s) {
                    s->set_quality(s, q);
                    Serial.printf("[CAM] quality -> %d\n", q);
                }
            }
        } else if (strstr(buf, "cam_framesize")) {
            const char* p = strstr(buf, "\"value\":");
            int fs = 0;
            if (p && sscanf(p + 8, "%d", &fs) == 1) {
                sensor_t* s = esp_camera_sensor_get();
                if (s) {
                    s->set_framesize(s, (framesize_t)fs);
                    // Flush stale frame buffers so the stream picks up the
                    // new resolution immediately instead of draining old frames.
                    for (int i = 0; i < 2; i++) {
                        camera_fb_t* fb = esp_camera_fb_get();
                        if (fb) esp_camera_fb_return(fb);
                    }
                    Serial.printf("[CAM] framesize -> %d\n", fs);
                }
            }
        }
    } else if (strcmp(topic, TOPIC_ACK) == 0) {
        if (strstr(buf, "image_received")) {
            awaitingAck = false;
            Serial.println("[MQTT] ack received -> sleep guard released");
        }
    }
}

static void publishAlert(const char* event) {
    char buf[192];
    snprintf(buf, sizeof(buf),
        "{\"event\":\"%s\",\"device_id\":\"" DEVICE_ID "\",\"uptime\":%lu,\"rssi\":%d}",
        event, (unsigned long)(millis() / 1000), (int)WiFi.RSSI());
    if (mqtt.connected()) mqtt.publish(TOPIC_ALERT, buf);
    Serial.printf("[PUB] %s %s\n", TOPIC_ALERT, buf);
    awaitingAck     = true;
    activeWindowEnd = millis() + ACTIVE_WINDOW_MS;
}

static void publishStatus() {
    if (!mqtt.connected()) return;
    sensor_t* s = esp_camera_sensor_get();
    int curQuality   = s ? s->status.quality   : -1;
    int curFramesize = s ? s->status.framesize : -1;
    char buf[320];
    snprintf(buf, sizeof(buf),
        "{\"status\":\"online\",\"device_id\":\"" DEVICE_ID "\","
        "\"ip\":\"%s\",\"rssi\":%d,\"uptime\":%lu,\"heap\":%u,"
        "\"quality\":%d,\"framesize\":%d,\"pir_enabled\":%s}",
        WiFi.localIP().toString().c_str(),
        (int)WiFi.RSSI(), (unsigned long)(millis() / 1000),
        (unsigned)ESP.getFreeHeap(),
        curQuality, curFramesize,
        pirEnabled ? "true" : "false");
    mqtt.publish(TOPIC_STATUS, buf, /*retained*/ true);
}

// FW-HTTP
#define STREAM_PART_BOUNDARY "frameboundary"
static const char* STREAM_CONTENT_TYPE =
    "multipart/x-mixed-replace;boundary=" STREAM_PART_BOUNDARY;
static const char* STREAM_BOUNDARY_HDR = "\r\n--" STREAM_PART_BOUNDARY "\r\n";
static const char* STREAM_PART_HDR_FMT =
    "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static esp_err_t captureHandler(httpd_req_t* req) {
    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) {
        httpd_resp_send_500(req);
        return ESP_FAIL;
    }
    httpd_resp_set_type(req, "image/jpeg");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    esp_err_t res = httpd_resp_send(req, (const char*)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    return res;
}

static esp_err_t streamHandler(httpd_req_t* req) {
    httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    char partHeader[64];
    while (true) {
        camera_fb_t* fb = esp_camera_fb_get();
        if (!fb) return ESP_FAIL;
        size_t hlen = snprintf(partHeader, sizeof(partHeader),
                               STREAM_PART_HDR_FMT, fb->len);
        esp_err_t res;
        res  = httpd_resp_send_chunk(req, STREAM_BOUNDARY_HDR,
                                     strlen(STREAM_BOUNDARY_HDR));
        if (res == ESP_OK) res = httpd_resp_send_chunk(req, partHeader, hlen);
        if (res == ESP_OK) res = httpd_resp_send_chunk(req,
                                     (const char*)fb->buf, fb->len);
        esp_camera_fb_return(fb);
        if (res != ESP_OK) return res;  // client disconnected
    }
}

static esp_err_t statusHandler(httpd_req_t* req) {
    sensor_t* s = esp_camera_sensor_get();
    int curQuality   = s ? s->status.quality   : -1;
    int curFramesize = s ? s->status.framesize : -1;
    char buf[320];
    snprintf(buf, sizeof(buf),
        "{\"device_id\":\"" DEVICE_ID "\",\"rssi\":%d,\"uptime\":%lu,"
        "\"heap\":%u,\"boot_count\":%d,"
        "\"quality\":%d,\"framesize\":%d,\"pir_enabled\":%s}",
        (int)WiFi.RSSI(), (unsigned long)(millis() / 1000),
        (unsigned)ESP.getFreeHeap(), bootCount,
        curQuality, curFramesize,
        pirEnabled ? "true" : "false");
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_send(req, buf, strlen(buf));
}

static bool startHttpServer() {
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = 80;
    cfg.max_uri_handlers = 8;

    if (httpd_start(&httpServer, &cfg) != ESP_OK) return false;

    httpd_uri_t uriCapture = {
        .uri = "/capture", .method = HTTP_GET,
        .handler = captureHandler, .user_ctx = NULL
    };
    httpd_uri_t uriStream = {
        .uri = "/stream", .method = HTTP_GET,
        .handler = streamHandler, .user_ctx = NULL
    };
    httpd_uri_t uriStatus = {
        .uri = "/status", .method = HTTP_GET,
        .handler = statusHandler, .user_ctx = NULL
    };
    httpd_register_uri_handler(httpServer, &uriCapture);
    httpd_register_uri_handler(httpServer, &uriStream);
    httpd_register_uri_handler(httpServer, &uriStatus);
    return true;
}

// FW-PWR
static void enterDeepSleep() {
    Serial.println("[PWR] entering deep sleep");
    if (httpServer) {
        httpd_stop(httpServer);
        httpServer = NULL;
    }
    if (mqtt.connected()) {
        // Publish offline status before disconnecting
        const char* msg = "{\"status\":\"sleeping\",\"device_id\":\"" DEVICE_ID "\"}";
        mqtt.publish(TOPIC_STATUS, msg, /*retained*/ true);
        mqtt.disconnect();
    }
    WiFi.disconnect(true);

    // Configure PIR pin as ext0 wakeup source (HIGH = motion).
    rtc_gpio_pullup_dis(PIR_PIN);
    rtc_gpio_pulldown_en(PIR_PIN);
    esp_sleep_enable_ext0_wakeup(PIR_PIN, 1);
    Serial.flush();
    esp_deep_sleep_start();
}
