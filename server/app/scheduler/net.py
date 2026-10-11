"""Ограничение числа одновременных iperf3-сессий.

Каждый слот это порт отдельного процесса `iperf3 -s` (сервер iperf3 обслуживает один тест за раз).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Grant:
    device_id: int
    job_id: str
    port: int
    granted_at: float


class NetScheduler:
    def __init__(self, ports: list[int], lease_s: float = 180.0):
        self.ports = list(ports)
        self.lease_s = lease_s
        self.holders: dict[int, Grant] = {}          # port -> grant
        self.waiting: deque[tuple[int, str]] = deque()

    def holder_port(self, device_id: int) -> int | None:
        for port, grant in self.holders.items():
            if grant.device_id == device_id:
                return port
        return None

    def request(self, device_id: int, job_id: str) -> Grant | None:
        """Слот, если он свободен (или уже выдан этой плате); иначе постановка в очередь."""
        port = self.holder_port(device_id)
        if port is not None:
            grant = self.holders[port]
            grant.job_id = job_id
            return grant
        free = [p for p in self.ports if p not in self.holders]
        if free:
            grant = Grant(device_id, job_id, free[0], time.monotonic())
            self.holders[free[0]] = grant
            return grant
        if not any(d == device_id for d, _ in self.waiting):
            self.waiting.append((device_id, job_id))
        return None

    def release(self, device_id: int) -> list[Grant]:
        """Освобождает слот платы и убирает её из очереди. Возвращает новые выдачи."""
        self.waiting = deque((d, j) for d, j in self.waiting if d != device_id)
        port = self.holder_port(device_id)
        if port is not None:
            del self.holders[port]
        return self._fill()

    def expire(self) -> list[Grant]:
        now = time.monotonic()
        for port, grant in list(self.holders.items()):
            if now - grant.granted_at > self.lease_s:
                del self.holders[port]
        return self._fill()

    def _fill(self) -> list[Grant]:
        granted = []
        while self.waiting:
            free = [p for p in self.ports if p not in self.holders]
            if not free:
                break
            device_id, job_id = self.waiting.popleft()
            grant = Grant(device_id, job_id, free[0], time.monotonic())
            self.holders[free[0]] = grant
            granted.append(grant)
        return granted

    def stats(self) -> dict:
        return {"active": len(self.holders), "waiting": len(self.waiting), "slots": len(self.ports)}
