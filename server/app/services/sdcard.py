"""Тестовая microSD: образ для скачивания (страница «microSD») и пакет агента для автообновления карт.

Образ microSD = база (стабильная тестовая Armbian, .img.zst, меняется редко) + текущий агент +
настройки стенда (agent.yaml: адрес сервера и токен). Собирается без root и без монтирования:
файлы пишутся прямо в ext4 первого раздела через debugfs. Пересборка — сама, когда меняется
агент, база или настройки; кнопкой «Обновить образ microSD» — вручную или с новой ссылкой на базу.
Пока новый образ собирается, скачивается прежний.

Пакет агента — tar.gz из server/../agent/napi_factory_agent; версия = хеш содержимого
(napi_factory_agent.selfupdate.tree_hash, тот же алгоритм, что у агента на карте).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import logging
import os
import shutil
import struct
import subprocess
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import REPO_ROOT, Profile, Settings
from ..db.database import now_iso
from . import imageprep

log = logging.getLogger("napi.sdcard")

AGENT_SRC = REPO_ROOT / "agent" / "napi_factory_agent"
AGENT_TEMPLATE = REPO_ROOT / "config" / "agent-napi-c.yaml"
UNIT = "napi-factory-agent.service"
BASE_MAX = 32 << 30                      # база microSD: не больше 32 ГиБ в распакованном виде
CHUNK = 4 << 20


class SdError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _selfupdate():
    spec = importlib.util.spec_from_file_location("napi_agent_selfupdate", AGENT_SRC / "selfupdate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # только stdlib, без импорта остального агента
    return mod


def ws_url(public_url: str) -> str:
    """http://host:port → ws://host:port/ws/agent (пусто: карта ищет сервер сама)."""
    u = (public_url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.startswith("https://"):
        return "wss://" + u[len("https://"):] + "/ws/agent"
    if u.startswith("http://"):
        return "ws://" + u[len("http://"):] + "/ws/agent"
    return u


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while buf := f.read(CHUNK):
            h.update(buf)
    return h.hexdigest()


class Debugfs:
    """Запись файлов в ext4 внутри образа диска (раздел по смещению), от обычного пользователя."""

    def __init__(self, disk: Path, offset: int):
        self.target = f"{disk}?offset={offset}"

    def run(self, cmds: list[str], write: bool = False) -> str:
        args = ["debugfs"] + (["-w"] if write else []) + ["-f", "-", self.target]
        r = subprocess.run(args, input="\n".join(cmds) + "\n", capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise SdError(f"debugfs: {r.stderr.strip()[-300:]}")
        return r.stdout

    def ls(self, path: str) -> list[tuple[str, int]] | None:
        """[(имя, mode)] каталога или None, если его нет."""
        r = subprocess.run(["debugfs", "-R", f"ls -p {path}", self.target], capture_output=True, text=True,
                           timeout=60)
        if "File not found" in r.stderr or r.returncode != 0:
            return None
        out = []
        for line in r.stdout.splitlines():
            parts = line.split("/")
            if len(parts) >= 6 and parts[5] not in (".", "..", ""):
                out.append((parts[5], int(parts[2], 8)))
        return out

    def rm_tree_cmds(self, path: str) -> list[str]:
        entries = self.ls(path)
        if entries is None:
            return []
        cmds = []
        for name, mode in entries:
            p = f"{path}/{name}"
            if mode & 0o170000 == 0o040000:
                cmds += self.rm_tree_cmds(p)
            else:
                cmds.append(f"rm {p}")
        cmds.append(f"rmdir {path}")
        return cmds

    def cat(self, path: str) -> bytes:
        r = subprocess.run(["debugfs", "-R", f"cat {path}", self.target], capture_output=True, timeout=60)
        return r.stdout

    def fsck(self) -> None:
        r = subprocess.run(["e2fsck", "-fn", self.target], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise SdError("файловая система образа с ошибками: " + (r.stdout + r.stderr).strip()[-300:])


def partition(disk: Path) -> int:
    """Смещение первого раздела Linux (MBR) в байтах."""
    with open(disk, "rb") as f:
        mbr = f.read(512)
    if mbr[510:512] != b"\x55\xaa":
        raise SdError("в базе нет таблицы разделов")
    for i in range(4):
        e = 446 + 16 * i
        if mbr[e + 4] == 0x83:
            return struct.unpack_from("<I", mbr, e + 8)[0] * 512
    raise SdError("в базе нет раздела Linux (MBR, тип 83)")


class SdCard:
    def __init__(self, settings: Settings, profile: Profile, on_change: Callable[[], None]):
        self.settings = settings
        self.profile = profile
        self.on_change = on_change
        self.dir = Path(settings.images_dir) / "sd"
        self.state_path = self.dir / "state.json"
        su = _selfupdate()
        self.agent_version = su.tree_hash(AGENT_SRC)
        self.agent_pkg = su.pack(AGENT_SRC)
        self.agent_sha = hashlib.sha256(self.agent_pkg).hexdigest()
        self.progress: dict | None = None        # {"stage", "pct", "text"} пока идёт работа
        self.error: str | None = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._pending_url: tuple[str, str | None] | None = None
        self._force = False

    # ------------------------------------------------------------------ состояние
    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, st: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
        os.replace(tmp, self.state_path)

    def agent_yaml(self) -> str:
        text = AGENT_TEMPLATE.read_text()
        lines = []
        for line in text.splitlines():
            if line.startswith("server_url:"):
                url = ws_url(self.settings.public_url)
                line = f'server_url: "{url}"' + ("" if url else "   # пусто: сервер ищется сам (UDP, затем mDNS)")
            elif line.startswith("token:"):
                line = f'token: "{self.settings.agent_token}"'
            lines.append(line)
        return "\n".join(lines) + "\n"

    def unit(self) -> str:
        return (AGENT_SRC / "systemd" / UNIT).read_text()

    def inputs_key(self, base: dict | None) -> str:
        h = hashlib.sha256()
        for part in ((base or {}).get("sha256", ""), self.agent_version, self.agent_yaml(), self.unit()):
            h.update(part.encode() + b"\0")
        return h.hexdigest()[:16]

    def agent_info(self) -> dict:
        return {"version": self.agent_version, "sha256": self.agent_sha, "size": len(self.agent_pkg)}

    def view(self) -> dict:
        st = self._state()
        img = st.get("image")
        base = st.get("base")
        stale = bool(img) and img.get("key") != self.inputs_key(base)
        return {
            "image": img and {k: v for k, v in img.items() if k != "key"},
            "base": base,
            "agent_version": self.agent_version,
            "server_url": ws_url(self.settings.public_url) or None,
            "progress": self.progress,
            "error": self.error,
            "stale": stale,
        }

    def image_path(self) -> Path | None:
        img = self._state().get("image")
        if not img:
            return None
        p = self.dir / img["file"]
        return p if p.is_file() else None

    # ------------------------------------------------------------------ команды
    def request(self, url: str | None, operator: str | None) -> None:
        """«Обновить образ microSD»: новая база по ссылке (если задана) и пересборка."""
        if url:
            try:
                url = imageprep.check_url(url)
            except imageprep.PrepError as e:
                raise SdError(str(e)) from e
            self._pending_url = (url, operator)
        if self.progress is not None and not url:
            raise SdError("образ уже собирается", 409)
        self._force = True
        self._cancel.set()
        self.error = None
        self._kick()

    def _kick(self) -> None:
        if self._wake is not None:
            self._wake.set()

    # ------------------------------------------------------------------ цикл
    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._cancel.set()
        if self._task:
            self._task.cancel()

    async def run(self) -> None:
        self._wake = asyncio.Event()
        self._wake.set()
        while True:
            await self._wake.wait()
            self._wake.clear()
            self._cancel = threading.Event()
            try:
                await asyncio.to_thread(self._work, self._cancel)
                self.error = None
            except imageprep.PrepCancelled:
                pass
            except (SdError, imageprep.PrepError, OSError, subprocess.SubprocessError) as e:
                log.warning("образ microSD: %s", e)
                self.error = str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("образ microSD")
                self.error = f"{type(e).__name__}: {e}"[:300]
            finally:
                self.progress = None
                self.on_change()

    def _progress(self, stage: str, pct: float | None, text: str) -> None:
        old = self.progress
        self.progress = {"stage": stage, "pct": None if pct is None else round(pct, 1), "text": text}
        if not old or old.get("stage") != stage or pct is None or abs((old.get("pct") or 0) - pct) >= 1:
            self.on_change()

    def _work(self, cancel: threading.Event) -> None:
        st = self._state()
        if self._pending_url:
            url, op = self._pending_url
            self._pending_url = None
            st["base"] = self._fetch_base(url, op, cancel)
            self._save(st)
        elif not st.get("base") and self.settings.sd_base:
            st["base"] = self._seed_base(self.settings.sd_base, cancel)
            self._save(st)
        base = st.get("base")
        if not base:
            raise SdError("не задана база образа microSD (NAPI_SD_BASE или «Обновить образ microSD» со ссылкой)")
        key = self.inputs_key(base)
        img = st.get("image")
        if img and img.get("key") == key and (self.dir / img["file"]).is_file() and not self._force:
            return
        self._force = False
        st["image"] = self._build(base, key, cancel)
        self._save(st)
        for p in self.dir.glob("napi-test-sd-*.img.xz"):
            if p.name != st["image"]["file"]:
                p.unlink(missing_ok=True)
        log.info("образ microSD готов: %s", st["image"]["file"])

    # ------------------------------------------------------------------ база
    def _seed_base(self, src: str, cancel: threading.Event) -> dict:
        if src.startswith(("http://", "https://")):
            return self._fetch_base(src, None, cancel)
        p = Path(src)
        if not p.is_file() or not p.name.endswith(".img.zst"):
            raise SdError(f"NAPI_SD_BASE: нет файла {src} (*.img.zst)")
        self._progress("base", None, "проверка базы")
        dest = self.dir / "base" / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copyfile(p, dest)
        return {"file": f"base/{p.name}", "source": p.name, "url": None, "sha256": _sha256(dest),
                "size": dest.stat().st_size, "set_at": now_iso(), "operator": None}

    def _fetch_base(self, url: str, operator: str | None, cancel: threading.Event) -> dict:
        n = int(time.time())

        def progress(phase: str, pct: float, text: str) -> None:
            self._progress("base", pct, f"база: {text}")

        man = imageprep.prepare(url, self.dir / "base", f"base-{n}", BASE_MAX, progress, cancel, "microsd")
        old = (self._state().get("base") or {}).get("file")
        if old:
            (self.dir / old).unlink(missing_ok=True)
            (self.dir / old.replace(".img.zst", ".manifest.json")).unlink(missing_ok=True)
        return {"file": f"base/{man['file']}", "source": man.get("source_file"), "url": url,
                "sha256": man["compressed_sha256"], "size": man["compressed_size"],
                "version": man.get("version"), "set_at": now_iso(), "operator": operator}

    # ------------------------------------------------------------------ сборка
    def _build(self, base: dict, key: str, cancel: threading.Event) -> dict:
        work = self.dir / ".work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            return self._build_in(work, base, key, cancel)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _check(self, cancel: threading.Event) -> None:
        if cancel.is_set():
            raise imageprep.PrepCancelled()

    def _build_in(self, work: Path, base: dict, key: str, cancel: threading.Event) -> dict:
        src = self.dir / base["file"]
        if not src.is_file():
            raise SdError(f"нет файла базы {base['file']}")
        disk = work / "disk.img"
        total = base.get("size") or src.stat().st_size
        self._progress("unpack", 0, "распаковка базы")
        with open(src, "rb") as fin, open(disk, "wb") as fout:
            p = subprocess.Popen(["zstd", "-dc", "-q"], stdin=subprocess.PIPE, stdout=fout)
            done = 0
            try:
                while buf := fin.read(CHUNK):
                    self._check(cancel)
                    p.stdin.write(buf)
                    done += len(buf)
                    self._progress("unpack", 25 * done / max(total, 1), "распаковка базы")
            finally:
                p.stdin.close()
                p.wait()
            if p.returncode != 0:
                raise SdError("база не распаковывается (zstd)")

        fs = Debugfs(disk, partition(disk))
        self._progress("write", 26, "запись агента и настроек")
        fs.fsck()
        agent_dir = work / "agent"
        with tarfile.open(fileobj=io.BytesIO(self.agent_pkg), mode="r:gz") as tar:
            tar.extractall(agent_dir, filter="data")
        sd_version = f"{datetime.now(timezone.utc):%Y.%m.%d}+{self.agent_version[:7]}"
        files = {
            "/etc/napi-factory/agent.yaml": (self.agent_yaml().encode(), 0o600),
            "/opt/napi-factory/VERSION": (sd_version.encode() + b"\n", 0o644),
            f"/etc/systemd/system/{UNIT}": (self.unit().encode(), 0o644),
        }
        cmds = fs.rm_tree_cmds("/opt/napi-factory/agent")
        for path in files:
            cmds.append(f"rm {path}")
        cmds.append(f"rm /etc/systemd/system/multi-user.target.wants/{UNIT}")
        for d in ("/opt", "/opt/napi-factory", "/opt/napi-factory/images", "/etc", "/etc/napi-factory",
                  "/etc/systemd", "/etc/systemd/system", "/etc/systemd/system/multi-user.target.wants",
                  "/var", "/var/lib", "/var/lib/napi-factory", "/opt/napi-factory/agent"):
            if d == "/opt/napi-factory/agent" or fs.ls(d) is None:
                cmds += [f"mkdir {d}", f"sif {d} mode 040755", f"sif {d} uid 0", f"sif {d} gid 0"]

        def put(local: Path, dest: str, mode: int) -> None:
            cmds.extend([f"write {local} {dest}", f"sif {dest} mode 0{0o100000 | mode:o}",
                         f"sif {dest} uid 0", f"sif {dest} gid 0"])

        for d in sorted(p for p in agent_dir.rglob("*") if p.is_dir()):
            dest = "/opt/napi-factory/agent/" + d.relative_to(agent_dir).as_posix()
            cmds += [f"mkdir {dest}", f"sif {dest} mode 040755", f"sif {dest} uid 0", f"sif {dest} gid 0"]
        for f in sorted(p for p in agent_dir.rglob("*") if p.is_file()):
            put(f, "/opt/napi-factory/agent/" + f.relative_to(agent_dir).as_posix(), 0o644)
        for i, (path, (data, mode)) in enumerate(files.items()):
            local = work / f"file{i}"
            local.write_bytes(data)
            put(local, path, mode)
        cmds.append(f"symlink /etc/systemd/system/multi-user.target.wants/{UNIT} /etc/systemd/system/{UNIT}")
        fs.run(cmds, write=True)
        fs.fsck()
        # Проверка: всё записалось как задумано
        for path, (data, _) in files.items():
            if fs.cat(path) != data:
                raise SdError(f"не записался {path}")
        for f in (p for p in agent_dir.rglob("*") if p.is_file()):
            if fs.cat("/opt/napi-factory/agent/" + f.relative_to(agent_dir).as_posix()) != f.read_bytes():
                raise SdError(f"не записался агент: {f.name}")
        self._check(cancel)

        name = f"napi-test-sd-{sd_version.replace('+', '-')}.img.xz"
        part = self.dir / (name + ".part")
        size = disk.stat().st_size
        self._progress("compress", 30, "сжатие образа")
        h = hashlib.sha256()
        with open(disk, "rb") as fin, open(part, "wb") as fout:
            p = subprocess.Popen(["xz", "-T0", "-3", "-c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

            def feed() -> None:
                done = 0
                try:
                    while buf := fin.read(CHUNK):
                        if cancel.is_set():
                            break
                        p.stdin.write(buf)
                        done += len(buf)
                        self._progress("compress", 30 + 69 * done / max(size, 1), "сжатие образа")
                finally:
                    p.stdin.close()

            t = threading.Thread(target=feed, daemon=True)
            t.start()
            while buf := p.stdout.read(CHUNK):
                fout.write(buf)
                h.update(buf)
            t.join()
            p.wait()
        if cancel.is_set():
            part.unlink(missing_ok=True)
            raise imageprep.PrepCancelled()
        if p.returncode != 0:
            part.unlink(missing_ok=True)
            raise SdError("xz: ошибка сжатия")
        os.replace(part, self.dir / name)
        return {"file": name, "sha256": h.hexdigest(), "size": (self.dir / name).stat().st_size,
                "image_size": size, "version": sd_version, "agent_version": self.agent_version,
                "base_sha256": base["sha256"], "base_source": base.get("source"),
                "server_url": ws_url(self.settings.public_url) or None, "built_at": now_iso(), "key": key}

    def agent_view(self, cards: list[dict[str, Any]]) -> dict:
        """Версии агента на картах на связи: {current, online, old: [...], errors: [...]}."""
        cur = [c for c in cards if (c.get("agent_version") or "").endswith("+" + self.agent_version)]
        errors = [{"no": c.get("no"), "detail": (c.get("update") or {}).get("detail")}
                  for c in cards if (c.get("update") or {}).get("state") == "error"]
        return {"version": self.agent_version, "current": len(cur), "online": len(cards), "errors": errors}
