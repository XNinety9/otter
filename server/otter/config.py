import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("OTTER_DATA_DIR", "./data")).resolve()
FIRMWARE_DIR = DATA_DIR / "firmwares"
DB_URL = os.environ.get("OTTER_DB_URL", f"sqlite:///{DATA_DIR / 'otter.db'}")

# Shared secret devices must send in X-Otter-Key. Empty = no check (LAN only!).
FLEET_KEY = os.environ.get("OTTER_FLEET_KEY", "")

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

# How often staged rollouts are evaluated, in seconds (0 = never, for tests).
ROLLOUT_TICK_S = float(os.environ.get("OTTER_ROLLOUT_TICK", "5"))
