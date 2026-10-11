"""Проверка BOOT снаружи: в финальный образ ничего не добавляется.

MAC NAPI-C выводится из cpuid и одинаков в тестовой и финальной системе. Пока задание
ждёт BOOT, сервер опрашивает подсеть платы (UDP-пакет на каждый адрес заполняет ARP),
ищет в /proc/net/arp IP с MAC платы и проверяет, что там отвечает SSH (Armbian).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

log = logging.getLogger("napi.bootnet")


def parse_arp(text: str) -> dict[str, list[str]]:
    """/proc/net/arp → {mac: [ip, …]} только для разрешённых записей (flags & 0x2)."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 4:
            continue
        try:
            complete = int(f[2], 16) & 0x2
        except ValueError:
            continue
        mac = f[3].lower()
        if complete and mac != "00:00:00:00:00:00":
            out.setdefault(mac, []).append(f[0])
    return out


def subnet_hosts(ip: str | None, prefix: int = 24) -> list[str]:
    if not ip:
        return []
    try:
        net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    except ValueError:
        return []
    if net.version != 4 or net.num_addresses > 1024:
        return []
    return [str(h) for h in net.hosts()]


class BootNet:
    """Сетевые операции (подменяются в тестах)."""

    def __init__(self, arp_path: str = "/proc/net/arp", port: int = 22):
        self.arp_path = arp_path
        self.port = port

    async def sweep(self, hosts: list[str]) -> None:
        if not hosts:
            return
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            for h in hosts:
                try:
                    s.sendto(b"\0", (h, 9))   # discard: ответ не нужен, нужен ARP-запрос ядра
                except OSError:
                    pass
        await asyncio.sleep(0.8)

    def arp(self) -> dict[str, list[str]]:
        try:
            with open(self.arp_path) as f:
                return parse_arp(f.read())
        except OSError:
            return {}

    async def ssh_banner(self, ip: str, timeout: float = 2.0) -> str | None:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, self.port), timeout)
        except (OSError, asyncio.TimeoutError):
            return None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout)
            banner = line.decode("ascii", "replace").strip()
            return banner if banner.startswith("SSH-") else None
        except (OSError, asyncio.TimeoutError):
            return None
        finally:
            writer.close()
