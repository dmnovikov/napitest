"""Процессы `iperf3 -s`, по одному на слот планировщика."""
from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("napi.iperf")


class IperfServers:
    def __init__(self, binary: str, ports: list[int]):
        self.binary = binary
        self.ports = ports
        self.procs: dict[int, asyncio.subprocess.Process] = {}
        self._task: asyncio.Task | None = None
        self.available = shutil.which(binary) is not None

    async def start(self) -> None:
        if not self.available:
            log.warning("%s не найден: NET-этап реальных плат не пройдёт iperf3", self.binary)
            return
        self._task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        while True:
            for port in self.ports:
                proc = self.procs.get(port)
                if proc is None or proc.returncode is not None:
                    try:
                        self.procs[port] = await asyncio.create_subprocess_exec(
                            self.binary, "-s", "-p", str(port),
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        )
                    except OSError as exc:
                        log.error("не удалось запустить iperf3 на порту %s: %s", port, exc)
            await asyncio.sleep(2)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for proc in self.procs.values():
            if proc.returncode is None:
                proc.terminate()
        for proc in self.procs.values():
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
