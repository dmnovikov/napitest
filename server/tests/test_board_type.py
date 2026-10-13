"""Тип платы NAPI-C / NAPI-P: автоопределение по интерфейсу связи, ручная замена, NET пропускается у NAPI-P."""
from conftest import Agent

USB = {"iface": "enx00e04c680001", "bus": "usb", "driver": "r8152", "mac": "00:e0:4c:68:00:01"}
END0 = {"iface": "end0", "bus": "platform", "driver": "rk_gmac-dwmac", "mac": "02:00:00:00:00:01"}


def device(client, dev_id):
    return next(d for d in client.get("/api/v1/state").json()["devices"] if d["device_id"] == dev_id)


def test_detect_by_uplink_and_skip_net_for_napi_p(client):
    c = Agent(client, "CPU1", uplink=END0)
    p = Agent(client, "CPU2", mac="02:00:00:00:00:02", uplink=USB)
    dc, dp = c.ack["device_id"], p.ack["device_id"]
    assert device(client, dc)["board"]["type"] == "napi-c"
    b = device(client, dp)["board"]
    assert b == {"type": "napi-p", "detected": "napi-p", "override": None, "uplink": USB}
    r = client.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [dc, dp]})
    assert r.status_code == 200, r.text
    pc, pp = c.recv("job")["params"], p.recv("job")["params"]
    assert pc["stages"]["net"] is True and pc["board"] == "napi-c" and "skip_reason" not in pc
    assert pp["stages"]["net"] is False and pp["board"] == "napi-p" and "NAPI-P" in pp["skip_reason"]["net"]
    assert pp["stages"]["ram"] is True and pp["stages"]["sdnand"] is True
    csv = client.get(f"/api/v1/batches/{r.json()['batch_id']}/export.csv").text
    assert "NAPI-P,NAPI-P,enx00e04c680001" in csv and "NAPI-C,NAPI-C,end0" in csv
    detail = client.get(f"/api/v1/batches/{r.json()['batch_id']}").json()
    assert {j["board_type"] for j in detail["jobs"]} == {"napi-c", "napi-p"}
    audit = client.get("/api/v1/audit").json()
    start = next(a for a in audit if a["action"] == "batch_start")
    assert start["detail"]["boards"] == {"napi-c": 1, "napi-p": 1}


def test_manual_override_keeps_detected_visible(client):
    p = Agent(client, "CPU2", mac="02:00:00:00:00:02", uplink=USB)
    dp = p.ack["device_id"]
    r = client.put("/api/v1/devices/board", json={"device_ids": [dp], "board": "napi-c"})
    assert r.status_code == 200 and "USB-адаптер" in r.json()["warnings"][0]
    b = device(client, dp)["board"]
    assert b["type"] == "napi-c" and b["override"] == "napi-c" and b["detected"] == "napi-p"
    # ручной тип переживает переподключение: определение по интерфейсу не затирает его
    p.close()
    p = Agent(client, "CPU2", mac="02:00:00:00:00:02", uplink=USB)
    assert device(client, dp)["board"]["type"] == "napi-c"
    r = client.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [dp]})
    job = p.recv("job")
    assert job["params"]["stages"]["net"] is True
    detail = client.get(f"/api/v1/jobs/{job['job_id']}").json()
    assert detail["board_type"] == "napi-c" and detail["board_detected"] == "napi-p"
    # снова «авто»
    assert client.put("/api/v1/devices/board", json={"device_ids": [dp], "board": None}).json()["warnings"] == []
    assert device(client, dp)["board"]["type"] == "napi-p"
    audit = [a for a in client.get("/api/v1/audit").json() if a["action"] == "board_type"]
    assert {a["detail"]["board"] for a in audit} == {"napi-c", "auto"}
    assert client.put("/api/v1/devices/board", json={"device_ids": [dp], "board": "napi-x"}).status_code == 422


def test_old_agent_without_uplink_is_napi_c(client):
    a = Agent(client, "CPU1")
    b = device(client, a.ack["device_id"])["board"]
    assert b["type"] == "napi-c" and b["detected"] is None and b["uplink"] is None


def test_bootcheck_looks_for_usb_adapter_mac(client):
    p = Agent(client, "CPU2", mac="02:00:00:00:00:02", uplink=USB)
    f = client.app.state.core.factory
    row = f.db.one("SELECT COALESCE(uplink_mac, mac) AS mac FROM device WHERE id = ?", (p.ack["device_id"],))
    assert row["mac"] == USB["mac"]
