"""Одно WebSocket-соединение агента с сервером (раздел 3.1 ТЗ).

hello → досылка очереди → heartbeat каждые 3 с + новые события + приём команд.
Реконнект с экспоненциальной задержкой. Адрес сервера: из конфига; если не задан или
недоступен: широковещательный UDP-запрос (сервер отвечает адресом), затем mDNS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import socket
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

import websockets

log = logging.getLogger("napi.ws")


DISCOVERY_PORT = 18089


def discover_udp(port: int = DISCOVERY_PORT, timeout: float = 1.5, tries: int = 3) -> list[str]:
    """Широковещательный поиск сервера: [ws-url, …] в порядке ответов."""
    urls: list[str] = []
    req = json.dumps({"type": "napi_discover", "v": 1}).encode()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return urls
    with s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(timeout)
        for _ in range(tries):
            try:
                s.sendto(req, ("255.255.255.255", port))
            except OSError:
                time.sleep(timeout)
                continue
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, _ = s.recvfrom(2048)
                except (socket.timeout, OSError):
                    break
                url = parse_discovery(data)
                if url and url not in urls:
                    urls.append(url)
            if urls:
                break
    return urls


def parse_discovery(data: bytes) -> str | None:
    try:
        msg = json.loads(data)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "napi_server":
        return None
    ws = msg.get("ws")
    return ws if isinstance(ws, str) and ws.startswith(("ws://", "wss://")) else None


def browse_mdns(timeout: float = 6.0) -> list[str]:
    """Поиск сервера через avahi-browse (_napi-factory._tcp). Пусто, если avahi нет."""
    if not shutil.which("avahi-browse"):
        return []
    try:
        out = subprocess.run(["avahi-browse", "-rptk", "_napi-factory._tcp"], capture_output=True,
                             text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_avahi(out)


def parse_avahi(out: str) -> list[str]:
    urls = []
    for line in out.splitlines():
        f = line.split(";")
        if len(f) < 9 or f[0] != "=" or f[2] != "IPv4":
            continue
        addr, port = f[7], f[8]
        path, scheme = "/ws/agent", "ws"
        for kv in (f[9] if len(f) > 9 else "").replace('"', " ").split():
            if kv.startswith("path="):
                path = kv[5:]
            if kv == "tls=1":
                scheme = "wss"
        url = f"{scheme}://{addr}:{port}{path}"
        if url not in urls:
            urls.append(url)
    return urls


def server_addr(url: str) -> tuple[str, int] | None:
    u = urlparse(url)
    if not u.hostname:
        return None
    return u.hostname, u.port or (443 if u.scheme == "wss" else 80)


class WSLink:
    def __init__(self, agent: Any, url: str, token: str, heartbeat_s: float = 3.0, mdns: bool = True,
                 discovery_port: int = 0):
        self.agent = agent
        self.url = url
        self.discovery_port = discovery_port
        self._found: str | None = None
        self.token = token
        self.heartbeat_s = heartbeat_s
        self.mdns = mdns
        self.ws: Any = None
        self.current_url: str | None = None
        self.connected = asyncio.Event()
        self._events = asyncio.Event()
        self._stop = False
        self._offline_until = 0.0
        self._fails = 0
        self._last_sent_seq = 0
        self.connects = 0

    # --- вызывается из агента -------------------------------------------
    def notify_events(self) -> None:
        loop = self.agent.loop
        if loop:
            loop.call_soon_threadsafe(self._events.set)

    def send_soon(self, msg: dict) -> None:
        ws = self.ws
        if ws is None:
            return
        asyncio.ensure_future(self._send(ws, msg))

    async def _send(self, ws: Any, msg: dict) -> None:
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:  # noqa: BLE001 — обрыв обработает основной цикл
            pass

    def http_base(self) -> str | None:
        """http(s)://хост:порт сервера, к которому подключён агент (для скачивания образов)."""
        url = self.current_url
        if not url:
            return None
        u = urlparse(url)
        return f"{'https' if u.scheme == 'wss' else 'http'}://{u.netloc}"

    def pause(self, seconds: float) -> None:
        """Разорвать соединение и не подключаться seconds (симуляция обрыва/перезагрузки)."""
        self._offline_until = time.monotonic() + seconds
        if self.ws is not None:
            asyncio.ensure_future(self.ws.close())

    def stop(self) -> None:
        self._stop = True
        if self.ws is not None:
            asyncio.ensure_future(self.ws.close())

    # --- основной цикл ----------------------------------------------------
    async def _resolve(self) -> str | None:
        """Адрес сервера: сначала широковещательный запрос (сервер отвечает своим адресом в сети платы,
        у сервера их бывает несколько), затем server_url из agent.yaml, затем mDNS."""
        if self._found and self._fails < 3:
            return self._found
        self._found = None
        if self.discovery_port:
            urls = await asyncio.to_thread(discover_udp, self.discovery_port)
            if urls:
                log.info("сервер найден широковещательным запросом: %s", urls[0])
                self._found, self._fails = urls[0], 0
                return self._found
        if self.url:
            return self.url
        if self.mdns:
            urls = await asyncio.to_thread(browse_mdns)
            if urls:
                log.info("mDNS: %s", urls[0])
                self._found, self._fails = urls[0], 0
                return self._found
        log.info("сервер не найден (UDP %s, mDNS), повтор", self.discovery_port or "выкл")
        return None

    async def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop:
            wait = self._offline_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            url = await self._resolve()
            if not url:
                await asyncio.sleep(5)
                continue
            try:
                async with websockets.connect(
                    url, extra_headers={"Authorization": f"Bearer {self.token}"}, open_timeout=10,
                    ping_interval=20, ping_timeout=20, max_size=4 * 1024 * 1024, close_timeout=2,
                ) as ws:
                    self.ws = ws
                    self.connects += 1
                    self._fails = 0
                    backoff = 1.0
                    self.agent.server_addr = server_addr(url)
                    self.current_url = url
                    await self._session(ws)
            except websockets.exceptions.InvalidStatusCode as e:
                log.warning("сервер отклонил подключение: %s", e)
                self._fails += 1
            except websockets.exceptions.ConnectionClosed as e:
                if e.rcvd and e.rcvd.code == 4401:
                    log.error("неверный токен агента")
                    backoff = 30.0
                log.info("соединение закрыто: %s", e)
            except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
                log.info("нет связи с %s: %s", url, e)
                self._fails += 1
            finally:
                self.ws = None
                self.connected.clear()
            if self._stop:
                break
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, 30.0)

    async def _session(self, ws: Any) -> None:
        await ws.send(json.dumps(self.agent.hello(), ensure_ascii=False))
        self._last_sent_seq = 0
        # Сначала накопленная очередь в порядке возникновения, затем снимок (heartbeat)
        for ev in self.agent.queue.pending():
            await ws.send(json.dumps(ev, ensure_ascii=False))
            self._last_sent_seq = ev["seq"]
        self.connected.set()
        # Невыполненные запросы этапов (net_request, serial_request) повторяются после реконнекта
        for msg in self.agent.outstanding_requests():
            await ws.send(json.dumps(msg))
        tasks = [asyncio.create_task(self._recv(ws)), asyncio.create_task(self._heartbeat(ws)),
                 asyncio.create_task(self._pump(ws))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    raise t.exception()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _recv(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            await self.agent.handle(msg)

    async def _pump(self, ws: Any) -> None:
        """Досылка очереди в порядке seq; после реконнекта — с начала (сервер дедуплицирует)."""
        while True:
            self._events.clear()
            for ev in self.agent.queue.pending():
                if ev["seq"] > self._last_sent_seq:
                    await ws.send(json.dumps(ev, ensure_ascii=False))
                    self._last_sent_seq = ev["seq"]
            await self._events.wait()

    async def _heartbeat(self, ws: Any) -> None:
        while True:
            await ws.send(json.dumps(self.agent.snapshot(), ensure_ascii=False))
            await asyncio.sleep(self.heartbeat_s)
            drop = self.agent.backend.sim_drop()
            if drop:
                log.info("симуляция обрыва связи на %.0f с", drop)
                self._offline_until = time.monotonic() + drop
                await ws.close()
                return
