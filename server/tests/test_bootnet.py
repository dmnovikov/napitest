"""BOOT без изменений образа: плата найдена в сети по MAC, отвечает SSH, агента нет."""
from fastapi.testclient import TestClient

from app.main import create_app
from app.scheduler.bootnet import parse_arp, subnet_hosts

from conftest import Agent, make_settings, wait_for

ARP = """IP address       HW type     Flags       HW address            Mask     Device
192.168.30.83    0x1         0x2         d2:a9:fd:81:b9:df     *        eth0
192.168.30.78    0x1         0x0         d2:a9:fd:81:b9:df     *        eth0
192.168.30.1     0x1         0x2         18:fd:74:68:44:dd     *        eth0
"""


def test_parse_arp_and_hosts():
    arp = parse_arp(ARP)
    assert arp["d2:a9:fd:81:b9:df"] == ["192.168.30.83"]          # неразрешённая запись .78 не считается
    assert len(subnet_hosts("192.168.30.78")) == 254
    assert subnet_hosts(None) == [] and subnet_hosts("bad") == []


class FakeNet:
    def __init__(self):
        self.up = False
        self.swept = 0

    async def sweep(self, hosts):
        self.swept += 1

    def arp(self):
        return {"02:00:00:00:00:01": ["10.0.0.9"]} if self.up else {}

    async def ssh_banner(self, ip, timeout=2.0):
        return "SSH-2.0-OpenSSH_10.2p1 Ubuntu-2ubuntu3.6" if self.up else None


def make(tmp_path, timeout_s=30.0):
    app = create_app(make_settings(tmp_path, boot_method="network", serial_source="cpuid"))
    core = app.state.core
    core.profile.data["boot"].update(timeout_s=timeout_s, confirm_s=0.2, probe_s=0.05)
    core.factory.bootnet = FakeNet()
    return app, core.factory.bootnet


def boot_stage(c, jid):
    return {s["stage"]: s for s in c.get(f"/api/v1/jobs/{jid}").json()["stages"]}["boot"]


def flashed(c):
    c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})
    a = Agent(c)
    r = c.post("/api/v1/batches", json={"mode": "flash_only", "device_ids": [a.ack["device_id"]], "confirm": True})
    assert r.status_code == 200, r.text
    jid = a.recv("job")["job_id"]
    a.pass_stage(jid, "flash")
    a.event(jid, "job_finished", data={"result": "pass"})
    wait_for(lambda: c.get(f"/api/v1/jobs/{jid}").json()["status"] == "awaiting_boot")
    return a, jid


def test_boot_by_network(tmp_path):
    app, net = make(tmp_path)
    with TestClient(app) as c:
        a, jid = flashed(c)
        assert boot_stage(c, jid)["detail"] == "выключите плату, выньте microSD, включите"
        a.close()                                         # оператор выключил плату
        wait_for(lambda: boot_stage(c, jid)["detail"] == "ждём загрузки с SD-NAND")
        net.up = True                                     # загрузилась с SD-NAND: MAC в сети, SSH отвечает
        wait_for(lambda: c.get(f"/api/v1/jobs/{jid}").json()["status"] == "pass")
        b = boot_stage(c, jid)
        assert b["state"] == "pass" and b["detail"] == "загрузка с SD-NAND, в сети 10.0.0.9"
        assert b["measurements"]["ssh_banner"].startswith("SSH-2.0") and b["warnings"] == []
        # плата работает с SD-NAND, агента там нет: квадрат показывает итог, а не «нет связи»
        dev = c.get("/api/v1/state").json()["devices"][0]
        assert dev["online"] is False and dev["tile"] == "pass"


