"""Live logs from devices (#96), on demand and in memory only.

Watching a device (its details page does it, and renews it while open) tells it, at its next
check-in or at once through its long poll, to send its log lines for WATCH_S seconds. The
server keeps the last MAX_LINES of each device, and streams new ones to the UI.
"""

import threading
import time
from collections import deque

from .events import broadcaster, wakeups

WATCH_S = 600
MAX_LINES = 500


class DeviceLogs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: dict[int, deque[tuple[float, str]]] = {}
        self._until: dict[int, float] = {}  # monotonic deadline of each watch
        self._told: dict[int, float] = {}  # the deadline each device last heard about

    def watch(self, device_id: int, mac: str) -> int:
        """Starts or renews a watch; wakes the device's long poll so it hears at once."""
        with self._lock:
            self._until[device_id] = time.monotonic() + WATCH_S
        wakeups.notify(mac)
        return WATCH_S

    def stop(self, device_id: int, mac: str) -> None:
        with self._lock:
            if self._until.pop(device_id, None) is not None:
                self._until[device_id] = 0  # tell the device to stop
        wakeups.notify(mac)

    def order(self, device_id: int) -> int | None:
        """Seconds the device should send its logs for, when that's news to it (else None)."""
        with self._lock:
            until = self._until.get(device_id)
            if until is None or self._told.get(device_id) == until:
                return None
            return max(0, round(until - time.monotonic()))

    def told(self, device_id: int) -> None:
        with self._lock:
            if (until := self._until.get(device_id)) is not None:
                self._told[device_id] = until
                if until == 0:
                    del self._until[device_id]

    def add(self, device_id: int, lines: list[str]) -> None:
        now = time.time()
        with self._lock:
            buffer = self._lines.setdefault(device_id, deque(maxlen=MAX_LINES))
            buffer.extend((now, line) for line in lines)
        broadcaster.publish("logs", {"device_id": device_id, "lines": [{"t": now, "text": line} for line in lines]})

    def get(self, device_id: int) -> list[dict]:
        with self._lock:
            return [{"t": t, "text": text} for t, text in self._lines.get(device_id, ())]

    def forget(self, device_id: int) -> None:
        with self._lock:
            for store in (self._lines, self._until, self._told):
                store.pop(device_id, None)


device_logs = DeviceLogs()
