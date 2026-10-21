"""Правки по замечаниям оператора 2026-10-21: прошивка после FAIL сети, «Без проверки сети»,
тип плат для партии, «сеть стенда?», «Только BOOTCHECK», время до оператора."""
from fastapi.testclient import TestClient

from app.main import create_app

from conftest import TOKEN, Agent, make_settings, wait_for
from test_server import device, start


def fail_net(a, jid, detail="NET: UDP tx 48 Mbit/s<81"):
    a.event(jid, "stage_finished", "net", {"state": "fail", "detail": detail})


def boot_ok(c, cpuid):
    return c.post("/api/v1/agent/boot_ok", json={"cpuid": cpuid, "uptime": 20},
                  headers={"Authorization": f"Bearer {TOKEN}"}).json()


def test_net_fail_still_flashes_and_checks_boot(client):
    a = Agent(client)
    start(client, "test_flash", [a.ack["device_id"]])
    jid = a.recv("job")["job_id"]
    a.pass_stage(jid, "ram")
    fail_net(a, jid)
    a.pass_stage(jid, "sdnand")
    # серийник выдаётся: FAIL сети прошивке не мешает
    a.ws.send_json({"type": "serial_request", "job_id": jid})
    assert "serial" in a.recv("serial")
    a.pass_stage(jid, "flash", "записан")
    a.event(jid, "job_finished", data={"result": "fail", "detail": "NET: UDP tx 48 Mbit/s<81"})
    wait_for(lambda: device(client, "CPU0001")["tile"] == "awaiting_boot")
    a.close()
    assert boot_ok(client, "CPU0001")["job_id"] == jid
    job = client.get(f"/api/v1/jobs/{jid}").json()
    assert job["status"] == "fail" and job["detail"] == "NET: UDP tx 48 Mbit/s<81 · прошита"
    assert {s["stage"]: s for s in job["stages"]}["boot"]["state"] == "pass"
    # плата ушла со связи (загрузилась с SD-NAND): на плитке итог, а не «нет связи»
    assert device(client, "CPU0001")["tile"] == "fail"


def test_net_skip_option(client):
    a = Agent(client)
    start(client, "test_only", [a.ack["device_id"]], options={"net_skip": True})
    params = a.recv("job")["params"]
    assert params["stages"]["net"] is False
    assert params["skip_reason"]["net"] == "NET пропущен (по выбору оператора)"
    a.close()


def test_board_for_whole_batch(client):
    a = Agent(client, uplink={"iface": "enx9c7f64df0902", "bus": "usb", "driver": "cdc_ether",
                              "mac": "9c:7f:64:df:09:02"})
    did = a.ack["device_id"]
    assert device(client, "CPU0001")["board"]["type"] == "napi-p"
    r = start(client, "test_only", [did], options={"board": "napi-c"})
    assert r["warnings"] and "USB-адаптер" in r["warnings"][0]
    params = a.recv("job")["params"]
    assert params["board"] == "napi-c" and params["stages"]["net"] is True
    b = device(client, "CPU0001")["board"]
    assert b["type"] == "napi-c" and b["override"] == "napi-c" and b["detected"] == "napi-p"
    a.close()


def test_most_boards_net_fail_is_stand_network(client):
    agents = [Agent(client, cpuid=f"CPU000{i}", mac=f"02:00:00:00:00:0{i}") for i in range(1, 5)]
    start(client, "test_only", [a.ack["device_id"] for a in agents])
    for i, a in enumerate(agents):
        jid = a.recv("job")["job_id"]
        a.pass_stage(jid, "ram")
        if i < 3:
            fail_net(a, jid)
        else:
            a.pass_stage(jid, "net")
        a.pass_stage(jid, "sdnand")
        a.event(jid, "job_finished", data={"result": "fail" if i < 3 else "pass"})
    wait_for(lambda: device(client, "CPU0004")["tile"] == "pass")
    tiles = [device(client, f"CPU000{i}")["tile"] for i in range(1, 4)]
    assert tiles == ["net_stand"] * 3
    summ = client.get("/api/v1/state").json()["summary"]
    assert summ["net_stand"] == 3 and summ["fail"] == 0
    for a in agents:
        a.close()


