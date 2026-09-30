import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("OTTER_DATA_DIR", "./data")).resolve()
FIRMWARE_DIR = DATA_DIR / "firmwares"
DB_URL = os.environ.get("OTTER_DB_URL", f"sqlite:///{DATA_DIR / 'otter.db'}")

# Shared secret devices must send in X-Otter-Key. Empty = no check (LAN only!).
FLEET_KEY = os.environ.get("OTTER_FLEET_KEY", "")
# Advertise the server over mDNS (_otter._tcp) for devices built without a server URL (#19).
MDNS = os.environ.get("OTTER_MDNS", "").lower() in ("1", "true", "yes")
MDNS_PORT = int(os.environ.get("OTTER_MDNS_PORT", "8000"))  # the port devices reach Otter on
# New devices wait for an approval in the dashboard before they can check in (#15).
# Public key (PEM, or the path to one) firmware must be signed with to be uploaded (#18).
SIGNING_PUBLIC_KEY = os.environ.get("OTTER_SIGNING_PUBLIC_KEY", "")
DEVICE_APPROVAL = os.environ.get("OTTER_DEVICE_APPROVAL", "").lower() in ("1", "true", "yes")

# Base URL devices use to download firmware, e.g. http://192.168.1.10:8000.
# Empty = derived from the incoming check-in request.
PUBLIC_URL = os.environ.get("OTTER_PUBLIC_URL", "").rstrip("/")

CHECKIN_INTERVAL_S = int(os.environ.get("OTTER_CHECKIN_INTERVAL", "30"))

# A device is "online" if it checked in within this delay (same rule as the web UI).
ONLINE_TIMEOUT_S = CHECKIN_INTERVAL_S * 2.5 + 5

# Tries per deployment when a device reports a transient (network) failure, and the base
# delay before handing it out again (multiplied by the attempt number).
DEPLOY_ATTEMPTS = int(os.environ.get("OTTER_DEPLOY_ATTEMPTS", "3"))
RETRY_DELAY_S = float(os.environ.get("OTTER_RETRY_DELAY", "10"))

# Notification targets, space or comma separated (see otter/notify.py). Empty = off.
NOTIFY_URLS = os.environ.get("OTTER_NOTIFY_URLS", "")
# Alert when a device hasn't checked in for this long (0 = never).
NOTIFY_OFFLINE_MINUTES = float(os.environ.get("OTTER_NOTIFY_OFFLINE_MINUTES", "10"))

# Home Assistant over MQTT discovery (see otter/mqtt.py). Empty URL = off.
MQTT_URL = os.environ.get("OTTER_MQTT_URL", "")  # mqtt://user:password@host:1883, or mqtts://
MQTT_DISCOVERY_PREFIX = os.environ.get("OTTER_MQTT_DISCOVERY_PREFIX", "homeassistant")
MQTT_BASE_TOPIC = os.environ.get("OTTER_MQTT_BASE_TOPIC", "otter")
# Let Home Assistant's "Install" button deploy firmware. Anyone who can publish on the broker could.
MQTT_INSTALL = os.environ.get("OTTER_MQTT_INSTALL", "").lower() in ("1", "true", "yes")

# How often staged rollouts are evaluated, in seconds (0 = never, for tests).
ROLLOUT_TICK_S = float(os.environ.get("OTTER_ROLLOUT_TICK", "5"))
