"""Меню «ОС на SD-NAND» на стороне агента: образ из каталога, Zeroimage, докачка образов с сервера."""
import asyncio
import hashlib
import http.server
import json
import os
import shutil
import threading

import pytest

from napi_factory_agent.images import selfcheck
from napi_factory_agent.testing import job_params, make_test_image
from napi_factory_agent.util import MiB

from .conftest import make_agent
from .test_runner import run, run_job


def catalog_image(images, version="2026.09"):
    """Образ из меню, как его кладёт синхронизация: images/catalog/<key>-<id>.*"""
    cat = images / "catalog"
    man = make_test_image(cat, version=version, seed=7)
    base = f"armbian-{version}"
    os.replace(cat / f"napi-c-armbian-{version}.img.zst", cat / f"{base}.img.zst")
    man["file"] = f"{base}.img.zst"
    (cat / f"{base}.manifest.json").write_text(json.dumps(man))
    (cat / f"napi-c-armbian-{version}.manifest.json").unlink()
    return man, f"catalog/{base}.manifest.json"


def flash_params(mode="flash_only", **flash):
    p = job_params(mode)
    p["flash"]["provision"] = {"method": "none"}
    p["flash"].update(flash)
    return p


def test_flash_from_catalog(agent_factory, images):
    man, mf = catalog_image(images)
    a = agent_factory()
    p = flash_params(manifest_file=mf, image_version="2026.09", os="armbian", os_name="Armbian 2026.09")
    job = run(run_job(a, "flash_only", params=p))
    assert job["result"] == "pass", job
    assert job["stages"]["flash"]["detail"].startswith("Armbian 2026.09")
    with open(a.backend.root + "/dev/mmcblk0", "rb") as f:
        assert hashlib.sha256(f.read(man["image_size"])).hexdigest() == man["image_sha256"]
    fin = [e for e in a.queue.pending() if e["kind"] == "stage_finished" and e["stage"] == "flash"][0]
    assert fin["data"]["measurements"]["os"] == "armbian"


def test_catalog_image_missing_is_stand_problem(agent_factory):
    a = agent_factory()
    p = flash_params(manifest_file="catalog/armbian-9.manifest.json", image_version="9")
    job = run(run_job(a, "flash_only", params=p))
    assert job["result"] == "incomplete", job
    assert "образа нет на microSD" in job["stages"]["flash"]["detail"]


@pytest.mark.parametrize("full", [False, True])
def test_zeroimage(agent_factory, full):
    a = agent_factory()
    dev = a.backend.root + "/dev/mmcblk0"
    with open(dev, "r+b") as f:
        f.write(b"\xff" * (70 * MiB))
    p = flash_params(method="zero", zero_full=full, zero_mib=64, os="zero")
    p["stages"]["boot"] = False
    job = run(run_job(a, "flash_only", params=p))
    assert job["result"] == "pass", job
    assert job["stages"]["boot"]["state"] == "skipped"
    with open(dev, "rb") as f:
        head = f.read(70 * MiB)
    if full:
        # симулятор ограничивает объём (pattern_limit 1 МиБ), но запись начинается с нуля
        assert head[:MiB] == bytes(MiB)
        assert "вся SD-NAND" in job["stages"]["flash"]["detail"]
    else:
        assert head[:64 * MiB] == bytes(64 * MiB) and head[64 * MiB:65 * MiB] == b"\xff" * MiB
        assert "первые" in job["stages"]["flash"]["detail"]
    assert not any(m.get("type") == "serial_request" for m in a.link.sent)


def test_zeroimage_in_test_flash_uses_quick_pattern(agent_factory):
    a = agent_factory()
    p = flash_params("test_flash", method="zero", zero_full=False, zero_mib=1, os="zero")
    p["stages"]["boot"] = False
    job = run(run_job(a, "test_flash", params=p))
    assert job["result"] == "pass", job
    sd = [e for e in a.queue.pending() if e["kind"] == "stage_finished" and e["stage"] == "sdnand"][0]
    assert sd["data"]["measurements"]["pattern"] == "quick"


# ---------------------------------------------------------------- синхронизация образов

class RangeHandler(http.server.BaseHTTPRequestHandler):
    files: dict = {}
    hits: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1]
        self.hits.append((name, self.headers.get("Range"), self.headers.get("Authorization")))
        data = self.files.get(name)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            self.send_response(206)
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    RangeHandler.files = {}
    RangeHandler.hits = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def served(tmp_path, server, name="armbian-5.img.zst"):
    src = tmp_path / "src"
    man = make_test_image(src, version="2026.09", seed=3)
    data = (src / "napi-c-armbian-2026.09.img.zst").read_bytes()
    man["file"] = name
    RangeHandler.files[name] = data
    return man, data


