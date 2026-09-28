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
