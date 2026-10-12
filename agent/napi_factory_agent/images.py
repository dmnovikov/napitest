"""Образ на тестовой microSD: манифест, размер из zstd frame header, самопроверка с кэшем.

Манифест (генерирует scripts/build-test-sd.sh):
{"version", "profile", "file", "compressed_size", "compressed_sha256", "image_size", "image_sha256"}
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .util import atomic_write_json, read_json

_ZSTD_SIZE = re.compile(r"Decompressed Size:.*\((\d+)\s*B\)")


class ManifestError(Exception):
    pass


def load_manifest(images_dir: str, manifest_file: str) -> dict[str, Any]:
    path = Path(images_dir) / manifest_file
    m = read_json(path)
    if not isinstance(m, dict):
        raise ManifestError(f"нет манифеста {path}")
    for k in ("version", "file", "compressed_size", "compressed_sha256", "image_size", "image_sha256"):
        if k not in m:
            raise ManifestError(f"манифест без поля {k}")
    return m


def image_path(images_dir: str, manifest_file: str, man: dict[str, Any]) -> Path:
    """Файл образа лежит рядом со своим манифестом (images/ или images/catalog/)."""
    return (Path(images_dir) / manifest_file).parent / man["file"]


def selfcheck_cache(state_dir: str, manifest_file: str) -> str:
    """Кэш самопроверки: свой на каждый манифест (образ microSD — прежний selfcheck.json)."""
    if "/" not in manifest_file:
        return str(Path(state_dir) / "selfcheck.json")
    return str(Path(state_dir) / ("selfcheck-" + manifest_file.replace("/", "_") + ".json"))


def selfcheck_key(path: Path, man: dict[str, Any]) -> str:
    st = path.stat()
    return f"{man['file']}:{st.st_size}:{int(st.st_mtime)}:{man['compressed_sha256']}"


def zstd_frame_size(path: str) -> int | None:
    """Размер распакованного образа из заголовка кадра zstd, без распаковки."""
    try:
        out = subprocess.run(["zstd", "-l", "-v", path], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _ZSTD_SIZE.search(out.stdout + out.stderr)
    return int(m.group(1)) if m else None


def sha256_file(path: str, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def selfcheck(images_dir: str, manifest_file: str, cache_path: str) -> dict[str, Any]:
    """Проверка сжатого файла по манифесту; результат кэшируется по (size, mtime).

    Хеш распакованного потока проверяется при каждой прошивке на лету.
    """
    try:
        m = load_manifest(images_dir, manifest_file)
    except ManifestError as e:
        return {"state": "fail", "detail": str(e)}
    f = image_path(images_dir, manifest_file, m)
    try:
        st = f.stat()
        key = selfcheck_key(f, m)
    except OSError:
        return {"state": "fail", "detail": f"нет файла {m['file']}"}
    cache = read_json(cache_path, {}) or {}
    if cache.get("key") == key and cache.get("state") == "pass":
        return {"state": "pass", "detail": "cached", "version": m["version"]}
    if st.st_size != int(m["compressed_size"]):
        res = {"state": "fail", "detail": f"размер {m['file']} {st.st_size}≠{m['compressed_size']}"}
    elif sha256_file(str(f)) != m["compressed_sha256"]:
        res = {"state": "fail", "detail": f"SHA-256 {m['file']} не совпал"}
    else:
        fs = zstd_frame_size(str(f))
        if fs is not None and fs != int(m["image_size"]):
            res = {"state": "fail", "detail": f"zstd frame {fs}≠{m['image_size']}"}
        else:
            res = {"state": "pass", "detail": "verified", "version": m["version"]}
    atomic_write_json(cache_path, {"key": key, **res})
    return res


def find_manifest(images_dir: str) -> str | None:
    try:
        names = sorted(n for n in os.listdir(images_dir) if n.endswith(".manifest.json"))
    except OSError:
        return None
    return names[-1] if names else None
