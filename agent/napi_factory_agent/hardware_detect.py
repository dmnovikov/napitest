"""Inventory накопителей и распознавание целевой SD-NAND (раздел 5.3 ТЗ).

Чистые функции над корнем sysfs и текстом mountinfo: тестируются на поддельных данных.
Номер /dev/mmcblkN никогда не используется как признак: только контроллер, тип,
размер, источник загрузки и смонтированные разделы.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .util import fmt_size

# Целые диски mmc; mmcblkXbootN и mmcblkXrpmb это служебные области eMMC, не кандидаты.
_DISK_RE = re.compile(r"^mmcblk\d+$")
_CTRL_RE = re.compile(r"^[0-9a-f]+\.(mmc|sdhci|dwmmc|mmc_host)$")


@dataclass
class BlockInfo:
    name: str
    dev: str                      # major:minor из sysfs
    type: str | None
    size_bytes: int
    controller: str | None
    by_path: str | None
    modalias: str | None
    removable: bool
    ro: bool
    is_boot: bool = False
    mounted: list[str] = field(default_factory=list)
    partitions: dict[str, str] = field(default_factory=dict)  # name -> major:minor

    def view(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("partitions", None)
        return d


def _rd(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def parse_mountinfo(text: str) -> list[dict[str, str]]:
    """Строки /proc/self/mountinfo: mount_id parent maj:min root mountpoint opts ... - fstype source sopts."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10 or "-" not in parts:
            continue
        sep = parts.index("-")
        out.append({
            "majmin": parts[2],
            "mountpoint": parts[4].replace("\\040", " "),
            "fstype": parts[sep + 1] if len(parts) > sep + 1 else "",
            "source": parts[sep + 2] if len(parts) > sep + 2 else "",
        })
    return out


# CHECKED NAPI-C (mmcblk0 на ff490000.mmc, type SD): путь sysfs SD-NAND (.../<ctrl>/mmc_host/mmcN/...) и тип карты (SD/MMC)
def _controller_from_path(real: str) -> str | None:
    # .../platform/ff490000.mmc/mmc_host/mmc0/mmc0:0001
    parts = real.split(os.sep)
    if "mmc_host" in parts:
        i = parts.index("mmc_host")
        if i > 0:
            return parts[i - 1]
    for p in reversed(parts):
        if _CTRL_RE.match(p):
            return p
    return None


def scan_blocks(sys_root: str | os.PathLike) -> list[BlockInfo]:
    """Все целые mmc-диски с атрибутами из sysfs."""
    sys_root = Path(sys_root)
    bdir = sys_root / "sys/block"
    res: list[BlockInfo] = []
    if not bdir.is_dir():
        return res
    for name in sorted(os.listdir(bdir)):
        if not _DISK_RE.match(name):
            continue
        d = bdir / name
        dev = _rd(d / "dev") or ""
        size = int(_rd(d / "size") or 0) * 512
        dev_link = d / "device"
        controller = None
        dtype = modalias = None
        if dev_link.exists():
            controller = _controller_from_path(os.path.realpath(dev_link))
            dtype = _rd(dev_link / "type")
            modalias = _rd(dev_link / "modalias")
        parts = {}
        for sub in sorted(os.listdir(d)):
            if sub.startswith(name) and (d / sub / "partition").exists():
                parts[sub] = _rd(d / sub / "dev") or ""
        res.append(BlockInfo(
            name=name, dev=dev, type=dtype, size_bytes=size, controller=controller,
            by_path=f"platform-{controller}" if controller else None, modalias=modalias,
            removable=_rd(d / "removable") == "1", ro=_rd(d / "ro") == "1", partitions=parts,
        ))
    return res


def _majmin_to_disk(sys_root: Path, blocks: list[BlockInfo], majmin: str) -> str | None:
    for b in blocks:
        if b.dev == majmin or majmin in b.partitions.values():
            return b.name
    # Не mmc (например, overlay поверх mmc) — пробуем через /sys/dev/block
    link = sys_root / "sys/dev/block" / majmin
    if link.exists():
        real = os.path.realpath(link)
        for b in blocks:
            if f"{os.sep}block{os.sep}{b.name}" in real:
                return b.name
    return None


