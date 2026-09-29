import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from . import api_auth, api_device, api_rollouts, api_ui, config
from .auth import require_user
from .db import engine
from .events import broadcaster, wakeups
from .metrics import REGISTRY
from .migrate import upgrade_database
from .mqtt import bridge
from .notify import notifier


log = logging.getLogger("otter")


async def watch_offline_devices_forever() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await run_in_threadpool(notifier.check_offline)
            if bridge.connected:
                await run_in_threadpool(bridge.publish_all)  # online/offline changes with time
        except Exception:
            log.exception("offline check failed")


async def evaluate_rollouts_forever() -> None:
    while True:
        await asyncio.sleep(config.ROLLOUT_TICK_S)
        try:
            await run_in_threadpool(api_rollouts.background_pass)
        except Exception:
            log.exception("background pass (rollouts, channels) failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    upgrade_database()
    broadcaster.bind(asyncio.get_running_loop())
    wakeups.bind(asyncio.get_running_loop())
    notifier.seed_offline()
    notifier.start()
    bridge.start()
    tasks = [asyncio.create_task(watch_offline_devices_forever())]
    if config.ROLLOUT_TICK_S > 0:
        tasks.append(asyncio.create_task(evaluate_rollouts_forever()))
    yield
    for task in tasks:
        task.cancel()
    bridge.stop()
    notifier.stop()


app = FastAPI(title="Otter", lifespan=lifespan)


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness probe for Docker and reverse proxies: the app is up and its database answers."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False, dependencies=[Depends(require_user)])
def metrics():
    """Prometheus scrape endpoint."""
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


app.include_router(api_auth.router)
app.include_router(api_device.router)
app.include_router(api_ui.router)
app.include_router(api_rollouts.router)
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