def sync_agent(tmp_path, server):
    a = make_agent(tmp_path)
    a.link.http_base = lambda: f"http://127.0.0.1:{server.server_port}"
    return a


def test_sync_downloads_verifies_and_cleans(tmp_path, server, images):
    man, data = served(tmp_path, server)
    a = sync_agent(tmp_path, server)
    cat = images / "catalog"
    cat.mkdir(exist_ok=True)
    (cat / "old-1.img.zst").write_bytes(b"old")
    (cat / "old-1.manifest.json").write_text("{}")
    a.images.on_catalog({"sync": True, "images": [{"file": man["file"], "manifest": man}]})
    run(a.images._sync_once())
    assert a.images.status[man["file"]]["state"] == "ready"
    assert (cat / man["file"]).read_bytes() == data
    assert sorted(os.listdir(cat)) == ["armbian-5.img.zst", "armbian-5.manifest.json"]
    assert RangeHandler.hits[0][2] == "Bearer t"
    # flash-самопроверка берёт кэш, записанный при скачивании
    sc = selfcheck(str(images), "catalog/armbian-5.manifest.json",
                   a.state_dir / "selfcheck-catalog_armbian-5.manifest.json.json")
    assert sc == {"state": "pass", "detail": "cached", "version": "2026.09"}
    # после перезапуска агента образ сразу «готов» (без повторного хеширования)
    b = make_agent(tmp_path)
    assert b.images.status == {man["file"]: {"state": "ready"}}
    assert b.hello()["images"] == {man["file"]: {"state": "ready"}}
    statuses = [m for m in a.link.sent if m.get("type") == "images_status"]
    assert statuses and statuses[-1]["files"][man["file"]]["state"] == "ready"


def test_sync_resumes_partial_download(tmp_path, server, images):
    man, data = served(tmp_path, server)
    a = sync_agent(tmp_path, server)
    cat = images / "catalog"
    cat.mkdir(exist_ok=True)
    (cat / (man["file"] + ".part")).write_bytes(data[:1000])
    a.images.on_catalog({"sync": True, "images": [{"file": man["file"], "manifest": man}]})
    run(a.images._sync_once())
    assert RangeHandler.hits[-1][1] == "bytes=1000-"
    assert (cat / man["file"]).read_bytes() == data


def test_sync_bad_checksum(tmp_path, server, images):
    man, data = served(tmp_path, server)
    RangeHandler.files[man["file"]] = data[:-1] + bytes([data[-1] ^ 1])
    a = sync_agent(tmp_path, server)
    a.images.on_catalog({"sync": True, "images": [{"file": man["file"], "manifest": man}]})
    run(a.images._sync_once())
    st = a.images.status[man["file"]]
    assert st["state"] == "error" and "контрольная сумма" in st["detail"]
    assert not (images / "catalog" / man["file"]).exists()


def test_sync_waits_between_batches_and_for_job(tmp_path, server, images):
    man, data = served(tmp_path, server)
    a = sync_agent(tmp_path, server)
    a.images.on_catalog({"sync": False, "images": [{"file": man["file"], "manifest": man}]})
    run(a.images._sync_once())
    assert a.images.status[man["file"]]["state"] == "waiting" and not RangeHandler.hits
    a.state["job"] = {"job_id": "j", "finished": False, "stages": {}}
    a.images.on_catalog({"sync": True, "images": [{"file": man["file"], "manifest": man}]})
    run(a.images._sync_once())
    assert not RangeHandler.hits                     # у платы задание: не качаем


def test_sync_no_space(tmp_path, server, images, monkeypatch):
    man, data = served(tmp_path, server)
    a = sync_agent(tmp_path, server)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(1, 1, 10))
    a.images.on_catalog({"sync": True, "images": [{"file": man["file"], "manifest": man}]})
    run(a.images._sync_once())
    assert "мало места" in a.images.status[man["file"]]["detail"]


def test_job_pauses_download(tmp_path, images):
    a = make_agent(tmp_path)
    ev = a.images._cancel
    asyncio.run(a.submit({"type": "job", "job_id": "j1", "mode": "test_only", "params": job_params("test_only")}))
    assert ev.is_set()
