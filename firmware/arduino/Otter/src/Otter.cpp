#include "Otter.h"

#include <ArduinoJson.h>

#if defined(ESP8266)
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>
#include <Updater.h>
#include <bearssl/bearssl_hash.h>
#define OTTER_DEFAULT_HW "esp8266"
#elif defined(ESP32)
#include <HTTPClient.h>
#include <Update.h>
#include <WiFi.h>
#include <esp_system.h>
#include <mbedtls/sha256.h>
#include <sdkconfig.h>
#define OTTER_DEFAULT_HW CONFIG_IDF_TARGET
#else
#error "Otter supports ESP8266 and ESP32 only"
#endif

namespace {

// Why the device last restarted, in the protocol's vocabulary.
const char *resetReason() {
#if defined(ESP8266)
  switch (ESP.getResetInfoPtr()->reason) {
    case REASON_DEFAULT_RST: return "power_on";
    case REASON_WDT_RST: return "watchdog";
    case REASON_EXCEPTION_RST: return "panic";
    case REASON_SOFT_WDT_RST: return "task_watchdog";
    case REASON_SOFT_RESTART: return "software";
    case REASON_DEEP_SLEEP_AWAKE: return "deep_sleep";
    case REASON_EXT_SYS_RST: return "external";
    default: return "unknown";
  }
#else
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power_on";
    case ESP_RST_EXT: return "external";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_watchdog";
    case ESP_RST_TASK_WDT: return "task_watchdog";
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deep_sleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    default: return "unknown";
  }
#endif
}

constexpr uint32_t kRetryMs = 10000;
constexpr uint32_t kHttpTimeoutMs = 15000;
constexpr size_t kChunkSize = 1024;

#if defined(ESP8266)
class Sha256 {
 public:
  Sha256() { br_sha256_init(&_ctx); }
  void update(const uint8_t *data, size_t len) { br_sha256_update(&_ctx, data, len); }
  void finish(uint8_t out[32]) { br_sha256_out(&_ctx, out); }

 private:
  br_sha256_context _ctx;
};
#else
class Sha256 {
 public:
  Sha256() {
    mbedtls_sha256_init(&_ctx);
    mbedtls_sha256_starts(&_ctx, 0);
  }
  ~Sha256() { mbedtls_sha256_free(&_ctx); }
  void update(const uint8_t *data, size_t len) { mbedtls_sha256_update(&_ctx, data, len); }
  void finish(uint8_t out[32]) { mbedtls_sha256_finish(&_ctx, out); }

 private:
  mbedtls_sha256_context _ctx;
};
#endif

void abortUpdate() {
#if defined(ESP8266)
  Update.end(false);  // not finished: discards the partial image
#else
  Update.abort();
#endif
}

}  // namespace

void OtterAgent::begin(const Config &config) {
  _cfg = config;
  if (!_cfg.hw) _cfg.hw = OTTER_DEFAULT_HW;
  _waitMs = 0;
  OTTER_LOG("agent started: %s %s on %s, server %s", _cfg.app, _cfg.version, _cfg.hw, _cfg.server);
}

void OtterAgent::loop() {
  if (WiFi.status() != WL_CONNECTED || millis() - _lastCheckin < _waitMs) return;
  _lastCheckin = millis();

  Order order;
  bool reached = checkin(order);
  if (_rebootRequested) {
    OTTER_LOG("rebooting, as asked from Otter");
    delay(500);
    ESP.restart();
  }
  if (order.deploymentId) {
    applyUpdate(order);  // only returns on failure or cancellation
    _waitMs = 0;
    return;
  }
  _waitMs = reached ? _intervalS * 1000UL : kRetryMs;
  if (_checkinAgain) {  // commands came: the next ones may be right behind
    _checkinAgain = false;
    _waitMs = 0;
  }
}

bool OtterAgent::onCommand(const char *name, CommandHandler handler) {
  Command *slot = nullptr;
  for (Command &c : _commands) {
    if (c.name && strcmp(c.name, name) == 0) {
      slot = &c;
      break;
    }
    if (!slot && !c.name) slot = &c;
  }
  if (!slot) return false;
  slot->name = name;
  slot->handler = handler;
  return true;
}

