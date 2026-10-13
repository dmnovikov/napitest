"""Обновление агента с сервера: карты прошиваются один раз, новый код приходит между партиями.

Сервер собирает пакет агента (tar.gz) из своей копии репозитория и присылает в сообщении
images поле agent = {version, sha256, size}. version — хеш содержимого пакета (tree_hash), агент
считает его у себя тем же способом. Если версии разные, агент (между партиями, без задания):
скачивает пакет, сверяет sha256 и хеш содержимого, проверяет запуском `-m napi_factory_agent --help`,
переключает символьную ссылку napi_factory_agent -> versions/<ver> и перезапускается.

Откат: update.json рядом со ссылкой. Новая версия подтверждается на hello_ack; если до этого
агент перезапустился MAX_TRIES раз (guard() в __main__), ссылка возвращается на прежнюю версию,
а новая попадает в bad и больше не ставится.

Модуль только на stdlib: сервер берёт отсюда pack() и tree_hash() (один алгоритм на обеих сторонах).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("napi.update")

PKG = "napi_factory_agent"
MAX_TRIES = 3
RETRY_S = 600                 # повтор после ошибки скачивания/проверки
KEEP = 2                      # версий в versions/: текущая и прежняя (для отката)


# ---------------------------------------------------------------------- пакет (общий с сервером)

def _files(pkg_dir: Path) -> list[tuple[str, Path]]:
    out = []
    for p in sorted(pkg_dir.rglob("*")):
        rel = p.relative_to(pkg_dir).as_posix()
        if p.is_file() and "__pycache__" not in rel.split("/") and not rel.endswith(".pyc"):
            out.append((rel, p))
    return out


def tree_hash(pkg_dir: str | Path) -> str:
    """Версия кода: хеш путей и содержимого файлов пакета (без __pycache__)."""
    h = hashlib.sha256()
    for rel, p in _files(Path(pkg_dir)):
        data = p.read_bytes()
        h.update(f"{rel}\0{len(data)}\0".encode())
        h.update(data)
    return h.hexdigest()[:12]


def pack(pkg_dir: str | Path) -> bytes:
    """Детерминированный tar.gz пакета: одинаковый код — одинаковые байты."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, p in _files(Path(pkg_dir)):
            data = p.read_bytes()
            ti = tarfile.TarInfo(f"{PKG}/{rel}")
            ti.size, ti.mode, ti.mtime = len(data), 0o644, 0
            tar.addfile(ti, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def code_version() -> str:
    return tree_hash(Path(__file__).resolve().parent)


# ---------------------------------------------------------------------- раскладка на карте

def _root() -> Path:
    """Каталог из PYTHONPATH (/opt/napi-factory/agent): в нём ссылка napi_factory_agent и versions/."""
    return Path(os.path.abspath(__file__)).parent.parent


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _point(root: Path, target: str) -> None:
    """Атомарно направить napi_factory_agent на versions/<target>."""
    tmp = root / f".{PKG}.link"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(f"versions/{target}", tmp)
    os.replace(tmp, root / PKG)
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def guard(root: Path | None = None) -> str | None:
    """Вызывается при каждом запуске агента (__main__): счётчик попыток и откат. Возвращает
    версию, на которую откатились, или None."""
    root = root or _root()
    path = root / "update.json"
    if not path.exists():
        return None
    try:
        return _guard(root, path)
    except OSError:                     # только чтение / нет прав: агент запускается как есть
        return None


def _guard(root: Path, path: Path) -> str | None:
    st = _read(path)
    pend = st.get("pending")
    if not pend:
        return None
    pend["tries"] = int(pend.get("tries", 0)) + 1
    if pend["tries"] > MAX_TRIES and pend.get("prev") and (root / "versions" / pend["prev"]).is_dir():
        _point(root, pend["prev"])
        st.setdefault("bad", [])
        if pend["version"] not in st["bad"]:
            st["bad"].append(pend["version"])
        st["rolled_back"] = pend["version"]
        st.pop("pending", None)
        _write(path, st)
        return pend["prev"]
    st["pending"] = pend
    _write(path, st)
    return None


class UpdateError(Exception):
    pass


class Updater:
    def __init__(self, agent: Any, root: Path | None = None):
        self.agent = agent
        self.root = root or _root()
        # Обновляется только агент, установленный на карте (factory_dir/agent), не копия из репозитория
        self.enabled = root is not None or self.root == Path(agent.cfg["factory_dir"]) / "agent"
        self.version = code_version()
        self.wanted: dict | None = None
        self.status: dict = {}
        self._failed: tuple[str, float] | None = None
        self.restart = self._exec                 # в тестах подменяется

    @property
    def state_path(self) -> Path:
        return self.root / "update.json"

    def info(self) -> dict:
        st = _read(self.state_path)
        out = {"version": self.version, **self.status}
        if st.get("rolled_back"):
            out["rolled_back"] = st["rolled_back"]
        return out

    def on_catalog(self, msg: dict) -> None:
        a = msg.get("agent")
        self.wanted = a if isinstance(a, dict) and a.get("version") and a.get("sha256") else None

    def confirm(self) -> None:
        """hello_ack получен: текущая версия работает, откат больше не нужен."""
        st = _read(self.state_path)
        pend = st.get("pending")
        if pend and pend.get("version") == self.version:
            st.pop("pending", None)
            _write(self.state_path, st)
            log.info("агент %s подтверждён", self.version)
            self._cleanup()

    def needed(self) -> bool:
        w = self.wanted
        if not self.enabled or not w or w["version"] == self.version:
            return False
        if w["version"] in (_read(self.state_path).get("bad") or []):
            self.status = {"state": "error", "detail": f"версия {w['version']} откатилась, не ставится"}
            return False
        if self._failed and self._failed[0] == w["version"] and time.monotonic() - self._failed[1] < RETRY_S:
            return False
        return True

    def fail(self, detail: str) -> None:
        self.status = {"state": "error", "detail": detail}
        self._failed = ((self.wanted or {}).get("version", ""), time.monotonic())

    # ------------------------------------------------------------------ установка (в потоке)
    def fetch(self, base_url: str, token: str) -> bytes:
        w = self.wanted or {}
        url = f"{base_url}/api/v1/agent/update/{w['version']}.tar.gz"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            raise UpdateError(f"сервер: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise UpdateError(f"сервер: {getattr(e, 'reason', e)}") from e
        if hashlib.sha256(data).hexdigest() != w["sha256"]:
            raise UpdateError("контрольная сумма пакета не совпала")
        return data

    def install(self, data: bytes) -> None:
        """Распаковать, проверить, переключить ссылку. После этого нужен restart()."""
        ver = (self.wanted or {})["version"]
        root = self.root
        versions = root / "versions"
        versions.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".new-", dir=versions))
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                for m in tar.getmembers():
                    parts = Path(m.name).parts
                    if not m.isfile() or not parts or parts[0] != PKG or ".." in parts or m.name.startswith("/"):
                        raise UpdateError(f"недопустимый файл в пакете: {m.name}")
                tar.extractall(stage, filter="data")
            pkg = stage / PKG
            if tree_hash(pkg) != ver:
                raise UpdateError("содержимое пакета не совпало с версией")
            env = dict(os.environ, PYTHONPATH=str(stage))
            r = subprocess.run([sys.executable, "-m", PKG, "--help"], env=env, cwd=stage, capture_output=True,
                               text=True, timeout=120)
            if r.returncode != 0:
                tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]
                raise UpdateError(f"новая версия не запускается: {tail[0]}")
            dest = versions / ver
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(pkg, dest)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        link = root / PKG
        if link.is_symlink():
            prev = os.readlink(link).split("/")[-1]
        else:
            # Первая установка: каталог с карты переезжает в versions/ и становится прежней версией
            prev = self.version
            if (versions / prev).exists():
                shutil.rmtree(versions / prev)
            os.replace(link, versions / prev)
        st = _read(self.state_path)
        st["pending"] = {"version": ver, "prev": prev, "tries": 0}
        st.pop("rolled_back", None)
        _write(self.state_path, st)
        _point(root, ver)
        log.info("агент обновлён %s -> %s, перезапуск", prev, ver)

    def _cleanup(self) -> None:
        versions = self.root / "versions"
        try:
            cur = os.readlink(self.root / PKG).split("/")[-1]
        except OSError:
            return
        keep = {cur, (_read(self.state_path).get("pending") or {}).get("prev")}
        dirs = sorted((p for p in versions.iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for p in dirs[KEEP:]:
            if p.name not in keep:
                shutil.rmtree(p, ignore_errors=True)

    @staticmethod
    def _exec() -> None:
        logging.shutdown()
        os.execv(sys.executable, list(getattr(sys, "orig_argv", [sys.executable, "-m", PKG, *sys.argv[1:]])))
