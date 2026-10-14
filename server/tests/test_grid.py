"""Платы без связи не попадают в партию; «Скрыть офлайн, обнаружить онлайн платы»."""
from fastapi.testclient import TestClient

from app.main import create_app

from conftest import Agent, make_settings, wait_for


def login(c):
    c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})


def ids(c):
    return {d["device_id"]: d for d in c.get("/api/v1/state").json()["devices"]}


def test_offline_board_not_started_and_hidden(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        login(c)
        a = Agent(c, cpuid="CPU0001", mac="02:00:00:00:00:01")
        b = Agent(c, cpuid="CPU0002", mac="02:00:00:00:00:02")
        da, db = a.ack["device_id"], b.ack["device_id"]
        a.ws.send_json({"type": "heartbeat", "job_id": None, "stages": {}})   # свободный агент тоже шлёт heartbeat
        b.close()
        wait_for(lambda: not ids(c)[db]["online"])
        r = c.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [da, db], "confirm": True})
        assert r.status_code == 409 and "нет связи с платами" in r.json()["detail"]
        assert c.get("/api/v1/batches").json() == []                 # партия не создана

        r = c.post("/api/v1/devices/refresh")
        assert r.status_code == 200 and r.json()["hidden"] == 1 and r.json()["reconnect"] == 1
        assert set(ids(c)) == {da}                                  # офлайн скрыта, онлайн осталась
        a.close()
        a = Agent(c, cpuid="CPU0001", mac="02:00:00:00:00:01")      # переподключилась: номер тот же
        assert ids(c)[da]["display_no"] == 1
        n = Agent(c, cpuid="CPU0003", mac="02:00:00:00:00:03")      # новая плата занимает свободный номер 2
        assert ids(c)[n.ack["device_id"]]["display_no"] == 2
        a.close()
        n.close()


def test_refresh_keeps_offline_board_with_job(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        login(c)
        a = Agent(c)
        da = a.ack["device_id"]
        r = c.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [da], "confirm": True})
        assert r.status_code == 200
        a.recv("job")
        a.close()
        wait_for(lambda: not ids(c)[da]["online"])
        r = c.post("/api/v1/devices/refresh").json()
        assert r["hidden"] == 0 and r["kept_busy"] == "1" and da in ids(c)
