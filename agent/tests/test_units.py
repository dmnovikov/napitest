import json

import pytest

from napi_factory_agent.blockio import BlockTarget, TargetMismatch
from napi_factory_agent.identity import read_cpuid, read_mac
from napi_factory_agent.images import selfcheck, zstd_frame_size
from napi_factory_agent.queue import EventQueue
from napi_factory_agent.stages.net import parse_iperf3
from napi_factory_agent.stages.ram import parse_kernel_memtest, parse_meminfo
from napi_factory_agent.stages.sdnand import pattern_block, plan_ranges
from napi_factory_agent.testing import make_test_image
from napi_factory_agent.ws_client import parse_avahi

# ---------------------------------------------------------------- очередь


def test_queue_seq_ack_restart(tmp_path):
    q = EventQueue(tmp_path / "q")
    a = q.put("job_started", "j1", {"mode": "test_only"})
    b = q.put("stage_started", "j1", stage="ram")
    assert (a["seq"], b["seq"]) == (1, 2)
    stream = q.stream
    q2 = EventQueue(tmp_path / "q")            # перезапуск агента
    assert q2.stream == stream
    assert [e["seq"] for e in q2.pending()] == [1, 2]
    assert q2.put("log", "j1")["seq"] == 3
    assert q2.ack(2) == 2
    assert [e["seq"] for e in q2.pending()] == [3]
    assert q2.ack(99, stream="other") == 0
    q2.ack(3)
    q3 = EventQueue(tmp_path / "q")
    assert q3.put("log", "j1")["seq"] == 4    # seq не повторяется после пустой очереди


def test_queue_limit_drops_logs_first(tmp_path):
    q = EventQueue(tmp_path / "q", max_bytes=3000)
    q.put("job_started", "j1", {"mode": "x"})
    for i in range(30):
        q.put("log", "j1", {"lines": ["x" * 100]})
    q.put("stage_finished", "j1", {"state": "pass"}, stage="ram")
    q.put("job_finished", "j1", {"result": "pass"})
    kinds = [e["kind"] for e in q.pending()]
    assert kinds[0] == "job_started" and "stage_finished" in kinds and kinds[-1] == "job_finished"
    assert kinds.count("log") < 30
    assert sum(p.stat().st_size for p in (tmp_path / "q").glob("0*.json")) <= 3000


# ---------------------------------------------------------------- RAM

DMESG_OK = """[    0.000000] early_memtest: # of tests: 4
[    0.000000]   0x0000000000200000 - 0x0000000000280000 pattern 4c494e5558667265
[    0.000000]   0x0000000000a00000 - 0x000000001f000000 pattern 4c494e5558667265
"""
DMESG_BAD = DMESG_OK + """[    0.000000]   0x0000000003a10000 bad mem addr 0x0000000003a10000 - 0x0000000003a10008 reserved
[    0.000000]   0x0000000003a20000 bad mem addr 0x0000000003a20000 - 0x0000000003a20008 reserved
"""


def test_parse_memtest():
    assert parse_kernel_memtest(DMESG_OK) == {"ran": True, "tests": 4, "ranges": 2, "errors": 0}
    assert parse_kernel_memtest(DMESG_BAD)["errors"] == 2
    assert parse_kernel_memtest("[ 0.1] Linux version 6.6\n")["ran"] is False


def test_parse_meminfo():
    m = parse_meminfo("MemTotal:  499000 kB\nMemAvailable:   300000 kB\nHugePages_Total: 0\n")
    assert m["MemTotal"] == 499000 * 1024 and m["MemAvailable"] == 300000 * 1024


# ---------------------------------------------------------------- NET

IPERF = {"start": {}, "end": {"sum_sent": {"bits_per_second": 95.2e6},
                              "sum_received": {"bits_per_second": 94.1e6}}}