def test_boot_microsd_left_in(tmp_path):
    app, net = make(tmp_path)
    with TestClient(app) as c:
        a, jid = flashed(c)
        a.close()
        wait_for(lambda: boot_stage(c, jid)["detail"] == "ждём загрузки с SD-NAND")
        b = Agent(c, current_job={"job_id": jid, "mode": "flash_only", "finished": True, "result": "pass",
                                  "stages": {"flash": {"state": "pass"}}})   # снова тестовая microSD
        net.up = True
        wait_for(lambda: boot_stage(c, jid)["detail"] == "загрузилась с microSD: выньте карту и перезагрузите")
        assert c.get(f"/api/v1/jobs/{jid}").json()["status"] == "awaiting_boot"
        try:
            c.app.state.core.db.execute("UPDATE job SET boot_deadline = '2000-01-01T00:00:00Z' WHERE id = ?",
                                        (jid,))                    # время ожидания вышло
            wait_for(lambda: boot_stage(c, jid)["state"] == "skipped")
            assert boot_stage(c, jid)["detail"] == "BOOTCHECK: не подтверждена — плата загрузилась с microSD"
            assert c.get(f"/api/v1/jobs/{jid}").json()["status"] == "pass"
        finally:
            b.close()


def test_boot_network_timeout_then_late(tmp_path):
    app, net = make(tmp_path, timeout_s=0.3)
    with TestClient(app) as c:
        a, jid = flashed(c)
        a.close()
        wait_for(lambda: boot_stage(c, jid)["state"] == "skipped")
        assert c.get(f"/api/v1/jobs/{jid}").json()["status"] == "pass"
        assert boot_stage(c, jid)["warnings"] == ["BOOTCHECK: загрузка не подтверждена"]
        net.up = True                                     # плата всё же загрузилась с SD-NAND
        wait_for(lambda: boot_stage(c, jid)["state"] == "pass")
        assert boot_stage(c, jid)["warnings"] == []


def test_serial_is_cpuid(tmp_path):
    app, _ = make(tmp_path)
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})
        a = Agent(c)
        c.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [a.ack["device_id"]]})
        jid = a.recv("job")["job_id"]
        for s in ("ram", "net", "sdnand"):
            a.pass_stage(jid, s)
        a.event(jid, "job_finished", data={"result": "pass"})
        wait_for(lambda: c.get(f"/api/v1/devices/{a.ack['device_id']}").json()["device"]["serial"] == "CPU0001")
        a.close()


def test_boot_skip_option_no_wait(tmp_path):
    app, net = make(tmp_path)
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})
        a = Agent(c)
        r = c.post("/api/v1/batches", json={"mode": "flash_only", "device_ids": [a.ack["device_id"]],
                                            "confirm": True, "options": {"boot": "skip"}})
        assert r.status_code == 200, r.text
        job = a.recv("job")
        assert job["params"]["stages"]["boot"] is False and job["params"]["boot"]["enabled"] is False
        jid = job["job_id"]
        a.pass_stage(jid, "flash")
        a.event(jid, "job_finished", data={"result": "pass"})
        wait_for(lambda: c.get(f"/api/v1/jobs/{jid}").json()["status"] == "pass")   # без ожидания BOOTCHECK
        assert boot_stage(c, jid)["state"] == "skipped" and net.swept == 0
        assert c.post("/api/v1/batches", json={"mode": "test_only", "device_ids": [a.ack["device_id"]],
                                               "options": {"boot": "maybe"}}).status_code == 400
        a.close()


def test_old_counter_serial_becomes_cpuid(tmp_path):
    settings = make_settings(tmp_path, serial_source="counter")
    app = create_app(settings)
    with TestClient(app) as c:
        a = Agent(c)
        c.app.state.core.db.execute("UPDATE device SET serial = 'NC-2610-0001'")
        a.close()
    settings2 = make_settings(tmp_path, serial_source="cpuid")    # та же база, новая схема
    with TestClient(create_app(settings2)) as c:
        c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})
        d = c.get("/api/v1/devices/1").json()["device"]
        assert d["serial"] == "CPU0001"
        log = c.get("/api/v1/audit").json()
        assert any(x["action"] == "serial_reassigned" and x["detail"]["old"] == "NC-2610-0001" for x in log)
