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
#include <mbedtls/sha256.h>
#include <sdkconfig.h>
#define OTTER_DEFAULT_HW CONFIG_IDF_TARGET
#else
#error "Otter supports ESP8266 and ESP32 only"
#endif

namespace {

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
  if (order.deploymentId) {
    applyUpdate(order);  // only returns on failure or cancellation
    _waitMs = 0;
    return;
  }
  _waitMs = reached ? _intervalS * 1000UL : kRetryMs;
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
