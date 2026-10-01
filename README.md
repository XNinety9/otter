<p align="center">
  <img src="docs/logo_readme.png" alt="Otter" width="240">
</p>

# Otter

[![CI](https://github.com/XNinety9/otter/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/XNinety9/otter/actions/workflows/ci.yml)

Fleet tracking and centralized OTA updates for home-made ESP32 / ESP8266 devices.

**Website and docs: [xninety9.github.io/otter](https://xninety9.github.io/otter)**

- **Inventory**: every device checks in periodically with its MAC, IP, firmware version,
  signal strength and uptime, and tells what it's made of: exact chip (`ESP32-S3 (QFN56)`,
  `ESP8266EX`…), flash and PSRAM sizes, radios. The dashboard opens on a fleet overview (online,
  updating, outdated, failed), and each device has a details page.
- **Firmware registry**: upload `.bin` images, versioned per application and hardware family.
- **OTA deployments**: target one device, a selection, a tag, or roll out a version to every device
  of an app. Progress streams live to the browser.
- **Staged rollouts**: release a firmware progressively (e.g. 10 %, 50 %, 100 % of the devices,
  optionally within a tag). Each stage starts once the previous one succeeded and a soak time
  has passed; if failures exceed a threshold, the rollout halts before touching the rest of the
  fleet. Canaries are picked among online devices. Pause, resume, skip to the next stage or abort
  from the UI.
- **Release channels**: publish a firmware on a channel (`stable`, `beta`, anything) and let devices
  follow one: they are updated automatically, never downgraded. A device follows its channel and
  `stable`, so beta testers get the newest of both. Publishing can also go through a staged rollout.
- **Tags**: group devices (`living-room`, `test-bench`…), filter the dashboard by tag and roll out
  to a tag. A tag deployment only targets the tagged devices running the firmware's app and
  hardware, and skips those already on that version.

Devices *pull*: they never expose a port, sleepy devices work, and NAT is not a problem.
The contract between devices and server is described in [docs/protocol.md](docs/protocol.md).

## Layout

```
server/        FastAPI + SQLite backend, serves the web UI (server/otter/static)
tools/         simulate.py (fake devices), push.sh (upload), release.sh (build and publish), make_logos.py
docs/          protocol spec, logo artwork
site/          website (GitHub Pages): landing page, demo capture, docs built from this README
deploy/        Caddyfile for docker-compose.https.yml
firmware/
  components/otter/   ESP-IDF component (ESP32 family, with bootloader rollback)
  arduino/Otter/      Arduino library (ESP8266 and ESP32)
  examples/           ready-to-flash PlatformIO projects
```

## Run the server

```sh
cd server
uv run uvicorn otter.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000.

| Variable                 | Default        | Purpose                                                  |
|--------------------------|----------------|----------------------------------------------------------|
| `OTTER_DATA_DIR`         | `./data`       | SQLite database and firmware images                      |
| `OTTER_CHECKIN_INTERVAL` | `30`           | Seconds between device check-ins                         |
| `OTTER_FLEET_KEY`        | *(empty)*      | Shared secret devices enroll with (`X-Otter-Key`)        |
| `OTTER_DEVICE_APPROVAL`  | *(off)*        | `1`: new devices wait for an approval in the dashboard   |
| `OTTER_SIGNING_PUBLIC_KEY` | *(none)*     | Refuse firmware uploads not signed with this key         |
| `OTTER_MDNS`             | *(off)*        | `1`: advertise the server on the LAN (`_otter._tcp`)     |
| `OTTER_MDNS_PORT`        | `8000`         | Port advertised (without `OTTER_PUBLIC_URL`)             |
| `OTTER_PUBLIC_URL`       | *(from request)* | Base URL put in download links, e.g. `http://10.0.0.5:8000` |

## Run with Docker

```sh
docker compose up -d
```

The UI is on port 8000; the database and firmware images live in the `otter-data` volume.
Settings (`OTTER_FLEET_KEY`, `OTTER_CHECKIN_INTERVAL`, `OTTER_PUBLIC_URL`) are read from the
environment or a `.env` file next to `docker-compose.yml`.

Prebuilt images for amd64 and arm64 (e.g. Raspberry Pi) are published from the `dev` branch:

```sh
docker run -d --name otter -p 8000:8000 -v otter-data:/data ghcr.io/xninety9/otter:dev
```

The container runs as UID 1000. With a bind mount instead of a volume, make sure that user can
write to the directory. `GET /healthz` reports whether the server and its database are up.

## HTTPS

Over plain HTTP, anyone on your network can read dashboard passwords, session cookies and the
fleet key, and could even serve a malicious firmware to a device (see
[Security](docs/protocol.md#security)). Put Otter behind HTTPS with the Caddy overlay:

```sh
# On a LAN: certificates from Caddy's own CA
OTTER_DOMAIN=otter.lan docker compose -f docker-compose.yml -f docker-compose.https.yml up -d

# With a public domain pointing at this host: Let's Encrypt
OTTER_DOMAIN=otter.example.com OTTER_TLS=you@example.com \
  docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
```

- Otter's own port 8000 is no longer published: everything goes through Caddy on 80/443 (HTTP
  redirects to HTTPS). `OTTER_PUBLIC_URL` defaults to `https://$OTTER_DOMAIN`. With other ports
  (`OTTER_HTTP_PORT`, `OTTER_HTTPS_PORT`), set `OTTER_PUBLIC_URL` to include the port.
- `OTTER_DOMAIN` must be the name devices and browsers use, resolvable on your network (router DNS,
  `/etc/hosts`…). Without a DNS name, the host's IP address works too with the local CA
  (`OTTER_DOMAIN=192.168.1.10`, devices use `https://192.168.1.10`).
- With Caddy's local CA, browsers and devices must trust its root certificate:

    ```sh
    docker compose -f docker-compose.yml -f docker-compose.https.yml \
      cp caddy:/data/caddy/pki/authorities/local/root.crt otter-ca.pem
    ```

    Import `otter-ca.pem` in your browser or system trust store, and build it into the devices:
    `OTTER_CA_CERT=/path/to/otter-ca.pem` when building the ESP-IDF or Arduino example (or
    `cert_pem` of the ESP-IDF agent, `config.caCert` of the Arduino library, in your own firmware). `tools/release.sh` reads the same variable for its uploads,
    `tools/push.sh` needs `CURL_CA_BUNDLE=otter-ca.pem`, and the simulator takes `--ca otter-ca.pem`.
    With Let's Encrypt, ESP-IDF devices need nothing: the ESP-IDF certificate bundle trusts it
    (Arduino: give Let's Encrypt's root, ISRG Root X1, as the CA).
    The local CA's root certificate is valid for 10 years; renewing Caddy's short-lived
    certificates doesn't concern devices.

- Without Docker, any TLS reverse proxy works. Uvicorn only trusts `X-Forwarded-*` headers from
  `127.0.0.1` by default; if the proxy runs elsewhere, set `FORWARDED_ALLOW_IPS` to its address,
  and never expose Otter's port directly when you do.

## Accounts and API tokens

The dashboard and its API require an account. Create the first one on the server:

```sh
cd server && uv run python -m otter.cli create-user admin
# with Docker:
docker compose exec otter python -m otter.cli create-user admin
```

Upgrading from a version without accounts: the dashboard shows a login page until you create one.

Scripts, CI and Prometheus use API tokens instead (`Authorization: Bearer otk_…`):

```sh
uv run python -m otter.cli create-token push --user admin   # printed once
uv run python -m otter.cli list-tokens
uv run python -m otter.cli revoke-token push
```

`tools/push.sh` sends `$OTTER_TOKEN` when it is set. Other commands: `set-password` (closes the
user's sessions), `list-users`, `delete-user`.

Passwords are hashed with argon2; sessions and tokens are stored as SHA-256 hashes only. Logins are
limited to 10 failures per address every 5 minutes. **Serve Otter over HTTPS as soon as it leaves
your desk**: over plain HTTP, passwords and cookies can be sniffed on the network (see [HTTPS](#https)).

### Device credentials

Devices enroll with the fleet key and then get a token of their own (kept in NVS by the ESP-IDF
agent and by the Arduino library on ESP32; an ESP8266 keeps using the fleet key): once a device
uses it, the fleet key alone can't impersonate it. From a device's panel,
**Revoke** blocks it and **Re-enroll** lets it get a new token. Set `OTTER_DEVICE_APPROVAL=1` to
approve each new device before it can check in. Details: [Authentication](docs/protocol.md#authentication).

### Signed firmware

Sign images with a key that never leaves your machine (or your CI secrets), and build devices that
only accept images signed with it: whoever controls the server or the network can't push theirs.

```sh
openssl ecparam -name prime256v1 -genkey -noout -out otter-signing.key   # keep it secret
export OTTER_SIGNING_KEY=$PWD/otter-signing.key
tools/release.sh otter-demo 1.2.0
```

With `OTTER_SIGNING_KEY` (a private key PEM, or its path), `tools/release.sh` builds the firmware
with the matching public key (`OTTER_SIGNING_PUBKEY`, the ESP-IDF agent's `signing_key_pem`) and
uploads each image with its signature; `tools/push.sh` signs too. Such devices refuse unsigned
images before downloading them, and images signed with another key before switching partitions:
the deployment fails with a clear error. The first signed-only firmware has to reach a device
the usual way (USB, or an update it still accepts unsigned).

To catch mistakes at upload, give the server the public key: `OTTER_SIGNING_PUBLIC_KEY` (PEM, or
its path; `openssl pkey -in otter-signing.key -pubout`). It then refuses unsigned or wrongly
signed images. In CI, store the private key in the `OTTER_SIGNING_KEY` secret. ECDSA P-256 is
recommended; RSA keys work too. The Arduino library checks signatures on ESP32 (`config.signingKey`;
its example takes `OTTER_SIGNING_PUBKEY` too); an ESP8266 given a key refuses every update, it
can't check them yet.

## Try it without hardware

```sh
uv run --with httpx tools/simulate.py --count 8 --fail-rate 0.1
```

Then, in the UI, upload any file starting with byte `0xE9` as a firmware for one of the simulated apps
(`weather-station`/`esp32`, `plant-sensor`/`esp8266`, `led-strip`/`esp32c3`) and hit **Roll out**:

```sh
python3 -c "import sys; sys.stdout.buffer.write(b'\xe9' + b'x' * 300_000)" > fake.bin
```

## Flash a real device

To put Otter in your own firmware, or adapt the examples to your devices, see
[Put Otter in your firmware](docs/firmware.md).

The server must listen on the LAN (`--host 0.0.0.0`) and `OTTER_SERVER` must be the
address devices can reach, e.g. your machine's LAN IP.

```sh
export WIFI_SSID=... WIFI_PASS=... OTTER_SERVER=http://192.168.1.10:8000

# ESP32 with ESP-IDF (bootloader rollback enabled): -e esp32, esp32c6 or esp32s3
cd firmware/examples/esp32-idf
pio run -e esp32 -t upload -t monitor

# or Arduino: ESP8266 (-e esp8266, or esp8266-1m for 1 MB boards) or ESP32 (-e esp32)
cd firmware/examples/arduino
pio run -e esp8266 -t upload -t monitor
```

The device shows up in the dashboard within seconds.

**Without `WIFI_SSID`**, the ESP-IDF example uses the network it saved last, or waits for one
over its serial port (see [Wi-Fi without recompiling](#wi-fi-without-recompiling)): one firmware
image for every network.

**Without `OTTER_SERVER`**, the ESP-IDF agent finds the server on the LAN over mDNS, if it runs
with `OTTER_MDNS=1`: then the server's IP can change without reflashing anything. Devices look
for it again after 3 failed check-ins in a row. The advertisement carries `OTTER_PUBLIC_URL` when
set (e.g. an `https://` name behind Caddy), else the host's addresses and `OTTER_MDNS_PORT`.
Multicast doesn't leave Docker's default bridge network: run Otter on the host, or with
`network_mode: host`, for this to work (and let the host's firewall accept Otter's port). To ship an update, bump the version
(`PROJECT_VER` in the ESP-IDF example's `CMakeLists.txt`, `[otter] version` in the Arduino
`platformio.ini`), rebuild, push the image and deploy it from the UI:

```sh
pio run
OTTER_TOKEN=otk_… ../../../tools/push.sh .pio/build/esp32/firmware.bin otter-demo esp32 1.0.1
```

With ESP-IDF, a new firmware is marked valid once it reaches the server (or when the app calls
`otter_mark_valid()` if `manual_mark_valid` is set). If it never does within
`rollback_timeout_s`, or crashes before, the bootloader reverts to the previous slot and Otter
reports the deployment as failed. The ESP8266 has no such safety net: only the image header
and SHA-256 are checked before rebooting.

### Install on a new board from the browser

Once a firmware is in Otter, new boards can get it without PlatformIO: plug the board into
your computer, click **⚡ Flash a new board** in the Firmwares section (or **Install…** on a
firmware), pick the board's serial port, and the dashboard erases it and writes the firmware
with [ESP Web Tools](https://esphome.github.io/esp-web-tools/). With the ESP-IDF example, which
speaks Improv, you then pick its Wi-Fi network in the same dialog.

- Builds of the examples also make `factory.bin`, the whole flash (bootloader, partitions, app),
  with `firmware/scripts/factory_image.py`; `tools/push.sh` and `tools/release.sh` upload it
  with the image. Add the script to your own projects' `extra_scripts` for the same.
- USB access from a web page (Web Serial) needs Chrome or Edge, and a secure page: open the
  dashboard on `http://localhost:8000` from the computer the board is plugged into, or over
  [HTTPS](#https).
- ESP Web Tools loads from unpkg.com: the browser needs Internet access.
- A board that was already in Otter and gets erased loses its device token: re-enroll it in its
  details page.

## Remote commands

From a device's panel, or for the selected devices in the list: **Reboot**, **Identify** (blink
its LED to find it on a shelf) or any command the firmware knows, with JSON arguments. They
reach a long-polling device within a second or two, and the panel shows each device's answer.

Register your own in the firmware:

```c
// ESP-IDF
static esp_err_t set_level(const char *args, char *result, size_t size, void *ctx)
{
    // args: {"level": 2}, parse it with cJSON
    snprintf(result, size, "level set");
    return ESP_OK;
}
otter_register_command("set_level", set_level, NULL);
```

```cpp
// Arduino
otter.onCommand("set_level", [](const String &args, String &message) {
  message = "level set";
  return true;
});
```

`reboot` and `identify` are built in; override `identify` to blink your own LED (the ESP-IDF
example drives the ESP32-C6-DevKitC-1's RGB LED). With the Arduino library, an ESP32 long-polls
too (the agent runs in its own task: register handlers before `begin()`); an ESP8266 receives
commands at its check-ins, every 30 s by default.

## Wi-Fi without recompiling

The ESP-IDF example speaks [Improv Wi-Fi](https://www.improv-wifi.com/) on its serial port: right
after flashing it (even a build without `WIFI_SSID`), give it a network from a browser with
[ESP Web Tools](https://esphome.github.io/esp-web-tools/) or Home Assistant, or from a terminal:

```sh
uv run --with pyserial tools/improv.py /dev/ttyACM0 --ssid MyNetwork   # asks for the password
uv run --with pyserial tools/improv.py /dev/ttyACM0 --state            # --info, --scan too
```

The network is saved on the device and used again at every boot. To move a device that is still
online to another network (new router, moving house), send it the `set_wifi` command from its
panel, with `{"ssid": "…", "password": "…"}`: if it can't join the new network within about 40 s,
it goes back to the previous one and the command fails. Otter never shows secret arguments
(`password`, `key`, `token`…) and erases them once the device has answered.

## Battery devices (deep sleep)

A device that deep-sleeps between measurements can't long-poll or run a background task. Call
`otter_checkin_once()` instead of `otter_start()` at each wake-up: it checks in once, runs the
commands, applies the configuration and any pending update, then returns, and you go back to
sleep. It tells the server when it will be back, so the dashboard doesn't show it offline
meanwhile ("sleeps, wakes every 10 min" in its panel).

```c
otter_checkin_once(&otter, 600);                  // back in 10 minutes
esp_deep_sleep(600ULL * 1000000);
```

Updates, commands and configuration changes wait for the next wake-up. A new firmware that can't
reach the server for 3 wake-ups in a row is rolled back. The ESP-IDF example has a sleepy variant
(`pio run -e esp32c6-sleepy`, app `otter-sleepy`, 60 s of sleep). With the Arduino library, call
`otter.checkinOnce(config, 600)` instead of `begin()`.

## Compressed and delta updates

Otter keeps a zlib copy of each image that compresses well (ESP32 images: about 40 % smaller) and
the ESP-IDF agent downloads that, inflating it on the fly with the decompressor in the chip's
ROM. Better still, when a device runs an image Otter knows (same app, hardware and version), the
update comes as a patch from that image ([detools](https://pypi.org/project/detools/), applied by
Espressif's `esp_delta_ota`): a few tens of KB instead of a megabyte. The device checks that it
really runs the patch's base first, and a retry downloads the whole image. Nothing to configure:
the smallest transfer is picked each time. Other agents keep downloading the plain image.

## Live logs

**Live logs** in a device's details page shows its log output as it runs, without a USB cable:
the device hears about it at once (through its long poll), sends the last lines it kept, then
new ones every 2 s while the page stays open, for 10 minutes at a time renewed by the page.
Nothing flows when nobody watches. The server keeps the last 500 lines per device, in memory
only. The ESP-IDF agent hooks `esp_log` for this (lines still go to the serial port); the
Arduino library doesn't send logs.

## Crash reports

When an ESP-IDF device crashes, the core dump stays in its `coredump` partition; at its next
check-in the agent sends a summary (task, panic reason, registers, stack) and erases it. Otter
matches it with the firmware it came from and, if it has that firmware's ELF file, shows a
readable call stack in the device's panel:

```
abort() was called at PC 0x42000631 on core 0
  panic_abort+18      panic.c:509
  abort+109           abort.c:38
  app_main+331        main.c:168
```

`tools/push.sh` and `tools/release.sh` upload the `firmware.elf` found next to the image; add it
later with `POST /api/firmwares/{id}/elf`. Decoding happens on the server in Python (no ESP-IDF
toolchain needed), and Otter checks that the ELF is the one the image was built from. On RISC-V
chips (C3, C6…) the device can't unwind its stack: after the crash address and the return
address, the callers are code addresses found on the stack, most likely but not certainly
right (shown faded). Crash counts appear next to each firmware version.

Your project needs a `coredump` data partition (the example's `partitions.csv` has one, 64 KB at
the end of a 4 MB flash) and `CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y`. A new partition table only
reaches a device over USB. The ELF files take room: about 4 MB per image compressed.

## Remote configuration

Settings that shouldn't need a new firmware (intervals, thresholds, feature flags): edit them in a
device's panel, for the device itself or for one of its tags, as a JSON object of strings, numbers
and booleans. The device's own values win over its tags'. Long-polling devices get a change
within a second, without rebooting, and the panel shows when a device is in sync.

```c
// ESP-IDF: read values anywhere (thread-safe), or react to changes
int interval_s = otter_config_get_int("interval_s", 60);
otter_on_config(on_config, NULL);  // on_config(const char *json, void *ctx), at start and on changes
```

```cpp
// Arduino
otter.onConfig([](const String &json) { /* parse it with ArduinoJson */ });
```

The ESP-IDF agent keeps the configuration in NVS, so it applies from boot even without network;
the Arduino library keeps it in RAM and gets it again at its first check-in after boot.

## Notifications

Otter can alert you when a deployment fails, a staged rollout halts, a device goes offline (and
comes back), restarts after a crash (panic, watchdog, brownout), or a new device shows up. List the targets in `OTTER_NOTIFY_URLS`, separated by spaces
or commas:

| Target | Format |
|---|---|
| [ntfy](https://ntfy.sh) | `ntfy+https://ntfy.sh/my-topic` (`user:password@` in the URL for a protected server) |
| Discord | the webhook URL, `https://discord.com/api/webhooks/…` |
| Anything else | any URL: receives `{"event", "title", "message", "severity", "data"}` as JSON |

`OTTER_NOTIFY_OFFLINE_MINUTES` (default 10, 0 disables it) sets when a silent device counts as
offline. Devices already offline when the server starts don't trigger alerts. With
`OTTER_PUBLIC_URL` set, notifications link back to the dashboard. Check the setup with:

```sh
curl -X POST http://localhost:8000/api/notifications/test
```

## Home Assistant

Otter shows up in Home Assistant through MQTT discovery: point it at the broker your Home Assistant
uses, and each device appears with a **Firmware** update entity (installed and latest version,
progress while updating) plus connectivity, Wi-Fi signal, IP address and uptime.

```sh
OTTER_MQTT_URL=mqtt://user:password@homeassistant.local:1883   # mqtts:// for TLS
```

The latest version is the newest firmware for the device's app and hardware: from its release
channels if it follows one, from the whole registry otherwise. Set `OTTER_MQTT_INSTALL=1` to let
the entity's **Install** button deploy it. Keep it off unless the broker is protected: anyone who
can publish on it could then trigger updates. `OTTER_MQTT_DISCOVERY_PREFIX` (default
`homeassistant`) and `OTTER_MQTT_BASE_TOPIC` (default `otter`) change the topics.

## Monitoring

`GET /metrics` serves Prometheus metrics:

| Metric | Type | Labels |
|---|---|---|
| `otter_devices` | gauge | `app`, `hw`, `version`, `online` |
| `otter_device_last_seen_timestamp_seconds` | gauge | `mac`, `name`, `app` |
| `otter_deployments` | gauge (by current status) | `status` |
| `otter_firmwares` | gauge | |
| `otter_checkins_total` | counter | `app` |
| `otter_firmware_downloads_total` | counter | `app`, `version` |
| `otter_firmware_download_bytes_total` | counter | |
| `otter_deployment_outcomes_total` | counter | `status` (`success`, `failed`, `cancelled`) |
| `otter_device_crashes_total` | counter | `app`, `reason` (`panic`, `task_watchdog`, `brownout`…) |

```yaml
# prometheus.yml
scrape_configs:
  - job_name: otter
    authorization:
      credentials: otk_…   # python -m otter.cli create-token prometheus --user admin
    static_configs:
      - targets: ["otter.local:8000"]
```

Useful queries:

```promql
sum(otter_devices{online="true"})                           # devices online
sum by (app, version) (otter_devices)                       # fleet by version
increase(otter_deployment_outcomes_total[1d])               # deployment outcomes per day
time() - otter_device_last_seen_timestamp_seconds > 300     # devices silent for 5 min
```

## Database migrations

The schema is managed with [Alembic](https://alembic.sqlalchemy.org/). The server upgrades its
database automatically at startup; databases created before migrations existed are adopted in
place, without data loss.

After changing `server/otter/models.py`, generate a migration, review it, and commit it with the
model change:

```sh
cd server
uv run alembic revision --autogenerate --rev-id 0002 -m "add device tags"
```

Revisions are numbered sequentially (`0002`, `0003`…). SQLite can't alter most things in place,
so migrations run in batch mode (tables are recreated). The test suite fails if the models and the
migrations drift apart.

## Release firmware from CI

`tools/release.sh <app> <version>` makes clean builds of every environment of an app listed in
[`firmware/apps.json`](firmware/apps.json), checks that each image embeds the version, and uploads
the images to Otter when `OTTER_TOKEN` is set (on the channel `OTTER_CHANNEL`, if any). The
version comes from `OTTER_VERSION`, so the example sources don't need editing.

```sh
OTTER_TOKEN=otk_… OTTER_SERVER=http://192.168.1.10:8000 WIFI_SSID=… WIFI_PASS=… \
  tools/release.sh otter-demo 1.6.0
```

On GitHub, pushing a tag `<app>/v<version>` does the same:

```sh
git tag otter-demo/v1.6.0 && git push origin otter-demo/v1.6.0
```

Configure the repository (Settings → Secrets and variables → Actions):

| Name | Kind | Purpose |
|---|---|---|
| `WIFI_SSID`, `WIFI_PASS` | secret | Baked into the firmware. Without them, CI only checks that the release builds. |
| `OTTER_SERVER` | secret | Server URL baked into the firmware (and upload URL by default) |
| `OTTER_PUSH_URL` | secret | Upload URL, if the runner reaches Otter differently than devices do |
| `OTTER_TOKEN` | secret | API token for the upload (`python -m otter.cli create-token ci --user admin`) |
| `OTTER_FLEET_KEY` | secret | Fleet key, if the server uses one |
| `OTTER_CA_CERT` | secret | PEM certificate to trust, for an HTTPS server with a private CA |
| `OTTER_CHANNEL` | variable | Publish uploads on this release channel |
| `OTTER_RUNNER` | variable | Runner label, e.g. `self-hosted` |

GitHub's runners can't reach a server on your LAN: expose Otter over HTTPS, or register a
[self-hosted runner](https://docs.github.com/actions/hosting-your-own-runners) on your network and
set `OTTER_RUNNER`. The images embed your Wi-Fi password: they are built and uploaded in one step
and never stored as workflow artifacts or release assets, which anyone can download on a public
repository. Without Wi-Fi settings, nothing is uploaded.

## Troubleshooting

**Updates crawl at a few KB/s and stall, with the server running outside Docker.** Check
`sysctl net.ipv4.tcp_mtu_probing`. With `1` (some distributions set it, e.g. Omarchy), Linux
takes the packet losses of a weak Wi-Fi link for an MTU problem and shrinks the segments it
sends, down to about a tenth of their size (`ss -ti` shows `mss:144` instead of `mss:1440` on
the download connection). Run Otter in Docker, whose network namespace uses the kernel default
(`0`), or set it back to `0` on the host.

**Devices on a weak link.** The ESP-IDF agent resumes a stalled download where it stopped
(see [Firmware download](docs/protocol.md#3-firmware-download)); the `Signal` column and the
device panel show how weak the link is. Below about -80 dBm, expect retries.

## Tests

```sh
cd server && uv run pytest
```
