"""Меню «ОС для внутреннего накопителя»: варианты (плата → ОС → ссылка), подготовка образов и раздача на карты.

Встроенные пункты: «Образ на microSD» (как раньше, без обновления), Zeroimage (нули, без файла)
и Custom (одноразовый образ клиента по ссылке). Варианты со ссылкой хранятся в базе; образ
скачивается один раз, его sha фиксируется. Старый образ работает, пока новый не готов.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path
from typing import Callable

from ..config import Profile, Settings
from ..db.database import Database, now_iso
from . import imageprep

log = logging.getLogger("napi.catalog")

SD, ZERO, CUSTOM = "sd", "zero", "custom"
BUILTIN_NAMES = {SD: "Образ на microSD", ZERO: "Zeroimage", CUSTOM: "Custom (образ клиента)"}


class CatalogError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Catalog:
    def __init__(self, settings: Settings, profile: Profile, db: Database, on_change: Callable[[], None]):
        self.settings = settings
        self.profile = profile
        self.db = db
        self.dir = Path(settings.images_dir)
        self.on_change = on_change              # только флаги: вызывается и из потока подготовки
        self.progress: dict[int, tuple[float, str]] = {}   # image_id -> (процент, текст), из потока
        self._wake: asyncio.Event | None = None
        self._cancel: dict[int, threading.Event] = {}
        self._task: asyncio.Task | None = None
        self.max_image = int((profile.section("sdnand").get("detect") or {}).get("size_min_bytes", 3_500_000_000))

    # ------------------------------------------------------------------ база

    def seed(self) -> None:
        """Первый запуск: варианты из профиля (os_images). Незаконченная подготовка — заново."""
        if self.db.one("SELECT 1 FROM os_variant LIMIT 1") is None:
            for i, item in enumerate(self.profile.data.get("os_images") or []):
                cur = self.db.execute(
                    "INSERT INTO os_variant (key, name, kind, sort, visible, created_at) VALUES (?, ?, 'url', ?, 1, ?)",
                    (item["key"], item["name"], (i + 1) * 10, now_iso()))
                if item.get("url"):
                    self._queue(cur.lastrowid, item["url"], None)
        self.db.execute("UPDATE os_image SET state = 'queued', detail = NULL WHERE state = 'working'")

    def _variant(self, key: str):
        return self.db.one("SELECT * FROM os_variant WHERE key = ?", (key,))

    def _queue(self, variant_id: int, url: str, operator: str | None) -> int:
        # Подготовка прежней ссылки, которая ещё не закончена, больше не нужна
        for row in self.db.all("SELECT id FROM os_image WHERE variant_id = ? AND state IN ('queued', 'working')",
                               (variant_id,)):
            self._stop(row["id"], "заменён новой ссылкой")
        name = imageprep.source_name(url)
        cur = self.db.execute(
            "INSERT INTO os_image (variant_id, url, state, source_file, version, operator, created_at)"
            " VALUES (?, ?, 'queued', ?, ?, ?, ?)",
            (variant_id, url, name, imageprep.version_of(name), operator, now_iso()))
        self._kick()
        return cur.lastrowid

    def _stop(self, image_id: int, why: str) -> None:
        ev = self._cancel.get(image_id)
        if ev:
            ev.set()
        self.db.execute("UPDATE os_image SET state = 'replaced', detail = ? WHERE id = ?", (why, image_id))
        self.progress.pop(image_id, None)

    # ------------------------------------------------------------------ операции оператора

    def add(self, name: str, url: str, operator: str) -> str:
        name = (name or "").strip()
        if not name:
            raise CatalogError("укажите название ОС")
        url = self._url(url)
        if self.db.one("SELECT 1 FROM os_variant WHERE name = ? AND kind = 'url'", (name,)):
            raise CatalogError(f"ОС «{name}» уже есть в списке")
        sort = (self.db.one("SELECT MAX(sort) AS s FROM os_variant WHERE kind = 'url'")["s"] or 0) + 10
        cur = self.db.execute(
            "INSERT INTO os_variant (key, name, kind, sort, visible, created_at) VALUES (?, ?, 'url', ?, 1, ?)",
            ("new", name, sort, now_iso()))
        key = self._key_for(name, cur.lastrowid)
        self.db.execute("UPDATE os_variant SET key = ? WHERE id = ?", (key, cur.lastrowid))
        self._queue(cur.lastrowid, url, operator)
        self.on_change()
        return key

    def _key_for(self, name: str, vid: int) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20]
        key = slug if slug and slug not in (SD, ZERO, CUSTOM) else f"os{vid}"
        if self.db.one("SELECT 1 FROM os_variant WHERE key = ? AND id != ?", (key, vid)):
            key = f"{key}-{vid}"
        return key

    def _url(self, url: str) -> str:
        try:
            return imageprep.check_url(url)
        except imageprep.PrepError as e:
            raise CatalogError(str(e)) from e

    def set_url(self, key: str, url: str, operator: str) -> None:
        v = self._variant(key)
        if v is None or v["kind"] != "url":
            raise CatalogError("нет такой ОС", 404)
        self._queue(v["id"], self._url(url), operator)
        self.on_change()

    def update(self, key: str, *, name: str | None = None, visible: bool | None = None,
               move: int | None = None) -> None:
        v = self._variant(key)
        if v is None or v["kind"] != "url":
            raise CatalogError("нет такой ОС", 404)
        if name is not None:
            name = name.strip()
            if not name:
                raise CatalogError("укажите название ОС")
            self.db.execute("UPDATE os_variant SET name = ? WHERE id = ?", (name, v["id"]))
        if visible is not None:
            self.db.execute("UPDATE os_variant SET visible = ? WHERE id = ?", (1 if visible else 0, v["id"]))
        if move:
            rows = self.db.all("SELECT id FROM os_variant WHERE kind = 'url' ORDER BY sort, id")
            ids = [r["id"] for r in rows]
            i = ids.index(v["id"])
            j = max(0, min(len(ids) - 1, i + (1 if move > 0 else -1)))
            ids[i], ids[j] = ids[j], ids[i]
            for n, vid in enumerate(ids):
                self.db.execute("UPDATE os_variant SET sort = ? WHERE id = ?", ((n + 1) * 10, vid))
        self.on_change()

    def set_custom(self, url: str, operator: str) -> None:
        url = self._url(url)
        v = self._variant(CUSTOM)
        if v is None:
            cur = self.db.execute(
                "INSERT INTO os_variant (key, name, kind, sort, visible, created_at) VALUES (?, ?, 'custom', 1000, 1, ?)",
                (CUSTOM, BUILTIN_NAMES[CUSTOM], now_iso()))
            vid = cur.lastrowid
        else:
            vid = v["id"]
        self._queue(vid, url, operator)
        self.on_change()

    # ------------------------------------------------------------------ представления

    def _image_view(self, row) -> dict | None:
        if row is None:
            return None
        out = {k: row[k] for k in ("id", "url", "state", "detail", "file", "source_file", "version",
                                   "image_size", "image_sha256", "compressed_size", "source_sha256",
                                   "operator", "created_at", "ready_at")}
        if row["id"] in self.progress:
            out["pct"], out["detail"] = self.progress[row["id"]]
        return out

    def variants(self, card_files: dict[str, int] | None = None, online: int = 0) -> list[dict]:
        """Все варианты со ссылкой (и Custom): текущий образ, подготовка новой ссылки, наличие на картах."""
        out = []
        for v in self.db.all("SELECT * FROM os_variant ORDER BY kind = 'custom', sort, id"):
            cur = self.db.one("SELECT * FROM os_image WHERE id = ?", (v["current_image_id"],)) \
                if v["current_image_id"] else None
            nxt = self.db.one(
                "SELECT * FROM os_image WHERE variant_id = ? AND id > COALESCE(?, 0) AND state != 'replaced'"
                " ORDER BY id DESC LIMIT 1", (v["id"], v["current_image_id"]))
            item = {"key": v["key"], "name": v["name"], "kind": v["kind"], "visible": bool(v["visible"]),
                    "current": self._image_view(cur), "next": self._image_view(nxt)}
            if cur is not None and card_files is not None:
                item["cards"] = {"ready": card_files.get(cur["file"], 0), "online": online}
            out.append(item)
        return out

    def menu(self, variants: list[dict]) -> list[dict]:
        """Пункты меню запуска по порядку; первый — выбор по умолчанию."""
        items = [{"key": v["key"], "name": v["name"], "kind": "url"} for v in variants
                 if v["kind"] == "url" and v["visible"]]
        items.append({"key": ZERO, "name": BUILTIN_NAMES[ZERO], "kind": ZERO})
        items.append({"key": SD, "name": BUILTIN_NAMES[SD], "kind": SD,
                      "version": self.profile.image_version})
        items.append({"key": CUSTOM, "name": BUILTIN_NAMES[CUSTOM], "kind": CUSTOM})
        return items

    def history(self, limit: int = 100) -> list[dict]:
        return [{"variant": r["vname"], "key": r["vkey"], **self._image_view(r)} for r in self.db.all(
            "SELECT i.*, v.name AS vname, v.key AS vkey FROM os_image i JOIN os_variant v ON v.id = i.variant_id"
            " ORDER BY i.id DESC LIMIT ?", (limit,))]

    def current(self, key: str):
        """Готовый образ варианта (строка os_image + имя варианта) или None."""
        return self.db.one(
            "SELECT i.*, v.name AS vname, v.key AS vkey, v.visible FROM os_variant v"
            " JOIN os_image i ON i.id = v.current_image_id WHERE v.key = ? AND i.state = 'ready'", (key,))

    def agent_catalog(self) -> list[dict]:
        """Что должно лежать на картах: текущие образы видимых вариантов и Custom."""
        out = []
        for r in self.db.all(
                "SELECT i.* FROM os_variant v JOIN os_image i ON i.id = v.current_image_id"
                " WHERE i.state = 'ready' AND (v.visible = 1 OR v.kind = 'custom') ORDER BY v.sort, v.id"):
            out.append({"file": r["file"], "manifest": self.manifest(r)})
        return out

    def manifest(self, r) -> dict:
        return {"version": r["version"], "profile": self.profile.name, "file": r["file"],
                "compressed_size": r["compressed_size"], "compressed_sha256": r["compressed_sha256"],
                "image_size": r["image_size"], "image_sha256": r["image_sha256"],
                "source_url": r["url"], "source_file": r["source_file"]}

    def file_allowed(self, name: str) -> Path | None:
        if "/" in name or name.startswith("."):
            return None
        row = self.db.one("SELECT 1 FROM os_image WHERE file = ? AND state = 'ready'", (name,))
        path = self.dir / name
        return path if row and path.is_file() else None

    # ------------------------------------------------------------------ подготовка

    def _kick(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def run(self) -> None:
        self._wake = asyncio.Event()
        while True:
            row = self.db.one("SELECT i.*, v.key AS vkey FROM os_image i JOIN os_variant v ON v.id = i.variant_id"
                              " WHERE i.state = 'queued' ORDER BY i.id LIMIT 1")
            if row is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            await self._prepare(row)

    async def _prepare(self, row) -> None:
        image_id = row["id"]
        base = f"{row['vkey']}-{image_id}"
        cancel = threading.Event()
        self._cancel[image_id] = cancel
        self.db.execute("UPDATE os_image SET state = 'working', detail = NULL WHERE id = ?", (image_id,))
        self.progress[image_id] = (0.0, "скачивание")
        self.on_change()
        last = [0.0]

        def progress(phase: str, pct: float, text: str) -> None:
            self.progress[image_id] = (round(pct, 1), text)
            if pct - last[0] >= 1 or phase == "ready":
                last[0] = pct
                self.on_change()

        try:
            man = await asyncio.to_thread(imageprep.prepare, row["url"], self.dir, base, self.max_image,
                                          progress, cancel, self.profile.name)
        except imageprep.PrepCancelled:
            log.info("подготовка %s отменена", base)
            return
        except imageprep.PrepError as e:
            self._fail(image_id, str(e))
            return
        except Exception as e:  # noqa: BLE001 — сбой одного образа не останавливает очередь
            log.exception("подготовка %s", base)
            self._fail(image_id, f"{type(e).__name__}: {e}"[:200])
            return
        finally:
            self._cancel.pop(image_id, None)
            self.progress.pop(image_id, None)
        if self.db.one("SELECT state FROM os_image WHERE id = ?", (image_id,))["state"] != "working":
            self._remove_files(base)       # ссылку сменили, пока шла подготовка
            return
        self.db.execute(
            "UPDATE os_image SET state = 'ready', detail = NULL, file = ?, version = ?, source_sha256 = ?,"
            " source_size = ?, image_size = ?, image_sha256 = ?, compressed_size = ?, compressed_sha256 = ?,"
            " ready_at = ? WHERE id = ?",
            (man["file"], man["version"], man["source_sha256"], man["source_size"], man["image_size"],
             man["image_sha256"], man["compressed_size"], man["compressed_sha256"], now_iso(), image_id))
        # Новый образ становится текущим; прежние файлы варианта удаляются
        for old in self.db.all("SELECT * FROM os_image WHERE variant_id = ? AND id != ? AND state = 'ready'",
                               (row["variant_id"], image_id)):
            self.db.execute("UPDATE os_image SET state = 'replaced' WHERE id = ?", (old["id"],))
            if old["file"]:
                self._remove_files(old["file"][: -len(".img.zst")])
        self.db.execute("UPDATE os_variant SET current_image_id = ? WHERE id = ?", (image_id, row["variant_id"]))
        log.info("образ %s готов: %s, sha %s", base, man["source_file"], man["image_sha256"][:12])
        self.on_change()

    def _fail(self, image_id: int, detail: str) -> None:
        log.warning("образ %s: %s", image_id, detail)
        self.db.execute("UPDATE os_image SET state = 'error', detail = ? WHERE id = ? AND state = 'working'",
                        (detail, image_id))
        self.on_change()

    def _remove_files(self, base: str) -> None:
        for suffix in (".img.zst", ".manifest.json"):
            try:
                (self.dir / f"{base}{suffix}").unlink()
            except OSError:
                pass

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        for ev in self._cancel.values():
            ev.set()
        if self._task:
            self._task.cancel()
