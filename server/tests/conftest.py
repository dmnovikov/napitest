import time

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import REPO_ROOT, Settings
from app.main import create_app

TOKEN = "test-agent-token-0123456789"


def make_settings(tmp_path, **overrides) -> Settings:
    s = Settings()
    s.db_path = str(tmp_path / "factory.db")
    # Профиль стенда, но с нормальными (не временными) слотами iperf3: тесты не зависят от правок на месте
    prof = yaml.safe_load((REPO_ROOT / "config" / "napi-c-rk3308.yaml").read_text())
    prof["net"]["max_parallel"] = 4
    # Существующие тесты проверяют BOOT через отчёт и серийник-счётчик; сеть и cpuid — в test_bootnet.py
    prof["boot"]["method"] = overrides.pop("boot_method", "report")
    prof["serial"]["source"] = overrides.pop("serial_source", "counter")
    # Без скачивания реальных образов из интернета; варианты ОС задают тесты test_images.py
    prof["os_images"] = overrides.pop("os_images", [])
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(prof, allow_unicode=True))
    s.profile_path = str(tmp_path / "profile.yaml")
    s.web_dist = str(tmp_path / "dist")
    s.agent_token = TOKEN
    s.admin_password = "secret-pass"
    s.allow_simulated = True
    s.iperf_enable = False
    s.discovery_port = 0
    s.images_dir = str(tmp_path / "images")
    s.tick_s = 0.05
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"})
        assert r.status_code == 200
        try:
            yield c
        finally:
            close_agents()


def hello(cpuid="CPU0001", mac="02:00:00:00:00:01", current_job=None, simulated=True, stream=None, boot_id=None,
          images=None):
    return {
        "type": "hello", "proto": 1, "cpuid": cpuid, "id_source": "dt-serial", "mac": mac,
        "mac_source": "cpuid-derived", "ip": "10.0.0.2", "hostname": "napi", "agent_version": "0.1.0",
        "test_sd_version": "2026.10.1", "kernel": "6.6", "ram_bytes": 512 << 20, "has_led": False,
        "simulated": simulated, "inventory": {"ok": True, "target": "mmcblk0", "devices": [], "reasons": []},
        "sd_selfcheck": {"state": "pass"}, "stream": stream or f"s-{cpuid}", "current_job": current_job,
        "boot_id": boot_id, "images": images or {},
    }


OPEN_AGENTS: list = []


class Agent:
    """Тестовый агент поверх TestClient websocket."""

    def __init__(self, client, cpuid="CPU0001", mac="02:00:00:00:00:01", **kw):
        self.ws = client.websocket_connect("/ws/agent", headers={"Authorization": f"Bearer {TOKEN}"})
        self.ws.__enter__()
        self.closed = False
        self.cpuid = cpuid
        self.stream = f"s-{cpuid}"
        self.seq = 0
        try:
            self.ws.send_json(hello(cpuid, mac, stream=self.stream, **kw))
            self.ack = self.recv("hello_ack")
        except BaseException as exc:
            self.closed = True
            self.ws.__exit__(type(exc), exc, None)
            raise
        OPEN_AGENTS.append(self)

    def recv(self, type_=None):
        while True:
            msg = self.ws.receive_json()
            if type_ is None or msg["type"] == type_:
                return msg

    def event(self, job_id, kind, stage=None, data=None, seq=None):
        if seq is None:
            self.seq += 1
            seq = self.seq
        self.ws.send_json({"type": "event", "stream": self.stream, "seq": seq, "ts": time.time(),
                           "job_id": job_id, "kind": kind, "stage": stage, "data": data or {}})
        return self.recv("ack")

    def heartbeat(self, job_id, mode, stages, finished=False, result=None):
        self.ws.send_json({"type": "heartbeat", "job_id": job_id, "mode": mode, "elapsed": 1, "stages": stages,
                           "finished": finished, "result": result})

    def pass_stage(self, job_id, stage, detail="ok", measurements=None):
        self.event(job_id, "stage_started", stage)
        self.event(job_id, "stage_finished", stage,
                   {"state": "pass", "t": 1, "detail": detail, "measurements": measurements or {}, "log": ""})

    def close(self):
        if not self.closed:
            self.closed = True
            self.ws.__exit__(None, None, None)


def close_agents():
    while OPEN_AGENTS:
        try:
            OPEN_AGENTS.pop().close()
        except Exception:
            pass


def wait_for(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met")
