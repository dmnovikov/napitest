"""Аппаратный слой: RealBackend (NAPI-C) и MockBackend (симулятор и тесты).

Этапы обращаются к оборудованию только через бэкенд. Mock никогда не подставляется
в real-режиме: выбор делается явно в main.py.
"""

from __future__ import annotations

import fcntl
import json
import os
import platform
import random
import socket
import struct
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .blockio import BlockTarget
from .fakesys import napi_default
from .hardware_detect import detect_storage



def clean_terminal(text: str) -> str:
    """Вывод так, как его показал бы терминал: memtester рисует прогресс через backspace."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        buf: list[str] = []
        for ch in raw.split("\r")[-1]:
            if ch == "\b":
                if buf:
                    buf.pop()
            elif ch >= " " or ch == "\t":
                buf.append(ch)
        line = "".join(buf).rstrip()
        if line:
            lines.append(" ".join(line.split()))
    return "\n".join(lines)

class _ProcSource:
    """Поток распакованного образа из `zstd -d -c`."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def read(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = self.proc.stdout.read(n - len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def ok(self) -> bool:
        rest = self.proc.stdout.read()
        rc = self.proc.wait(timeout=60)
        return rc == 0 and not rest


class Backend:
    simulated = False
    root: str = "/"
    state_dir: str = "/var/lib/napi-factory"

    # --- общие реализации поверх root ---------------------------------
    def read_meminfo(self) -> str:
        return Path(self.root, "proc/meminfo").read_text()

    def mountinfo(self) -> str:
        return Path(self.root, "proc/self/mountinfo").read_text()

    def mounts(self) -> str:
        try:
            return Path(self.root, "proc/mounts").read_text()
        except OSError:
            return ""

    def detect(self, rules: dict[str, Any]) -> dict[str, Any]:
        return detect_storage(self.root, self.mountinfo(), rules, self.mounts())

    @contextmanager
    def decompress(self, path: str) -> Iterator[_ProcSource]:
        proc = subprocess.Popen(["zstd", "-d", "-c", "-q", path], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        try:
            yield _ProcSource(proc)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def pattern_limit_bytes(self) -> int | None:
        return None

    def has_led(self) -> bool:
        return False

    def identify(self, seconds: int) -> None:
        pass

    def hostname(self) -> str:
        return socket.gethostname()

    def kernel(self) -> str:
        return platform.release()

    def on_job_start(self, job_id: str, mode: str) -> None:
        pass

    def on_job_finished(self, snapshot: dict[str, Any]) -> None:
        pass

    def sim_drop(self) -> float:
        return 0.0


class RealBackend(Backend):
    """Реальная NAPI-C под тестовой Armbian. Агент работает от root."""

    def __init__(self, state_dir: str, led_name: str | None = None):
        self.root = "/"
        self.state_dir = state_dir
        self.led_name = led_name

    def destructive_allowed(self, params: dict[str, Any]) -> tuple[bool, str]:
        if params.get("destructive_enabled") is True:
            return True, ""
        return False, "profile not validated"

    def read_dmesg(self) -> str:
        # VALIDATE ON NAPI-C: буфер dmesg вмещает вывод memtest (log_buf_len в cmdline при необходимости)
        try:
            return subprocess.run(["dmesg"], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def run_memtester(self, mib: int, passes: int, timeout: float, ctx, tick=None) -> tuple[int, str, int]:
        proc = subprocess.Popen(["memtester", f"{mib}M", str(passes)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        out: list[str] = []
        reader = threading.Thread(target=lambda: out.extend(proc.stdout), daemon=True)
        reader.start()
        start = time.monotonic()
        end = start + timeout
        try:
            while proc.poll() is None:
                ctx.check()
                if tick:
                    tick(time.monotonic() - start)
                if time.monotonic() > end:
                    proc.kill()
                    return -1, "".join(out) + "\ntimeout", 1
                time.sleep(0.5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        reader.join(5)
        text = clean_terminal("".join(out))
        errs = sum(1 for line in text.splitlines() if "FAILURE" in line)
        return proc.returncode, text, errs

    def iface_ip(self, iface: str) -> str | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface[:15].encode()))  # SIOCGIFADDR
            return socket.inet_ntoa(res[20:24])
        except OSError:
            return None
        finally:
            s.close()

    def tcp_reachable(self, host: str, port: int, timeout: float = 5) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def run_iperf(self, host: str, port: int, seconds: int, reverse: bool, ctx,
                  udp_mbps: float | None = None) -> dict[str, Any]:
        cmd = ["iperf3", "-c", host, "-p", str(port), "-t", str(seconds), "-J"]
        if reverse:
            cmd.append("-R")
        if udp_mbps:
            cmd += ["-u", "-b", f"{udp_mbps:g}M"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 30)
            return json.loads(r.stdout or "{}") or {"error": f"rc {r.returncode}"}
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            return {"error": str(e)}

    def open_target(self, inv: dict[str, Any]) -> BlockTarget:
        # Устройство открывается по имени из sysfs, затем сверяется major:minor и размер
        return BlockTarget(f"/dev/{inv['target']}", inv["target_dev"], int(inv["target_size"]),
                           require_blockdev=True)

    def _led(self) -> Path | None:
        # VALIDATE ON NAPI-C: есть ли на плате LED, управляемый из Linux
        base = Path("/sys/class/leds")
        if not base.is_dir():
            return None
        names = sorted(os.listdir(base))
        if self.led_name:
            return base / self.led_name if self.led_name in names else None
        for n in names:
            if "mmc" not in n:
                return base / n
        return None

    def has_led(self) -> bool:
        return self._led() is not None

    def identify(self, seconds: int) -> None:
        led = self._led()
        if not led:
            return
        try:
            old = (led / "trigger").read_text()
            cur = old.split("[")[1].split("]")[0] if "[" in old else "none"
            (led / "trigger").write_text("timer")
            time.sleep(seconds)
            (led / "trigger").write_text(cur)
        except OSError:
            pass


class _MockTarget(BlockTarget):
    def __init__(self, *a, write_bps: float, read_bps: float, corrupt: Callable[[int, bytes], bytes] | None,
                 **kw):
        super().__init__(*a, **kw)
        self.write_bps = write_bps
        self.read_bps = read_bps
        self.corrupt = corrupt

    def write_at(self, offset, data):
        super().write_at(offset, data)
        if self.write_bps:
            time.sleep(len(data) / self.write_bps)

    def read_at(self, offset, n):
        data = super().read_at(offset, n)
        if self.read_bps:
            time.sleep(n / self.read_bps)
        if self.corrupt:
            data = self.corrupt(offset, data)
        return data


class _CorruptSource(_ProcSource):
    def __init__(self, inner: _ProcSource):
        self.inner = inner
        self.done = False

    def read(self, n):
        b = self.inner.read(n)
        if b and not self.done:
            self.done = True
            b = bytes([b[0] ^ 0xFF]) + b[1:]
        return b

    def ok(self):
        return self.inner.ok()


SIM_STAGES = ("ram", "net", "sdnand", "flash")


class MockBackend(Backend):
    """Симулированная NAPI-C: поддельные sysfs/proc, «SD-NAND» как sparse-файл.

    Ёмкость SD-NAND честно 3.7 GB (sparse), но объём паттерн-теста ограничен
    pattern_limit, а скорость I/O задаётся throttling'ом. В hello simulated=true.
    """

    simulated = True

    def __init__(self, base_dir: str, cpuid: str, *, speed: float = 1.0, fail_rate: float = 0.0,
                 drop_rate: float = 0.0, seed: int | None = None, ram_mib: int = 512,
                 pattern_limit: int = 4 * 1024 * 1024, fail_stage: str | None = None):
        self.base = Path(base_dir)
        self.root = str(self.base / "root")
        self.state_dir = str(self.base / "state")
        self.cpuid = cpuid
        self.speed = speed
        self.fail_rate = fail_rate
        self.drop_rate = drop_rate
        self.rnd = random.Random(seed if seed is not None else cpuid)
        self.ram_mib = ram_mib
        self.pattern_limit = pattern_limit
        self.forced_fail = fail_stage
        self.fault: str | None = None
        self.fault_kind: str | None = None
        self.log: list[str] = []
        self.boot_hook: Callable[[dict[str, Any]], None] | None = None
        self._build()

    # --- поддельное оборудование ---------------------------------------
    def _build(self) -> None:
        root = Path(self.root)
        if not (root / "sys/block").exists():
            _, mi = napi_default(root)
            (root / "proc/self").mkdir(parents=True, exist_ok=True)
            (root / "proc/self/mountinfo").write_text(mi)
        (root / "proc/device-tree").mkdir(parents=True, exist_ok=True)
        (root / "proc/device-tree/serial-number").write_bytes(self.cpuid.encode() + b"\0")
        dev = root / "dev/mmcblk0"
        dev.parent.mkdir(parents=True, exist_ok=True)
        size = int((root / "sys/block/mmcblk0/size").read_text()) * 512
        if not dev.exists() or dev.stat().st_size != size:
            with open(dev, "wb") as f:
                f.truncate(size)
        h = int(self.cpuid[-6:], 16) if all(c in "0123456789abcdef" for c in self.cpuid[-6:]) else 0
        mac = "02:%02x:%02x:%02x:%02x:%02x" % (0x4E, 0x43, (h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF)
        self.ip = f"10.99.{(h >> 8) & 0xFF}.{h & 0xFF or 1}"
        net = root / "sys/class/net/end0"
        (net / "statistics").mkdir(parents=True, exist_ok=True)
        for k, v in {"address": mac, "addr_assign_type": "0", "carrier": "1", "speed": "100",
                     "duplex": "full"}.items():
            (net / k).write_text(v + "\n")
        for k in ("rx_errors", "tx_errors"):
            (net / "statistics" / k).write_text("0\n")
        self._write_meminfo(self.ram_mib)

    def _write_meminfo(self, mib: int) -> None:
        kb = mib * 1024 - 20 * 1024
        Path(self.root, "proc/meminfo").write_text(
            f"MemTotal:       {kb} kB\nMemFree:        {kb - 90000} kB\nMemAvailable:   {kb - 60000} kB\n")

    def _k(self, s: float) -> float:
        return s / max(self.speed, 0.01)

    def destructive_allowed(self, params: dict[str, Any]) -> tuple[bool, str]:
        return True, "sim"

    def on_job_start(self, job_id: str, mode: str) -> None:
        self.fault = None
        net = Path(self.root, "sys/class/net/end0")
        (net / "carrier").write_text("1\n")
        self._write_meminfo(self.ram_mib)
        if self.forced_fail:
            self.fault = self.forced_fail
        elif self.rnd.random() < self.fail_rate:
            stages = [s for s in SIM_STAGES if mode != "flash_only" or s == "flash"]
            if mode == "test_only":
                stages = [s for s in stages if s != "flash"]
            self.fault = self.rnd.choice(stages)
        self.fault_kind = None
        if self.fault == "ram":
            self.fault_kind = self.rnd.choice(["memtest", "memtester", "size"])
            if self.fault_kind == "size":
                self._write_meminfo(256)
        elif self.fault == "net":
            self.fault_kind = getattr(self, "forced_kind", None) or self.rnd.choice(["carrier", "slow", "loss"])
            if self.fault_kind == "carrier":
                (net / "carrier").write_text("0\n")
        elif self.fault == "flash":
            self.fault_kind = self.rnd.choice(["source", "readback"])
        if self.fault:
            self.log.append(f"инъекция ошибки: {self.fault}/{self.fault_kind}")

    def on_job_finished(self, snapshot: dict[str, Any]) -> None:
        if self.boot_hook:
            self.boot_hook(snapshot)

    def read_dmesg(self) -> str:
        lines = ["[    0.000000] early_memtest: # of tests: 4"]
        for i in range(4):
            lines.append(f"[    0.000000]   0x0000000000200000 - 0x000000001f000000 pattern {i:016x}")
        if self.fault == "ram" and self.fault_kind == "memtest":
            lines.append("[    0.000000]   0x0000000003a10000 bad mem addr 0x0000000003a10000 - "
                         "0x0000000003a10008 reserved")
            lines.append("[    0.000000]   0x0000000007b20000 bad mem addr 0x0000000007b20000 - "
                         "0x0000000007b20008 reserved")
        return "\n".join(lines) + "\n"

    def run_memtester(self, mib, passes, timeout, ctx, tick=None):
        total = min(self._k(8.0 * passes), timeout)
        start = time.monotonic()
        while (left := total - (time.monotonic() - start)) > 0:
            ctx.sleep(min(left, 0.5))
            if tick:
                tick((time.monotonic() - start) / total * mib * 2.45 * passes)
        if self.fault == "ram" and self.fault_kind == "memtester":
            return 1, "  Bit Flip            : FAILURE: possible bad address line at offset 0x0123\n", 1
        return 0, f"memtester (sim) {mib}M x{passes}: ok\n", 0

    def iface_ip(self, iface):
        return self.ip

    def tcp_reachable(self, host, port, timeout=5):
        return True

    def run_iperf(self, host, port, seconds, reverse, ctx, udp_mbps=None):
        ctx.sleep(self._k(min(seconds, 5)))
        mbps = 94.0 + self.rnd.random() * 1.5
        if self.fault == "net" and self.fault_kind == "slow":
            mbps = 41.0
        if udp_mbps:
            loss = 0.04
            if reverse and self.fault == "net" and self.fault_kind == "loss":
                loss = 6.5
            if self.fault == "net" and self.fault_kind == "slow":
                return {"end": {"sum_received": {"bits_per_second": 41e6, "lost_percent": 0.0}}}
            return {"end": {"sum_received": {"bits_per_second": min(mbps, udp_mbps) * 1e6 * (1 - loss / 100),
                                             "lost_percent": loss}}}
        # Как на реальной NAPI-C (2026-10-10): TCP-приём проседает при чистом канале
        if reverse and self.fault == "net" and self.fault_kind == "tcp_rx":
            mbps = 36.0
        return {"end": {"sum_received": {"bits_per_second": mbps * 1e6}}}

    def open_target(self, inv):
        corrupt = None
        if self.fault == "sdnand":
            def corrupt(off, data):
                return bytes([data[0] ^ 0x5A]) + data[1:] if off > 0 else data
        elif self.fault == "flash" and self.fault_kind == "readback":
            def corrupt(off, data):
                return bytes([data[0] ^ 0x01]) + data[1:] if off == 0 else data
        return _MockTarget(str(Path(self.root, "dev", inv["target"])), inv["target_dev"],
                           int(inv["target_size"]), require_blockdev=False,
                           write_bps=512 * 1024 * self.speed, read_bps=2 * 1024 * 1024 * self.speed,
                           corrupt=corrupt)

    @contextmanager
    def decompress(self, path):
        with super().decompress(path) as src:
            if self.fault == "flash" and self.fault_kind == "source":
                yield _CorruptSource(src)
            else:
                yield src

    def pattern_limit_bytes(self):
        return self.pattern_limit

    def has_led(self):
        return True

    def identify(self, seconds):
        self.log.append(f"identify {seconds}s")

    def hostname(self):
        return f"sim-{self.cpuid[-4:]}"

    def kernel(self):
        return "6.6.0-sim"

    def sim_drop(self) -> float:
        if self.drop_rate and self.rnd.random() < self.drop_rate:
            return self.rnd.uniform(4, 15)
        return 0.0
