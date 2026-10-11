"""Общие утилиты: атомарная запись, форматирование размеров."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

MiB = 1024 * 1024
GiB = 1024 * MiB


def atomic_write_bytes(path: str | os.PathLike, data: bytes) -> None:
    """Запись через временный файл + fsync + rename: файл либо старый, либо новый целиком."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def atomic_write_json(path: str | os.PathLike, obj: Any) -> None:
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=1).encode())


def read_json(path: str | os.PathLike, default: Any = None) -> Any:
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except (FileNotFoundError, ValueError):
        return default


def read_text(path: str | os.PathLike, default: str | None = None) -> str | None:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def fmt_size(n: int) -> str:
    """Короткий размер для detail: 3.7G, 512M."""
    if n >= GiB:
        return f"{n / GiB:.1f}G"
    return f"{n / MiB:.0f}M"


def align_up(n: int, a: int) -> int:
    return (n + a - 1) // a * a
