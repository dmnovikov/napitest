import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.scheduler.net import NetScheduler
from app.services.factory import verdict

from conftest import TOKEN, Agent, make_settings, wait_for

ALL_PASS = {s: {"state": "pass"} for s in ("ram", "net", "sdnand")}


def device(client, cpuid):
    for d in client.get("/api/v1/state").json()["devices"]:
        if d["cpuid"] == cpuid:
            return d
    return None


def start(client, mode, ids, **kw):
    body = {"mode": mode, "device_ids": ids, "confirm": True, **kw}
    r = client.post("/api/v1/batches", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_required(client):
    client.cookies.clear()
    assert client.get("/api/v1/state").status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401


def test_agent_bad_token(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/agent", headers={"Authorization": "Bearer wrong"}) as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_register_and_state(client):
    a = Agent(client)
    assert a.ack["cpuid"] == "CPU0001"
    d = device(client, "CPU0001")
    assert d["online"] and d["tile"] == "ready" and d["display_no"] == 1
    assert d["cpuid_tail"] == "0001"
    a.close()


def test_assigned_id_when_no_cpuid(client):
    a = Agent(client, cpuid=None)
    assert a.ack["cpuid"].startswith("A") and len(a.ack["cpuid"]) == 17
    d = device(client, a.ack["cpuid"])
    assert d["id_source"] == "assigned"
    a.close()


def test_simulated_rejected_in_production(tmp_path):
    app = create_app(make_settings(tmp_path, allow_simulated=False))
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            Agent(c)
        assert exc.value.code == 4403


def test_destructive_requires_confirm(client):
    a = Agent(client)
    did = a.ack["device_id"]
    r = client.post("/api/v1/batches", json={"mode": "test_flash", "device_ids": [did]})
    assert r.status_code == 400
    a.close()


def test_test_only_pass_assigns_serial(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    job = a.recv("job")
    jid = job["job_id"]
    assert job["mode"] == "test_only"
    assert job["params"]["stages"] == {"ram": True, "net": True, "sdnand": True, "flash": False, "boot": False}
    assert job["params"]["sdnand"]["pattern"] == "quick"
    assert job["params"]["destructive_enabled"] is client.app.state.core.profile.hardware_validated
    a.event(jid, "job_started", data={"mode": "test_only"})
    a.heartbeat(jid, "test_only", {"ram": {"state": "running", "pct": 10}})
    wait_for(lambda: device(client, "CPU0001")["tile"] == "busy")
    for s in ("ram", "net", "sdnand"):
        a.pass_stage(jid, s)
    a.event(jid, "job_finished", data={"result": "pass", "stages": ALL_PASS})
    d = wait_for(lambda: (x := device(client, "CPU0001"))["tile"] == "pass" and x)
    assert d["serial"].startswith("NC-")
    assert d["job"]["stages"]["flash"]["state"] == "skipped"
    rows = client.get(f"/api/v1/batches/{d['job']['batch_id']}/export.csv").text
    assert d["serial"] in rows and "ram_state" in rows
    a.close()


def test_event_dedup(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    jid = a.recv("job")["job_id"]
    a.event(jid, "stage_finished", "ram", {"state": "fail", "t": 2, "detail": "RAM: 2 err"}, seq=5)
    # повтор того же seq с другим содержимым не применяется
    ack = a.event(jid, "stage_finished", "ram", {"state": "pass", "t": 2, "detail": "ok"}, seq=5)
    assert ack["seq"] == 5
    card = client.get(f"/api/v1/devices/{did}").json()
    ram = [s for s in card["jobs"][0]["stages"] if s["stage"] == "ram"][0]
    assert ram["state"] == "fail" and ram["detail"] == "RAM: 2 err"
    a.close()


def test_test_flash_boot_flow(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_flash", [did])
    job = a.recv("job")
    jid = job["job_id"]
    assert job["params"]["sdnand"]["pattern"] == "beyond_image"
    # серийник до прохождения тестов не выдаётся
    a.ws.send_json({"type": "serial_request", "job_id": jid})
    assert "error" in a.recv("serial")
    for s in ("ram", "net", "sdnand"):
        a.pass_stage(jid, s)
    a.ws.send_json({"type": "serial_request", "job_id": jid})
    serial = a.recv("serial")
    assert serial["serial"].startswith("NC-") and serial["batch"].startswith("B-")
    ver = client.app.state.core.profile.image_version
    a.pass_stage(jid, "flash", "записан", {"image_version": ver, "sha256_readback": "ab" * 32,
                                           "image_size": 1000})
    a.event(jid, "job_finished", data={"result": "pass"})
    d = wait_for(lambda: (x := device(client, "CPU0001"))["tile"] == "awaiting_boot" and x)
    assert d["job"]["stages"]["boot"]["state"] == "running"
    a.close()  # плату выключили и вынули microSD
    d = wait_for(lambda: (x := device(client, "CPU0001"))["tile"] == "awaiting_boot" and not x["online"] and x)

    r = client.post("/api/v1/agent/boot_ok", json={"cpuid": "CPU0001", "image_version": ver, "uptime": 20},
                    headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = client.post("/api/v1/agent/boot_ok", json={"cpuid": "CPU0001", "image_version": ver, "uptime": 20},
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.json() == {"ok": True, "job_id": jid}
    job = client.get(f"/api/v1/jobs/{jid}").json()
    assert job["status"] == "pass" and job["image_version"] == ver
    assert [s for s in job["stages"] if s["stage"] == "boot"][0]["state"] == "pass"
    # повторный отчёт не ломает итог
    r = client.post("/api/v1/agent/boot_ok", json={"cpuid": "CPU0001"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.json()["job_id"] is None


def test_test_flash_no_flash_when_test_fails(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_flash", [did])
    jid = a.recv("job")["job_id"]
    a.event(jid, "stage_finished", "ram", {"state": "fail", "detail": "RAM: 2 err"})
    a.ws.send_json({"type": "serial_request", "job_id": jid})
    assert "error" in a.recv("serial")
    a.event(jid, "job_finished", data={"result": "fail"})
    d = wait_for(lambda: (x := device(client, "CPU0001"))["tile"] == "fail" and x)
    assert d["serial"] is None
    assert d["job"]["stages"]["ram"]["detail"] == "RAM: 2 err"
    a.close()


def test_inconsistent_agent_pass_is_fail(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    jid = a.recv("job")["job_id"]
    a.pass_stage(jid, "ram")
    a.event(jid, "job_finished", data={"result": "pass"})  # NET и SD не пройдены
    wait_for(lambda: device(client, "CPU0001")["tile"] == "fail")
    a.close()


def test_boot_timeout(tmp_path):
    app = create_app(make_settings(tmp_path))
    app.state.core.profile.data["boot"]["timeout_s"] = 0.3
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})
        a = Agent(c)
        c.agents_to_close = [a]
        start(c, "flash_only", [a.ack["device_id"]])
        jid = a.recv("job")["job_id"]
        a.pass_stage(jid, "flash")
        a.event(jid, "job_finished", data={"result": "pass"})
        # загрузка не подтверждена — не дефект: PASS с замечанием, CHECKB серый
        d = wait_for(lambda: (x := device(c, "CPU0001"))["tile"] == "pass" and x, timeout=5)
        b = d["job"]["stages"]["boot"]
        assert b["state"] == "skipped" and b["detail"] == "BOOTCHECK: загрузка не подтверждена"
        assert d["job"]["warnings_count"] == 1
        assert c.get("/api/v1/state").json()["summary"]["fail"] == 0
        # отчёт пришёл позже: BOOTCHECK пройден, замечание снято
        assert boot(c, "CPU0001")["job_id"] == jid
        j = c.get(f"/api/v1/jobs/{jid}").json()
        assert j["status"] == "pass" and j["detail"] is None
        b = {s["stage"]: s for s in j["stages"]}["boot"]
        assert b["state"] == "pass" and b["warnings"] == []
        assert device(c, "CPU0001")["tile"] == "pass"
        # повторный отчёт уже ничего не меняет
        assert boot(c, "CPU0001")["job_id"] is None
        a.close()


def test_late_boot_not_applied_when_other_stage_failed(tmp_path):
    app = create_app(make_settings(tmp_path))
    app.state.core.profile.data["boot"]["timeout_s"] = 0.3
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})
        a = Agent(c)
        start(c, "flash_only", [a.ack["device_id"]])
        jid = a.recv("job")["job_id"]
        a.event(jid, "stage_finished", stage="flash", data={"state": "fail", "detail": "FLASH_FAIL readback"})
        a.event(jid, "job_finished", data={"result": "fail"})
        wait_for(lambda: device(c, "CPU0001")["tile"] == "fail")
        assert boot(c, "CPU0001")["job_id"] is None
        assert c.get(f"/api/v1/jobs/{jid}").json()["status"] == "fail"
        a.close()


def test_flash_only_serial_requires_tested(client):
    a = Agent(client)
    start(client, "flash_only", [a.ack["device_id"]])
    jid = a.recv("job")["job_id"]
    a.ws.send_json({"type": "serial_request", "job_id": jid})
    assert a.recv("serial")["error"] == "device has no TEST_PASS"
    a.close()


def test_reconnect_restores_running_job(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    jid = a.recv("job")["job_id"]
    a.event(jid, "job_started")
    a.pass_stage(jid, "ram")
    a.close()
    wait_for(lambda: device(client, "CPU0001")["tile"] == "offline")
    # реконнект: агент сообщает текущее задание, затем досылает очередь
    snap = {"job_id": jid, "mode": "test_only", "elapsed": 50, "finished": False,
            "stages": {"ram": {"state": "pass"}, "net": {"state": "pass"}, "sdnand": {"state": "running", "pct": 50}}}
    b = Agent(client, current_job=snap)
    assert b.ack["device_id"] == did
    b.seq = a.seq
    b.pass_stage(jid, "net")
    d = wait_for(lambda: (x := device(client, "CPU0001"))["tile"] == "busy" and x)
    assert d["job"]["stages"]["sdnand"]["state"] == "running"
    b.pass_stage(jid, "sdnand")
    b.event(jid, "job_finished", data={"result": "pass"})
    wait_for(lambda: device(client, "CPU0001")["tile"] == "pass")
    b.close()


def test_agent_lost_job_marks_interrupted(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    jid = a.recv("job")["job_id"]
    a.event(jid, "job_started")
    a.close()
    b = Agent(client)  # current_job = None: state на microSD потерян
    assert client.get(f"/api/v1/jobs/{jid}").json()["status"] == "interrupted"
    assert device(client, "CPU0001")["tile"] == "interrupted"
    b.close()


def test_stop_queued_and_running(client):
    a = Agent(client, cpuid="C1", mac="02:00:00:00:00:01")
    b = Agent(client, cpuid="C2", mac="02:00:00:00:00:02")
    res = start(client, "test_only", [a.ack["device_id"], b.ack["device_id"]])
    ja = a.recv("job")["job_id"]
    b.recv("job")
    a.event(ja, "job_started")
    r = client.post(f"/api/v1/batches/{res['batch_id']}/stop", json={"device_ids": []})
    assert len(r.json()["jobs"]) == 2
    assert a.recv("cancel")["job_id"] == ja
    # queued задание отменено сразу, running ждёт итога от агента
    assert device(client, "C2")["job"]["status"] == "cancelled"
    assert device(client, "C1")["job"]["status"] == "running"
    a.event(ja, "job_finished", data={"result": "cancelled"})
    wait_for(lambda: device(client, "C1")["job"]["status"] == "cancelled")
    batch = client.get(f"/api/v1/batches/{res['batch_id']}").json()["batch"]
    assert batch["finished_at"] is not None
    a.close()
    b.close()


def test_busy_device_rejected(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    r = client.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [did]})
    assert r.status_code == 409
    a.close()


def test_duplicate_cpuid_rejected(client):
    a = Agent(client, cpuid="DUP", mac="02:00:00:00:00:01")
    with pytest.raises(WebSocketDisconnect) as exc:
        Agent(client, cpuid="DUP", mac="02:00:00:00:00:99")
    assert exc.value.code == 4409
    a.close()


def test_net_slots_limited(client):
    agents = [Agent(client, cpuid=f"N{i}", mac=f"02:00:00:00:01:{i:02x}") for i in range(6)]
    ids = [a.ack["device_id"] for a in agents]
    start(client, "test_only", ids)
    jobs = [a.recv("job")["job_id"] for a in agents]
    for a, j in zip(agents, jobs):
        a.ws.send_json({"type": "net_request", "job_id": j})
    grants = [a.recv("net_grant") for a in agents[:4]]
    assert sorted(g["port"] for g in grants) == [5201, 5202, 5203, 5204]
    state = client.get("/api/v1/state").json()["summary"]["net"]
    assert state == {"active": 4, "waiting": 2, "slots": 4}
    agents[0].ws.send_json({"type": "net_release", "job_id": jobs[0]})
    assert agents[4].recv("net_grant")["port"] == grants[0]["port"]
    for a in agents:
        a.close()


def test_golden_batch_recorded(client):
    a = Agent(client)
    did = a.ack["device_id"]
    client.post(f"/api/v1/devices/{did}/golden", json={"is_golden": True})
    g = start(client, "test_only", [did], kind="golden")
    jid = a.recv("job")["job_id"]
    for s in ("ram", "net", "sdnand"):
        a.pass_stage(jid, s)
    a.event(jid, "job_finished", data={"result": "pass"})
    wait_for(lambda: client.get(f"/api/v1/batches/{g['batch_id']}").json()["batch"]["golden"] == "pass")
    p = start(client, "test_only", [did])
    assert client.get(f"/api/v1/batches/{p['batch_id']}").json()["batch"]["golden"] == "pass"
    assert device(client, "CPU0001")["is_golden"] is True
    a.close()


def test_label_only_after_pass(client):
    a = Agent(client)
    did = a.ack["device_id"]
    assert client.get(f"/api/v1/devices/{did}/label").status_code == 409
    start(client, "test_only", [did])
    jid = a.recv("job")["job_id"]
    for s in ("ram", "net", "sdnand"):
        a.pass_stage(jid, s)
    a.event(jid, "job_finished", data={"result": "pass"})
    wait_for(lambda: device(client, "CPU0001")["tile"] == "pass")
    page = client.get(f"/api/v1/devices/{did}/label")
    assert page.status_code == 200 and "<svg" in page.text
    a.close()


def test_ui_ws_snapshot_and_updates(client):
    with client.websocket_connect("/ws/ui") as ui:
        snap = ui.receive_json()
        assert snap["type"] == "snapshot" and snap["summary"]["total"] == 0
        a = Agent(client)
        msg = ui.receive_json()
        while msg["type"] != "devices":
            msg = ui.receive_json()
        assert msg["devices"][0]["cpuid"] == "CPU0001"
        a.close()


def test_verdict():
    en = {"ram": True, "net": True, "sdnand": True, "flash": True, "boot": True}
    st = {s: {"state": "pass"} for s in en}
    assert verdict(en, st) == "pass"
    st["boot"] = {"state": "running"}
    assert verdict(en, st) is None
    assert verdict(en, st, include_boot=False) == "pass"
    st["flash"] = {"state": "interrupted"}
    assert verdict(en, st) == "interrupted"


def test_net_scheduler_expire():
    ns = NetScheduler([1, 2], lease_s=0.0)
    assert ns.request(1, "a").port == 1
    assert ns.request(2, "b").port == 2
    assert ns.request(3, "c") is None
    time.sleep(0.01)
    granted = ns.expire()
    assert [g.device_id for g in granted] == [3]


def boot(client, cpuid, version=None):
    version = version or client.app.state.core.profile.image_version
    return client.post("/api/v1/agent/boot_ok", json={"cpuid": cpuid, "image_version": version, "uptime": 9},
                       headers={"Authorization": f"Bearer {TOKEN}"}).json()


def test_boot_report_before_job_finished(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "flash_only", [did])
    jid = a.recv("job")["job_id"]
    a.event(jid, "job_started")
    a.pass_stage(jid, "flash")
    a.close()  # job_finished остался в очереди на microSD
    assert boot(client, "CPU0001")["job_id"] == jid
    assert client.get(f"/api/v1/jobs/{jid}").json()["status"] == "pass"
    # поздний job_finished не меняет итог
    b = Agent(client, current_job={"job_id": jid, "mode": "flash_only", "finished": True, "result": "pass",
                                   "stages": {"flash": {"state": "pass"}}})
    b.seq = 10
    b.event(jid, "job_finished", data={"result": "pass"})
    assert client.get(f"/api/v1/jobs/{jid}").json()["status"] == "pass"


def test_boot_report_before_flash_result(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "flash_only", [did])
    jid = a.recv("job")["job_id"]
    a.event(jid, "job_started")
    assert boot(client, "CPU0001")["job_id"] == jid       # FLASH ещё не подтверждён
    assert client.get(f"/api/v1/jobs/{jid}").json()["status"] == "running"
    a.pass_stage(jid, "flash")
    a.event(jid, "job_finished", data={"result": "pass"})
    wait_for(lambda: client.get(f"/api/v1/jobs/{jid}").json()["status"] == "pass")


def test_event_from_moved_card_applies_to_original_board(client):
    a = Agent(client, cpuid="BOARD1", mac="02:00:00:00:00:01")
    start(client, "test_only", [a.ack["device_id"]])
    jid = a.recv("job")["job_id"]
    a.close()
    b = Agent(client, cpuid="BOARD2", mac="02:00:00:00:00:02")  # та же карта в другой плате
    b.event(jid, "stage_finished", "ram", {"state": "fail", "detail": "RAM: 2 err"})
    b.event(jid, "job_finished", data={"result": "fail"})
    assert client.get(f"/api/v1/jobs/{jid}").json()["status"] == "fail"
    assert device(client, "BOARD2")["job"] is None


def test_memtester_option(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    assert a.recv("job")["params"]["ram"]["memtester_max_mib"] == 64   # по умолчанию быстрый
    client.post("/api/v1/stop", json={"device_ids": [did]})
    start(client, "test_only", [did], options={"memtester": "full"})
    assert a.recv("job")["params"]["ram"]["memtester_max_mib"] == 256
    r = client.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [did], "options": {"memtester": "x"}})
    assert r.status_code in (400, 409)


def test_pass_with_warnings(client):
    a = Agent(client)
    did = a.ack["device_id"]
    b = start(client, "test_only", [did])
    jid = a.recv("job")["job_id"]
    a.pass_stage(jid, "ram")
    a.event(jid, "stage_finished", "net", {"state": "pass", "t": 30, "detail": "100Full, tx 86, rx TCP 36/UDP 95",
                                           "warnings": ["NET: TCP rx 36 Mbit/s<80, UDP rx 95 Mbit/s, потери 0.04 %"]})
    a.pass_stage(jid, "sdnand")
    a.event(jid, "job_finished", data={"result": "pass"})
    d = wait_for(lambda: (x := device(client, "CPU0001"))["tile"] == "pass" and x)
    assert d["job"]["warnings_count"] == 1
    assert d["job"]["stages"]["net"]["warnings"][0].startswith("NET: TCP rx 36")
    assert client.get("/api/v1/state").json()["summary"]["pass_warn"] == 1
    card = client.get(f"/api/v1/devices/{did}").json()
    assert [s for s in card["jobs"][0]["stages"] if s["stage"] == "net"][0]["warnings"]
    assert "TCP rx 36" in client.get(f"/api/v1/batches/{b['batch_id']}/export.csv").text


def test_retest_warning(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    jid = a.recv("job")["job_id"]
    for s in ("ram", "net", "sdnand"):
        a.pass_stage(jid, s)
    a.event(jid, "job_finished", data={"result": "pass"})
    wait_for(lambda: device(client, "CPU0001")["tile"] == "pass")
    assert device(client, "CPU0001")["tested_before"] is True
    r = client.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [did], "confirm": True})
    assert r.status_code == 409
    seen = r.json()["extra"]["retest"][0]
    assert seen["device_id"] == did and seen["last_status"] == "pass" and seen["serial"].startswith("NC-")
    # прошивка этой платы ещё не делалась: «Только прошивка» без предупреждения
    assert client.post("/api/v1/batches", json={"mode": "flash_only", "device_ids": [did], "confirm": True}).status_code == 200
    client.post("/api/v1/stop", json={"device_ids": [did]})
    a.recv("job")
    r = client.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [did], "confirm": True,
                                             "options": {"allow_retest": True}})
    assert r.status_code == 200
    assert any(e["action"] == "retest_confirmed" for e in client.get("/api/v1/audit").json())


def test_blocked_stage_is_incomplete_not_fail(client):
    a = Agent(client)
    did = a.ack["device_id"]
    start(client, "test_only", [did])
    jid = a.recv("job")["job_id"]
    a.pass_stage(jid, "ram")
    a.pass_stage(jid, "net")
    a.event(jid, "stage_finished", "sdnand", {"state": "skipped", "detail": "SD: запись запрещена (profile not validated)",
                                              "measurements": {"blocked": True}})
    a.event(jid, "job_finished", data={"result": "incomplete", "detail": "SD: запись запрещена (profile not validated)"})
    d = wait_for(lambda: (x := device(client, "CPU0001"))["tile"] == "incomplete" and x)
    assert d["job"]["status"] == "incomplete" and d["serial"] is None
    s = client.get("/api/v1/state").json()["summary"]
    assert s["incomplete"] == 1 and s["fail"] == 0 and s["pass"] == 0


def test_expected_boards(client):
    assert client.get("/api/v1/state").json()["summary"]["expected"] == 0
    assert client.put("/api/v1/expected", json={"count": 8}).json() == {"ok": True, "expected": 8}
    assert client.get("/api/v1/state").json()["summary"]["expected"] == 8
    assert client.put("/api/v1/expected", json={"count": -1}).status_code == 400


def test_net_option_selects_method(client):
    p = client.app.state.core.profile
    assert p.job_params("test_only", {}, False)["net"]["method"] == "udp"
    full = p.job_params("test_only", {"net": "full"}, False)["net"]
    assert full["method"] == "tcp" and full["udp_rate_mbps"] == 90 and full["udp_max_loss_rx_pct"] == 2.0
    with pytest.raises(ValueError):
        p.job_params("test_only", {"net": "max"}, False)


def test_reconnected_tested_board_shows_previous(client):
    a = Agent(client, boot_id="boot-1")
    start(client, "test_only", [a.ack["device_id"]])
    jid = a.recv("job")["job_id"]
    for s in ("ram", "net", "sdnand"):
        a.pass_stage(jid, s)
    a.event(jid, "job_finished", data={"result": "pass"})
    d = wait_for(lambda: (x := device(client, "CPU0001"))["tile"] == "pass" and x)
    assert d["previous"] is None
    a.close()
    # та же загрузка (обрыв связи, перезапуск сервера): итог остаётся на квадрате
    b = Agent(client, boot_id="boot-1", current_job={"job_id": jid, "mode": "test_only", "finished": True,
                                                    "result": "pass", "stages": {}})
    assert device(client, "CPU0001")["tile"] == "pass"
    b.close()
    # плату перезагрузили/переподключили: «готова», но видно, что уже проверялась
    c = Agent(client, boot_id="boot-2")
    d = device(client, "CPU0001")
    assert d["tile"] == "ready" and d["tested_before"]
    assert d["previous"]["status"] == "pass" and d["previous"]["mode"] == "test_only"
    assert d["previous"]["job_id"] == jid and d["previous"]["batch"]
    c.close()



def test_old_boot_timeout_fail_reclassified(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})
        a = Agent(c)
        start(c, "flash_only", [a.ack["device_id"]])
        jid = a.recv("job")["job_id"]
        a.pass_stage(jid, "flash")
        a.event(jid, "job_finished", data={"result": "pass"})
        wait_for(lambda: c.get(f"/api/v1/jobs/{jid}").json()["status"] == "awaiting_boot")
        a.close()
        db = c.app.state.core.db          # запись как при прежнем правиле: таймаут BOOT = FAIL
        db.execute("UPDATE stage_result SET state = 'fail', detail = 'BOOT: no report' WHERE job_id = ? AND stage = 'boot'", (jid,))
        db.execute("UPDATE job SET status = 'fail', result = 'fail', detail = 'BOOT: no report' WHERE id = ?", (jid,))
    with TestClient(create_app(make_settings(tmp_path))) as c:      # перезапуск сервера
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})
        j = c.get(f"/api/v1/jobs/{jid}").json()
        assert j["status"] == "pass" and j["detail"] is None
        b = {s["stage"]: s for s in j["stages"]}["boot"]
        assert b["state"] == "skipped" and b["warnings"] == ["BOOTCHECK: загрузка не подтверждена"]
