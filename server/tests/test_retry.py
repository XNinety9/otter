from datetime import timedelta

from otter import config, rollouts
from otter.db import SessionLocal, utcnow
from otter.models import Deployment
from test_ota_flow import device_id


def deploy(client, checkin, upload):
    checkin()
    fw = upload("1.1.0")
    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [device_id(client)]})
    return checkin()["update"]


def fail(client, dep_id, error, **extra):
    res = client.post(f"/api/v1/deployments/{dep_id}/progress", json={"state": "failed", "error": error, **extra})
    assert res.status_code == 200
    return client.get("/api/devices").json()[0]["last_deployment"]


def make_due(dep_id):
    with SessionLocal() as session:
        session.get(Deployment, dep_id).retry_at = utcnow() - timedelta(seconds=1)
        session.commit()


def test_transient_failure_is_retried_after_a_delay(client, checkin, upload):
    order = deploy(client, checkin, upload)
    dep = fail(client, order["deployment_id"], "connection lost")
    assert (dep["status"], dep["attempts"]) == ("pending", 2)
    assert dep["error"] == "attempt 1 failed: connection lost"

    assert checkin()["update"] is None  # not before the retry delay
    make_due(order["deployment_id"])
    again = checkin()["update"]
    assert again["deployment_id"] == order["deployment_id"]

    client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "downloading", "progress": 10})
    dep = client.get("/api/devices").json()[0]["last_deployment"]
    assert (dep["status"], dep["attempts"], dep["error"]) == ("downloading", 2, None)
    checkin("1.1.0")
    assert client.get("/api/devices").json()[0]["last_deployment"]["status"] == "success"


def test_gives_up_after_the_last_attempt(client, checkin, upload):
    order = deploy(client, checkin, upload)
    dep_id = order["deployment_id"]
    for _ in range(config.DEPLOY_ATTEMPTS - 1):
        assert fail(client, dep_id, "download timeout")["status"] == "pending"
        make_due(dep_id)
        checkin()
    dep = fail(client, dep_id, "download timeout")
    assert dep["status"] == "failed"
    assert dep["error"] == f"download timeout (attempt {config.DEPLOY_ATTEMPTS}/{config.DEPLOY_ATTEMPTS})"
    make_due(dep_id)
    assert checkin()["update"] is None


def test_permanent_failures_are_not_retried(client, checkin, upload):
    order = deploy(client, checkin, upload)
    dep = fail(client, order["deployment_id"], "sha256 mismatch")
    assert (dep["status"], dep["attempts"], dep["error"]) == ("failed", 1, "sha256 mismatch")


def test_device_can_say_whether_to_retry(client, checkin, upload):
    order = deploy(client, checkin, upload)
    assert fail(client, order["deployment_id"], "radio glitch", retryable=True)["status"] == "pending"
    make_due(order["deployment_id"])
    checkin()
    assert fail(client, order["deployment_id"], "connection lost", retryable=False)["status"] == "failed"


def test_a_rollout_stage_waits_for_retries(client, checkin, upload):
    checkin()
    fw = upload("1.1.0")
    client.post("/api/rollouts", json={"firmware_id": fw["id"], "soak_s": 0})
    order = checkin()["update"]

    def evaluate():
        with SessionLocal() as session:
            rollouts.evaluate(session)
            session.commit()
        return client.get("/api/rollouts").json()[0]["status"]

    fail(client, order["deployment_id"], "connection lost")
    assert evaluate() == "running"  # still being retried
    for _ in range(config.DEPLOY_ATTEMPTS - 1):
        make_due(order["deployment_id"])
        checkin()
        fail(client, order["deployment_id"], "connection lost")
    assert evaluate() == "halted"


def test_retries_are_not_counted_as_failures_in_metrics(client, checkin, upload):
    def failed_total():
        for line in client.get("/metrics").text.splitlines():
            if line.startswith('otter_deployment_outcomes_total{status="failed"}'):
                return float(line.split()[-1])
        return 0.0

    before = failed_total()
    order = deploy(client, checkin, upload)
    fail(client, order["deployment_id"], "connection lost")
    assert failed_total() == before
