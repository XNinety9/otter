# Otter device protocol (v1)

This is the contract between a device agent (ESP-IDF component, Arduino library, simulator…)
and the Otter server: JSON requests over HTTP(S), always initiated by the device, so devices
never need an open port. **Use HTTPS**: see [Security](#security) for what plain HTTP exposes.

All device endpoints live under `/api/v1`. A device built without a server URL can find it on
the LAN over mDNS: service type `_otter._tcp`, whose TXT record `url` (when present) is the URL
to use; otherwise `http://<address>:<port>` of the service.

### Authentication

A device first authenticates with the fleet key (`OTTER_FLEET_KEY` on the server; empty = no
check, LAN only):

```
X-Otter-Key: <fleet key>
```

The check-in response to such a request carries a `token` for this device alone. The device
stores it (NVS for the ESP-IDF agent) and sends it instead on every request, downloads included:

```
Authorization: Bearer otd_…
```

Once a device has used its token, the fleet key no longer works for it: someone who extracted
the fleet key from any device's flash can't act as it. A token only acts for its own device
(its check-ins, deployments and commands). Agents that ignore `token` keep working with the
fleet key; they just get a new, unused token at each check-in. The server stores SHA-256 hashes
only.

| Answer | Meaning | What the device does |
|---|---|---|
| `401` to a token | unknown token: re-enrolled from the dashboard, or database reset | forget it, check in with the fleet key |
| `401` to the fleet key | wrong key, or this device uses a token | nothing useful: check the configuration |
| `403` | revoked, or awaiting approval (`OTTER_DEVICE_APPROVAL`) | retry later |

From the dashboard, **Revoke** blocks a device (token and fleet key) and **Re-enroll** lets its
next fleet-key check-in get a new token. With `OTTER_DEVICE_APPROVAL=1`, new devices appear in the
dashboard but are refused until approved.

## 1. Check-in

The device calls this at boot and then every `checkin_interval_s` seconds.

```
POST /api/v1/checkin
Content-Type: application/json

{
  "mac": "a4:cf:12:34:56:78",     // required, identifies the device
  "hw": "esp32",                  // required, hardware family: esp32, esp32c3, esp8266…
  "chip": "ESP32-D0WD-V3",        // optional, exact chip, named as esptool does (shown in the UI)
  "chip_rev": "3.1",              // optional, chip revision: major.minor
  "app": "weather-station",       // required, firmware application name
  "fw_version": "1.2.0",          // required, currently running version
  "ip": "192.168.1.42",           // optional, server falls back to the TCP peer address
  "rssi": -61,                    // optional, Wi-Fi signal in dBm
  "uptime_s": 3600,               // optional
  "ota_slot_size": 1966080,       // optional, bytes available for an update image (see below)
  "free_heap": 284104,            // optional, health: free heap in bytes
  "min_free_heap": 251360,        // optional, lowest free heap since boot
  "reset_reason": "power_on",     // optional, why the device last restarted (see below)
  "boot_count": 17,               // optional, incremented at each boot
  "config_version": "5dd2cdab1550ab80", // optional, remote configuration it has ("" = none)
  "next_checkin_s": 600,          // optional, a sleeping device: when it will be back (see below)
  "wait_s": 30                    // optional, long polling (see below)
}
```

Response:

```json
{
  "checkin_interval_s": 30,
  "update": null,
  "commands": [],
  "token": null,                  // a new token when the request used the fleet key
  "config": null                  // the configuration, when config_version is outdated
}
```

or, when an update is scheduled for this device:

```json
{
  "checkin_interval_s": 30,
  "update": {
    "deployment_id": 12,
    "version": "1.3.0",
    "url": "http://otter.local:8000/api/v1/firmwares/4/download",
    "size": 912384,
    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "signature": "MEUCIQ…",        // base64, when the image was signed (see Security)
    "compressed": { … },           // optional, see "Firmware download"
    "delta": { … }                 // optional, see "Firmware download"
  }
}
```

### OTA slot size

`ota_slot_size` is the room for an update image: the size of the next OTA partition on
ESP-IDF (`esp_ota_get_next_update_partition(NULL)->size`), `ESP.getFreeSketchSpace()` on
Arduino. The server never sends a device an image bigger than that: deploying one to it is
refused, and tag deployments, rollouts and release channels skip it. Without the field, the
server can't check and trusts the device; a check-in without it keeps the last value.

### Health

`reset_reason` is one of `power_on`, `software` (restart, e.g. after an update), `deep_sleep`,
`external` (reset pin), `panic`, `int_watchdog`, `task_watchdog`, `watchdog`, `brownout`,
`power_glitch`, `cpu_lockup`, `usb`, `jtag`, `sdio`, `efuse`, `unknown` (lowercase letters,
digits and `_`). The server flags a device that restarted (a new `boot_count`, or `uptime_s`
going down) because of a panic, watchdog, brownout, power glitch or CPU lockup: highlighted in the UI, counted in `otter_device_crashes_total`
and notified as `device_crashed`.

### Configuration

Key/value settings edited in the dashboard, per tag and per device (the device's own values
win over its tags', and tags apply in alphabetical order). An agent that supports them always
sends `config_version` (`""` until it has one); when it differs from the current version, the
answer carries the whole configuration:

```json
"config": { "version": "5dd2cdab1550ab80", "values": { "interval_s": 60, "unit": "C", "debug": false } }
```

The device applies it, keeps it (the ESP-IDF agent in NVS, so it applies from boot even
offline) and checks in again right away with the new `config_version`, which the dashboard
shows as "in sync". Values are strings, numbers or booleans; keys `[a-z][a-z0-9_]{0,31}`; at
most 32 keys and 2 KB per device or tag. Agents that don't send `config_version` never get any.

### Sleeping devices

A battery device that deep-sleeps between check-ins sends `wait_s: 0` and `next_checkin_s`,
the time until its next wake-up. It counts as online until then (plus a margin: 25 % and a
minute), so it isn't reported offline while asleep. Deployments, commands and configuration
changes wait for its next check-in. Without `next_checkin_s`, a device is expected every
`checkin_interval_s`.

### Long polling

With `wait_s` > 0, if nothing is waiting for the device, the server holds the request open for
up to `wait_s` seconds (capped at 60) and answers as soon as a deployment, a command or a new
configuration concerns it, so they reach it within a second or two instead of at the next
check-in. Set the HTTP timeout
to `wait_s` plus a margin.

Agents should wait `checkin_interval_s` **minus the time the request took** before the next
check-in: against a long-polling server they poll again immediately, against a server
that answers at once they fall back to the plain interval. A device should send
`wait_s: 0` when it needs an immediate answer (e.g. a new firmware waiting to be
marked valid).

### Commands

The check-in response may also carry remote commands queued from the dashboard (at most 5
per check-in: the rest follow at the next one):

```json
{
  "checkin_interval_s": 30,
  "update": null,
  "commands": [
    { "id": 7, "name": "reboot", "args": null },
    { "id": 8, "name": "blink", "args": { "times": 3 } }
  ]
}
```

The device runs them in order and acknowledges each one:

```
POST /api/v1/commands/{id}/result
Content-Type: application/json

{ "ok": true, "message": "blinked 3 times" }    // message optional, at most 200 characters
```

`ok: false` with a message for a failure, e.g. `"unknown command"` for a name the firmware
doesn't know. After commands, a device should check in again right away (more may be queued).
`reboot` is acknowledged first, then the device restarts once every command is acknowledged.
Commands reach a long-polling device within a second or two; one still queued after 10 min
expires instead of running at an unexpected time.

Names are lowercase letters, digits and `_` (at most 32); `args` is a JSON object (at most 512
bytes) or null. Built into the agents: `reboot` and `identify` (make the device show itself,
e.g. blink an LED); applications register their own.

### Crash reports

After a crash, at its first check-in that reaches the server, an ESP-IDF device sends its core
dump summary (then erases it):

```
POST /api/v1/crashes            (?mac=<mac> when authenticating with the fleet key)
Content-Type: application/json

{
  "elf_sha256": "2d8042e2",       // app_elf_sha256 of the crashed app (a prefix is fine)
  "task": "main",
  "reason": "abort() was called at PC 0x42000631 on core 0",
  "pc": 1082169736,
  "backtrace": [ … ],             // Xtensa: the backtrace
  "registers": { "ra": …, "sp": … }, "stack": [ … ]   // RISC-V: registers and stack words
}
```

The server finds the firmware whose image names that ELF hash, and decodes the addresses with
its ELF file when it has it.

## 2. Progress reports

While applying an update, the device reports its progress. Reports are best effort: a lost
report must never abort the update. Sending one every ~5–10 % is plenty.

```
POST /api/v1/deployments/{deployment_id}/progress
Content-Type: application/json

{ "state": "downloading", "progress": 42 }
```

| state         | meaning                                                          |
|---------------|------------------------------------------------------------------|
| `downloading` | image is being streamed to the inactive OTA partition            |
| `rebooting`   | image written and verified, device is about to restart           |
| `failed`      | update aborted; include `"error": "<short reason>"`              |

With `failed`, a device may add `"retryable": true|false` to say whether a new attempt could
succeed. Without it, the server guesses from `error`: network problems (`connection lost`,
`download timeout`, `cannot reach…`) are retried, anything else (`sha256 mismatch`, image
validation, not enough space…) is final. A retried deployment goes back to pending and is handed
out again at a later check-in, after 10 s, 20 s… (`OTTER_RETRY_DELAY`), up to
`OTTER_DEPLOY_ATTEMPTS` attempts (default 3).

`progress` is 0–100. A `409 Conflict` answer means the deployment was cancelled (or
superseded) from the UI: the device should abort the update and keep running its current
image. There is no `success` state sent by the device: the update is
confirmed when the device checks in again **reporting the target `fw_version`**. If it
checks in after `rebooting` with any other version, the server marks the deployment as
failed (typically a bootloader rollback).

## 3. Firmware download

```
GET /api/v1/firmwares/{id}/download
```

Returns the raw `.bin` image. The device must verify the SHA-256 of what it wrote
against `update.sha256` before switching boot partitions.

When compressing saves at least 5 %, the update order also offers the image zlib-compressed:
`"compressed": {"url": "…/download?format=zlib", "size": 750629, "format": "zlib"}` (ESP32
images shrink by about 40 %). An agent that can inflate downloads that instead and inflates it
while writing (the ESP-IDF agent uses the ROM's `tinfl`, with a 32 KB window); `size` and
`sha256` still describe the image itself. Ranges count compressed bytes, so a resumed download
continues the stream where it stopped.

When the device runs an image Otter has (same app, hardware and version as it reports), the order
may also offer a patch from it, made with detools (heatshrink compression), when it is less than
half the compressed image, at the first attempt only:
`"delta": {"url": "…/delta?base=12", "size": 19693, "base_hash": "…", "format": "detools-heatshrink"}`.
`base_hash` is the SHA-256 the base image carries at its end, which is what
`esp_partition_get_sha256()` returns for the running partition: the agent uses the patch only
if they match (an image flashed by other means differs), reading the running image as the patch
source and writing the result like a downloaded image, which it then verifies with `sha256` and
the signature as usual. A patch that fails reports `delta patch failed`, which is retried with
the whole image.

A device can resume an interrupted download with `Range: bytes=<offset>-`: the server answers
`206 Partial Content` with the rest of the image. On a weak Wi-Fi link, a connection that
stops receiving data for a few seconds rarely recovers (the server's TCP waits twice as long
after each loss), so the ESP-IDF agent opens a new one after 8 s of silence and resumes where it
stopped, up to 10 times per attempt, instead of starting over.

## Agent loop (reference)

```
boot:
    mark running app valid (ESP-IDF rollback)   # once the app is confirmed healthy
loop:
    t0 = now()
    resp = checkin(wait_s = checkin_interval_s)
    for command in resp.commands:
        run it, then POST its result
    if resp.commands:
        reboot if asked, else check in again right away
    if resp.update:
        report(downloading, 0)
        stream url -> OTA partition, report every ~10 %
        verify size + sha256, otherwise report(failed, error) and continue
        report(rebooting, 100)
        restart
    sleep(max(0, resp.checkin_interval_s - (now() - t0)))
```

## Security

What protects what, and what doesn't yet:

| Threat | Plain HTTP | HTTPS (server) | Status |
|---|---|---|---|
| Someone on the network reads the fleet key, device data, or dashboard passwords and cookies | **exposed** | protected | HTTPS: ESP-IDF done, Arduino to do ([#17](https://github.com/XNinety9/otter/issues/17)) |
| Someone on the network (e.g. ARP spoofing) serves a malicious firmware | **exposed**: `sha256` comes through the same channel, it only detects corruption | protected, **if the device verifies the certificate** | HTTPS: ESP-IDF done, Arduino to do (#17) |
| The server itself, or its storage, is compromised and serves a malicious firmware | exposed | exposed | **signed firmware** (ESP-IDF agent): devices built with the public key refuse images not signed with the private key, which never reaches the server ([#18](https://github.com/XNinety9/otter/issues/18)) |
| A device's key is extracted from its flash | the shared fleet key opens every device's API | same | per-device tokens (ESP-IDF agent): the fleet key alone can't act as an enrolled device, and a device can be revoked alone ([#15](https://github.com/XNinety9/otter/issues/15)); an extracted fleet key can still enroll new devices, unless `OTTER_DEVICE_APPROVAL` is on |

Deployment options:

- **Server**: run it behind a TLS reverse proxy. `docker-compose.https.yml` puts Caddy in front of
  Otter, with Let's Encrypt for a public domain or Caddy's own CA on a LAN, and stops publishing
  Otter's plain-HTTP port (see "HTTPS" in the README).
- **ESP-IDF agent**: use an `https://` server URL and give the CA certificate in
  `otter_config_t.cert_pem` (the example takes it from `OTTER_CA_CERT` at build time). Without
  it, the ESP-IDF certificate bundle is used, which covers public certificates such as Let's
  Encrypt but not a private CA. Validated on an ESP32-C6 with Caddy's local CA: check-ins and
  OTA updates over HTTPS, at the same speed as plain HTTP (~220 KB/s).
- **Arduino library**: an `https://` server needs `config.caCert` (the example takes
  `OTTER_CA_CERT` at build time); the certificate is always checked. Validated on an ESP32-C6
  (Arduino core 3) with Caddy's local CA: check-ins, commands and a signed OTA update. On ESP32
  it enrolls and keeps its token in NVS, and checks signatures (`config.signingKey`); an ESP8266
  keeps using the fleet key, can't check signatures (given a key, it refuses every update), and
  needs its clock set (`configTime()`) for TLS. ESP8266 over HTTPS isn't tested on hardware yet.

Devices must verify the server's certificate: TLS without verification still lets anyone on
the network impersonate the server.

**Signatures**: `signature` in an update order is the base64 of `openssl dgst -sha256 -sign`
over the whole image (DER ECDSA, or RSA PKCS#1 v1.5). An agent configured with a public key
refuses an order without one (report `failed`, `unsigned firmware refused…`) and checks the
SHA-256 of what it wrote against it before switching partitions (`invalid signature…`); both
are final, not retried.

