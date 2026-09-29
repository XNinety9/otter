import os
import tempfile

# Must be set before otter.config is imported.
os.environ["OTTER_DATA_DIR"] = tempfile.mkdtemp(prefix="otter-test-")
os.environ["OTTER_ROLLOUT_TICK"] = "0"  # tests drive rollout evaluation themselves

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from otter.db import Base, engine  # noqa: E402
from otter.main import app  # noqa: E402


def reset_database() -> None:
    """Empty database: no tables, no migration history."""
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture
def client():
    reset_database()
    with TestClient(app) as c:
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
    def _checkin(version="1.0.0", mac="AA:BB:CC:00:00:01", app="weather", hw="esp32"):
        res = client.post(
            "/api/v1/checkin", json={"mac": mac, "hw": hw, "app": app, "fw_version": version, "rssi": -60}
        )
        assert res.status_code == 200, res.text
        return res.json()

    return _checkin
