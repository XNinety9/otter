/*
 * An Otter device with Arduino (ESP8266 or ESP32): joins Wi-Fi, starts the Otter agent, then
 * does its job.
 *
 * Start your own sketch from this file. What to adapt is marked [ADAPT 1] to [ADAPT 5]. See
 * docs/firmware.md for the whole guide, including platformio.ini.
 *
 *   [ADAPT 1] config.app       your application's name in Otter
 *   [ADAPT 2] onCommand()      what the dashboard can ask the device to do
 *   [ADAPT 3] onConfig()       your settings, edited in the dashboard
 *   [ADAPT 4] Config           keys, certificate
 *   [ADAPT 5] loop()           your device's actual work
 *
 * The version comes from platformio.ini (custom_otter_version), Wi-Fi and server from the
 * environment at build time.
 */

#include <Arduino.h>
#include <Otter.h>

#if defined(ESP8266)
#include <ESP8266WiFi.h>
#else
#include <WiFi.h>
#endif

OtterAgent otter;

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("\nconnecting to " WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\nIP %s\n", WiFi.localIP().toString().c_str());

  // Handlers first: on ESP32 the agent starts checking in, in its own task, at begin().

  // [ADAPT 3] Settings edited in the dashboard (a JSON object, e.g. {"greeting": "hello"}),
  // kept in RAM: called at the first check-in after boot, then at each change. Parse what you
  // need, e.g. with ArduinoJson, and keep the values for loop(). On ESP32 this runs in the
  // agent's task: protect what loop() reads too, or just copy plain values.
  otter.onConfig([](const String &config) { Serial.printf("configuration: %s\n", config.c_str()); });

  // [ADAPT 2] Commands sent from the dashboard: args is a JSON object, set message (shown in
  // the dashboard) and return true on success. "reboot" and "identify" (blinks LED_BUILTIN)
  // are built in. This one answers with its arguments: replace it with yours.
  otter.onCommand("echo", [](const String &args, String &message) {
    message = args;
    return true;
  });

  OtterAgent::Config config;
  config.server = OTTER_SERVER;
  // [ADAPT 1] Your application's name: every image uploaded for this device must use it too.
  config.app = "otter-demo-arduino";
  config.version = APP_VERSION;
  config.fleetKey = OTTER_FLEET_KEY;
  // [ADAPT 4] Optional: signed updates only, and the CA of an https:// server (both passed at
  // build time, see version.py).
#ifdef OTTER_SIGNING_PUBKEY_PEM
  config.signingKey = OTTER_SIGNING_PUBKEY_PEM;  // only accept updates signed with this key
#endif
#ifdef OTTER_CA_PEM
  config.caCert = OTTER_CA_PEM;  // for an https:// server, e.g. Caddy's local CA
#endif
  otter.begin(config);
}

void loop() {
  otter.loop();  // ESP8266: checks in when due; ESP32: nothing to do, the agent has its task
  // [ADAPT 5] The device's actual work goes here. Keep it non-blocking on ESP8266 (no long
  // delay()): the agent only checks in from otter.loop(). For a battery device, call
  // otter.checkinOnce(config, seconds) instead of begin(), then deep-sleep.
}
