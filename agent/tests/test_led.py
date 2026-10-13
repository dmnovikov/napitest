"""Светодиод: брак — частое мигание до нового задания; «Идентифицировать» — горит непрерывно, затем прежний режим."""
import asyncio

from napi_factory_agent.backends import RealBackend
from napi_factory_agent.testing import job_params

from .test_runner import run_job


def fake_led(tmp_path, trigger="heartbeat"):
    led = tmp_path / "leds" / "rockpis:blue:user"
    led.mkdir(parents=True)
    (led / "trigger").write_text(f"none timer [{trigger}] mmc0")
    (led / "max_brightness").write_text("255\n")
    for f in ("brightness", "delay_on", "delay_off"):
        (led / f).write_text("0")
    return led


def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(RealBackend, "leds_dir", tmp_path / "leds")
    return RealBackend(str(tmp_path / "state"), led_name="rockpis:blue:user")


def test_fail_blink_and_back(tmp_path, monkeypatch):
    led = fake_led(tmp_path)
    be = backend(tmp_path, monkeypatch)
    assert be.has_led()
    be.led_mode("fail")
    assert (led / "trigger").read_text() == "timer"
    assert (led / "delay_on").read_text() == "50" and (led / "delay_off").read_text() == "50"
    be.led_mode("normal")
    assert (led / "trigger").read_text() == "heartbeat"


def test_identify_steady_then_previous_mode(tmp_path, monkeypatch):
    led = fake_led(tmp_path)
    be = backend(tmp_path, monkeypatch)
    be.led_mode("fail")
    seen = {}
    monkeypatch.setattr("napi_factory_agent.backends.time.sleep",
                        lambda s: seen.update(trigger=(led / "trigger").read_text(),
                                              brightness=(led / "brightness").read_text(), s=s))
    be.identify(10)
    assert seen == {"trigger": "none", "brightness": "255", "s": 10}
    assert (led / "trigger").read_text() == "timer"   # снова мигание брака


def test_restart_after_fail_restores_heartbeat(tmp_path, monkeypatch):
    # Агент перезапустился, пока светодиод мигал: «нормальный» режим — heartbeat, а не timer
    led = fake_led(tmp_path, trigger="timer")
    be = backend(tmp_path, monkeypatch)
    be.led_mode("normal")
    assert (led / "trigger").read_text() == "heartbeat"


def test_runner_sets_led_by_result(agent_factory):
    a = agent_factory(cpuid="5a4e0000000000aa", fail_stage="ram")
    job = asyncio.run(run_job(a, "test_only", params=job_params("test_only")))
    assert job["result"] == "fail"
    leds = [x for x in a.backend.log if x.startswith("led ")]
    assert leds == ["led normal", "led fail"]
    b = agent_factory(cpuid="5a4e0000000000bb")
    job = asyncio.run(run_job(b, "test_only"))
    assert job["result"] == "pass"
    assert [x for x in b.backend.log if x.startswith("led ")] == ["led normal", "led normal"]
