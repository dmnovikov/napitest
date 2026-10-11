import asyncio
import json

import pytest

from napi_factory_agent.runner import Agent, compute_result, initial_stages
from napi_factory_agent.testing import job_params
from napi_factory_agent.util import MiB

from .conftest import make_agent


def run(coro):
    return asyncio.run(coro)


async def run_job(agent, mode, job_id="j1", params=None, cancel_after=None):
    await agent.submit({"type": "job", "job_id": job_id, "mode": mode, "params": params or job_params(mode)})
    if cancel_after is not None:
        await asyncio.sleep(cancel_after)
        agent.cancel(job_id)
    await agent.wait_job()
    return agent.job


def states(job):
    return {k: v["state"] for k, v in job["stages"].items()}


def test_test_only(agent_factory):
    a = agent_factory()
    job = run(run_job(a, "test_only"))
    assert job["result"] == "pass", job
    assert states(job) == {"ram": "pass", "net": "pass", "sdnand": "pass", "flash": "skipped", "boot": "skipped"}
    assert "kernel memtest 0 err" in job["stages"]["ram"]["detail"]
    assert job["stages"]["net"]["detail"].startswith("100Full")
    # образ не пишется: в начале «SD-NAND» паттерн, а не образ — и provision нет
    assert not any(m.get("type") == "serial_request" for m in a.link.sent)
    kinds = [e["kind"] for e in a.queue.pending()]
    assert kinds[0] == "job_started" and kinds[-1] == "job_finished"
    assert kinds.count("stage_finished") == 3


def test_test_flash_full(agent_factory, images):
    a = agent_factory()
    job = run(run_job(a, "test_flash"))
    assert job["result"] == "pass", job
    st = states(job)
    assert st["flash"] == "pass" and st["boot"] == "pending"
    fin = [e for e in a.queue.pending() if e["kind"] == "stage_finished" and e["stage"] == "flash"][0]
    m = fin["data"]["measurements"]
    assert m["sha256_stream"] == m["manifest_sha256"] == m["sha256_readback"]
    assert m["provision"]["serial"] == "NC-2610-0001"
    sd = [e for e in a.queue.pending() if e["kind"] == "stage_finished" and e["stage"] == "sdnand"][0]
    assert sd["data"]["measurements"]["pattern"] == "beyond_image"
    assert sd["data"]["measurements"]["ranges"][0][0] >= 2 * MiB   # за концом образа
    # образ действительно на «SD-NAND»
    man = json.loads((images / "napi-c-armbian-2026.10.0.manifest.json").read_text())
    dev = (a.backend.root + "/dev/mmcblk0")
    with open(dev, "rb") as f:
        import hashlib
        assert hashlib.sha256(f.read(man["image_size"])).hexdigest() == man["image_sha256"]
        f.seek(man["image_size"])
        rec = json.loads(f.read(MiB).rstrip(b"\0"))
    assert rec["serial"] == "NC-2610-0001" and rec["cpuid"] == a.cpuid


def test_test_flash_no_flash_on_fail(agent_factory):
    a = agent_factory(fail_stage="net")
    job = run(run_job(a, "test_flash"))
    assert job["result"] == "fail"
    assert job["stages"]["net"]["state"] == "fail"
    assert job["stages"]["flash"]["state"] == "skipped"
    assert job["stages"]["flash"]["detail"] == "тесты не пройдены"
    assert not any(m.get("type") == "serial_request" for m in a.link.sent)


def test_flash_only(agent_factory):
    a = agent_factory()
    job = run(run_job(a, "flash_only"))
    assert job["result"] == "pass"
    assert states(job) == {"ram": "skipped", "net": "skipped", "sdnand": "skipped", "flash": "pass", "boot": "pending"}


@pytest.mark.parametrize("kind,detail", [("source", "FLASH_FAIL source"), ("readback", "FLASH_FAIL readback")])
def test_flash_source_vs_readback(agent_factory, kind, detail):
    a = agent_factory(fail_stage="flash")
    orig = a.backend.on_job_start

    def start(job_id, mode):
        orig(job_id, mode)
        a.backend.fault_kind = kind
    a.backend.on_job_start = start
    job = run(run_job(a, "flash_only"))
    assert job["result"] == "fail"
    assert job["stages"]["flash"]["detail"] == detail


def test_flash_serial_refused(agent_factory):
    a = agent_factory(serial_error="device has no TEST_PASS")
    job = run(run_job(a, "flash_only"))
    assert job["result"] == "fail"
    assert "provision" in job["stages"]["flash"]["detail"]


