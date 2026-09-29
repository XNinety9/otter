<p align="center">
  <img src="docs/logo_readme.png" alt="Otter" width="240">
</p>

# Otter

[![CI](https://github.com/XNinety9/otter/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/XNinety9/otter/actions/workflows/ci.yml)

Fleet tracking and centralized OTA updates for home-made ESP32 / ESP8266 devices.

- **Inventory**: every device checks in periodically with its MAC, IP, firmware version,
  signal strength and uptime. The dashboard shows who is online and when each device was last seen.
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
  `/etc/hosts`…).
- With Caddy's local CA, browsers and devices must trust its root certificate:

  ```sh
  docker compose -f docker-compose.yml -f docker-compose.https.yml \
    cp caddy:/data/caddy/pki/authorities/local/root.crt otter-ca.pem
  ```

  Import `otter-ca.pem` in your browser or system trust store, and give it to the devices
  (`cert_pem` of the ESP-IDF agent). The simulator takes it with `--ca otter-ca.pem`.
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
OTTER_TOKEN=otk_… ../../../tools/push.sh .pio/build/esp32/firmware.bin otter-demo esp32 1.0.1
```

With ESP-IDF, a new firmware is marked valid once it reaches the server (or when the app calls
`otter_mark_valid()` if `manual_mark_valid` is set). If it never does within
`rollback_timeout_s`, or crashes before, the bootloader reverts to the previous slot and Otter
reports the deployment as failed. The ESP8266 has no such safety net: only the image header
and SHA-256 are checked before rebooting.

## Notifications

Otter can alert you when a deployment fails, a staged rollout halts, a device goes offline (and
comes back), or a new device shows up. List the targets in `OTTER_NOTIFY_URLS`, separated by spaces
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

## Tests

```sh
cd server && uv run pytest
```
