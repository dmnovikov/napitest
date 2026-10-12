"""Подготовка образа ОС по ссылке: скачивание, сверка, проверка «образ всего диска», упаковка в .zst.

Выполняется в отдельном потоке. Результат — <name>.img.zst и манифест того же формата, что на
тестовой microSD (agent/napi_factory_agent/images.py), плюс сведения об источнике.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import unquote, urljoin, urlparse

CHUNK = 4 << 20
USER_AGENT = "napi-factory-server"
_VERSION = re.compile(r"(\d+\.\d+(?:\.\d+)*)")

Progress = Callable[[str, float, str], None]      # (фаза, процент 0–100, текст)


class PrepError(Exception):
    pass


class PrepCancelled(Exception):
    pass


def source_name(url: str) -> str:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    if not name:
        raise PrepError("в ссылке нет имени файла")
    return name


def version_of(name: str) -> str:
    m = _VERSION.search(name)
    return m.group(1) if m else name


def check_url(url: str) -> str:
    url = (url or "").strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise PrepError("нужна ссылка http(s)://…")
    source_name(url)
    return url


def decompressor(name: str) -> list[str] | None:
    """Команда распаковки по расширению; None — файл уже является образом."""
    low = name.lower()
    if low.endswith(".xz"):
        return ["xz", "-dc"]
    if low.endswith(".zst"):
        return ["zstd", "-dc", "-q"]
    if low.endswith(".gz"):
        return ["gzip", "-dc"]
    return None


def check_disk_header(head: bytes) -> str | None:
    """Образ всего диска: сигнатура MBR (у GPT — защитный MBR) и таблица разделов. None — годится."""
    if len(head) < 1024 or head[510:512] != b"\x55\xaa":
        return "это не образ диска: нет таблицы разделов (MBR/GPT)"
    if head[512:520] == b"EFI PART":
        return None
    parts = [head[446 + 16 * i: 462 + 16 * i] for i in range(4)]
    if any(p[4] != 0 for p in parts):
        return None
    return "это не образ диска: таблица разделов пуста"


def fetch_sums(url: str, timeout: float = 15) -> dict[str, str]:
    """SHA256SUMS рядом с файлом, если есть: {имя: sha256}."""
    try:
        req = urllib.request.Request(urljoin(url, "SHA256SUMS"), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read(1 << 20).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", f[0]):
            out[f[-1].lstrip("*").rsplit("/", 1)[-1]] = f[0].lower()
    return out


def _download(url: str, dest: Path, progress: Progress, cancel: threading.Event) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        raise PrepError(f"скачивание: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise PrepError(f"скачивание: {getattr(e, 'reason', e)}") from e
    total = int(resp.headers.get("Content-Length") or 0)
    h = hashlib.sha256()
    got = 0
    with resp, open(dest, "wb") as out:
        while True:
            if cancel.is_set():
                raise PrepCancelled()
            try:
                buf = resp.read(CHUNK)
            except OSError as e:
                raise PrepError(f"скачивание прервано: {e}") from e
            if not buf:
                break
            out.write(buf)
            h.update(buf)
            got += len(buf)
            mib = got >> 20
            progress("download", 50 * got / total if total else 0,
                     f"скачивание {mib} из {total >> 20} МБ" if total else f"скачивание {mib} МБ")
    if total and got != total:
        raise PrepError(f"скачано {got} из {total} байт")
    return got, h.hexdigest()


def _stream(src: Path, cmd: list[str] | None, on_input: Callable[[int], None],
            cancel: threading.Event) -> Iterator[bytes]:
    """Распакованный поток образа; on_input(сколько сжатых байт прочитано)."""
    if cmd is None:
        with open(src, "rb") as f:
            done = 0
            while buf := f.read(CHUNK):
                if cancel.is_set():
                    raise PrepCancelled()
                done += len(buf)
                on_input(done)
                yield buf
        return
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fed = [0]

    def feed():
        try:
            with open(src, "rb") as f:
                while buf := f.read(CHUNK):
                    if cancel.is_set():
                        break
                    proc.stdin.write(buf)
                    fed[0] += len(buf)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    t = threading.Thread(target=feed, daemon=True)
    t.start()
    try:
        while buf := proc.stdout.read(CHUNK):
            if cancel.is_set():
                raise PrepCancelled()
            on_input(fed[0])
            yield buf
        if proc.wait() != 0:
            raise PrepError("файл повреждён: не распаковывается")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t.join(5)


def prepare(url: str, out_dir: str | Path, base: str, max_size: int, progress: Progress,
            cancel: threading.Event, profile: str = "") -> dict:
    """Скачать и подготовить образ. Возвращает манифест; файлы <base>.img.zst и <base>.manifest.json."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / f".work-{base}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        name = source_name(url)
        src = work / "source"
        src_size, src_sha = _download(url, src, progress, cancel)
        sums = fetch_sums(url)
        if name in sums and sums[name] != src_sha:
            raise PrepError("контрольная сумма не совпала с SHA256SUMS на сервере")
        cmd = decompressor(name)

        # 1. Проверка: распаковывается, образ всего диска, помещается на SD-NAND
        def p1(done):
            progress("check", 50 + 20 * done / max(src_size, 1), "проверка образа")
        h = hashlib.sha256()
        size = 0
        head = b""
        for buf in _stream(src, cmd, p1, cancel):
            if len(head) < 1024:
                head += buf[:1024 - len(head)]
            h.update(buf)
            size += len(buf)
            if size > max_size:
                raise PrepError(f"образ больше SD-NAND (> {max_size // 1_000_000} МБ)")
        err = check_disk_header(head)
        if err:
            raise PrepError(err)
        if size % 512:
            raise PrepError("размер образа не кратен 512 байт")
        image_sha = h.hexdigest()

        # 2. Упаковка в zstd с размером в заголовке кадра (агент сверяет его без распаковки)
        zst = work / "image.img.zst"
        z = subprocess.Popen(["zstd", "-q", "-3", "-T0", f"--stream-size={size}", "-o", str(zst)],
                             stdin=subprocess.PIPE, stderr=subprocess.PIPE)

        def p2(done):
            progress("pack", 70 + 30 * done / max(src_size, 1), "упаковка")
        h2 = hashlib.sha256()
        try:
            for buf in _stream(src, cmd, p2, cancel):
                h2.update(buf)
                z.stdin.write(buf)
            z.stdin.close()
            if z.wait() != 0:
                raise PrepError("zstd: " + z.stderr.read().decode(errors="replace")[:200])
        finally:
            if z.poll() is None:
                z.kill()
                z.wait()
        if h2.hexdigest() != image_sha:
            raise PrepError("повторная распаковка дала другой образ")
        ch = hashlib.sha256()
        with open(zst, "rb") as f:
            while buf := f.read(CHUNK):
                ch.update(buf)
        man = {
            "version": version_of(name), "profile": profile, "file": f"{base}.img.zst",
            "compressed_size": zst.stat().st_size, "compressed_sha256": ch.hexdigest(),
            "image_size": size, "image_sha256": image_sha,
            "source_url": url, "source_file": name, "source_size": src_size, "source_sha256": src_sha,
        }
        os.replace(zst, out_dir / man["file"])
        tmp = work / "manifest.json"
        tmp.write_text(json.dumps(man, indent=1, ensure_ascii=False))
        os.replace(tmp, out_dir / f"{base}.manifest.json")
        progress("ready", 100, "готов")
        return man
    finally:
        shutil.rmtree(work, ignore_errors=True)