void OtterAgent::runCommand(int id, const char *name, const String &args) {
  CommandHandler handler;
  for (Command &c : _commands) {
    if (c.name && strcmp(c.name, name) == 0) handler = c.handler;
  }
  bool ok = false;
  String message;
  if (handler) {
    OTTER_LOG("command %s %s", name, args.c_str());
    ok = handler(args, message);
  } else if (strcmp(name, "reboot") == 0) {
    _rebootRequested = true;  // once every command is acknowledged
    ok = true;
    message = "rebooting";
  } else if (strcmp(name, "identify") == 0) {
#ifdef LED_BUILTIN
    pinMode(LED_BUILTIN, OUTPUT);
    int level = digitalRead(LED_BUILTIN);
    for (int i = 0; i < 10; i++) {  // an even count: back to where it was
      level = !level;
      digitalWrite(LED_BUILTIN, level);
      delay(250);
    }
    message = "blinked LED_BUILTIN";
#else
    OTTER_LOG("*** identify requested from Otter ***");
    message = "logged only: this board has no LED_BUILTIN";
#endif
    ok = true;
  } else {
    message = "unknown command";
  }

  JsonDocument doc;
  doc["ok"] = ok;
  if (message.length()) doc["message"] = message.substring(0, 200);
  String body;
  serializeJson(doc, body);
  if (post("/api/v1/commands/" + String(id) + "/result", body, nullptr) != 200) {
    OTTER_LOG("command %d: result not delivered", id);
  }
}

int OtterAgent::post(const String &path, const String &body, String *response) {
  WiFiClient client;
  HTTPClient http;
  if (!http.begin(client, String(_cfg.server) + path)) return -1;
  http.setTimeout(kHttpTimeoutMs);
  http.addHeader("Content-Type", "application/json");
  if (_cfg.fleetKey && *_cfg.fleetKey) http.addHeader("X-Otter-Key", _cfg.fleetKey);
  int code = http.POST(body);
  if (response && code > 0) *response = http.getString();
  http.end();
  return code;
}

bool OtterAgent::checkin(Order &order) {
  JsonDocument doc;
  doc["mac"] = WiFi.macAddress();
  doc["hw"] = _cfg.hw;
  doc["app"] = _cfg.app;
  doc["fw_version"] = _cfg.version;
  doc["ip"] = WiFi.localIP().toString();
  doc["rssi"] = WiFi.RSSI();
  doc["uptime_s"] = millis() / 1000;
  doc["ota_slot_size"] = ESP.getFreeSketchSpace();  // room for an update image
  doc["free_heap"] = ESP.getFreeHeap();
#if defined(ESP32)
  doc["min_free_heap"] = ESP.getMinFreeHeap();
#endif
  // No boot_count: the server spots restarts from uptime_s going down.
  doc["reset_reason"] = resetReason();
  doc["config_version"] = _configVersion;  // "": none yet
  String body;
  serializeJson(doc, body);

  String response;
  int code = post("/api/v1/checkin", body, &response);
  if (code != 200) {
    OTTER_LOG("check-in failed (HTTP %d)", code);
    return false;
  }

  JsonDocument resp;
  if (deserializeJson(resp, response)) {
    OTTER_LOG("check-in: invalid JSON response");
    return false;
  }
  uint32_t interval = resp["checkin_interval_s"] | 0;
  if (interval > 0) _intervalS = interval;

  JsonObject config = resp["config"];
  if (!config.isNull() && config["values"].is<JsonObject>()) {
    _configVersion = config["version"] | "";
    _configJson = "";
    serializeJson(config["values"], _configJson);
    OTTER_LOG("configuration %s received", _configVersion.c_str());
    if (_configHandler) _configHandler(_configJson);
    _checkinAgain = true;  // confirm the new version right away
  }

  for (JsonObject command : resp["commands"].as<JsonArray>()) {
    const char *name = command["name"] | "";
    String args;
    if (command["args"].is<JsonObject>()) serializeJson(command["args"], args);
    runCommand(command["id"] | 0, name, args.length() ? args : String("{}"));
    _checkinAgain = true;
  }

  JsonObject update = resp["update"];
  if (!update.isNull()) {
    order.version = update["version"] | "";
    order.url = update["url"] | "";
    order.sha256 = update["sha256"] | "";
    order.size = update["size"] | 0;
    if (order.url.length() && order.sha256.length() == 64 && order.size > 0) {
      order.deploymentId = update["deployment_id"] | 0;
    } else {
      OTTER_LOG("malformed update order");
    }
  }
  return true;
}

