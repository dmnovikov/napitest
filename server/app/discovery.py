"""Обнаружение сервера широковещательным UDP: в образах нет IP сервера.

Агент (и отчёт первой загрузки) шлёт на 255.255.255.255:<port> запрос
{"type": "napi_discover", "v": 1}; сервер отвечает адресом, по которому он виден
с подсети запрашивающего. Токен в обмене не участвует.

Плата в сети, подключённой к серверу напрямую, получает адрес сервера в этой сети, даже если
задан NAPI_PUBLIC_URL: у сервера бывает несколько сетей (на проде 192.168.16.x и 192.168.55.x),
и через чужой адрес плата пошла бы через роутер (NAT, BOOTCHECK её не увидит). NAPI_PUBLIC_URL —
для запросов из сетей, которые сервер напрямую не видит (сервер за NAT или прокси).
"""
from __future__ import annotations

import asyncio
import json
import logging
import ipaddress
import socket

from .scheduler.bootnet import parse_routes
from .version import __version__

log = logging.getLogger("napi.discovery")


def local_ip_for(peer: str) -> str | None:
    """IP этого хоста на интерфейсе, через который виден peer."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((peer, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def direct_peer(peer: str, route_path: str = "/proc/net/route") -> bool:
    """peer в сети, подключённой к серверу напрямую (без шлюза)."""
    try:
        addr = ipaddress.IPv4Address(peer)
        with open(route_path) as f:
            return any(addr in n for n in parse_routes(f.read()))
    except (ValueError, OSError):
        return False


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, port: int, public_url: str, route_path: str = "/proc/net/route"):
        self.port = port
        self.public_url = public_url.rstrip("/")
        self.route_path = route_path
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def reply_for(self, data: bytes, peer: str) -> dict | None:
        try:
            msg = json.loads(data[:1024])
        except ValueError:
            return None
        if not isinstance(msg, dict) or msg.get("type") != "napi_discover":
            return None
        ip = local_ip_for(peer)
        if ip and (not self.public_url or direct_peer(peer, self.route_path)):
            http = f"http://{ip}:{self.port}"
        elif self.public_url:
            http = self.public_url
        else:
            return None
        ws = "ws" + http[4:] if http.startswith("http") else http
        return {"type": "napi_server", "v": 1, "http": http, "ws": ws + "/ws/agent", "version": __version__}

    def datagram_received(self, data, addr):
        reply = self.reply_for(data, addr[0])
        if reply and self.transport:
            self.transport.sendto(json.dumps(reply).encode(), addr)


class DiscoveryResponder:
    def __init__(self, listen_port: int, http_port: int, public_url: str = ""):
        self.listen_port = listen_port
        self.protocol = _Protocol(http_port, public_url)
        self.transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", self.listen_port))
        except OSError as e:
            sock.close()
            log.error("обнаружение: порт UDP %s занят (%s), платы без адреса сервера не найдут его",
                      self.listen_port, e)
            return
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self.protocol, sock=sock)
        log.info("обнаружение: UDP %s", self.listen_port)

    async def stop(self) -> None:
        if self.transport:
            self.transport.close()