def test_retry_bootcheck(tmp_path):
    app = create_app(make_settings(tmp_path))
    app.state.core.profile.data["boot"]["timeout_s"] = 0.3
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})
        a = Agent(c)
        b = Agent(c, cpuid="CPU0002", mac="02:00:00:00:00:02")
        start(c, "flash_only", [a.ack["device_id"]])
        jid = a.recv("job")["job_id"]
        a.pass_stage(jid, "flash")
        a.event(jid, "job_finished", data={"result": "pass"})
        wait_for(lambda: device(c, "CPU0001")["tile"] == "boot_unconfirmed", timeout=5)
        a.close()
        # не прошитую плату повторить нельзя
        r = c.post("/api/v1/devices/bootcheck", json={"device_ids": [b.ack["device_id"]]})
        assert r.status_code == 409 and "заданий не было" in r.json()["detail"]
        # плата без связи (на ней прошитая ОС): ожидание загрузки начинается заново
        app.state.core.profile.data["boot"]["timeout_s"] = 600
        r = c.post("/api/v1/devices/bootcheck", json={"device_ids": [a.ack["device_id"]]})
        assert r.status_code == 200 and r.json() == {"jobs": 1}
        d = wait_for(lambda: (x := device(c, "CPU0001"))["tile"] == "awaiting_boot" and x)
        assert d["job"]["stages"]["boot"]["state"] == "running"
        assert c.get("/api/v1/state").json()["summary"]["active"][0]["operator_at"]
        assert boot_ok(c, "CPU0001")["job_id"] == jid
        j = c.get(f"/api/v1/jobs/{jid}").json()
        assert j["status"] == "pass"
        assert {s["stage"]: s for s in j["stages"]}["boot"]["warnings"] == []
        audit = [x["action"] for x in c.get("/api/v1/audit").json()]
        assert "boot_retry" in audit
        b.close()


def test_napi_p_gets_test_pass_without_net(client):
    # у NAPI-P сеть пропущена, а не пройдена: отметка «тесты пройдены» всё равно ставится,
    # иначе «Только прошивка» ей отказывает
    a = Agent(client, uplink={"iface": "enx1", "bus": "usb", "driver": "cdc_ether", "mac": "9c:7f:64:df:09:02"})
    did = a.ack["device_id"]
    start(client, "test_flash", [did])
    jid = a.recv("job")["job_id"]
    a.pass_stage(jid, "ram")
    a.pass_stage(jid, "sdnand")
    a.ws.send_json({"type": "serial_request", "job_id": jid})
    assert "serial" in a.recv("serial")
    assert client.app.state.core.db.one("SELECT tested_pass FROM device WHERE id = ?", (did,))["tested_pass"] == 1
    a.close()


def test_flash_only_after_net_only_fail(client):
    # «Только прошивка» после тестов, где не прошла только сеть: прошивать можно (серийник выдаётся)
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    jid = a.recv("job")["job_id"]
    a.pass_stage(jid, "ram")
    fail_net(a, jid)
    a.pass_stage(jid, "sdnand")
    a.event(jid, "job_finished", data={"result": "fail"})
    wait_for(lambda: device(client, "CPU0001")["tile"] == "fail")
    start(client, "flash_only", [did], options={"allow_retest": True})
    jid2 = a.recv("job")["job_id"]
    a.ws.send_json({"type": "serial_request", "job_id": jid2})
    assert "serial" in a.recv("serial")
    a.close()


def test_flash_only_refused_after_ram_fail(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    jid = a.recv("job")["job_id"]
    a.event(jid, "stage_finished", "ram", {"state": "fail", "detail": "RAM: 2 err"})
    a.event(jid, "job_finished", data={"result": "fail"})
    wait_for(lambda: device(client, "CPU0001")["tile"] == "fail")
    start(client, "flash_only", [did], options={"allow_retest": True})
    jid2 = a.recv("job")["job_id"]
    a.ws.send_json({"type": "serial_request", "job_id": jid2})
    assert a.recv("serial")["error"] == "device has no TEST_PASS"
    a.close()