def test_destructive_disabled_on_real_guard(agent_factory):
    a = agent_factory()
    # Предохранитель как у RealBackend
    a.backend.destructive_allowed = lambda p: (False, "profile not validated") if not p.get(
        "destructive_enabled") else (True, "")
    job = run(run_job(a, "test_flash", params=job_params("test_flash", destructive_enabled=False)))
    sd = job["stages"]["sdnand"]
    # блокировка предохранителем — не дефект платы: серый «пропущен», итог «не выполнено»
    assert sd["state"] == "skipped" and sd["blocked"] is True
    assert sd["detail"] == "SD: запись запрещена (profile not validated)"
    assert job["stages"]["flash"]["state"] == "skipped"
    assert job["result"] == "incomplete"
    # «Только тесты» без SD-теста не должен стать PASS
    b = agent_factory(cpuid="5a4e00000000a0b2")
    b.backend.destructive_allowed = a.backend.destructive_allowed
    job = run(run_job(b, "test_only", params=job_params("test_only", destructive_enabled=False)))
    assert job["result"] == "incomplete" and job["stages"]["ram"]["state"] == "pass"


def test_same_job_id_not_restarted(agent_factory):
    a = agent_factory()

    async def go():
        await run_job(a, "test_only", "jX")
        n = len(a.queue.pending())
        started = await a.submit({"type": "job", "job_id": "jX", "mode": "test_only", "params": job_params()})
        assert started is False
        assert len(a.queue.pending()) == n
    run(go())


def test_second_job_rejected_while_active(agent_factory):
    a = agent_factory()

    async def go():
        await a.submit({"type": "job", "job_id": "j1", "mode": "test_only", "params": job_params()})
        ok = await a.submit({"type": "job", "job_id": "j2", "mode": "test_only", "params": job_params()})
        assert ok is False
        await a.wait_job()
        assert a.job["job_id"] == "j1"
    run(go())


def test_cancel_before_flash(agent_factory):
    a = agent_factory()
    a.backend.speed = 2  # memtester ~4 с
    job = run(run_job(a, "test_flash", cancel_after=0.3))
    assert job["result"] == "cancelled"
    assert job["stages"]["ram"]["state"] == "skipped"
    assert job["stages"]["flash"]["state"] == "skipped"
    assert not any(m.get("type") == "serial_request" for m in a.link.sent)


def test_cancel_during_flash_interrupted(agent_factory):
    a = agent_factory()
    a.backend.speed = 1  # запись 2 MiB ~4 с

    async def go():
        await a.submit({"type": "job", "job_id": "jf", "mode": "flash_only", "params": job_params("flash_only")})
        for _ in range(200):
            await asyncio.sleep(0.05)
            if a.job["stages"]["flash"].get("pct", 0) > 5:
                break
        a.cancel("jf")
        await a.wait_job()
    run(go())
    assert a.job["result"] == "interrupted"
    assert a.job["stages"]["flash"]["state"] == "interrupted"


def test_restart_marks_interrupted(tmp_path, images):
    a = make_agent(tmp_path)
    a.state["job"] = {"job_id": "jr", "mode": "test_flash", "params": job_params("test_flash"), "cpuid": a.cpuid,
                      "stages": initial_stages("test_flash", job_params("test_flash")), "started_ts": 1.0,
                      "finished": False, "result": None, "finished_ts": None}
    a.state["job"]["stages"]["ram"] = {"state": "pass", "t": 3}
    a.state["job"]["stages"]["net"] = {"state": "running", "pct": 30}
    a.save()
    b = Agent(a.cfg, a.backend, str(a.state_dir))
    assert b.job["finished"] and b.job["result"] == "interrupted"
    assert b.job["stages"]["net"]["state"] == "interrupted"
    assert b.job["stages"]["flash"]["state"] == "skipped"
    assert b.queue.pending()[-1]["kind"] == "job_finished"


def test_card_moved_to_other_board_drops_job(tmp_path, images):
    a = make_agent(tmp_path)
    a.state["job"] = {"job_id": "jr", "mode": "test_only", "params": {}, "cpuid": "other",
                      "stages": initial_stages("test_only", {}), "started_ts": 1.0, "finished": False}
    a.save()
    b = Agent(a.cfg, a.backend, str(a.state_dir))
    assert b.job is None


def test_ram_failures(agent_factory):
    for kind, expect in [("memtest", "RAM: 2 err (kernel memtest)"), ("size", "RAM: size 236M≠512M"),
                         ("memtester", "RAM: 1 err (memtester)")]:
        a = agent_factory(cpuid=f"5a4e0000000{len(kind):05d}", fail_stage="ram")
        orig = a.backend.on_job_start

        def start(job_id, mode, kind=kind, a=a, orig=orig):
            orig(job_id, mode)
            a.backend.fault_kind = kind
            a.backend._write_meminfo(256 if kind == "size" else 512)
        a.backend.on_job_start = start
        job = run(run_job(a, "test_only", job_id=f"j-{kind}"))
        assert job["stages"]["ram"]["detail"] == expect, job["stages"]["ram"]
        assert job["result"] == "fail"


