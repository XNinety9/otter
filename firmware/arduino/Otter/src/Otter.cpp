#include "Otter.h"

#include <ArduinoJson.h>

#include <memory>

#if defined(ESP8266)
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>
#include <WiFiClientSecure.h>
#include <Updater.h>
#include <bearssl/bearssl_hash.h>
#define OTTER_DEFAULT_HW "esp8266"
#elif defined(ESP32)
#include <HTTPClient.h>
#include <Preferences.h>
#include <Update.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_system.h>
#include <esp_wifi.h>
#include <mbedtls/base64.h>
#include <mbedtls/pk.h>
#include <mbedtls/sha256.h>
#include <sdkconfig.h>

#include "otter_chip.h"
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
  // esp_reset_reason_t by value: its order is stable, and the newer names don't exist in
  // every Arduino core.
  static const char *const names[] = {
      "unknown", "power_on", "external", "software", "panic", "int_watchdog", "task_watchdog", "watchdog",
      "deep_sleep", "brownout", "sdio", "usb", "jtag", "efuse", "power_glitch", "cpu_lockup",
  };
  unsigned reason = static_cast<unsigned>(esp_reset_reason());
  return reason < sizeof(names) / sizeof(names[0]) ? names[reason] : "unknown";
#endif
}

constexpr uint32_t kRetryMs = 10000;
constexpr uint32_t kHttpTimeoutMs = 15000;
// Longest long poll: HTTPClient's timeout is a uint16_t in milliseconds.
constexpr uint32_t kMaxWaitS = 45;
constexpr size_t kChunkSize = 1024;
// A download receiving nothing for this long reconnects and resumes, at most kMaxResumes
// times: with the cores' small TCP window, a stalled connection rarely recovers on a weak link.
constexpr uint32_t kStallMs = 8000;
constexpr int kMaxResumes = 10;

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

// Checks the image's signature (see "Signed firmware" in the README): nullptr when valid.
const char *verifySignature(const char *pem, const uint8_t digest[32], const String &signature) {
#if defined(ESP32)
  unsigned char sig[512];
  size_t len = 0;
  if (mbedtls_base64_decode(sig, sizeof(sig), &len, reinterpret_cast<const unsigned char *>(signature.c_str()),
                            signature.length()) != 0) {
    return "invalid signature: malformed";
  }
  mbedtls_pk_context key;
  mbedtls_pk_init(&key);
  const char *err = nullptr;
  if (mbedtls_pk_parse_public_key(&key, reinterpret_cast<const unsigned char *>(pem), strlen(pem) + 1) != 0) {
    err = "firmware signing key unreadable";
  } else if (mbedtls_pk_verify(&key, MBEDTLS_MD_SHA256, digest, 32, sig, len) != 0) {
    err = "invalid signature: not signed with this device's key";
  }
  mbedtls_pk_free(&key);
  return err;
#else
  (void)pem, (void)digest, (void)signature;
  return "signatures aren't supported on ESP8266 yet";
#endif
}

// A TLS client that trusts only the given CA certificate.
class SecureClient : public WiFiClientSecure {
 public:
  explicit SecureClient(const char *caCert) {
#if defined(ESP8266)
    if (caCert) {
      _anchors.reset(new BearSSL::X509List(caCert));
      setTrustAnchors(_anchors.get());
    }
    setBufferSizes(4096, 1024);  // RAM is short: most servers accept a smaller fragment length
#else
    if (caCert) setCACert(caCert);
#endif
  }

#if defined(ESP8266)
 private:
  std::unique_ptr<BearSSL::X509List> _anchors;
#endif
};

// Opens url on the plain or the TLS client. https:// always checks the server's certificate.
bool open(HTTPClient &http, WiFiClient &plain, WiFiClient &secure, const String &url, const char *caCert) {
  if (!url.startsWith("https://")) return http.begin(plain, url);
  if (!caCert) {
    OTTER_LOG("%s: set config.caCert to reach an https:// server", url.c_str());
    return false;
  }
  return http.begin(secure, url);
}

