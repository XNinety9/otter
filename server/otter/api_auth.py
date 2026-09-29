"""Login, logout and session status for the web UI."""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from . import auth
from .db import SessionLocal
from .models import AuthSession, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


@router.get("/status")
def status(request: Request):
    """Public: whether someone is logged in, and whether any account exists yet."""
    user, _ = auth.current_user(request)
    with SessionLocal() as session:
        has_users = session.scalar(select(func.count(User.id))) > 0
    return {"user": user.username if user else None, "has_users": has_users}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    client = request.client.host if request.client else "unknown"
    if not auth.throttle.allowed(client):
        raise HTTPException(429, "too many failed logins, try again in a few minutes")
    auth.check_same_origin(request)
    with SessionLocal() as session:
        user = auth.authenticate(session, body.username.strip(), body.password)
        if user is None:
            auth.throttle.failed(client)
            raise HTTPException(401, "invalid username or password")
        token = auth.create_session(session, user)
        session.commit()
    auth.throttle.succeeded(client)
    auth.set_session_cookie(response, request, token)
    return {"user": user.username}


@router.post("/logout")
def logout(request: Request, response: Response):
    if cookie := request.cookies.get(auth.COOKIE):
        with SessionLocal() as session:
            session.execute(delete(AuthSession).where(AuthSession.token_hash == auth.token_hash(cookie)))
            session.commit()
    response.delete_cookie(auth.COOKIE, path="/")
    return {"user": None}
