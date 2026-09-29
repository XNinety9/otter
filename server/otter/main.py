import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import api_device, api_ui, config
from .db import engine
from .events import broadcaster, wakeups
from .migrate import upgrade_database


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    upgrade_database()
    broadcaster.bind(asyncio.get_running_loop())
    wakeups.bind(asyncio.get_running_loop())
    yield


app = FastAPI(title="Otter", lifespan=lifespan)


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness probe for Docker and reverse proxies: the app is up and its database answers."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok"}


app.include_router(api_device.router)
app.include_router(api_ui.router)
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
