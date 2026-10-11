"""Этапы на NAPI-C. Единый интерфейс: run(ctx) -> StageResult.

Этапы выполняются в отдельном потоке; с асинхронной частью агента общаются через ctx.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


class StageCancelled(Exception):
    """Оператор нажал «Остановить»."""


class StageTimeout(Exception):
    pass


@dataclass
class StageResult:
    state: str                       # pass | fail | skipped | error | interrupted
    detail: str = ""
    measurements: dict[str, Any] = field(default_factory=dict)
    # Замечания при PASS: этап пройден, но есть особенность, которую надо видеть в отчёте
    warnings: list[str] = field(default_factory=list)


class StageCtx:
    """Окружение этапа: бэкенд, параметры, прогресс, лог, отмена, запросы к серверу."""

    def __init__(self, name: str, job_id: str, mode: str, params: dict[str, Any], backend: Any,
                 cancel: threading.Event, timeout_s: float,
                 on_progress: Callable[[float | None, str | None], None],
                 request: Callable[[dict, str, float], dict | None],
                 shared: dict[str, Any], images_dir: str, server_addr: tuple[str, int] | None,
                 cpuid: str | None):
        self.name = name
        self.job_id = job_id
        self.mode = mode
        self.params = params
        self.backend = backend
        self.cancel = cancel
        self.deadline = time.monotonic() + timeout_s
        self._on_progress = on_progress
        self._request = request
        self.log_lines: deque[str] = deque(maxlen=200)
        self.shared = shared          # общие данные между этапами одного задания
        self.images_dir = images_dir
        self.server_addr = server_addr
        self.cpuid = cpuid
        # Флаг «идёт запись образа»: отмена в этот момент = interrupted
        self.writing = False

    @property
    def p(self) -> dict[str, Any]:
        return self.params.get(self.name, {}) or {}

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def check(self) -> None:
        if self.cancel.is_set():
            raise StageCancelled()
        if time.monotonic() > self.deadline:
            raise StageTimeout()

    def progress(self, pct: float | None = None, detail: str | None = None) -> None:
        self._on_progress(pct, detail)

    def log(self, line: str) -> None:
        self.log_lines.append(f"{time.strftime('%H:%M:%S')} {line}")

    def request(self, msg: dict, resp_type: str, timeout: float) -> dict | None:
        """Синхронный запрос к серверу (net_request→net_grant, serial_request→serial).

        Ждёт ответ до timeout с проверкой отмены. None = нет ответа.
        """
        return self._request(msg, resp_type, min(timeout, max(self.remaining(), 0)))

    def sleep(self, s: float) -> None:
        end = time.monotonic() + s
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 0.2))
