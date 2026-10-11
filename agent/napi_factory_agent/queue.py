"""Офлайн-очередь событий на microSD (раздел 3.1 п.6 ТЗ).

Одно событие = один файл <seq>.json, запись через rename. seq монотонный и сохраняется
между перезапусками, stream (uuid4) постоянный для этой очереди. Сервер дедуплицирует
по (stream, seq). Файлы удаляются по ack.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .util import atomic_write_bytes, atomic_write_json, read_json

# События, которые нельзя выбрасывать при переполнении
CRITICAL_KINDS = {"job_started", "stage_started", "stage_finished", "job_finished"}


class EventQueue:
    def __init__(self, directory: str | os.PathLike, max_bytes: int = 32 * 1024 * 1024):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        meta = read_json(self.dir / "meta.json", {}) or {}
        self.stream: str = meta.get("stream") or uuid.uuid4().hex
        last = max((self._seq_of(p) for p in self._files()), default=0)
        self._next = max(int(meta.get("next_seq", 1)), last + 1)
        self._save_meta()

    @staticmethod
    def _seq_of(p: Path) -> int:
        return int(p.stem)

    def _files(self) -> list[Path]:
        return sorted((p for p in self.dir.glob("*.json") if p.stem.isdigit()), key=self._seq_of)

    def _save_meta(self) -> None:
        atomic_write_json(self.dir / "meta.json", {"stream": self.stream, "next_seq": self._next})

    def put(self, kind: str, job_id: str | None, data: dict[str, Any] | None = None,
            stage: str | None = None) -> dict[str, Any]:
        with self._lock:
            seq = self._next
            self._next += 1
            # Сначала счётчик: после сбоя seq не повторится
            self._save_meta()
            ev = {"type": "event", "stream": self.stream, "seq": seq, "ts": time.time(),
                  "job_id": job_id, "kind": kind, "stage": stage, "data": data or {}}
            atomic_write_bytes(self.dir / f"{seq:012d}.json", json.dumps(ev, ensure_ascii=False).encode())
            self._enforce_limit()
            return ev

    def _enforce_limit(self) -> None:
        files = self._files()
        total = sum(p.stat().st_size for p in files)
        if total <= self.max_bytes:
            return
        for p in files:
            if total <= self.max_bytes:
                break
            ev = read_json(p, {}) or {}
            if ev.get("kind") in CRITICAL_KINDS:
                continue
            size = p.stat().st_size
            p.unlink(missing_ok=True)
            total -= size

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for p in self._files():
                ev = read_json(p)
                if ev is not None:
                    out.append(ev)
            return out

    def ack(self, seq: int, stream: str | None = None) -> int:
        """Удаляет события с seq ≤ ack. Возвращает число удалённых."""
        if stream and stream != self.stream:
            return 0
        n = 0
        with self._lock:
            for p in self._files():
                if self._seq_of(p) <= seq:
                    p.unlink(missing_ok=True)
                    n += 1
        return n

    def __len__(self) -> int:
        return len(self._files())
