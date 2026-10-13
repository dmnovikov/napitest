"""Идентификация платы (раздел 3.2 ТЗ): cpuid из OTP, MAC и его источник."""

from __future__ import annotations

import os
import socket
import struct
import subprocess
from pathlib import Path
import logging
from typing import Any

log = logging.getLogger("napi.identity")


def _clean_serial(raw: bytes) -> str | None:
    s = raw.replace(b"\x00", b"").decode("ascii", "replace").strip().lower()
    if not s or set(s) <= {"0"} or set(s) <= {"f"}:
        return None
    return s


def rockchip_serial(cpuid: bytes) -> str:
    """serial# так, как его выводит U-Boot Rockchip (rockchip_cpuid_set) из cpuid в OTP.

    Проверено на NAPI-C 2026-10-10: OTP rockchip-otp0, смещение 7, 16 байт →
    /proc/device-tree/serial-number 9e098bbcae8b6599.
    """
    import zlib

    def crc32_no_comp(crc: int, data: bytes) -> int:
        return (~zlib.crc32(data, ~crc & 0xFFFFFFFF)) & 0xFFFFFFFF

    half = len(cpuid) // 2
    low = bytes(cpuid[1 + (i << 1)] for i in range(half))
    high = bytes(cpuid[i << 1] for i in range(half))
    serial = crc32_no_comp(0, low)
    serial |= crc32_no_comp(serial, high) << 32
    return f"{serial:016x}"


def _otp_serial(root: Path, ident_cfg: dict[str, Any]) -> str | None:
    off, ln = ident_cfg.get("otp_offset"), ident_cfg.get("otp_length")
    if off is None or not ln:
        return None
    nv = ident_cfg.get("nvmem_path", "/sys/bus/nvmem/devices/rockchip-otp0/nvmem")
    try:
        with open(root / nv.lstrip("/"), "rb") as f:
            f.seek(int(off))
            b = f.read(int(ln))
    except OSError:
        return None
    if len(b) != int(ln) or not (set(b) - {0, 0xFF}):
        return None
    return rockchip_serial(b)


def read_cpuid(root: str | os.PathLike, ident_cfg: dict[str, Any],
               assigned: str | None) -> tuple[str | None, str]:
    """Возвращает (cpuid, id_source): otp-verified | otp-cpuid | dt-serial | assigned | none.

    ID — serial# U-Boot, вычисленный из заводского cpuid чипа в OTP. Если место cpuid в OTP задано
    в конфиге, агент пересчитывает его сам: совпадение с device-tree → otp-verified; расхождение
    (U-Boot взял serial# из сохранённого env, например с клонированной карты) → берётся OTP.
    """
    root = Path(root)
    dt = None
    try:
        dt = _clean_serial((root / "proc/device-tree/serial-number").read_bytes())
    except OSError:
        pass
    otp = _otp_serial(root, ident_cfg)
    if dt and otp:
        if dt == otp:
            return dt, "otp-verified"
        log.warning("serial-number device-tree %s ≠ cpuid из OTP %s: используется OTP", dt, otp)
        return otp, "otp-cpuid"
    if dt:
        return dt, "dt-serial"
    if otp:
        return otp, "otp-cpuid"
    if assigned:
        return assigned, "assigned"
    return None, "none"


def read_mac(root: str | os.PathLike, iface: str, known_clone_macs: list[str]) -> dict[str, Any]:
    """MAC интерфейса и его источник.

    VALIDATE ON NAPI-C: вендорный U-Boot выводит ethaddr из cpuid; ядро видит такой адрес как
    addr_assign_type=0 с битом locally administered. Случайный MAC (assign_type=1) у клонов
    карт приводит к неуникальности после перезагрузки.
    """
    base = Path(root) / "sys/class/net" / iface
    try:
        mac = (base / "address").read_text().strip().lower()
    except OSError:
        return {"mac": None, "mac_source": "unknown", "mac_unique_hint": False}
    try:
        assign = int((base / "addr_assign_type").read_text().strip())
    except (OSError, ValueError):
        assign = -1
    first = int(mac.split(":")[0], 16) if mac else 0
    if assign == 1:
        src = "random"
    elif assign == 0 and first & 0x02:
        src = "cpuid-derived"
    elif assign == 0:
        src = "permanent"
    else:
        src = "unknown"
    unique = (src in ("cpuid-derived", "permanent") and mac not in {m.lower() for m in known_clone_macs}
              and mac != "00:00:00:00:00:00")
    return {"mac": mac, "mac_source": src, "mac_unique_hint": unique}


def regenerate_machine_id(root: str = "/") -> bool:
    """Регенерация /etc/machine-id на клонированной карте (только реальный бэкенд).

    VALIDATE ON NAPI-C: systemd-machine-id-setup присутствует в тестовой Armbian.
    """
    mid = Path(root) / "etc/machine-id"
    try:
        if mid.exists():
            mid.unlink()
        subprocess.run(["systemd-machine-id-setup"], check=True, capture_output=True, timeout=30)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def route_iface(root: str | os.PathLike, ip: str | None) -> str | None:
    """Интерфейс, через который идёт маршрут до ip (по /proc/net/route); без ip — маршрут по умолчанию."""
    try:
        lines = (Path(root) / "proc/net/route").read_text().splitlines()[1:]
    except OSError:
        return None
    try:
        target = struct.unpack("<I", socket.inet_aton(ip))[0] if ip else 0
    except OSError:
        target = 0
    best: tuple[int, int, str] | None = None
    for line in lines:
        f = line.split()
        if len(f) < 8:
            continue
        try:
            dest, flags, metric, mask = int(f[1], 16), int(f[3], 16), int(f[6]), int(f[7], 16)
        except ValueError:
            continue
        if not flags & 1 or (target & mask) != dest:          # RTF_UP
            continue
        key = (bin(mask).count("1"), -metric, f[0])
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best else None


def iface_info(root: str | os.PathLike, iface: str) -> dict[str, Any]:
    """Шина и драйвер сетевого интерфейса: usb (USB-Ethernet адаптер) или встроенный (platform)."""
    base = Path(root) / "sys/class/net" / iface
    dev = base / "device"
    if dev.exists():
        path = os.path.realpath(dev)
        bus = "usb" if "/usb" in path else "platform"
        try:
            driver = os.path.basename(os.path.realpath(dev / "driver"))
        except OSError:
            driver = None
    else:
        bus, driver = "virtual", None
    try:
        mac = (base / "address").read_text().strip().lower()
    except OSError:
        mac = None
    return {"iface": iface, "bus": bus, "driver": driver, "mac": mac}


def uplink(root: str | os.PathLike, server_ip: str | None) -> dict[str, Any] | None:
    """Через что плата подключена к серверу: тип платы NAPI-C/NAPI-P сервер определяет по шине."""
    iface = route_iface(root, server_ip)
    return iface_info(root, iface) if iface else None
