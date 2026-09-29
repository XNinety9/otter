"""In-process pub/sub feeding the UI's Server-Sent Events stream.

Endpoints run in FastAPI's threadpool, so publishing hops back onto the event loop
with call_soon_threadsafe.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager

log = logging.getLogger("otter.events")


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listeners: list[Callable[[str, object], None]] = []

    def add_listener(self, listener: Callable[[str, object], None]) -> None:
        """Also called for every event, in the publishing thread (e.g. the MQTT bridge)."""
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[str, object], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    def publish(self, kind: str, data: object = None) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind, data)
            except Exception:
                log.exception("event listener failed on %s", kind)
        if self._loop is None:
            return
        message = f"event: {kind}\ndata: {json.dumps(data)}\n\n"
        self._loop.call_soon_threadsafe(self._fanout, message)

    def _fanout(self, message: str) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Slow client: drop it, the browser's EventSource will reconnect and resync.
                self._subscribers.discard(queue)


class Wakeups:
    """Lets long-polling check-ins sleep until something is scheduled for their device."""

    def __init__(self) -> None:
        self._waiting: dict[str, set[asyncio.Event]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @contextmanager
    def watch(self, mac: str) -> Iterator[asyncio.Event]:
        """Subscribe before looking at the device's state, so no notification is missed."""
        event = asyncio.Event()
        self._waiting.setdefault(mac, set()).add(event)
        try:
            yield event
        finally:
            waiters = self._waiting.get(mac, set())
            waiters.discard(event)
            if not waiters:
                self._waiting.pop(mac, None)

    def notify(self, mac: str) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake, mac)

    def _wake(self, mac: str) -> None:
        for event in self._waiting.get(mac, ()):
            event.set()


broadcaster = Broadcaster()
wakeups = Wakeups()
