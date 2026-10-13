"""Интерфейс связи с сервером (тип платы NAPI-C / NAPI-P определяет сервер) и пропуск NET по причине от сервера."""
from napi_factory_agent.backends import MockBackend
from napi_factory_agent.config import load_config
from napi_factory_agent.identity import route_iface, uplink
from napi_factory_agent.runner import Agent
from napi_factory_agent.testing import job_params

from .conftest import FakeLink
from .test_runner import run, run_job


def agent_with(tmp_path, link_kind):
    be = MockBackend(str(tmp_path / link_kind), "5a4e00000000b001", speed=1000, uplink=link_kind,
                     pattern_limit=1024 * 1024)
    cfg = load_config(None, {"token": "t", "factory_dir": str(tmp_path / "factory"), "server_url": ""})
    a = Agent(cfg, be, str(tmp_path / f"state-{link_kind}"))
    a.link = FakeLink(a)
    return a


def test_hello_reports_uplink(tmp_path, images):
    c = agent_with(tmp_path, "end0").hello()["uplink"]
    assert c["iface"] == "end0" and c["bus"] == "platform" and c["driver"] == "rk_gmac-dwmac"
    a = agent_with(tmp_path, "usb")
    a.server_addr = ("10.99.0.1", 8080)
    h = a.hello()
    assert h["uplink"]["bus"] == "usb" and h["uplink"]["driver"] == "r8152"
    assert h["uplink"]["iface"].startswith("enx") and h["mac"].startswith("02:")   # MAC платы (end0) остаётся


def test_route_longest_prefix(tmp_path):
    (tmp_path / "proc/net").mkdir(parents=True)
    (tmp_path / "proc/net/route").write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "end0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        "enx1\t0010A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
        "down\t0010A8C0\t00000000\t0000\t0\t0\t1\tFFFFFFFF\t0\t0\t0\n")
    assert route_iface(tmp_path, "192.168.16.100") == "enx1"
    assert route_iface(tmp_path, "8.8.8.8") == "end0"
    assert route_iface(tmp_path, None) == "end0"
    assert uplink(tmp_path / "nothing", None) is None


def test_net_skipped_with_server_reason(agent_factory):
    a = agent_factory()
    p = job_params("test_only")
    p["stages"]["net"] = False
    p["skip_reason"] = {"net": "NAPI-P: сеть не проверяется"}
    job = run(run_job(a, "test_only", params=p))
    assert job["result"] == "pass", job
    assert job["stages"]["net"] == {"state": "skipped", "detail": "NAPI-P: сеть не проверяется"}
