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

# How often staged rollouts are evaluated, in seconds (0 = never, for tests).
ROLLOUT_TICK_S = float(os.environ.get("OTTER_ROLLOUT_TICK", "5"))