def detect_storage(sys_root: str | os.PathLike, mountinfo: str, rules: dict[str, Any],
                   mounts: str = "") -> dict[str, Any]:
    """Распознаёт SD-NAND. Возвращает inventory (api.md 1.4) с ok/target/reasons.

    ok=True только если ровно один кандидат удовлетворяет всем правилам и источник
    загрузки однозначно определён.
    """
    sys_root = Path(sys_root)
    blocks = scan_blocks(sys_root)
    reasons: list[str] = []
    mi = parse_mountinfo(mountinfo)

    # Смонтированные разделы по major:minor
    for m in mi:
        disk = _majmin_to_disk(sys_root, blocks, m["majmin"])
        if disk:
            next(b for b in blocks if b.name == disk).mounted.append(m["mountpoint"])
    # /proc/mounts как второй источник (по имени устройства-источника)
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/"):
            continue
        src = parts[0][5:]
        for b in blocks:
            if (src == b.name or src in b.partitions) and parts[1] not in b.mounted:
                b.mounted.append(parts[1])

    # Источник загрузки: диск, на котором смонтирован / (и /boot)
    boot_disks: set[str] = set()
    root_found = False
    for m in mi:
        if m["mountpoint"] in ("/", "/boot", "/boot/efi"):
            disk = _majmin_to_disk(sys_root, blocks, m["majmin"])
            if m["mountpoint"] == "/":
                root_found = disk is not None
            if disk:
                boot_disks.add(disk)
                where = m["source"] or m["majmin"]
                reasons.append(f"{disk}: источник загрузки ({m['mountpoint']} на {where})")
    for b in blocks:
        b.is_boot = b.name in boot_disks
    boot_device = ",".join(sorted(boot_disks)) or None
    if not root_found:
        reasons.append("корневая ФС не на mmc-диске: источник загрузки не определён, запись запрещена")

    exp_ctrl = rules.get("expected_controller")
    need_ctrl = bool(rules.get("require_controller_match", True))
    smin = int(rules.get("size_min_bytes", 0))
    smax = int(rules.get("size_max_bytes", 1 << 62))
    allowed = set(rules.get("allowed_types") or ["MMC", "SD"])

    candidates = []
    for b in blocks:
        why = []
        if b.is_boot:
            why.append("источник загрузки")
        if b.mounted:
            why.append("смонтировано: " + ", ".join(b.mounted))
        if b.ro:
            why.append("read-only")
        if b.type not in allowed:
            why.append(f"тип {b.type} не из {sorted(allowed)}")
        if not (smin <= b.size_bytes <= smax):
            why.append(f"размер {fmt_size(b.size_bytes)} вне допуска {fmt_size(smin)}..{fmt_size(smax)}")
        if need_ctrl:
            if not exp_ctrl:
                why.append("ожидаемый контроллер не задан в профиле")
            elif b.controller != exp_ctrl:
                why.append(f"контроллер {b.controller} ≠ {exp_ctrl}")
        if why:
            if not b.is_boot or len(why) > 1:
                reasons.append(f"{b.name}: не цель ({'; '.join(why)})")
        else:
            candidates.append(b)
            reasons.append(f"{b.name}: контроллер {b.controller}, тип {b.type}, "
                           f"размер {fmt_size(b.size_bytes)} в допуске, не загрузочное, не смонтировано")

    target = None
    ok = False
    if not root_found:
        pass
    elif len(candidates) == 1:
        target = candidates[0]
        ok = True
    elif not candidates:
        reasons.append("SD-NAND не найдена")
    else:
        reasons.append("неоднозначно: несколько кандидатов " + ", ".join(c.name for c in candidates))

    return {
        "devices": [b.view() for b in blocks],
        "target": target.name if target else None,
        "target_dev": target.dev if target else None,
        "target_size": target.size_bytes if target else None,
        "boot_device": boot_device,
        "reasons": reasons,
        "ok": ok,
    }


def target_summary(inv: dict[str, Any]) -> str:
    """Короткая причина отказа распознавания для detail."""
    if inv.get("ok"):
        return f"SD-NAND {inv['target']} {fmt_size(inv['target_size'])}"
    for r in reversed(inv.get("reasons", [])):
        if "неоднозначно" in r or "не найдена" in r or "не определён" in r:
            return r
    return "SD-NAND не распознана"
