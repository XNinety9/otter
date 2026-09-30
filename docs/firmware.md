# Put Otter in your firmware

The Otter agent is the part of your firmware that talks to the server: it checks in, applies
updates, runs the commands and receives the settings you send from the dashboard. This guide
shows what to put in your firmware, and what to change in the examples to make them your own.

There are two agents, with the same features unless noted:

- **ESP-IDF** (ESP32 family): the component in `firmware/components/otter`, example in
  `firmware/examples/esp32-idf`. Recommended for ESP32: bootloader rollback, patches and
  compressed downloads, crash reports.
- **Arduino** (ESP8266 and ESP32): the library in `firmware/arduino/Otter`, example in
  `firmware/examples/arduino`.

In both examples, what you have to adapt is marked `[ADAPT]` in the code.

## Three names that matter

Otter matches firmware images to devices with three values:

| Name | What it is | Where it comes from |
|---|---|---|
| **App** | The kind of device: `weather-station`, `garage-door`… | Your code (`app_name`, `config.app`), and every image you upload |
| **Hardware** | The chip: `esp32`, `esp32c3`, `esp32c6`, `esp8266`… | Detected by the agent; given when uploading an image |
| **Version** | `1.4.2`: compared part by part, so `1.10.0` is newer than `1.9.0` | Your build (`CMakeLists.txt`, `platformio.ini` or `OTTER_VERSION`) |

A device only gets images with its app and hardware. The version must change with every image
you upload: a device that comes back from an update still reporting its old version counts as a
failed update (the bootloader rolled it back).

## ESP-IDF

### Starting from the example

Copy `firmware/examples/esp32-idf` and `firmware/components` into your repository (or add this
repository as a git submodule), then go through these files:

| File | What to change |
|---|---|
| `src/main.c` | `[ADAPT 1]` the app name, `[ADAPT 2]` your commands, `[ADAPT 3]` your settings, `[ADAPT 4]` the agent's options, `[ADAPT 5]` your device's work |
| `CMakeLists.txt` | The version, and the path to the `components` folder |
| `platformio.ini` | One environment per board you build for |
| `sdkconfig.defaults` | Your board's flash size |
| `partitions.csv` | Bigger slots if your flash is bigger than 4 MB (see [Partitions](#partitions)) |
| `src/idf_component.yml` | `led_strip` only serves the example's `identify` command: drop it if you drop that |
| `firmware/apps.json` | Your app, its project folder, and the hardware of each environment, for `tools/release.sh` |

The Wi-Fi code in `main.c` (with `improv.c`, see [Wi-Fi](#wi-fi)) can stay as it is.

### Adding Otter to an existing project

1. **The component.** Copy `firmware/components/otter` into your project's `components` folder,
   or add its parent folder to `EXTRA_COMPONENT_DIRS` in your `CMakeLists.txt`. Its other
   dependencies (cJSON, mDNS, delta patches) come from the ESP component registry by themselves.
2. **The settings.** Add to your `sdkconfig.defaults`, then delete the generated `sdkconfig` so
   they apply:

    ```
    # Required: a broken update rolls back
    CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y
    CONFIG_PARTITION_TABLE_CUSTOM=y
    CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
    # Recommended: downloads that hold up on weak Wi-Fi
    CONFIG_LWIP_TCP_WND_DEFAULT=23040
    CONFIG_LWIP_TCP_RECVMBOX_SIZE=32
    CONFIG_ESP_WIFI_RX_BA_WIN=16
    # Optional: crash reports (with a coredump partition)
    CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y
    CONFIG_ESP_COREDUMP_DATA_FORMAT_ELF=y
    ```

3. **The partitions**: two OTA slots, see [Partitions](#partitions).
4. **The code**, once the network is up and `nvs_flash_init()` done:

    ```c
    #include "otter_agent.h"

    otter_config_t otter = {
        .server_url = "http://192.168.1.10:8000", // NULL: found over mDNS
        .app_name = "weather-station",
        .fleet_key = "…",                          // if the server has one
    };
    ESP_ERROR_CHECK(otter_start(&otter));          // runs in its own task
    ```

The agent needs the network up before it starts, but you don't need to wait for the server:
it keeps trying in the background.

### Partitions

An update is written to the OTA slot that isn't running, then the device boots it: the
partition table needs `otadata` and two app slots big enough for your firmware. The example's
`partitions.csv` fits a 4 MB flash, with two 1.9 MB slots and 64 KB for crash reports:

```
nvs,      data, nvs,      0x9000,   0x4000
otadata,  data, ota,      0xd000,   0x2000
phy_init, data, phy,      0xf000,   0x1000
ota_0,    app,  ota_0,    0x10000,  0x1E0000
ota_1,    app,  ota_1,    0x1F0000, 0x1E0000
coredump, data, coredump, 0x3D0000, 0x10000
```

With a bigger flash, make the slots bigger and add your own data partitions after them.
Devices report the size of their slot, and Otter doesn't send them images that don't fit. **A partition
table can't be changed over the air**: flash it over USB, so choose it before your devices go
to their places.

### The agent's settings

`otter_config_t`, in `otter_agent.h`:

| Field | Default | Meaning |
|---|---|---|
| `server_url` | found over mDNS | `http://…` or `https://…`, no trailing slash. Without it, the agent finds a server started with `OTTER_MDNS=1` |
| `app_name` | required | See [Three names that matter](#three-names-that-matter) |
| `fleet_key` | none | The server's `OTTER_FLEET_KEY`: the device enrolls with it and then uses a token of its own |
| `hw` | the chip | Rarely needed: only if two boards with the same chip need different images |
| `version` | the app description (`PROJECT_VER`) | Rarely needed |
| `cert_pem` | the ESP-IDF certificate bundle | The CA certificate of an `https://` server with a private CA (e.g. Caddy's) |
| `signing_key_pem` | none | Only accept images signed with this key: see [Signed firmware](../README.md#signed-firmware) |
| `manual_mark_valid` | `false` | See [Validating a new firmware](#validating-a-new-firmware) |
| `rollback_timeout_s` | 300 | See below |

The strings must stay valid for the whole program: string literals or static buffers.

### Validating a new firmware

After an update, the new firmware runs "on trial": if it never confirms itself, the bootloader
goes back to the previous one at the next reset. By default the agent confirms it as soon as it
reaches the server. That proves the network works, not that your device does. To confirm it
yourself, once your sensors read and your relays switch:

```c
otter_config_t otter = { …, .manual_mark_valid = true };
…
if (sensors_ok()) {
    otter_mark_valid();
}
```

A firmware not confirmed within `rollback_timeout_s` (5 minutes by default) is rolled back, and
Otter shows the update as failed.

### Commands

Commands are sent from the dashboard, to one device or to a selection. Register yours before
`otter_start()`:

```c
static esp_err_t open_door(const char *args, char *result, size_t result_size, void *ctx)
{
    // args is a JSON object: "{}" or e.g. {"seconds": 5}; parse it with cJSON if needed
    relay_on();
    vTaskDelay(pdMS_TO_TICKS(1000));
    relay_off();
    snprintf(result, result_size, "door opened");  // shown in the dashboard
    return ESP_OK;                                 // or an error: the command shows as failed
}

otter_register_command("open_door", open_door, NULL);
```

Names use lowercase letters, digits and `_`. Handlers run in the agent's task, one after the
other: keep them short, or hand the work to your own task. `reboot` is built in, and so is
`identify`, which only logs until you register your own (the example blinks an LED whose pin
comes from the configuration: `led_gpio`, `led_type`). Arguments named like a secret
(`password`, `key`, `token`…) are masked in the dashboard.

### Configuration

Settings edited in the dashboard, per device or per tag, reach the device within a second and
stay in its flash, so they apply from boot even offline:

```c
int interval = otter_config_get_int("interval_s", 60);            // default when missing
bool heater = otter_config_get_bool("heater", false);
char unit[8];
otter_config_get_str("unit", unit, sizeof(unit), "C");

static void on_config(const char *json, void *ctx) { /* react to a change */ }
otter_on_config(on_config, NULL);
```

The getters can be called from any task. The keys are yours: document them for whoever edits
them in the dashboard.

### Battery devices

A device that deep-sleeps can't keep a connection open. Call `otter_checkin_once()` instead of
`otter_start()` at each wake-up, then sleep:

```c
do_the_measurement();
otter_checkin_once(&otter, 600);          // commands, settings, updates; back in 10 min
esp_deep_sleep(600ULL * 1000000);
```

Otter then shows the device as online while it sleeps. An update is applied at the next
wake-up. A new firmware that can't reach the server for 3 wake-ups in a row is rolled back. The
example's `esp32c6-sleepy` environment does this.

### Wi-Fi

The agent doesn't manage Wi-Fi: your firmware connects, the agent uses the connection. The
example's Wi-Fi code is worth copying as it is:

- the network comes from `WIFI_SSID` and `WIFI_PASS` at build time, or from the one saved on
  the device: one firmware image can then serve every network;
- `improv.c` lets you set the network over USB right after flashing (from a browser with ESP Web
  Tools, or `tools/improv.py`);
- the `set_wifi` command moves a device to another network from the dashboard, and back if the
  new one doesn't work.

## Arduino

### In your sketch

Add the library (a copy of `firmware/arduino/Otter`, or its path in `lib_deps`), then:

```cpp
#include <Otter.h>

OtterAgent otter;

void setup() {
  // … connect to Wi-Fi first
  otter.onCommand("open_door", [](const String &args, String &message) {
    openDoor();
    message = "door opened";
    return true;
  });
  otter.onConfig([](const String &json) { /* parse with ArduinoJson */ });

  OtterAgent::Config config;
  config.server = "http://192.168.1.10:8000";
  config.app = "garage-door";
  config.version = "1.2.0";
  config.fleetKey = "…";
  otter.begin(config);
}

void loop() {
  otter.loop();
  // your work, without long delay() on ESP8266
}
```

`firmware/examples/arduino/src/main.cpp` does the same, with `[ADAPT]` markers. Its version comes
from `custom_otter_version` in `platformio.ini` (or `OTTER_VERSION`). `identify` is built in: it
blinks `LED_BUILTIN` when the board defines one.

### `OtterAgent::Config`

| Field | Meaning |
|---|---|
| `server` | Required: `http://…` or `https://…` |
| `app`, `version` | Required: see [Three names that matter](#three-names-that-matter) |
| `fleetKey` | The server's fleet key |
| `hw` | Defaults to the chip |
| `caCert` | Required for an `https://` server: the CA certificate (PEM) to check it against |
| `signingKey` | Only accept images signed with this public key (ESP32) |

### ESP8266 and ESP32 differences

| | ESP32 | ESP8266 |
|---|---|---|
| Check-ins | In the agent's own task, long polling: commands arrive within a second | From `otter.loop()`, every 30 s by default |
| Where handlers run | In the agent's task: register them before `begin()` | Inside `otter.loop()` |
| Device token | Kept in NVS | Not used: it keeps the fleet key |
| Signed firmware | Checked | Not supported: given a key, it refuses every update |
| HTTPS | `caCert` | `caCert`, and the clock must be set first (`configTime()`) |
| Battery devices | `otter.checkinOnce(config, seconds)` instead of `begin()` | Same (`ESP.deepSleep()` needs GPIO16 wired to RST) |

The Arduino library downloads plain images: patches, compressed downloads and crash reports
are only in the ESP-IDF agent.

## Build-time settings

Both examples read these environment variables when they build. Put them in `firmware/.env`,
which git ignores, and `source` it: never commit a Wi-Fi password or a key.

| Variable | Meaning | Without it |
|---|---|---|
| `WIFI_SSID`, `WIFI_PASS` | The Wi-Fi network | ESP-IDF: the network saved on the device, or Improv |
| `OTTER_SERVER` | The server's URL | ESP-IDF: found over mDNS |
| `OTTER_FLEET_KEY` | The server's fleet key | No key sent |
| `OTTER_CA_CERT` | CA certificate for `https://` (PEM, or its path) | ESP-IDF: public CAs; Arduino: plain HTTP only |
| `OTTER_SIGNING_PUBKEY` | Only accept images signed with this key (`tools/release.sh` derives it from `OTTER_SIGNING_KEY`) | Unsigned images accepted |
| `OTTER_VERSION` | The version | `CMakeLists.txt` / `platformio.ini` |

The version is read when the build is configured: after changing it, clean the build
(`rm -rf .pio/build/<env>`). `tools/release.sh` always does.

## Shipping updates

Once the first firmware is on your devices (over USB), the next ones go through Otter:

```sh
tools/release.sh weather-station 1.3.0   # builds every environment, uploads each image
```

`tools/release.sh` reads `firmware/apps.json` to know your app's project and environments:

```json
"weather-station": {
  "project": "firmware/weather-station",
  "environments": { "esp32c6": "esp32c6", "esp32": "esp32" }
}
```

Each key of `environments` is a PlatformIO environment, each value the hardware name Otter
knows the devices by. Then deploy from the dashboard: to a device, a tag, a release channel, or
as a staged rollout. See the guide for [releases from CI](../README.md#release-firmware-from-ci)
and [signed firmware](../README.md#signed-firmware).

## Checklist

- [ ] The app name is the same in the code and in every image you upload.
- [ ] The version changes with every image, and the build is clean after changing it.
- [ ] Two OTA slots, big enough, and `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` (ESP-IDF).
- [ ] With `manual_mark_valid`, your code calls `otter_mark_valid()` once the device works.
- [ ] Command handlers are short; on ESP32 with Arduino, registered before `begin()`.
- [ ] Wi-Fi password and keys come from the environment, never from committed files.
- [ ] Before flashing a fleet: the partition table is final.
