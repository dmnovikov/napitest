"""Прогноз времени задания и партии; страница без входа: идущие тесты и образ microSD."""
from fastapi.testclient import TestClient

from conftest import Agent, wait_for


def device(client, dev_id):
    return next(d for d in client.get("/api/v1/state").json()["devices"] if d["device_id"] == dev_id)


def history(f, batch, stage, t, params):
    """Прошлое успешное задание с этапом заданной длительности."""
    import json
    jid = f"old-{stage}-{t}"
    f.db.execute("INSERT INTO job (id, batch_id, device_id, mode, status, created_at, params_json)"
                 " VALUES (?, ?, 1, 'test_only', 'pass', '2026-10-01T00:00:00+00:00', ?)", (jid, batch, json.dumps(params)))
    f.db.execute("INSERT INTO stage_result (job_id, stage, state, t, updated_at) VALUES (?, ?, 'pass', ?, ?)",
                 (jid, stage, t, "2026-10-01T00:00:00+00:00"))


def test_job_and_batch_eta(client):
    a = Agent(client, "CPU1")
    dev = a.ack["device_id"]
    r = client.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [dev]})
    assert r.status_code == 200, r.text
    job = a.recv("job")
    f = client.app.state.core.factory
    params = job["params"]
    b = r.json()["batch_id"]
    for t in (100, 120, 140):
        history(f, b, "ram", t, params)
        history(f, b, "sdnand", t / 2, params)
    # сеть — другой вариант (TCP): в медиану для UDP не идёт
    history(f, b, "net", 999, {**params, "net": {**params["net"], "method": "tcp"}})
    f.__dict__.pop("_eta_cache", None)
    v = device(client, dev)["job"]
    # RAM 120 + SD 60; для NET истории нет — прогноз занижен
    assert v["eta_s"] == 180 and v["eta_partial"] is True
    # идёт RAM, 25 % за 30 с: осталось 90 с по проценту
    a.heartbeat(job["job_id"], "test_only", {"ram": {"state": "running", "t": 30, "pct": 25},
                                             "net": {"state": "pending"}, "sdnand": {"state": "pending"}})
    wait_for(lambda: device(client, dev)["job"]["eta_s"] == 150)
    active = client.get("/api/v1/state").json()["summary"]["active"]
    assert [(x["boards"], x["eta_partial"]) for x in active] == [(1, True)] and active[0]["eta_at"]


def test_public_status_without_login(client, settings):
    a = Agent(client, "CPU1")
    client.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [a.ack["device_id"]]})
    anon = TestClient(client.app)
    r = anon.get("/api/v1/public")
    assert r.status_code == 200
    body = r.json()
    assert len(body["active"]) == 1 and body["active"][0]["boards"] == 1 and body["active"][0]["operator"] == "admin"
    assert body["sd_image"] is None
    # образ microSD скачивается без входа (здесь его ещё нет — 404, а не 401); остальное — под паролем
    assert anon.get("/api/v1/sdcard/download").status_code == 404
    assert anon.get("/api/v1/state").status_code == 401
    assert anon.get("/api/v1/sdcard").status_code == 401
