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
    const char *fleetKey = nullptr;  // optional, sent as X-Otter-Key
    const char *hw = nullptr;        // optional, defaults to "esp8266" / the ESP32 target
  };

  // Strings in config must stay valid for the program's lifetime.
  void begin(const Config &config);

  // Call from loop(). Checks in when due (non-blocking otherwise). While an update is
  // applied it blocks, then reboots into the new firmware.
  void loop();

  // Forces a check-in on the next loop().
  void checkinNow() { _waitMs = 0; }

  // Handler of a remote command sent from Otter. args is its arguments as a JSON object
  // ("{}" without any). Set message (optional, shown in the dashboard) and return true on
  // success. Runs inside loop(): keep it short.
  using CommandHandler = std::function<bool(const String &args, String &message)>;

  // Registers, or replaces, the handler of a remote command (up to 8). Built in: "reboot",
  // and "identify", which blinks LED_BUILTIN when the board defines it.
  bool onCommand(const char *name, CommandHandler handler);

 private:
  struct Order {
    int deploymentId = 0;
    String version, url, sha256;
    size_t size = 0;
  };

  bool checkin(Order &order);
  void runCommand(int id, const char *name, const String &args);
  void applyUpdate(const Order &order);
  const char *flash(const Order &order, bool &cancelled);
  bool report(int deploymentId, const char *state, int progress, const char *error = nullptr);
  int post(const String &path, const String &body, String *response);

  Config _cfg;
  uint32_t _intervalS = 30;
  uint32_t _lastCheckin = 0;
  uint32_t _waitMs = 0;

  static constexpr int kMaxCommands = 8;
  struct Command {
    const char *name = nullptr;
    CommandHandler handler;
  };
  Command _commands[kMaxCommands];
  bool _rebootRequested = false;
  bool _checkinAgain = false;
};