bool OtterAgent::report(int deploymentId, const char *state, int progress, const char *error) {
  JsonDocument doc;
  doc["state"] = state;
  doc["progress"] = progress;
  if (error) doc["error"] = error;
  String body;
  serializeJson(doc, body);

  int code = post(String("/api/v1/deployments/") + deploymentId + "/progress", body, nullptr);
  if (code != 200) OTTER_LOG("progress report got HTTP %d", code);
  return code != 409;  // 409: cancelled from the UI
}

void OtterAgent::applyUpdate(const Order &order) {
  OTTER_LOG("updating %s -> %s (%u bytes)", _cfg.version, order.version.c_str(), (unsigned)order.size);
  if (!report(order.deploymentId, "downloading", 0)) {
    OTTER_LOG("update cancelled by server");
    return;
  }

  bool cancelled = false;
  const char *err = flash(order, cancelled);
  if (cancelled) {
    OTTER_LOG("update cancelled by server");
    return;
  }
  if (err) {
    OTTER_LOG("update failed: %s", err);
    report(order.deploymentId, "failed", 0, err);
    return;
  }

  report(order.deploymentId, "rebooting", 100);
  OTTER_LOG("update written, rebooting into %s", order.version.c_str());
  delay(500);
  ESP.restart();
}

// Streams the image into the OTA slot. Returns an error message, or nullptr on success.
const char *OtterAgent::flash(const Order &order, bool &cancelled) {
  WiFiClient client;
  HTTPClient http;
  if (!http.begin(client, order.url)) return "bad firmware URL";
  http.setTimeout(kHttpTimeoutMs);
  if (_cfg.fleetKey && *_cfg.fleetKey) http.addHeader("X-Otter-Key", _cfg.fleetKey);
  if (http.GET() != 200) {
    http.end();
    return "firmware download refused";
  }
  if (!Update.begin(order.size)) {
    http.end();
    return "not enough space for the image";
  }

  auto *stream = http.getStreamPtr();
  uint8_t buf[kChunkSize];
  Sha256 sha;
  size_t received = 0;
  int lastReported = 0;
  uint32_t lastData = millis();
  const char *err = nullptr;

  while (received < order.size) {
    size_t available = stream->available();
    if (!available) {
      if (!http.connected()) {
        err = "connection lost";
        break;
      }
      if (millis() - lastData > kHttpTimeoutMs) {
        err = "download timeout";
        break;
      }
      delay(1);
      continue;
    }
    size_t n = stream->readBytes(buf, std::min({available, sizeof(buf), order.size - received}));
    if (!n) continue;
    lastData = millis();
    if (Update.write(buf, n) != n) {
      err = "flash write failed";
      break;
    }
    sha.update(buf, n);
    received += n;

    int pct = received * 100 / order.size;
    if (pct - lastReported >= 10 && received < order.size) {
      lastReported = pct;
      if (!report(order.deploymentId, "downloading", pct)) {
        cancelled = true;
        break;
      }
    }
  }
  http.end();

  if (!err && !cancelled) {
    uint8_t digest[32];
    char hex[65];
    sha.finish(digest);
    for (int i = 0; i < 32; i++) sprintf(&hex[i * 2], "%02x", digest[i]);
    if (!order.sha256.equalsIgnoreCase(hex)) err = "sha256 mismatch";
  }
  if (err || cancelled) {
    abortUpdate();
    return err;
  }
  if (!Update.end()) return "image rejected";
  return nullptr;
}
