"""Authentication for the web UI and its API.

Browsers log in with a username and password and get a session cookie (HttpOnly,
SameSite=Lax). Scripts, CI and Prometheus use API tokens (`Authorization: Bearer
otk_…`). Only SHA-256 hashes of session and API tokens are stored; passwords are
hashed with argon2. Accounts and tokens are managed with `python -m otter.cli`.
"""

import hashlib
import re
import secrets
import threading
import time
from datetime import timedelta
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import config
from .db import SessionLocal, utcnow
from .models import ApiToken, AuthSession, User

COOKIE = "otter_session"
SESSION_LIFETIME = timedelta(days=30)
TOKEN_PREFIX = "otk_"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

hasher = PasswordHasher()
# Checked when the username is unknown, so response times don't reveal which accounts exist.
_DUMMY_HASH = hasher.hash("otter-dummy-password")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    return hasher.hash(password)


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username))
    try:
        hasher.verify(user.password_hash if user else _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return None
    return user


def create_session(session: Session, user: User) -> str:
    session.execute(delete(AuthSession).where(AuthSession.expires_at < utcnow()))
    token = secrets.token_urlsafe(32)
    session.add(AuthSession(token_hash=token_hash(token), user=user, expires_at=utcnow() + SESSION_LIFETIME))
    return token


def new_api_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    secure = request.url.scheme == "https" or config.PUBLIC_URL.startswith("https://")
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def current_user(request: Request) -> tuple[User | None, bool]:
    """(user, via_token). The user is detached from any session."""
    authorization = request.headers.get("authorization", "")
    with SessionLocal() as session:
        if authorization.lower().startswith("bearer "):
            token = session.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash(authorization[7:].strip())))
            if token is None:
                return None, True
            # Track usage, without writing on every Prometheus scrape.
            if token.last_used_at is None or utcnow() - token.last_used_at > timedelta(minutes=5):
                token.last_used_at = utcnow()
                session.commit()
            return token.user, True
        if cookie := request.cookies.get(COOKIE):
            login = session.scalar(
                select(AuthSession).where(AuthSession.token_hash == token_hash(cookie), AuthSession.expires_at > utcnow())
            )
            if login is not None:
                return login.user, False
    return None, False


# What a viewer may still do besides reading: watching a device's live logs.
VIEWER_WRITES = re.compile(r"^/api/devices/\d+/logs/(watch|stop)$")


def require_user(request: Request) -> User:
    """FastAPI dependency protecting the UI API."""
    user, via_token = current_user(request)
    if user is None:
        raise HTTPException(401, "authentication required")
    if request.method not in SAFE_METHODS:
        if not via_token:
            check_same_origin(request)
        if user.role != "admin" and not VIEWER_WRITES.match(request.url.path):
            raise HTTPException(403, "read-only account: ask an admin")
    request.state.user = user
    request.state.via_token = via_token
    return user


def check_same_origin(request: Request) -> None:
    """Cookie-authenticated writes must come from Otter's own pages (CSRF defense on top of SameSite)."""
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-site request refused")


class LoginThrottle:
    """At most MAX_FAILURES failed logins per client address per WINDOW_S."""

    MAX_FAILURES = 10
    WINDOW_S = 300

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        failures = [t for t in self._failures.get(key, []) if now - t < self.WINDOW_S]
        self._failures[key] = failures
        return failures

    def allowed(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) < self.MAX_FAILURES

    def failed(self, key: str) -> None:
        with self._lock:
            self._recent(key, time.monotonic()).append(time.monotonic())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


throttle = LoginThrottle()
