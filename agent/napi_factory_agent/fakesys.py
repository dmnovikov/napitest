"""Построение поддельного дерева /sys и /proc для тестов и симулятора.

Структура повторяет ядро Linux для mmc:
  /sys/devices/platform/<ctrl>/mmc_host/mmcN/mmcN:0001/block/mmcblkK
  /sys/block/mmcblkK -> ../devices/.../block/mmcblkK
  /sys/block/mmcblkK/device -> ../../../mmcN:0001   (type, modalias)
  /sys/dev/block/MAJ:MIN -> ../../devices/.../block/mmcblkK[/mmcblkKpP]
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FakePart:
    name: str
    majmin: str
    size_bytes: int = 64 * 1024 * 1024


@dataclass
class FakeDisk:
    name: str
    majmin: str
    controller: str
    host: str = "mmc0"
    type: str = "MMC"
    size_bytes: int = 3909091328
    ro: bool = False
    removable: bool = False
    partitions: list[FakePart] = field(default_factory=list)


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(os.path.relpath(target, link.parent), link)


def build_fake_sys(root: str | os.PathLike, disks: list[FakeDisk]) -> Path:
    root = Path(root)
    sysd = root / "sys"
    for d in disks:
        card = sysd / "devices/platform" / d.controller / "mmc_host" / d.host / f"{d.host}:0001"
        _w(card / "type", d.type + "\n")
        _w(card / "modalias", "mmc:block\n")
        bdir = card / "block" / d.name
        _w(bdir / "size", f"{d.size_bytes // 512}\n")
        _w(bdir / "ro", "1\n" if d.ro else "0\n")
        _w(bdir / "removable", "1\n" if d.removable else "0\n")
        _w(bdir / "dev", d.majmin + "\n")
        _link(bdir / "device", card)
        _link(sysd / "block" / d.name, bdir)
        _link(sysd / "dev/block" / d.majmin, bdir)
        for p in d.partitions:
            pdir = bdir / p.name
            _w(pdir / "dev", p.majmin + "\n")
            _w(pdir / "partition", p.name[len(d.name) + 1:] + "\n" if p.name.startswith(d.name + "p") else "1\n")
            _w(pdir / "size", f"{p.size_bytes // 512}\n")
            _link(sysd / "dev/block" / p.majmin, pdir)
    return root


def mountinfo_line(mount_id: int, majmin: str, mountpoint: str, source: str, fstype: str = "ext4") -> str:
    return f"{mount_id} 1 {majmin} / {mountpoint} rw,relatime shared:1 - {fstype} {source} rw\n"


def standard_mountinfo(root_majmin: str, root_source: str, boot_majmin: str | None = None,
                       boot_source: str | None = None) -> str:
    lines = [mountinfo_line(22, root_majmin, "/", root_source)]
    lines.append("23 22 0:5 / /dev rw,nosuid shared:2 - devtmpfs udev rw\n")
    lines.append("24 22 0:21 / /proc rw shared:3 - proc proc rw\n")
    lines.append("25 22 0:22 / /sys rw shared:4 - sysfs sysfs rw\n")
    if boot_majmin:
        lines.append(mountinfo_line(26, boot_majmin, "/boot", boot_source or "/dev/x", "vfat"))
    lines.append("27 22 0:30 / /tmp rw shared:5 - tmpfs tmpfs rw\n")
    return "".join(lines)


def napi_default(root: str | os.PathLike, swap_numbers: bool = False,
                 sdnand_size: int = 3909091328, microsd_size: int = 15931539456) -> tuple[Path, str]:
    """Типовая NAPI-C: microSD на ff480000.mmc (загрузка), SD-NAND на ff490000.mmc.

    swap_numbers меняет номера mmcblk местами, чтобы проверить независимость от нумерации.
    Возвращает (root, mountinfo).
    """
    sd_name, nand_name = ("mmcblk0", "mmcblk1") if swap_numbers else ("mmcblk1", "mmcblk0")
    sd_mm = "179:0" if swap_numbers else "179:32"
    nand_mm = "179:32" if swap_numbers else "179:0"
    sd_major, sd_minor = sd_mm.split(":")
    sd = FakeDisk(sd_name, sd_mm, "ff480000.mmc", host="mmc1", type="SD", size_bytes=microsd_size,
                  removable=False,
                  partitions=[FakePart(f"{sd_name}p1", f"{sd_major}:{int(sd_minor) + 1}")])
    nand = FakeDisk(nand_name, nand_mm, "ff490000.mmc", host="mmc0", type="SD", size_bytes=sdnand_size)
    build_fake_sys(root, [sd, nand])
    mi = standard_mountinfo(f"{sd_major}:{int(sd_minor) + 1}", f"/dev/{sd_name}p1")
    return Path(root), mi
