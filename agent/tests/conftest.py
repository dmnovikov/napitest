import asyncio

import pytest

from napi_factory_agent.backends import MockBackend
from napi_factory_agent.config import load_config
from napi_factory_agent.runner import Agent
from napi_factory_agent.testing import make_test_image


class FakeLink:
    """Подмена WSLink: отвечает на net_request/serial_request как сервер."""

    def __init__(self, agent, serial_error=None, grant=True):
        self.agent = agent
        self.sent = []
        self.serial_error = serial_error
        self.grant = grant

    def notify_events(self):
        pass

    def send_soon(self, msg):
        self.sent.append(msg)
        t = msg.get("type")
        if t == "net_request" and self.grant:
            asyncio.ensure_future(self.agent.handle(
                {"type": "net_grant", "job_id": msg["job_id"], "host": "127.0.0.1", "port": 5201}))
        elif t == "serial_request":
            reply = {"type": "serial", "job_id": msg["job_id"]}
            if self.serial_error:
                reply["error"] = self.serial_error
            else:
                reply.update(serial="NC-2610-0001", batch="B-1", date="2026-10-10")
            asyncio.ensure_future(self.agent.handle(reply))


@pytest.fixture
def images(tmp_path):
    d = tmp_path / "factory" / "images"
    make_test_image(d, size=2 * 1024 * 1024)
    (tmp_path / "factory" / "VERSION").write_text("2026.10.1\n")
    return d


def make_agent(tmp_path, cpuid="5a4e00000000a001", fail_stage=None, link=True, **kw):
    be = MockBackend(str(tmp_path / f"hw-{cpuid}"), cpuid, speed=1000, fail_stage=fail_stage,
                     pattern_limit=1024 * 1024)
    cfg = load_config(None, {"token": "t", "factory_dir": str(tmp_path / "factory"), "server_url": ""})
    agent = Agent(cfg, be, str(tmp_path / f"state-{cpuid}"))
    if link:
        agent.link = FakeLink(agent, **kw)
    return agent


@pytest.fixture
def agent_factory(tmp_path, images):
    def f(**kw):
        return make_agent(tmp_path, **kw)
    return f