// Modem sleep off while it matters (a check-in on a weak link, a download), then back.
class RadioAwake {
 public:
  RadioAwake() {
#if defined(ESP32)
    _changed = esp_wifi_get_ps(&_saved) == ESP_OK && _saved != WIFI_PS_NONE && esp_wifi_set_ps(WIFI_PS_NONE) == ESP_OK;
#else
    _saved = WiFi.getSleepMode();
    _changed = _saved != WIFI_NONE_SLEEP && WiFi.setSleepMode(WIFI_NONE_SLEEP);
#endif
  }
  ~RadioAwake() {
#if defined(ESP32)
    if (_changed) esp_wifi_set_ps(_saved);
#else
    if (_changed) WiFi.setSleepMode(_saved);
#endif
  }

 private:
#if defined(ESP32)
  wifi_ps_type_t _saved = WIFI_PS_NONE;
#else
  WiFiSleepType_t _saved = WIFI_NONE_SLEEP;
#endif
  bool _changed = false;
};

}  // namespace

void OtterAgent::setup(const Config &config) {
  _cfg = config;
  if (!_cfg.hw) _cfg.hw = OTTER_DEFAULT_HW;
  _waitMs = 0;
#if defined(ESP32)
  // The token from a previous boot (see "Authentication" in docs/protocol.md). The ESP8266
  // keeps using the fleet key: without persistence it would lock itself out at its next boot.
  Preferences prefs;
  if (prefs.begin("otter", true)) {
    _token = prefs.getString("token", "");
    prefs.end();
  }
#endif
  OTTER_LOG("agent started: %s %s on %s, server %s", _cfg.app, _cfg.version, _cfg.hw, _cfg.server);
}

void OtterAgent::begin(const Config &config) {
  setup(config);
#if defined(ESP32)
  _longPoll = true;
  TaskHandle_t handle = nullptr;
  if (xTaskCreate(task, "otter", 12288, this, 5, &handle) == pdPASS) {
    _task = handle;
  } else {
    _longPoll = false;  // fall back to loop()
    OTTER_LOG("can't start the agent's task: use loop()");
  }
#endif
}

#if defined(ESP32)
void OtterAgent::task(void *agent) {
  auto *self = static_cast<OtterAgent *>(agent);
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      delay(1000);
      continue;
    }
    uint32_t waitMs = self->cycle();
    if (waitMs) ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(waitMs));  // checkinNow() ends the wait
  }
}
#else
void OtterAgent::task(void *) {}
#endif

void OtterAgent::loop() {
  if (_task || WiFi.status() != WL_CONNECTED || millis() - _lastCheckin < _waitMs) return;
  _lastCheckin = millis();
  _waitMs = cycle();
}

void OtterAgent::checkinNow() {
  _waitMs = 0;
#if defined(ESP32)
  if (_task) xTaskNotifyGive(static_cast<TaskHandle_t>(_task));
#endif
}

// One check-in and what comes back. Returns how long to wait before the next one.
uint32_t OtterAgent::cycle() {
  uint32_t started = millis();
  Order order;
  bool reached = checkin(order);
  if (_rebootRequested) {
    OTTER_LOG("rebooting, as asked from Otter");
    delay(500);
    ESP.restart();
  }
  if (order.deploymentId) {
    applyUpdate(order);  // only returns on failure or cancellation
    return 0;
  }
  if (!reached) return kRetryMs;
  if (_checkinAgain) {  // commands or a configuration came: the next ones may be right behind
    _checkinAgain = false;
    return 0;
  }
  // A long-polling server already made us wait: poll again right away.
  uint32_t elapsed = millis() - started, interval = _intervalS * 1000UL;
  return elapsed >= interval ? 0 : interval - elapsed;
}