def test_parse_iperf3():
    assert parse_iperf3(IPERF) == pytest.approx(94.1)
    with pytest.raises(ValueError):
        parse_iperf3({"error": "unable to connect to server"})
    with pytest.raises(ValueError):
        parse_iperf3({"end": {}})


def test_parse_avahi():
    out = ("+;end0;IPv4;napi;_napi-factory._tcp;local\n"
           '=;end0;IPv4;napi;_napi-factory._tcp;local;srv.local;192.168.10.1;8080;"path=/ws/agent"\n'
           "=;end0;IPv6;napi;_napi-factory._tcp;local;srv.local;fe80::1;8080;\n")
    assert parse_avahi(out) == ["ws://192.168.10.1:8080/ws/agent"]


# ---------------------------------------------------------------- SD-NAND


def test_plan_ranges_quick_within_capacity():
    cap = 3_909_091_328
    r = plan_ranges("quick", cap, 64 << 20, 3)
    assert r[0][0] == 0 and len(r) == 3
    assert all(o + l <= cap for o, l in r)
    assert r[-1][0] + r[-1][1] > cap - (64 << 20)       # конец покрыт


def test_plan_ranges_beyond_image():
    r = plan_ranges("beyond_image", 1000 << 20, 64 << 20, 3, start_at=600 << 20)
    assert r == [(600 << 20, 400 << 20)]
    assert plan_ranges("beyond_image", 100, 10, 3, start_at=200) == []


def test_pattern_depends_on_offset():
    assert pattern_block("s", 0, 64) != pattern_block("s", 4096, 64)
    assert pattern_block("s", 0, 64) == pattern_block("s", 0, 64)


def test_blocktarget_size_and_bounds(tmp_path):
    f = tmp_path / "dev"
    f.write_bytes(b"\0" * 8192)
    with pytest.raises(TargetMismatch):
        BlockTarget(str(f), None, 4096, require_blockdev=False)
    with pytest.raises(TargetMismatch):
        BlockTarget(str(f), "179:0", 8192, require_blockdev=True)   # обычный файл не блочное устройство
    t = BlockTarget(str(f), None, 8192, require_blockdev=False)
    with pytest.raises(ValueError):
        t.write_at(8000, b"x" * 500)          # за ёмкостью
    t.write_at(4096, b"abc")
    t.sync()
    t.flush_cache()
    assert t.read_at(4096, 3) == b"abc"
    t.close()


# ---------------------------------------------------------------- образ


def test_selfcheck_cache(tmp_path):
    man = make_test_image(tmp_path, size=1 << 20)
    assert zstd_frame_size(str(tmp_path / man["file"])) == 1 << 20
    cache = str(tmp_path / "sc.json")
    mf = "napi-c-armbian-2026.10.0.manifest.json"
    assert selfcheck(str(tmp_path), mf, cache)["detail"] == "verified"
    assert selfcheck(str(tmp_path), mf, cache)["detail"] == "cached"
    m = json.loads((tmp_path / mf).read_text())
    m["compressed_sha256"] = "0" * 64
    (tmp_path / mf).write_text(json.dumps(m))
    assert selfcheck(str(tmp_path), mf, cache)["state"] == "fail"


# ---------------------------------------------------------------- идентификация


