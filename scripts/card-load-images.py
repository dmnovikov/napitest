#!/usr/bin/env python3
"""Образы ОС на тестовую microSD напрямую с сервера стенда, через кардридер (без сети плат).

Кладёт на карту текущие образы из каталога стенда так же, как это делает агент: файл, манифест и
отметку «проверен», чтобы после загрузки платы агент ничего не скачивал заново.

    sudo python3 card-load-images.py            # найти карту в кардридере и записать
    sudo python3 card-load-images.py --check    # только показать, что на карте
    sudo python3 card-load-images.py /dev/sdb1  # явно указать раздел карты

Со страницы «microSD» стенда скрипт запускает systemd (napi-card-reader.path → .service, от root): сервер
кладёт запрос {"action": "check" | "load"} в файл, ход работы и итог скрипт пишет в файл состояния
(--request, --status). Из запроса берётся только действие: путей и команд в нём нет.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DB = os.environ.get("NAPI_DB_PATH", "/var/lib/napi-factory/factory.db")
IMAGES = Path(os.environ.get("NAPI_IMAGES_DIR", "/var/lib/napi-factory/images"))
CARD_CATALOG = "opt/napi-factory/images/catalog"
CARD_STATE = "var/lib/napi-factory"
RESERVE = 256 << 20
CHUNK = 4 << 20
AGENT_SRC = Path(__file__).resolve().parent.parent / "agent" / "napi_factory_agent"


class Fail(Exception):
    pass


class Status:
    """Файл состояния для страницы «microSD» (без --status ничего не пишет)."""

    def __init__(self, path: str | None, action: str):
        self.path = Path(path) if path else None
        self.data: dict = {"state": "working", "action": action, "started": time.time(), "cards": [],
                           "progress": None, "error": None, "log": []}
        self._last = 0.0
        self.write()

    def write(self) -> None:
        if self.path is None:
            return
        self.data["ts"] = time.time()
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False))
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)

    def progress(self, file: str, pct: float, text: str) -> None:
        self.data["progress"] = {"file": file, "pct": round(pct, 1), "text": text}
        if time.monotonic() - self._last >= 1:
            self._last = time.monotonic()
            self.write()

    def say(self, line: str) -> None:
        print(line, flush=True)
        self.data["log"] = (self.data["log"] + [line.strip()])[-40:]
        self.write()

    def finish(self, error: str | None = None) -> None:
        self.data.update(state="error" if error else "done", error=error, progress=None)
        self.write()


STATUS: Status = Status(None, "load")


def die(msg: str) -> None:
    raise Fail(msg)


def wanted() -> list[dict]:
    """Те же образы и манифесты, что сервер рассылает агентам (Catalog.agent_catalog)."""
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT i.* FROM os_variant v JOIN os_image i ON i.id = v.current_image_id"
        " WHERE i.state = 'ready' AND (v.visible = 1 OR v.kind = 'custom') ORDER BY v.sort, v.id").fetchall()
    out = []
    for r in rows:
        base = r["file"][: -len(".img.zst")]
        profile = json.loads((IMAGES / f"{base}.manifest.json").read_text())["profile"]
        out.append({"version": r["version"], "profile": profile, "file": r["file"],
                    "compressed_size": r["compressed_size"], "compressed_sha256": r["compressed_sha256"],
                    "image_size": r["image_size"], "image_sha256": r["image_sha256"],
                    "source_url": r["url"], "source_file": r["source_file"]})
    return out


def usb_partitions() -> list[str]:
    out = subprocess.run(["lsblk", "-J", "-o", "NAME,TYPE,TRAN,RM,FSTYPE,MOUNTPOINT,SIZE"],
                         capture_output=True, text=True, check=True).stdout
    parts = []
    for disk in json.loads(out)["blockdevices"]:
        if disk.get("type") != "disk" or not (disk.get("tran") == "usb" or disk.get("rm") in (True, "1", 1)):
            continue
        for p in disk.get("children") or []:
            if p.get("fstype") == "ext4" and not p.get("mountpoint"):
                parts.append("/dev/" + p["name"])
    return parts


def is_card(root: Path) -> bool:
    return (root / "opt/napi-factory/agent").is_dir() and (root / "etc/napi-factory/agent.yaml").is_file()


def sha256(path: Path, name: str = "") -> str:
    h = hashlib.sha256()
    size, done = max(path.stat().st_size, 1), 0
    with open(path, "rb") as f:
        while buf := f.read(CHUNK):
            h.update(buf)
            done += len(buf)
            if name:
                STATUS.progress(name, 100 * done / size, "сверка записанного")
    return h.hexdigest()


def card_view(root: Path, images: list[dict]) -> list[dict]:
    cat = root / CARD_CATALOG
    out = []
    for man in images:
        f, part = cat / man["file"], cat / (man["file"] + ".part")
        item = {"file": man["file"], "source_file": man["source_file"], "version": man["version"],
                "size": int(man["compressed_size"])}
        if ready(root, man):
            item["state"] = "ready"
        elif part.exists():
            item.update(state="partial", have=part.stat().st_size)
        elif f.exists():
            item["state"] = "unverified"
        else:
            item["state"] = "none"
        out.append(item)
    return out


def fix_system(root: Path) -> None:
    """Заодно: исправленный юнит первого запуска (без него SSH на карте поднимается не при каждой загрузке)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("napi_cardfix", AGENT_SRC / "cardfix.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if mod.fix_unit(root):
            STATUS.say("  исправлен юнит napi-sd-firstboot (запуск SSH)")
    except (OSError, AttributeError, ImportError) as e:
        print(f"  юнит napi-sd-firstboot не проверен: {e}")


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def cache_path(root: Path, file: str) -> Path:
    base = file[: -len(".img.zst")]
    return root / CARD_STATE / f"selfcheck-catalog_{base}.manifest.json.json"


def key_of(path: Path, man: dict) -> str:
    st = path.stat()
    return f"{man['file']}:{st.st_size}:{int(st.st_mtime)}:{man['compressed_sha256']}"


def ready(root: Path, man: dict) -> bool:
    path = root / CARD_CATALOG / man["file"]
    try:
        if path.stat().st_size != int(man["compressed_size"]):
            return False
        cache = json.loads(cache_path(root, man["file"]).read_text())
    except (OSError, ValueError):
        return False
    return cache.get("key") == key_of(path, man) and cache.get("state") == "pass"


def show(root: Path, images: list[dict]) -> None:
    cat = root / CARD_CATALOG
    for man in images:
        f, part = cat / man["file"], cat / (man["file"] + ".part")
        if ready(root, man):
            state = "готов"
        elif part.exists():
            state = f"скачано {part.stat().st_size >> 20} из {man['compressed_size'] >> 20} МБ"
        elif f.exists():
            state = "файл есть, но не отмечен как проверенный"
        else:
            state = "нет"
        STATUS.say(f"  {man['file']} ({man['source_file']}, версия {man['version']}): {state}")


def load(root: Path, images: list[dict]) -> None:
    cat = root / CARD_CATALOG
    cat.mkdir(parents=True, exist_ok=True)
    (root / CARD_STATE).mkdir(parents=True, exist_ok=True)
    keep = set()
    for man in images:
        base = man["file"][: -len(".img.zst")]
        keep |= {man["file"], f"{base}.manifest.json"}
    for n in sorted(os.listdir(cat)):                       # старые образы и недокачанные куски
        if n not in keep and n.endswith((".img.zst", ".manifest.json", ".part")):
            (cat / n).unlink()
            STATUS.say(f"  удалён {n}")
    for man in images:
        dst = cat / man["file"]
        if ready(root, man):
            STATUS.say(f"  {man['file']}: уже на карте")
            continue
        src = IMAGES / man["file"]
        need = int(man["compressed_size"])
        if src.stat().st_size != need:
            die(f"{src}: размер не совпадает с каталогом стенда")
        have = dst.stat().st_size if dst.exists() else 0
        free = shutil.disk_usage(cat).free + have
        if free < need + RESERVE:
            die(f"мало места на карте: нужно {need >> 20} МБ, свободно {free >> 20} МБ")
        t = time.monotonic()
        STATUS.say(f"  {man['file']}: запись {need >> 20} МБ…")
        tmp = cat / (man["file"] + ".copy")
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            done = 0
            while buf := fi.read(CHUNK):
                fo.write(buf)
                done += len(buf)
                if done % (32 << 20) < CHUNK:
                    os.fsync(fo.fileno())           # честный процент: данные на карте, а не в памяти
                STATUS.progress(man["file"], 100 * done / max(need, 1), "запись на карту")
            fo.flush()
            os.fsync(fo.fileno())
        subprocess.run(["sync"], check=False)
        with open("/proc/sys/vm/drop_caches", "w") as f:    # сверка читает с карты, а не из памяти
            f.write("1")
        if sha256(tmp, man["file"]) != man["compressed_sha256"]:
            tmp.unlink()
            die(f"{man['file']}: контрольная сумма после записи не совпала — карта или кардридер неисправны")
        os.replace(tmp, dst)
        base = man["file"][: -len(".img.zst")]
        write_json(cat / f"{base}.manifest.json", man)
        write_json(cache_path(root, man["file"]),
                   {"key": key_of(dst, man), "state": "pass", "detail": "verified", "version": man.get("version")})
        STATUS.say(f"  {man['file']}: записан и сверен за {time.monotonic() - t:.0f} с")


def read_request(path: str) -> str:
    """Действие из файла запроса сервера. Файл чужой (пишет сервис стенда): читается без перехода по ссылкам."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            action = json.loads(f.read(4096)).get("action")
    except (OSError, ValueError, AttributeError):
        return "check"
    return action if action in ("check", "load") else "check"


def work(args, check: bool) -> None:
    if os.geteuid() != 0:
        die("нужен root: sudo python3 card-load-images.py")
    images = wanted()
    if not images:
        die("в каталоге стенда нет готовых образов")
    parts = [args.partition] if args.partition else usb_partitions()
    if not parts:
        die("карта в кардридере не найдена (нет USB-диска с разделом ext4)")
    found = 0
    for dev in parts:
        mnt = tempfile.mkdtemp(prefix="napi-card-")
        if subprocess.run(["mount", dev, mnt]).returncode != 0:
            os.rmdir(mnt)
            STATUS.say(f"{dev}: не монтируется, пропущен")
            continue
        try:
            root = Path(mnt)
            if not is_card(root):
                STATUS.say(f"{dev}: это не тестовая microSD стенда, пропущен")
                continue
            found += 1
            STATUS.say(f"{dev}: тестовая microSD")
            card = {"dev": dev, "images": card_view(root, images)}
            try:
                card["version"] = (root / "opt/napi-factory/VERSION").read_text().strip()
            except OSError:
                pass
            STATUS.data["cards"].append(card)
            if not check:
                fix_system(root)
                load(root, images)
            show(root, images)
            card["images"] = card_view(root, images)
            STATUS.write()
        finally:
            subprocess.run(["sync"], check=False)
            subprocess.run(["umount", mnt], check=False)
            os.rmdir(mnt)
    if not found:
        die("тестовая microSD стенда не найдена")
    STATUS.say("Готово: карту можно вынимать." if not check else "Карта не изменена.")


def main() -> None:
    global STATUS
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("partition", nargs="?", help="раздел карты, например /dev/sdb1 (по умолчанию ищется сам)")
    ap.add_argument("--check", action="store_true", help="только показать состояние карты")
    ap.add_argument("--request", help="файл запроса сервера стенда (действие берётся из него)")
    ap.add_argument("--status", help="файл состояния для страницы «microSD»")
    args = ap.parse_args()
    check = args.check
    if args.request:
        check = read_request(args.request) != "load"
    STATUS = Status(args.status, "check" if check else "load")
    try:
        work(args, check)
    except Fail as e:
        print("ОШИБКА: " + str(e), file=sys.stderr)
        STATUS.finish(str(e))
        sys.exit(0 if args.status else 1)      # для systemd это не сбой юнита: итог в файле состояния
    except Exception as e:  # noqa: BLE001 — страница должна увидеть любой сбой
        STATUS.finish(f"{type(e).__name__}: {e}")
        raise
    STATUS.finish()


if __name__ == "__main__":
    main()
