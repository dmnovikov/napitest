"""Образы ОС с сервера на тестовую microSD (меню «ОС на SD-NAND»).

Сервер присылает {"type": "images", "sync": bool, "images": [{"file", "manifest"}]}. Агент держит в
images/catalog ровно эти образы: недостающие скачивает с сервера (с докачкой), сверяет sha256,
лишние удаляет. Качает и удаляет только между партиями (sync от сервера) и когда у платы нет
задания: скачивание не мешает тесту сети и записи SD-NAND. «Образ на microSD» (images/) не трогается.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .images import selfcheck_cache, selfcheck_key
from .util import atomic_write_json, read_json

log = logging.getLogger("napi.images")

CHUNK = 1 << 20
RESERVE = 256 << 20          # оставить свободным на microSD
CATALOG = "catalog"


class SyncError(Exception):
    pass


class SyncCancelled(Exception):
    pass


def base_of(file: str) -> str:
    return file[: -len(".img.zst")] if file.endswith(".img.zst") else file


class ImageSync:
    def __init__(self, agent: Any, images_dir: str, state_dir: str):
        self.agent = agent
        self.dir = Path(images_dir) / CATALOG
        self.state_dir = str(state_dir)
        self.wanted: list[dict] | None = None      # None: каталог от сервера ещё не получен
        self.allowed = False
        self.status: dict[str, dict] = {}
        self._cancel = threading.Event()
        self._wake: asyncio.Event | None = None
        self._last_sent = 0.0
        self._scan()

    # ------------------------------------------------------------------ состояние
    def manifest_file(self, file: str) -> str:
        return f"{CATALOG}/{base_of(file)}.manifest.json"

    def _verified(self, man: dict) -> bool:
        """Файл уже проверен (кэш самопроверки совпадает), без повторного хеширования."""
        path = self.dir / man["file"]
        try:
            if path.stat().st_size != int(man["compressed_size"]):
                return False
            key = selfcheck_key(path, man)
        except (OSError, KeyError, ValueError):
            return False
        cache = read_json(selfcheck_cache(self.state_dir, self.manifest_file(man["file"])), {}) or {}
        return cache.get("key") == key and cache.get("state") == "pass"

    def _scan(self) -> None:
        try:
            names = sorted(n for n in os.listdir(self.dir) if n.endswith(".manifest.json"))
        except OSError:
            return
        for n in names:
            man = read_json(self.dir / n)
            if isinstance(man, dict) and man.get("file") and self._verified(man):
                self.status[man["file"]] = {"state": "ready"}

    def _set(self, file: str, state: str, pct: float | None = None, detail: str | None = None,
             force: bool = False) -> None:
        old = self.status.get(file) or {}
        st = {"state": state}
        if pct is not None:
            st["pct"] = round(pct, 1)
        if detail:
            st["detail"] = detail
        self.status[file] = st
        now = time.monotonic()
        if force or old.get("state") != state or now - self._last_sent >= 2:
            self._last_sent = now
            self._report()

    def _report(self) -> None:
        loop, link = self.agent.loop, self.agent.link
        if not (loop and link):
            return
        msg = {"type": "images_status", "files": dict(self.status)}
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is loop:
            link.send_soon(msg)
        else:                                   # из потока скачивания
            loop.call_soon_threadsafe(link.send_soon, msg)

    def _busy(self) -> bool:
        job = self.agent.job
        return bool(job and not job.get("finished"))

    # ------------------------------------------------------------------ события
    def on_catalog(self, msg: dict) -> None:
        self.wanted = [i for i in (msg.get("images") or []) if isinstance(i, dict) and i.get("file")]
        self.allowed = bool(msg.get("sync"))
        if not self.allowed:
            self._cancel.set()
        self.kick()

    def pause(self) -> None:
        """Плата получила задание: скачивание останавливается (докачается после)."""
        self._cancel.set()

    def kick(self) -> None:
        if self._wake is not None:
            self._wake.set()

    # ------------------------------------------------------------------ цикл
    async def run(self) -> None:
        self._wake = asyncio.Event()
        self._wake.set()
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._sync_once()
            except Exception:  # noqa: BLE001 — сбой синхронизации не должен останавливать агента
                log.exception("синхронизация образов")
            # повтор, если что-то не скачалось (ошибка сети, пауза)
            if self.wanted and any((self.status.get(i["file"]) or {}).get("state") != "ready" for i in self.wanted):
                asyncio.get_running_loop().call_later(30, self.kick)

    async def _sync_once(self) -> None:
        if self.wanted is None:
            return
        if self.agent.loop is None or self.agent.loop.is_closed():
            self.agent.loop = asyncio.get_running_loop()
        wanted = {i["file"]: i["manifest"] for i in self.wanted}
        for file in list(self.status):
            if file not in wanted:
                self.status.pop(file, None)
        for file, man in wanted.items():
            if (self.status.get(file) or {}).get("state") != "ready":
                if self._verified(man):
                    self.status[file] = {"state": "ready"}
                else:
                    self._set(file, "waiting", detail="ждёт паузы между партиями" if not self.allowed else None)
        self._report()
        if not self.allowed or self._busy():
            return
        up = getattr(self.agent, "updater", None)
        if up is not None and up.needed():
            await self._update(up)          # при успехе агент перезапускается и сюда не возвращается
        self._remove_stale(set(wanted))
        for file, man in wanted.items():
            if (self.status.get(file) or {}).get("state") == "ready":
                continue
            if not self.allowed or self._busy():
                return
            self._cancel = threading.Event()
            try:
                await asyncio.to_thread(self._fetch, file, man, self._cancel)
                self._set(file, "ready", force=True)
                log.info("образ %s на карте", file)
            except SyncCancelled:
                self._set(file, "waiting", detail="пауза: идёт задание", force=True)
                return
            except SyncError as e:
                log.warning("образ %s: %s", file, e)
                self._set(file, "error", detail=str(e), force=True)

    async def _update(self, up: Any) -> None:
        """Новая версия агента с сервера (selfupdate): скачать, проверить, переключить, перезапуститься."""
        from .selfupdate import UpdateError
        try:
            base = self.agent.link.http_base() if self.agent.link and hasattr(self.agent.link, "http_base") else None
            if not base:
                raise UpdateError("нет связи с сервером")
            up.status = {"state": "installing", "detail": up.wanted["version"]}
            data = await asyncio.to_thread(up.fetch, base, self.agent.cfg["token"])
            await asyncio.to_thread(up.install, data)
        except (UpdateError, OSError, subprocess.SubprocessError) as e:
            log.warning("обновление агента: %s", e)
            up.fail(str(e))
            self.agent.link.send_soon(self.agent.hello())
            return
        up.status = {"state": "restarting", "detail": up.wanted["version"]}
        self.agent.link.send_soon(self.agent.hello())
        await asyncio.sleep(0.5)            # hello уходит на сервер до перезапуска
        up.restart()

    def _remove_stale(self, keep: set[str]) -> None:
        try:
            names = os.listdir(self.dir)
        except OSError:
            return
        keep_names = keep | {f"{base_of(f)}.manifest.json" for f in keep} | {f + ".part" for f in keep}
        for n in names:
            if n not in keep_names and (n.endswith((".img.zst", ".manifest.json", ".part"))):
                try:
                    (self.dir / n).unlink()
                    log.info("удалён старый образ %s", n)
                except OSError:
                    pass

    # ------------------------------------------------------------------ скачивание (поток)
    def _url(self, file: str) -> str:
        base = self.agent.link.http_base() if self.agent.link and hasattr(self.agent.link, "http_base") else None
        if not base:
            raise SyncError("нет связи с сервером")
        return f"{base}/api/v1/agent/images/{file}"

    def _fetch(self, file: str, man: dict, cancel: threading.Event) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        need = int(man["compressed_size"])
        part = self.dir / (file + ".part")
        have = part.stat().st_size if part.exists() else 0
        if have > need:
            part.unlink()
            have = 0
        free = shutil.disk_usage(self.dir).free
        if free < need - have + RESERVE:
            raise SyncError(f"мало места на microSD: нужно {(need - have) >> 20} МБ, свободно {free >> 20} МБ")
        h = hashlib.sha256()
        if have:
            with open(part, "rb") as f:
                while buf := f.read(CHUNK):
                    h.update(buf)
        headers = {"Authorization": f"Bearer {self.agent.cfg['token']}"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            resp = urllib.request.urlopen(urllib.request.Request(self._url(file), headers=headers), timeout=30)
        except urllib.error.HTTPError as e:
            raise SyncError(f"сервер: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise SyncError(f"сервер: {getattr(e, 'reason', e)}") from e
        with resp:
            if have and resp.status != 206:      # сервер отдал файл целиком
                have = 0
                h = hashlib.sha256()
            with open(part, "ab" if have else "wb") as out:
                got = have
                while True:
                    if cancel.is_set():
                        raise SyncCancelled()
                    try:
                        buf = resp.read(CHUNK)
                    except OSError as e:
                        raise SyncError(f"обрыв скачивания: {e}") from e
                    if not buf:
                        break
                    out.write(buf)
                    h.update(buf)
                    got += len(buf)
                    self._set(file, "downloading", 100 * got / max(need, 1))
                out.flush()
                os.fsync(out.fileno())
        if got != need:
            raise SyncError(f"скачано {got >> 20} из {need >> 20} МБ")
        if h.hexdigest() != man["compressed_sha256"]:
            part.unlink()
            raise SyncError("контрольная сумма не совпала, файл удалён")
        final = self.dir / file
        os.replace(part, final)
        mf = self.manifest_file(file)
        atomic_write_json(self.dir / Path(mf).name, man)
        atomic_write_json(selfcheck_cache(self.state_dir, mf),
                          {"key": selfcheck_key(final, man), "state": "pass", "detail": "verified",
                           "version": man.get("version")})
        # Каталог на диск, чтобы файлы пережили выключение платы
        fd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