def test_net_no_grant_fails(agent_factory):
    a = agent_factory(grant=False)
    p = job_params("test_only")
    p["net"]["grant_timeout_s"] = 1
    job = run(run_job(a, "test_only", params=p))
    assert job["stages"]["net"]["detail"] == "NET: no iperf3 slot"


def test_compute_result_modes():
    s = initial_stages("test_only", {})
    for k in ("ram", "net", "sdnand"):
        s[k] = {"state": "pass"}
    assert compute_result("test_only", s)[0] == "pass"
    s = initial_stages("test_flash", {})
    for k in ("ram", "net", "sdnand"):
        s[k] = {"state": "pass"}
    assert compute_result("test_flash", s)[0] == "fail"   # flash не выполнена
    s["flash"] = {"state": "pass"}
    assert compute_result("test_flash", s)[0] == "pass"
    s = initial_stages("flash_only", {"stages": {"boot": False}})
    assert s["boot"]["state"] == "skipped"


def test_net_quick_is_udp_only(agent_factory):
    a = agent_factory()
    job = run(run_job(a, "test_only"))
    net = job["stages"]["net"]
    assert net["state"] == "pass" and net["detail"].startswith("100Full, UDP 90/90 Mbit/s, потери 0.04/0.04 %")
    fin = [e for e in a.queue.pending() if e["kind"] == "stage_finished" and e["stage"] == "net"][0]
    m = fin["data"]["measurements"]
    assert m["udp_rx_loss_pct"] == 0.04 and "tx_mbps" not in m and m["errors_delta"] == {}


def test_net_full_tcp_rx_low_is_pass_with_warning(agent_factory):
    # Как на реальной NAPI-C: TCP-приём 36 Мбит/с, UDP-приём в норме
    a = agent_factory(fail_stage="net")
    a.backend.forced_kind = "tcp_rx"
    p = job_params("test_only")
    p["net"]["method"] = "tcp"
    job = run(run_job(a, "test_only", params=p))
    net = job["stages"]["net"]
    assert job["result"] == "pass", job
    assert net["state"] == "pass" and net["warn"] is True
    assert net["detail"].startswith("100Full, UDP 90/90") and net["detail"].endswith("/36")
    fin = [e for e in a.queue.pending() if e["kind"] == "stage_finished" and e["stage"] == "net"][0]
    assert fin["data"]["warnings"][0].startswith("NET: TCP rx 36")
    assert fin["data"]["measurements"]["rx_mbps"] == 36.0
    assert "ЗАМЕЧАНИЕ" in fin["data"]["log"]


def test_net_slow_fails_on_udp(agent_factory):
    a = agent_factory(fail_stage="net")
    a.backend.forced_kind = "slow"
    job = run(run_job(a, "test_only"))
    assert job["stages"]["net"]["detail"] == "NET: UDP tx 41 Mbit/s<81"


def test_net_rx_loss_fails_after_retry(agent_factory):
    a = agent_factory(fail_stage="net")
    a.backend.forced_kind = "loss"
    job = run(run_job(a, "test_only"))
    assert job["stages"]["net"]["detail"] == "NET: UDP rx потери 6.5 %>2 %"
    fin = [e for e in a.queue.pending() if e["kind"] == "stage_finished" and e["stage"] == "net"][0]
    assert "повтор" in fin["data"]["log"]


def test_net_retry_success_is_warning(agent_factory):
    a = agent_factory()
    orig = a.backend.run_iperf
    calls = {"n": 0}

    def flaky(host, port, seconds, reverse, ctx, udp_mbps=None):
        r = orig(host, port, seconds, reverse, ctx, udp_mbps=udp_mbps)
        if udp_mbps and reverse:
            calls["n"] += 1
            if calls["n"] == 1:   # единичный выброс, как 11 % на реальной плате
                r["end"]["sum_received"]["lost_percent"] = 11.0
        return r
    a.backend.run_iperf = flaky
    job = run(run_job(a, "test_only"))
    net = job["stages"]["net"]
    assert job["result"] == "pass" and net["warn"] is True
    fin = [e for e in a.queue.pending() if e["kind"] == "stage_finished" and e["stage"] == "net"][0]
    assert fin["data"]["warnings"] == ["NET: UDP rx с повтора: потери 11.00 % → 0.04 %"]


def test_net_error_counters_grow_fails(agent_factory):
    a = agent_factory()
    orig = a.backend.run_iperf

    def bad(host, port, seconds, reverse, ctx, udp_mbps=None):
        import pathlib
        f = pathlib.Path(a.backend.root) / "sys/class/net/end0/statistics/rx_crc_errors"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("7\n")
        return orig(host, port, seconds, reverse, ctx, udp_mbps=udp_mbps)
    a.backend.run_iperf = bad
    job = run(run_job(a, "test_only"))
    assert job["stages"]["net"]["detail"] == "NET: errors rx_crc_errors +7"
