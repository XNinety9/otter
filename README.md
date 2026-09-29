# 🦦 Otter

[![CI](https://github.com/XNinety9/otter/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/XNinety9/otter/actions/workflows/ci.yml)

Fleet tracking and centralized OTA updates for home-made ESP32 / ESP8266 devices.

- **Inventory**: every device checks in periodically with its MAC, IP, firmware version,
  signal strength and uptime. The dashboard shows who is online and when each device was last seen.
- **Firmware registry**: upload `.bin` images, versioned per application and hardware family.
- **OTA deployments**: target one device, a selection, a tag, or roll out a version to every device
  of an app. Progress streams live to the browser.
- **Tags**: group devices (`living-room`, `test-bench`…), filter the dashboard by tag and roll out
  to a tag. A tag deployment only targets the tagged devices running the firmware's app and
  hardware, and skips those already on that version.

Devices *pull*: they never expose a port, sleepy devices work, and NAT is not a problem.
The contract between devices and server is described in [docs/protocol.md](docs/protocol.md).

## Layout

```
server/        FastAPI + SQLite backend, serves the web UI (server/otter/static)
tools/         simulate.py: a swarm of fake devices speaking the protocol
docs/          protocol spec
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
| `OTTER_FLEET_KEY`        | *(empty)*      | Shared secret devices send in `X-Otter-Key`              |
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

The server must listen on the LAN (`--host 0.0.0.0`) and `OTTER_SERVER` must be the
address devices can reach, e.g. your machine's LAN IP.

```sh
export WIFI_SSID=... WIFI_PASS=... OTTER_SERVER=http://192.168.1.10:8000

# ESP32 with ESP-IDF (bootloader rollback enabled)
cd firmware/examples/esp32-idf
pio run -t upload -t monitor

# or Arduino: ESP8266 (-e esp8266) or ESP32 (-e esp32)
cd firmware/examples/arduino
pio run -e esp8266 -t upload -t monitor
```

The device shows up in the dashboard within seconds. To ship an update, bump the version
(`PROJECT_VER` in the ESP-IDF example's `CMakeLists.txt`, `[otter] version` in the Arduino
`platformio.ini`), rebuild, push the image and deploy it from the UI:

```sh
pio run
../../../tools/push.sh .pio/build/esp32/firmware.bin otter-demo esp32 1.0.1
```

With ESP-IDF, a new firmware is marked valid once it reaches the server (or when the app calls
`otter_mark_valid()` if `manual_mark_valid` is set). If it never does within
`rollback_timeout_s`, or crashes before, the bootloader reverts to the previous slot and Otter
reports the deployment as failed. The ESP8266 has no such safety net: only the image header
and SHA-256 are checked before rebooting.

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

```yaml
# prometheus.yml
scrape_configs:
  - job_name: otter
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

## Tests

```sh
cd server && uv run pytest
```
