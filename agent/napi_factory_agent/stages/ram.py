"""Этап RAM (раздел 5.1): kernel memtest из dmesg + memtester в userspace."""

from __future__ import annotations

import re
import time

from . import StageCtx, StageResult

_MEMTEST_MARK = re.compile(r"(early_memtest|memtest:|# of tests)", re.I)
_BAD = re.compile(r"(bad mem addr|bad memory)", re.I)
_MEMTEST_RANGE = re.compile(r"^\s*(\[[^\]]*\]\s*)?0x[0-9a-f]+\s*-\s*0x[0-9a-f]+\s+pattern", re.I)


# VALIDATE ON NAPI-C: формат вывода CONFIG_MEMTEST тестового ядра
def parse_kernel_memtest(dmesg: str) -> dict:
    """Разбор вывода CONFIG_MEMTEST.

    Ядро печатает `early_memtest: # of tests: N`, затем по диапазону
    `  0x... - 0x... pattern ...`, а на сбое `  ... bad mem addr 0x... - 0x... reserved`.
    """
    ran = False
    tests = None
    ranges = 0
    errors = 0
    for line in dmesg.splitlines():
        if _MEMTEST_MARK.search(line):
            ran = True
            m = re.search(r"# of tests:\s*(\d+)", line)
            if m:
                tests = int(m.group(1))
        if _MEMTEST_RANGE.search(line):
            ran = True
            ranges += 1
        if _BAD.search(line):
            errors += 1
    return {"ran": ran, "tests": tests, "ranges": ranges, "errors": errors}


def parse_meminfo(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)\s*kB", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def run(ctx: StageCtx) -> StageResult:
    p = ctx.p
    be = ctx.backend
    meas: dict = {}

    # 1. Объём RAM против профиля (не зашивать 512 MiB как факт)
    mi = parse_meminfo(be.read_meminfo())
    total = mi.get("MemTotal", 0)
    meas["mem_total_bytes"] = total
    exp = int(p.get("expected_mib", 512))
    tol = int(p.get("tolerance_mib", 64))
    total_mib = total // (1024 * 1024)
    ctx.log(f"MemTotal {total_mib} MiB, ожидается {exp}±{tol} MiB")
    if abs(total_mib - exp) > tol:
        return StageResult("fail", f"RAM: size {total_mib}M≠{exp}M", meas)

    # 2. Kernel memtest (основной тест, прошёл до старта userspace)
    km = parse_kernel_memtest(be.read_dmesg())
    meas["kernel_memtest"] = km
    ctx.log(f"kernel memtest: ran={km['ran']} tests={km['tests']} ranges={km['ranges']} errors={km['errors']}")
    if km["errors"]:
        return StageResult("fail", f"RAM: {km['errors']} err (kernel memtest)", meas)
    if not km["ran"] and p.get("memtest_required", True):
        return StageResult("fail", "RAM: no kernel memtest", meas)
    ctx.progress(30, "memtester")
    ctx.check()

    # 3. memtester на безопасном объёме под нагрузкой ОС
    avail_mib = mi.get("MemAvailable", 0) // (1024 * 1024)
    size_mib = min(int(p.get("memtester_max_mib", 256)), avail_mib - int(p.get("memtester_reserve_mib", 160)))
    passes = int(p.get("memtester_passes", 1))
    meas["memtester_mib"] = size_mib
    if size_mib < 16:
        return StageResult("fail", f"RAM: memtester no room ({avail_mib}M avail)", meas)
    t0 = time.monotonic()
    # memtester не сообщает прогресс: оценка по времени (NAPI-C: ~2.45 с на МиБ за проход)
    est_s = max(size_mib * passes * float(p.get("memtester_s_per_mib", 2.45)), 1.0)

    def tick(elapsed: float) -> None:
        frac = min(elapsed / est_s, 0.99)
        ctx.progress(30 + 70 * frac, f"memtester {size_mib}M {int(100 * frac)} %")
    rc, out, errs = be.run_memtester(size_mib, passes, max(ctx.remaining(), 1), ctx, tick=tick)
    meas["memtester_s"] = round(time.monotonic() - t0, 1)
    meas["memtester_rc"] = rc
    for line in out.splitlines()[-20:]:
        ctx.log("memtester: " + line)
    if rc != 0 or errs:
        return StageResult("fail", f"RAM: {errs or 1} err (memtester)", meas)
    kpart = f"kernel memtest {km['errors']} err" if km["ran"] else "kernel memtest n/a"
    return StageResult("pass", f"{kpart}, memtester {passes} pass", meas)
