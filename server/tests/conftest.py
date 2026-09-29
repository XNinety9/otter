import os
import tempfile

# Must be set before otter.config is imported.
os.environ["OTTER_DATA_DIR"] = tempfile.mkdtemp(prefix="otter-test-")
os.environ["OTTER_ROLLOUT_TICK"] = "0"  # tests drive rollout evaluation themselves

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from otter.auth import hash_password, throttle  # noqa: E402
from otter.db import Base, SessionLocal, engine  # noqa: E402
from otter.models import User  # noqa: E402
from otter.main import app  # noqa: E402


def reset_database() -> None:
    """Empty database: no tables, no migration history."""
    throttle._failures.clear()
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


TEST_USER, TEST_PASSWORD = "admin", "correct horse battery"


def create_user(username=TEST_USER, password=TEST_PASSWORD):
    with SessionLocal() as session:
        session.add(User(username=username, password_hash=hash_password(password)))
        session.commit()


@pytest.fixture
def client():
    """Logged in as an admin; call client.cookies.clear() to act anonymously."""
    reset_database()
    with TestClient(app) as c:
        create_user()
        assert c.post("/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}).status_code == 200
        yield c


def fake_image(tag: bytes = b"") -> bytes:
    return b"\xe9" + b"\x00" * 1023 + tag


@pytest.fixture
def upload(client):
    def _upload(version="1.1.0", app="weather", hw="esp32", tag=b""):
        res = client.post(
            "/api/firmwares",
            data={"app": app, "hw": hw, "version": version},
            files={"file": ("fw.bin", fake_image(tag or version.encode()))},
        )
        assert res.status_code == 201, res.text
        return res.json()

    return _upload


@pytest.fixture
def checkin(client):
    def _checkin(version="1.0.0", mac="AA:BB:CC:00:00:01", app="weather", hw="esp32", **extra):
        res = client.post(
            "/api/v1/checkin", json={"mac": mac, "hw": hw, "app": app, "fw_version": version, "rssi": -60, **extra}
        )
        assert res.status_code == 200, res.text
        return res.json()

    return _checkin
