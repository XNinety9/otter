#pragma once

// Otter agent for Arduino (ESP8266 / ESP32): checks in with an Otter server and
// applies OTA updates. Protocol: docs/protocol.md.

#include <Arduino.h>

#include <functional>

#ifndef OTTER_LOG
#define OTTER_LOG(fmt, ...) Serial.printf("[otter] " fmt "\n", ##__VA_ARGS__)
#endif

class OtterAgent {
 public:
  struct Config {
    const char *server = nullptr;    // required, e.g. "http://192.168.1.10:8000"
    const char *app = nullptr;       // required, firmware application name
    const char *version = nullptr;   // required, running firmware version
    const char *fleetKey = nullptr;  // optional, sent as X-Otter-Key until the device has a token
    const char *hw = nullptr;        // optional, defaults to "esp8266" / the ESP32 target
    // Optional. Public key (PEM) updates must be signed with: other images are refused
    // (ESP32; an ESP8266 given a key refuses every update, it can't check signatures yet).
    const char *signingKey = nullptr;
  };

  // Strings in config must stay valid for the program's lifetime.
  // ESP32: the agent runs in its own task and long-polls the server, so updates, commands
  // and configuration changes arrive within a second or two; loop() has nothing to do.
  // ESP8266: call loop() from your loop(), it checks in every checkin_interval_s.
  void begin(const Config &config);

  // ESP8266 (harmless on ESP32): checks in when due. While an update is applied it blocks,
  // then reboots into the new firmware.
  void loop();

  // Checks in as soon as possible.
  void checkinNow();

  // For devices that deep-sleep between check-ins, instead of begin(): checks in once
  // (no long polling), runs the commands, applies the configuration and any pending update
  // (which restarts the device into it), then returns. nextCheckinS is when the device will
  // check in again, so Otter doesn't show it offline meanwhile. Returns whether the server
  // answered.
  bool checkinOnce(const Config &config, uint32_t nextCheckinS);

  // Handler of a remote command sent from Otter. args is its arguments as a JSON object
  // ("{}" without any). Set message (optional, shown in the dashboard) and return true on
  // success. Runs in the agent's task on ESP32, inside loop() on ESP8266: keep it short.
  using CommandHandler = std::function<bool(const String &args, String &message)>;

  // Registers, or replaces, the handler of a remote command (up to 8). Built in: "reboot",
  // and "identify", which blinks LED_BUILTIN when the board defines it.
  bool onCommand(const char *name, CommandHandler handler);

  // Remote configuration edited in Otter (a JSON object of settings, per tag and per device).
  // The handler runs at the first check-in after boot, then each time the configuration
  // changes (in the agent's task on ESP32). It is kept in RAM only: parse what you need,
  // e.g. with ArduinoJson.
  using ConfigHandler = std::function<void(const String &configJson)>;
  void onConfig(ConfigHandler handler) { _configHandler = handler; }
  const String &config() const { return _configJson; }

 private:
  struct Order {
    int deploymentId = 0;
    String version, url, sha256, signature;
    size_t size = 0;
  };

  void setup(const Config &config);
  uint32_t cycle();
  static void task(void *agent);
  bool checkin(Order &order);
  void runCommand(int id, const char *name, const String &args);
  void applyUpdate(const Order &order);
  const char *flash(const Order &order, bool &cancelled);
  bool report(int deploymentId, const char *state, int progress, const char *error = nullptr);
  int post(const String &path, const String &body, String *response, uint32_t timeoutMs);
  void authenticate(class HTTPClient &http);
  void saveToken(const String &token);

  Config _cfg;
  uint32_t _intervalS = 30;
  uint32_t _lastCheckin = 0;
  uint32_t _waitMs = 0;
  bool _longPoll = false;      // ESP32 with begin(): the agent's task can wait on the server
  bool _oneShot = false;       // checkinOnce()
  uint32_t _nextCheckinS = 0;  // announced by checkinOnce()
  void *_task = nullptr;       // ESP32: the agent's FreeRTOS task
  String _token;               // this device's own token (ESP32, kept in NVS)

  static constexpr int kMaxCommands = 8;
  struct Command {
    const char *name = nullptr;
    CommandHandler handler;
  };
  Command _commands[kMaxCommands];
  bool _rebootRequested = false;
  String _configJson = "{}", _configVersion;
  ConfigHandler _configHandler;
  bool _checkinAgain = false;
};