def test_cpuid_sources(tmp_path):
    (tmp_path / "proc/device-tree").mkdir(parents=True)
    (tmp_path / "proc/device-tree/serial-number").write_bytes(b"A3F2C0FFEE\0")
    assert read_cpuid(tmp_path, {}, None) == ("a3f2c0ffee", "dt-serial")
    # реальный OTP NAPI-C (2026-10-10): cpuid по смещению 7, 16 байт → serial# 9e098bbcae8b6599
    otp = bytes.fromhex("524b330891fe2255524b593332303033000000000235221703000000480000000001") + bytes(30)
    nv = tmp_path / "sys/bus/nvmem/devices/rockchip-otp0"
    nv.mkdir(parents=True)
    (nv / "nvmem").write_bytes(otp)
    cfg = {"nvmem_path": "/sys/bus/nvmem/devices/rockchip-otp0/nvmem", "otp_offset": 7, "otp_length": 16}
    # device-tree не совпал с OTP (чужой serial# из env U-Boot): берётся OTP
    assert read_cpuid(tmp_path, cfg, None) == ("9e098bbcae8b6599", "otp-cpuid")
    (tmp_path / "proc/device-tree/serial-number").write_bytes(b"9e098bbcae8b6599\0")
    assert read_cpuid(tmp_path, cfg, None) == ("9e098bbcae8b6599", "otp-verified")
    (tmp_path / "proc/device-tree/serial-number").write_bytes(b"0000\0")
    assert read_cpuid(tmp_path, cfg, None) == ("9e098bbcae8b6599", "otp-cpuid")
    assert read_cpuid(tmp_path, {}, "Aabc") == ("Aabc", "assigned")
    assert read_cpuid(tmp_path, {}, None) == (None, "none")


def test_mac_source(tmp_path):
    n = tmp_path / "sys/class/net/end0"
    n.mkdir(parents=True)
    (n / "address").write_text("02:4e:43:aa:bb:cc\n")
    (n / "addr_assign_type").write_text("0\n")
    assert read_mac(tmp_path, "end0", [])["mac_source"] == "cpuid-derived"
    assert read_mac(tmp_path, "end0", ["02:4E:43:AA:BB:CC"])["mac_unique_hint"] is False
    (n / "addr_assign_type").write_text("1\n")
    r = read_mac(tmp_path, "end0", [])
    assert r["mac_source"] == "random" and r["mac_unique_hint"] is False
    assert read_mac(tmp_path, "eth9", [])["mac"] is None


def test_kernel_memtest_napi_c_6_18():
    # Реальный dmesg NAPI-C, Armbian 6.18.55-current-rockchip64, memtest=4 (2026-10-10):
    # ядро печатает только число проходов, без построчных диапазонов.
    from napi_factory_agent.stages.ram import parse_kernel_memtest
    dmesg = ("[    0.000000] early_memtest: # of tests: 4\n"
             "[    0.000000] Kernel command line: root=UUID=3f31b0cc rootwait cma=16M memtest=4\n")
    km = parse_kernel_memtest(dmesg)
    assert km["ran"] and km["tests"] == 4 and km["errors"] == 0


def test_clean_terminal_memtester_progress():
    from napi_factory_agent.backends import clean_terminal
    raw = "  Stuck Address       : testing   0\b\b\b\b\b\b\b\b\b\b\b           \b\b\b\b\b\b\b\b\b\b\bok         \n"
    assert clean_terminal(raw) == "Stuck Address : ok"


# ---------------------------------------------------------------- обнаружение сервера


def test_parse_discovery():
    from napi_factory_agent.ws_client import parse_discovery
    assert parse_discovery(b'{"type":"napi_server","ws":"ws://10.0.0.5:8080/ws/agent"}') == "ws://10.0.0.5:8080/ws/agent"
    assert parse_discovery(b'{"type":"napi_server","ws":"http://x"}') is None
    assert parse_discovery(b'{"type":"other","ws":"ws://x"}') is None
    assert parse_discovery(b"garbage") is None


def test_discover_udp_broadcast():
    import socket
    import threading

    from napi_factory_agent.ws_client import discover_udp
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("0.0.0.0", 0))
    port = srv.getsockname()[1]
    srv.settimeout(5)

    def answer():
        try:
            data, addr = srv.recvfrom(1024)
        except OSError:
            return
        if json.loads(data)["type"] == "napi_discover":
            srv.sendto(b'{"type":"napi_server","ws":"ws://192.0.2.1:8080/ws/agent"}', addr)
    t = threading.Thread(target=answer)
    t.start()
    try:
        assert discover_udp(port, timeout=1, tries=2) == ["ws://192.0.2.1:8080/ws/agent"]
    finally:
        t.join()
        srv.close()
