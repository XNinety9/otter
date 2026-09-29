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

  OtterAgent::Config config;
  config.server = OTTER_SERVER;
  config.app = "otter-demo-arduino";
  config.version = APP_VERSION;
  config.fleetKey = OTTER_FLEET_KEY;
  otter.begin(config);
  // Remote configuration from the dashboard, e.g. {"greeting": "hello"}.
  otter.onConfig([](const String &config) { Serial.printf("configuration: %s\n", config.c_str()); });
  // A remote command to try from the dashboard: answers with its arguments.
  otter.onCommand("echo", [](const String &args, String &message) {
    message = args;
    return true;
  });
}

void loop() {
  otter.loop();
  // The device's actual work goes here (keep it non-blocking).
}
