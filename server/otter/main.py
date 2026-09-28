import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api_device, api_ui, config
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
app.include_router(api_device.router)
app.include_router(api_ui.router)
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
