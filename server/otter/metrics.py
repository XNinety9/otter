"""Prometheus metrics, served on /metrics.

Fleet state (devices, deployments, firmwares) is read from the database at scrape time;
event counters (check-ins, downloads, deployment outcomes) live in this process and
restart from zero with it, as Prometheus expects.
"""

from collections import Counter as Tally
from datetime import UTC

from prometheus_client import (
    CollectorRegistry,
    Counter,
    GCCollector,
    PlatformCollector,
    ProcessCollector,
    disable_created_metrics,
)
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import event, func, select

from . import config
from .db import SessionLocal, utcnow
from .models import FINAL_STATES, Deployment, Device, Firmware, silence_allowed

disable_created_metrics()  # no *_created series: noise in dashboards

REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)
GCCollector(registry=REGISTRY)

CHECKINS = Counter("otter_checkins", "Device check-ins received", ["app"], registry=REGISTRY)
DOWNLOADS = Counter("otter_firmware_downloads", "Firmware downloads started", ["app", "version"], registry=REGISTRY)
DOWNLOAD_BYTES = Counter("otter_firmware_download_bytes", "Bytes of firmware served", registry=REGISTRY)
CRASHES = Counter("otter_device_crashes", "Device restarts after a crash (panic, watchdog, brownout…)", ["app", "reason"], registry=REGISTRY)
OUTCOMES = Counter("otter_deployment_outcomes", "Deployments reaching a final state", ["status"], registry=REGISTRY)


@event.listens_for(Deployment.status, "set")
def _count_outcome(target, value, oldvalue, initiator):
    # Catches every path that finishes a deployment: check-in, failure report, cancel, supersede.
    if value in FINAL_STATES and value != oldvalue:
        OUTCOMES.labels(value).inc()


def online(d, now) -> bool:
    return (now - d.last_seen).total_seconds() < silence_allowed(config.ONLINE_TIMEOUT_S, d.next_checkin_s)


class FleetCollector:
    def collect(self):
        now = utcnow()
        with SessionLocal() as session:
            devices = session.execute(
                select(
                    Device.mac, Device.name, Device.app, Device.hw, Device.fw_version, Device.last_seen,
                    Device.next_checkin_s,
                )
            ).all()
            deployments = session.execute(select(Deployment.status, func.count()).group_by(Deployment.status)).all()
            firmwares = session.scalar(select(func.count(Firmware.id)))

        by_group = Tally(
            (d.app, d.hw, d.fw_version, "true" if online(d, now) else "false") for d in devices
        )
        fleet = GaugeMetricFamily(
            "otter_devices", "Known devices", labels=["app", "hw", "version", "online"]
        )
        for labels, count in sorted(by_group.items()):
            fleet.add_metric(labels, count)
        yield fleet

        last_seen = GaugeMetricFamily(
            "otter_device_last_seen_timestamp_seconds",
            "Last time each device checked in",
            labels=["mac", "name", "app"],
        )
        for d in devices:
            last_seen.add_metric([d.mac, d.name or "", d.app], d.last_seen.replace(tzinfo=UTC).timestamp())
        yield last_seen

        by_status = GaugeMetricFamily("otter_deployments", "Deployments by current status", labels=["status"])
        for status, count in sorted(deployments):
            by_status.add_metric([status], count)
        yield by_status

        yield GaugeMetricFamily("otter_firmwares", "Firmware images in the registry", value=firmwares)


REGISTRY.register(FleetCollector())
