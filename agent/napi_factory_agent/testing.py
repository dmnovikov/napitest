"""Помощники для тестов и симулятора: маленький образ .img.zst с манифестом."""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
from pathlib import Path


def write_manifest(img_zst: str | os.PathLike, version: str, profile: str = "napi-c-rk3308") -> Path:
    """Манифест для готового .img.zst (так же, как scripts/build-test-sd.sh)."""
    img_zst = Path(img_zst)
    comp = img_zst.read_bytes()
    h = hashlib.sha256()
    size = 0
    proc = subprocess.Popen(["zstd", "-d", "-c", "-q", str(img_zst)], stdout=subprocess.PIPE)
    while chunk := proc.stdout.read(1 << 20):
        h.update(chunk)
        size += len(chunk)
    if proc.wait() != 0:
        raise RuntimeError("zstd -d failed")
    man = {"version": version, "profile": profile, "file": img_zst.name,
           "compressed_size": len(comp), "compressed_sha256": hashlib.sha256(comp).hexdigest(),
           "image_size": size, "image_sha256": h.hexdigest()}
    mpath = img_zst.with_name(img_zst.name[: -len(".img.zst")] + ".manifest.json")
    mpath.write_text(json.dumps(man, indent=1))
    return mpath


def make_test_image(images_dir: str | os.PathLike, version: str = "2026.10.0",
                    size: int = 2 * 1024 * 1024, seed: int = 1) -> dict:
    """Создаёт napi-c-armbian-<ver>.img.zst + manifest. Возвращает манифест."""
    d = Path(images_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = f"napi-c-armbian-{version}"
    img = d / f"{name}.img"
    rnd = random.Random(seed)
    # Половина случайных данных, половина нулей — похоже на реальный образ
    img.write_bytes(rnd.randbytes(size // 2) + b"\0" * (size - size // 2))
    subprocess.run(["zstd", "-q", "-f", "-3", str(img), "-o", str(d / f"{name}.img.zst")], check=True)
    img.unlink()
    mpath = write_manifest(d / f"{name}.img.zst", version)
    return json.loads(mpath.read_text())


def job_params(mode: str = "test_only", version: str = "2026.10.0", **over) -> dict:
    """Параметры задания как их формирует сервер из профиля (api.md 1.3)."""
    p = {
        "profile": "napi-c-rk3308", "destructive_enabled": True,
        "stages": {"ram": True, "net": True, "sdnand": True, "flash": True, "boot": True},
        "ram": {"expected_mib": 512, "tolerance_mib": 64, "memtest_required": True,
                "memtester_reserve_mib": 160, "memtester_max_mib": 256, "memtester_passes": 1, "timeout_s": 900},
        "net": {"iface": "end0", "expect_speed": 100, "expect_duplex": "full", "iperf_seconds": 10,
                "min_mbps": 80, "grant_timeout_s": 600, "timeout_s": 900, "max_rx_errors": 0, "max_tx_errors": 0,
                "udp_seconds": 3, "udp_rate_mbps": 90, "udp_max_loss_tx_pct": 0.5, "udp_max_loss_rx_pct": 2.0,
                "udp_retry": True, "method": "udp"},
        "sdnand": {"pattern": "quick", "quick_ranges": 3, "range_mib": 64, "block_kib": 1024, "timeout_s": 3600,
                   "detect": {"expected_controller": "ff490000.mmc", "require_controller_match": True,
                              "size_min_bytes": 3500000000, "size_max_bytes": 4400000000,
                              "allowed_types": ["MMC", "SD"]}},
        "flash": {"image_version": version, "image_file": f"napi-c-armbian-{version}.img.zst",
                  "manifest_file": f"napi-c-armbian-{version}.manifest.json", "block_kib": 1024,
                  "timeout_s": 3600, "provision": {"method": "tail-json", "size_bytes": 1048576}},
        "boot": {"enabled": True},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(p.get(k), dict):
            p[k].update(v)
        else:
            p[k] = v
    return p
