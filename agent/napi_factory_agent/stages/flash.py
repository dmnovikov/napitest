"""Этап FLASH (раздел 6.3): потоковая запись образа на SD-NAND, два хеша, provision.

Хеш распакованного потока отличает плохой источник (FLASH_FAIL source) от плохой NAND
(FLASH_FAIL readback).
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..blockio import TargetMismatch
from ..hardware_detect import target_summary
from ..images import ManifestError, load_manifest, selfcheck, zstd_frame_size
from ..util import MiB, align_up, fmt_size
from . import StageCtx, StageResult
from .sdnand import recognize


def provision_offset(image_size: int) -> int:
    return align_up(image_size, MiB)


def run(ctx: StageCtx) -> StageResult:
    p = ctx.p
    be = ctx.backend
    meas: dict[str, Any] = {}

    # 1. Предохранитель профиля
    ok, why = be.destructive_allowed(ctx.params)
    if not ok:
        return StageResult("skipped", f"FLASH: запись запрещена ({why})", {**meas, "blocked": True})

    # 2. Манифест и самопроверка (кэш допустим)
    try:
        man = load_manifest(ctx.images_dir, p.get("manifest_file", ""))
    except ManifestError as e:
        return StageResult("fail", f"FLASH: {e}", meas)
    meas.update(image_version=man["version"], image_size=int(man["image_size"]),
                sha256_compressed=man["compressed_sha256"], manifest_sha256=man["image_sha256"])
    if p.get("image_version") and p["image_version"] != man["version"]:
        return StageResult("fail", f"FLASH: image {man['version']}≠{p['image_version']}", meas)
    sc = selfcheck(ctx.images_dir, p.get("manifest_file", ""), str(Path(be.state_dir) / "selfcheck.json"))
    ctx.log(f"самопроверка образа: {sc['state']} {sc.get('detail', '')}")
    if sc["state"] != "pass":
        return StageResult("fail", "FLASH_FAIL source", meas)
    image_file = str(Path(ctx.images_dir) / man["file"])
    image_size = int(man["image_size"])
    fs = zstd_frame_size(image_file)
    meas["zstd_frame_size"] = fs
    if fs is not None and fs != image_size:
        return StageResult("fail", "FLASH_FAIL source", meas)

    # 3. Повторное распознавание непосредственно перед записью
    inv = recognize(ctx)
    if not inv["ok"]:
        return StageResult("fail", "FLASH: " + target_summary(inv), meas)
    capacity = int(inv["target_size"])
    prov = p.get("provision") or {}
    prov_size = int(prov.get("size_bytes", MiB)) if prov.get("method") == "tail-json" else 0
    need = provision_offset(image_size) + prov_size
    meas.update(target=inv["target"], capacity_bytes=capacity)
    if need > capacity:
        return StageResult("fail", f"FLASH: image {fmt_size(need)} > {fmt_size(capacity)}", meas)

    try:
        tgt = be.open_target(inv)
    except (TargetMismatch, OSError) as e:
        ctx.log(f"open: {e}")
        return StageResult("fail", "FLASH: target mismatch", meas)

    block = int(p.get("block_kib", 4096)) * 1024
    try:
        # 4. Потоковая распаковка, SHA-256 на лету
        ctx.writing = True
        ctx.shared["flash_write_started"] = True
        h = hashlib.sha256()
        written = 0
        unsynced = 0
        sync_every = int(p.get("sync_mib", 16)) * 1024 * 1024   # честный процент записи, см. sdnand
        t0 = time.monotonic()
        with be.decompress(image_file) as src:
            while True:
                ctx.check()
                buf = src.read(block)
                if not buf:
                    break
                if written + len(buf) > image_size:
                    ctx.log("поток длиннее манифеста")
                    return StageResult("fail", "FLASH_FAIL source", meas)
                h.update(buf)
                tgt.write_at(written, buf)
                written += len(buf)
                unsynced += len(buf)
                if sync_every and unsynced >= sync_every:
                    tgt.sync()
                    unsynced = 0
                ctx.progress(80 * written / image_size, f"запись {100 * written // image_size} %")
            src_ok = src.ok()
        stream_sha = h.hexdigest()
        meas.update(sha256_stream=stream_sha, written_bytes=written)
        if not src_ok or written != image_size or stream_sha != man["image_sha256"]:
            ctx.log(f"источник: rc_ok={src_ok} written={written} sha={stream_sha}")
            return StageResult("fail", "FLASH_FAIL source", meas)

        # 5. fsync + сброс кеша
        tgt.sync()
        tgt.flush_cache()
        write_s = time.monotonic() - t0
        meas["write_s"] = round(write_s, 1)
        meas["write_mbps"] = round(image_size / MiB / max(write_s, 0.001), 1)
        ctx.writing = False

        # 6. Readback всей длины образа
        t1 = time.monotonic()
        rh = hashlib.sha256()
        pos = 0
        while pos < image_size:
            ctx.check()
            n = min(block, image_size - pos)
            rh.update(tgt.read_at(pos, n))
            pos += n
            ctx.progress(80 + 15 * pos / image_size, f"проверка {100 * pos // image_size} %")
        rb_sha = rh.hexdigest()
        meas["sha256_readback"] = rb_sha
        meas["verify_s"] = round(time.monotonic() - t1, 1)
        meas["direct_io"] = tgt.direct
        if rb_sha != man["image_sha256"]:
            return StageResult("fail", "FLASH_FAIL readback", meas)

        # 7. provision: серийник, партия, дата, версия образа
        ctx.progress(96, "provision")
        resp = ctx.request({"type": "serial_request", "job_id": ctx.job_id}, "serial", 60)
        if not resp:
            return StageResult("fail", "FLASH: provision no serial", meas)
        if resp.get("error"):
            return StageResult("fail", f"FLASH: provision {resp['error']}", meas)
        record = {"serial": resp["serial"], "batch": resp.get("batch"),
                  "date": resp.get("date") or datetime.now(timezone.utc).date().isoformat(),
                  "image_version": man["version"], "cpuid": ctx.cpuid}
        meas["provision"] = record
        if prov.get("method") == "tail-json":
            # VALIDATE ON NAPI-C: согласовать место provision-записи с финальным образом
            off = provision_offset(image_size)
            data = json.dumps(record).encode().ljust(prov_size, b"\0")
            ctx.writing = True
            tgt.write_at(off, data)
            tgt.sync()
            tgt.flush_cache()
            ctx.writing = False
            if tgt.read_at(off, prov_size) != data:
                return StageResult("fail", "FLASH: provision readback", meas)
            meas["provision_offset"] = off
        else:
            return StageResult("fail", f"FLASH: provision method {prov.get('method')!r} not implemented", meas)
    except OSError as e:
        ctx.log(f"I/O error: {e}")
        return StageResult("fail", "FLASH: I/O error", meas)
    finally:
        tgt.close()
    return StageResult("pass", f"{man['version']} {fmt_size(image_size)}, sha OK, {record['serial']}", meas)