bool OtterAgent::checkinOnce(const Config &config, uint32_t nextCheckinS) {
  setup(config);
  _oneShot = true;
  _nextCheckinS = nextCheckinS;
  RadioAwake awake;  // awake for seconds only: on a weak link, answers got lost in modem sleep
  bool reached = false;
  // A few rounds: to confirm a configuration, fetch commands queued behind others, or check
  // in again after a failed update.
  for (int round = 0; round < 5; round++) {
    Order order;
    reached = checkin(order);
    if (_rebootRequested) {
      OTTER_LOG("rebooting, as asked from Otter");
      delay(500);
      ESP.restart();
    }
    if (order.deploymentId) {
      applyUpdate(order);  // restarts into the new firmware, returns on failure
      continue;
    }
    if (!reached && round == 0) {
      OTTER_LOG("check-in failed, trying again in 2 s");  // a link that just woke up
      delay(2000);
      continue;
    }
    if (!reached || !_checkinAgain) break;
    _checkinAgain = false;
  }
  return reached;
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
  // A few tries: right after a command that touches the network the first connection often
  // fails, and the server can't send the command again (it could run twice).
  int code = -1;
  for (int attempt = 0; attempt < 3 && code < 0; attempt++) {
    if (attempt) delay(2000);
    code = post("/api/v1/commands/" + String(id) + "/result", body, nullptr, kHttpTimeoutMs);
  }
  if (code != 200) OTTER_LOG("command %d: result not delivered (HTTP %d)", id, code);
}

void OtterAgent::authenticate(HTTPClient &http) {
  if (_token.length()) {
    http.addHeader("Authorization", "Bearer " + _token);
  } else if (_cfg.fleetKey && *_cfg.fleetKey) {
    http.addHeader("X-Otter-Key", _cfg.fleetKey);
  }
}

void OtterAgent::saveToken(const String &token) {
  _token = token;
#if defined(ESP32)
  Preferences prefs;
  if (prefs.begin("otter", false)) {
    if (token.length()) {
      prefs.putString("token", token);
    } else {
      prefs.remove("token");
    }
    prefs.end();
  }
#endif
}

int OtterAgent::post(const String &path, const String &body, String *response, uint32_t timeoutMs) {
  WiFiClient plain;
  SecureClient secure(_cfg.caCert);
  HTTPClient http;
  if (!open(http, plain, secure, String(_cfg.server) + path, _cfg.caCert)) return -1;
  http.setTimeout(timeoutMs > 65000 ? 65000 : timeoutMs);
  http.addHeader("Content-Type", "application/json");
  authenticate(http);
  int code = http.POST(body);
  if (response && code > 0) *response = http.getString();
  http.end();
  return code;
}

bool OtterAgent::checkin(Order &order) {
  uint32_t waitS = _longPoll && !_oneShot ? std::min(_intervalS, kMaxWaitS) : 0;
  JsonDocument doc;
  doc["mac"] = WiFi.macAddress();
  doc["hw"] = _cfg.hw;
#if defined(ESP32)
  char chipRev[8];
  otter_chip_revision(chipRev, sizeof(chipRev));
  doc["chip"] = otter_chip_model();
  doc["chip_rev"] = chipRev;
  if (otter_flash_size()) doc["flash_size"] = otter_flash_size();
  if (otter_psram_size()) doc["psram_size"] = otter_psram_size();
  char radio[80];
  otter_chip_radio(radio, sizeof(radio));
  doc["radio"] = radio;
#else
  doc["chip"] = esp_is_8285() ? "ESP8285" : "ESP8266EX";
  doc["flash_size"] = ESP.getFlashChipRealSize();
  doc["radio"] = "Wi-Fi 4";
#endif
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
  doc["wait_s"] = waitS;
  if (_nextCheckinS) doc["next_checkin_s"] = _nextCheckinS;
  String body;
  serializeJson(doc, body);

  String response;
  int code = post("/api/v1/checkin", body, &response, waitS * 1000 + kHttpTimeoutMs);
  if (code == 401 && _token.length()) {
    // Re-enrolled in Otter (or its database was reset): enroll again with the fleet key.
    OTTER_LOG("token refused, enrolling again with the fleet key");
    saveToken("");
    return false;
  }
  if (code != 200) {
    OTTER_LOG("check-in failed (HTTP %d)%s%s", code, code == 403 ? ": " : "", code == 403 ? response.c_str() : "");
    return false;
  }

  JsonDocument resp;
  if (deserializeJson(resp, response)) {
    OTTER_LOG("check-in: invalid JSON response");
    return false;
  }
  uint32_t interval = resp["checkin_interval_s"] | 0;
  if (interval > 0) _intervalS = interval;

#if defined(ESP32)
  const char *token = resp["token"] | "";
  if (*token) {
    saveToken(token);
    OTTER_LOG("enrolled: this device now has its own token");
  }
#endif

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
    order.signature = update["signature"] | "";
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

  int code = post(String("/api/v1/deployments/") + deploymentId + "/progress", body, nullptr, kHttpTimeoutMs);
  if (code != 200) OTTER_LOG("progress report got HTTP %d", code);
  return code != 409;  // 409: cancelled from the UI
}

