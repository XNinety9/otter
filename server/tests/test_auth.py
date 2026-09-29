import io
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from conftest import TEST_PASSWORD, TEST_USER
from otter import cli
from otter.auth import COOKIE, LoginThrottle
from otter.db import SessionLocal, utcnow
from otter.models import ApiToken, AuthSession


def login(client, password=TEST_PASSWORD, **headers):
    return client.post("/api/auth/login", json={"username": TEST_USER, "password": password}, headers=headers)


def run_cli(monkeypatch, capsys, *argv, stdin=""):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    cli.main(list(argv))
    return capsys.readouterr().out


@pytest.mark.parametrize("path", ["/api/devices", "/api/firmwares", "/api/rollouts", "/api/tags", "/api/config", "/metrics"])
def test_ui_api_requires_login(client, path):
    client.cookies.clear()
    assert client.get(path).status_code == 401


def test_open_endpoints_stay_open(client, checkin):
    client.cookies.clear()
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200  # the page itself; it redirects to login client-side
    checkin()  # devices keep using the fleet key, not user accounts
    assert client.get("/api/auth/status").json() == {"user": None, "has_users": True}


def test_login_logout(client):
    client.cookies.clear()
    assert login(client, "wrong password").status_code == 401
    res = login(client)
    assert res.status_code == 200
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/auth/status").json()["user"] == TEST_USER
    assert client.get("/api/devices").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/devices").status_code == 401
    with SessionLocal() as session:
        assert session.scalar(select(func.count(AuthSession.id))) == 1  # only the fixture's session left


def test_expired_session_is_rejected(client):
    with SessionLocal() as session:
        for login_session in session.scalars(select(AuthSession)):
            login_session.expires_at = utcnow() - timedelta(minutes=1)
        session.commit()
    assert client.get("/api/devices").status_code == 401


def test_cross_site_writes_are_refused(client, checkin):
    checkin()
    dev = client.get("/api/devices").json()[0]["id"]
    body = {"name": "x"}
    assert client.patch(f"/api/devices/{dev}", json=body, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.patch(f"/api/devices/{dev}", json=body, headers={"origin": "http://testserver"}).status_code == 200
    assert client.patch(f"/api/devices/{dev}", json=body).status_code == 200  # no Origin header (non-browser client)


def test_login_is_throttled(client, monkeypatch):
    monkeypatch.setattr(LoginThrottle, "MAX_FAILURES", 3)
    client.cookies.clear()
    for _ in range(3):
        assert login(client, "nope").status_code == 401
    assert login(client).status_code == 429  # even with the right password


def test_api_tokens(client, monkeypatch, capsys, upload):
    out = run_cli(monkeypatch, capsys, "create-token", "ci", "--user", TEST_USER)
    token = out.strip().splitlines()[-1]
    assert token.startswith("otk_")
    with SessionLocal() as session:
        assert session.scalar(select(ApiToken.token_hash)) != token  # only a hash is stored

    client.cookies.clear()
    headers = {"authorization": f"Bearer {token}"}
    assert client.get("/metrics", headers=headers).status_code == 200
    res = client.post(  # what tools/push.sh does, from any origin
        "/api/firmwares",
        headers={**headers, "origin": "https://ci.example"},
        data={"app": "weather", "hw": "esp32", "version": "9.9.9"},
        files={"file": ("fw.bin", b"\xe9" + b"\0" * 64)},
    )
    assert res.status_code == 201
    assert client.get("/api/devices", headers={"authorization": "Bearer otk_wrong"}).status_code == 401

    assert "last used" in run_cli(monkeypatch, capsys, "list-tokens")
    run_cli(monkeypatch, capsys, "revoke-token", "ci")
    assert client.get("/metrics", headers=headers).status_code == 401


def test_cli_users(client, monkeypatch, capsys):
    run_cli(monkeypatch, capsys, "create-user", "bob", "--password-stdin", stdin="hunter2hunter2\n")
    assert "bob" in run_cli(monkeypatch, capsys, "list-users")
    client.cookies.clear()
    res = client.post("/api/auth/login", json={"username": "bob", "password": "hunter2hunter2"})
    assert res.status_code == 200

    run_cli(monkeypatch, capsys, "set-password", "bob", "--password-stdin", stdin="another-password\n")
    assert client.get("/api/devices").status_code == 401  # sessions closed on password change

    with pytest.raises(SystemExit):
        run_cli(monkeypatch, capsys, "create-user", "eve", "--password-stdin", stdin="short\n")
