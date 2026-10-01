"""Activity log (#100): every change made through the UI API, by whom, and how it went.

The HTTP middleware in main.py records each write once it's answered. Endpoints say what
they did in readable words with describe(); otherwise the endpoint's name and ids stand in.
Kept KEEP days.
"""

from datetime import timedelta

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, select

from .db import SessionLocal, utcnow
from .models import AuditEvent

KEEP = timedelta(days=90)
_pruned_at = None


def describe(request: Request, text: str) -> None:
    request.state.audit = text


def default_text(request: Request) -> str:
    route = request.scope.get("route")
    if route is None:
        return f"{request.method} {request.url.path}"
    ids = " ".join(f"#{v}" for v in request.scope.get("path_params", {}).values())
    return f"{route.name.replace('_', ' ')} {ids}".strip()


class AuditMiddleware:
    """Records the UI API's writes once answered. Plain ASGI, so streams (the UI's events,
    devices' long polls) pass through untouched."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope["method"] in ("GET", "HEAD", "OPTIONS")
            or not path.startswith("/api/")
            or path.startswith(("/api/v1/", "/api/auth/"))
        ):
            return await self.app(scope, receive, send)
        status = 500

        async def send_and_note(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_note)
        finally:
            await run_in_threadpool(record, Request(scope), status)


def record(request: Request, status: int) -> None:
    global _pruned_at
    user = getattr(request.state, "user", None)
    if user is None:
        return
    with SessionLocal() as session:
        session.add(
            AuditEvent(
                username=user.username,
                via_token=getattr(request.state, "via_token", False),
                method=request.method,
                path=request.url.path,
                text=getattr(request.state, "audit", None) or default_text(request),
                status=status,
            )
        )
        if _pruned_at is None or utcnow() - _pruned_at > timedelta(hours=1):
            session.execute(delete(AuditEvent).where(AuditEvent.at < utcnow() - KEEP))
            _pruned_at = utcnow()
        session.commit()


def recent(limit: int = 200) -> list[AuditEvent]:
    with SessionLocal() as session:
        return list(session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)))