void OtterAgent::applyUpdate(const Order &order) {
  OTTER_LOG("updating %s -> %s (%u bytes)", _cfg.version, order.version.c_str(), (unsigned)order.size);
  if (_cfg.signingKey && !order.signature.length()) {
    // No need to download it: it would be refused anyway.
    OTTER_LOG("update failed: unsigned firmware refused");
    report(order.deploymentId, "failed", 0, "unsigned firmware refused: this device only accepts signed images");
    return;
  }
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
  RadioAwake awake;  // modem sleep caps the throughput
  if (!Update.begin(order.size)) return "not enough space for the image";

  uint8_t buf[kChunkSize];
  Sha256 sha;
  size_t received = 0;
  int lastReported = 0;
  const char *err = nullptr;

  for (int connection = 0; received < order.size && !err && !cancelled; connection++) {
    if (connection > kMaxResumes) {
      err = "connection lost";
      break;
    }
    if (connection) OTTER_LOG("download stalled at %u bytes, resuming (%d/%d)", (unsigned)received, connection, kMaxResumes);
    WiFiClient plain;
    SecureClient secure(_cfg.caCert);
    HTTPClient http;
    if (!open(http, plain, secure, order.url, _cfg.caCert)) {
      err = "bad firmware URL";
      break;
    }
    http.setTimeout(kStallMs);
    authenticate(http);
    if (received) http.addHeader("Range", "bytes=" + String((unsigned)received) + "-");
    int code = http.GET();
    if (code != (received ? 206 : 200)) {
      http.end();
      if (code > 0) {
        err = "firmware download refused";  // the server said no: trying again won't help
      } else {
        delay(1000);  // couldn't reach it: counts as a resume
      }
      continue;
    }

    auto *stream = http.getStreamPtr();
    uint32_t lastData = millis();
    while (received < order.size) {
      size_t available = stream->available();
      if (!available) {
        if (!http.connected() || millis() - lastData > kStallMs) break;  // resume on a new connection
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
        lastData = millis();  // the report took time, not the server
      }
    }
    http.end();
  }

  if (!err && !cancelled) {
    uint8_t digest[32];
    char hex[65];
    sha.finish(digest);
    for (int i = 0; i < 32; i++) sprintf(&hex[i * 2], "%02x", digest[i]);
    if (!order.sha256.equalsIgnoreCase(hex)) {
      err = "sha256 mismatch";
    } else if (_cfg.signingKey) {
      err = verifySignature(_cfg.signingKey, digest, order.signature);
    }
  }
  if (err || cancelled) {
    abortUpdate();
    return err;
  }
  if (!Update.end()) return "image rejected";
  return nullptr;
}
