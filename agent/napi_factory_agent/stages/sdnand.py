"""Этап SD-NAND (раздел 5.3): распознавание + паттерн-тест.

Проверка LBA не равна проверке физических ячеек: тест ловит отказы контроллера, I/O-ошибки,
подмену ёмкости и грубые дефекты, но не износ отдельных ячеек.
"""

from __future__ import annotations

import random
import time
from typing import Any

from ..blockio import TargetMismatch
from ..hardware_detect import target_summary
from ..images import ManifestError, load_manifest
from ..util import MiB, align_up, fmt_size
from . import StageCtx, StageResult


def pattern_block(seed: str, offset: int, n: int) -> bytes:
    """Псевдослучайный шаблон, зависящий от seed и смещения: сдвинутые/перепутанные блоки не совпадут."""
    return random.Random(f"{seed}:{offset}").randbytes(n)


def plan_ranges(mode: str, capacity: int, range_bytes: int, n_ranges: int,
                start_at: int = 0) -> list[tuple[int, int]]:
    """Список (offset, length) для паттерн-теста, всё в пределах [start_at, capacity)."""
    if start_at >= capacity:
        return []
    span = capacity - start_at
    if mode in ("full", "beyond_image"):
        return [(start_at, span)]
    # quick: начало, середина(ы), конец
    rb = min(range_bytes, span)
    n = max(2, n_ranges)
    out = []
    for i in range(n):
        off = start_at + (span - rb) * i // (n - 1)
        off -= off % 4096
        out.append((off, min(rb, capacity - off)))
    # убрать перекрытия
    merged: list[tuple[int, int]] = []
    for off, ln in sorted(out):
        if merged and off < merged[-1][0] + merged[-1][1]:
            po, pl = merged[-1]
            merged[-1] = (po, max(po + pl, off + ln) - po)
        else:
            merged.append((off, ln))
    return merged


def _limit(ranges: list[tuple[int, int]], limit: int | None) -> list[tuple[int, int]]:
    """Ограничение объёма (только mock-бэкенд симулятора)."""
    if not limit:
        return ranges
    out, left = [], limit
    per = max(4096, limit // max(len(ranges), 1))
    for off, ln in ranges:
        take = min(ln, per, left)
        take -= take % 4096
        if take > 0:
            out.append((off, take))
            left -= take
    return out


def recognize(ctx: StageCtx) -> dict[str, Any]:
    rules = (ctx.params.get("sdnand") or {}).get("detect") or {}
    inv = ctx.backend.detect(rules)
    ctx.shared["inventory"] = inv
    for r in inv["reasons"]:
        ctx.log("detect: " + r)
    return inv


def run(ctx: StageCtx) -> StageResult:
    p = ctx.p
    be = ctx.backend
    meas: dict[str, Any] = {}

    inv = recognize(ctx)
    meas["target"] = inv.get("target")
    meas["boot_device"] = inv.get("boot_device")
    if not inv["ok"]:
        return StageResult("fail", "SD: " + target_summary(inv), meas)
    capacity = int(inv["target_size"])
    meas["capacity_bytes"] = capacity
    rules = p.get("detect") or {}
    smin, smax = int(rules.get("size_min_bytes", 0)), int(rules.get("size_max_bytes", 1 << 62))
    if not smin <= capacity <= smax:
        return StageResult("fail", f"SD: size {fmt_size(capacity)}≠{fmt_size((smin + smax) // 2)}", meas)

    # Выбор области по режиму
    if ctx.mode == "test_flash" and (ctx.params.get("flash") or {}).get("method") != "zero":
        pmode = "beyond_image"
    else:
        pmode = p.get("pattern", "quick")
        if pmode not in ("quick", "full"):
            pmode = "quick"
    meas["pattern"] = pmode
    start_at = 0
    if pmode == "beyond_image":
        fl = ctx.params.get("flash") or {}
        try:
            man = load_manifest(ctx.images_dir, fl.get("manifest_file", ""))
        except ManifestError as e:
            return StageResult("fail", f"SD: {e}", meas)
        start_at = align_up(int(man["image_size"]), MiB)
        if start_at >= capacity:
            return StageResult("fail", f"SD: image {fmt_size(start_at)} > {fmt_size(capacity)}", meas)

    ok, why = be.destructive_allowed(ctx.params)
    if not ok:
        # Не дефект платы: тест не выполнялся. Серый «пропущен», итог задания «не выполнено».
        return StageResult("skipped", f"SD: запись запрещена ({why})", {**meas, "blocked": True})

    ranges = plan_ranges(pmode, capacity, int(p.get("range_mib", 64)) * MiB,
                         int(p.get("quick_ranges", 3)), start_at)
    limit = be.pattern_limit_bytes()
    if limit:
        ranges = _limit(ranges, limit)
        meas["sim_limited_bytes"] = limit
    total = sum(ln for _, ln in ranges)
    meas["ranges"] = [[o, l] for o, l in ranges]
    meas["tested_bytes"] = total
    block = int(p.get("block_kib", 1024)) * 1024
    seed = ctx.job_id
    ctx.log(f"паттерн {pmode}: {len(ranges)} диап., {fmt_size(total)}, с {fmt_size(start_at)}")

    done = 0
    # Периодический fsync: иначе запись уходит в кеш страниц, процент «убегает» вперёд
    # и замирает на финальном sync (SD-NAND пишет ~8 МБ/с)
    sync_every = int(p.get("sync_mib", 16)) * 1024 * 1024
    t0 = time.monotonic()
    try:
        tgt = be.open_target(inv)
    except (TargetMismatch, OSError) as e:
        ctx.log(f"open: {e}")
        return StageResult("fail", "SD: target mismatch", meas)
    try:
        unsynced = 0
        for off, ln in ranges:
            pos = off
            while pos < off + ln:
                ctx.check()
                n = min(block, off + ln - pos)
                tgt.write_at(pos, pattern_block(seed, pos, n))
                pos += n
                done += n
                unsynced += n
                if sync_every and unsynced >= sync_every:
                    tgt.sync()
                    unsynced = 0
                ctx.progress(100 * done / (2 * total), f"запись {100 * done // (2 * total)} %")
            tgt.sync()
        tgt.flush_cache()
        for off, ln in ranges:
            pos = off
            while pos < off + ln:
                ctx.check()
                n = min(block, off + ln - pos)
                if tgt.read_at(pos, n) != pattern_block(seed, pos, n):
                    meas["mismatch_offset"] = pos
                    ctx.log(f"несовпадение на смещении {pos}")
                    return StageResult("fail", f"SD: mismatch @{fmt_size(pos)}", meas)
                pos += n
                done += n
                ctx.progress(100 * done / (2 * total), f"проверка {100 * done // (2 * total)} %")
        meas["direct_io"] = tgt.direct
    except OSError as e:
        ctx.log(f"I/O error: {e}")
        return StageResult("fail", "SD: I/O error", meas)
    finally:
        tgt.close()
    dt = time.monotonic() - t0
    meas["duration_s"] = round(dt, 1)
    sim = " (sim)" if limit else ""
    return StageResult("pass", f"{pmode} {fmt_size(total)} OK{sim}", meas)
