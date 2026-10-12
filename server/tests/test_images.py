"""Меню «ОС на SD-NAND»: подготовка образа по ссылке, пароль, раздача на карты, выбор при запуске."""
import functools
import hashlib
import http.server
import json
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import imageprep

from conftest import TOKEN, Agent, close_agents, make_settings, wait_for


def disk_image(size=1 << 20, seed=1) -> bytes:
    data = bytearray(hashlib.sha256(str(seed).encode()).digest() * (size // 32))
    data[510:512] = b"\x55\xaa"
    data[512:520] = b"EFI PART"
    return bytes(data)


@pytest.fixture
def web(tmp_path):
    """Сайт с образами (как download.napilinux.ru)."""
    root = tmp_path / "www"
    root.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def put(name, data: bytes, xz=True):
        if xz:
            data = subprocess.run(["xz", "-c", "-0"], input=data, capture_output=True, check=True).stdout
        (root / name).write_bytes(data)
        return f"http://127.0.0.1:{srv.server_port}/{name}"
    put.root = root
    yield put
    srv.shutdown()


def make_client(tmp_path, **kw):
    app = create_app(make_settings(tmp_path, **kw))
    c = TestClient(app)
    c.__enter__()
    assert c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret-pass"}).status_code == 200
    return c


def variant(c, key):
    return next(v for v in c.get("/api/v1/images").json()["variants"] if v["key"] == key)


def wait_ready(c, key, timeout=20):
    return wait_for(lambda: (variant(c, key)["current"] or {}).get("state") == "ready" and variant(c, key),
                    timeout)


def test_prepare_from_url_and_serve_to_cards(tmp_path, web):
    img = disk_image()
    url = web("Armbian_2026.09_Napi-c.img.xz", img)
    c = make_client(tmp_path, os_images=[{"key": "armbian", "name": "Armbian", "url": url}])
    try:
        v = wait_ready(c, "armbian")
        cur = v["current"]
        assert cur["image_sha256"] == hashlib.sha256(img).hexdigest()
        assert cur["image_size"] == len(img) and cur["version"] == "2026.09"
        assert cur["source_file"] == "Armbian_2026.09_Napi-c.img.xz"
        menu = c.get("/api/v1/images").json()["menu"]
        assert [m["key"] for m in menu] == ["armbian", "zero", "sd", "custom"]   # по умолчанию — первый
        # .zst распаковывается в тот же образ
        zst = tmp_path / "images" / cur["file"]
        assert subprocess.run(["zstd", "-dc", str(zst)], capture_output=True).stdout == img
        # карта получает каталог и скачивает файл по токену агента, с докачкой
        a = Agent(c, "CPU1")
        cat = a.recv("images")
        assert cat["sync"] is True and cat["images"][0]["file"] == cur["file"]
        assert cat["images"][0]["manifest"]["compressed_sha256"] == hashlib.sha256(zst.read_bytes()).hexdigest()
        path = f"/api/v1/agent/images/{cur['file']}"
        assert c.get(path).status_code == 401
        r = c.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200 and r.content == zst.read_bytes()
        r = c.get(path, headers={"Authorization": f"Bearer {TOKEN}", "Range": "bytes=10-"})
        assert r.status_code == 206 and r.content == zst.read_bytes()[10:]
        assert c.get("/api/v1/agent/images/..%2Ffactory.db",
                     headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404
    finally:
        close_agents()
        c.__exit__(None, None, None)


@pytest.mark.parametrize("kind,why", [("zeros", "нет таблицы разделов"), ("garbage", "не распаковывается")])
def test_bad_image_rejected(tmp_path, web, kind, why):
    url = web("bad.img.xz", b"\0" * (1 << 20)) if kind == "zeros" else web("bad.img.xz", b"not xz", xz=False)
    c = make_client(tmp_path, os_images=[{"key": "armbian", "name": "Armbian", "url": url}])
    try:
        nxt = wait_for(lambda: (variant(c, "armbian")["next"] or {}).get("state") == "error"
                       and variant(c, "armbian")["next"])
        assert why in nxt["detail"]
        assert variant(c, "armbian")["current"] is None
    finally:
        c.__exit__(None, None, None)


def test_sha256sums_mismatch(tmp_path, web):
    url = web("a-1.0.img.xz", disk_image())
    (web.root / "SHA256SUMS").write_text("0" * 64 + "  a-1.0.img.xz\n")
    c = make_client(tmp_path, os_images=[{"key": "armbian", "name": "Armbian", "url": url}])
    try:
        nxt = wait_for(lambda: (variant(c, "armbian")["next"] or {}).get("state") == "error"
                       and variant(c, "armbian")["next"])
        assert "SHA256SUMS" in nxt["detail"]
    finally:
        c.__exit__(None, None, None)


def test_too_big_image(tmp_path, web):
    url = web("big-1.0.img.xz", disk_image(2 << 20))
    with pytest.raises(imageprep.PrepError, match="больше SD-NAND"):
        imageprep.prepare(url, tmp_path / "out", "x", 1 << 20, lambda *a: None, threading.Event())
    assert not list((tmp_path / "out").iterdir())          # рабочие файлы убраны


def test_change_url_needs_password_and_keeps_old_until_ready(tmp_path, web):
    url1 = web("os-1.0.img.xz", disk_image(seed=1))
    c = make_client(tmp_path, os_images=[{"key": "armbian", "name": "Armbian", "url": url1}])
    try:
        old = wait_ready(c, "armbian")["current"]
        url2 = web("os-2.0.img.xz", disk_image(seed=2))
        r = c.put("/api/v1/images/armbian", json={"url": url2, "password": "wrong"})
        assert r.status_code == 403
        assert variant(c, "armbian")["next"] is None
        r = c.put("/api/v1/images/armbian", json={"url": url2, "password": "secret-pass"})
        assert r.status_code == 200
        v = variant(c, "armbian")
        assert v["current"]["id"] == old["id"]               # старый работает, пока новый не готов
        v = wait_for(lambda: variant(c, "armbian")["current"]["version"] == "2.0" and variant(c, "armbian"))
        assert v["current"]["url"] == url2
        assert not (tmp_path / "images" / old["file"]).exists()   # прежний файл удалён
        audit = c.get("/api/v1/audit").json()
        assert any(a["action"] == "password_failed" for a in audit)
        edit = next(a for a in audit if a["action"] == "image_edit")
        assert edit["detail"]["old_url"] == url1 and "password" not in edit["detail"]
        hist = c.get("/api/v1/images").json()["history"]
        assert [h["state"] for h in hist[:2]] == ["ready", "replaced"]
    finally:
        c.__exit__(None, None, None)


def test_add_hide_and_custom(tmp_path, web):
    c = make_client(tmp_path)
    try:
        url = web("debian-12.img.xz", disk_image())
        r = c.post("/api/v1/images", json={"name": "Debian", "url": url, "password": "secret-pass"})
        assert r.status_code == 200
        key = r.json()["key"]
        assert key == "debian"
        assert c.post("/api/v1/images", json={"name": "X", "url": "ftp://x/y.img", "password": "secret-pass"}
                      ).status_code == 400
        wait_ready(c, key)
        assert c.put(f"/api/v1/images/{key}", json={"visible": False, "password": "secret-pass"}).status_code == 200
        menu = [m["key"] for m in c.get("/api/v1/images").json()["menu"]]
        assert key not in menu and menu[0] == "zero"
        # Custom: одноразовый, по ссылке, с паролем; в списке «Образы» как отдельный слот
        assert c.post("/api/v1/images/custom", json={"url": url, "password": "x"}).status_code == 403
        assert c.post("/api/v1/images/custom", json={"url": url, "password": "secret-pass"}).status_code == 200
        v = wait_ready(c, "custom")
        assert v["kind"] == "custom"
        a = Agent(c, "CPU1")
        files = [i["file"] for i in a.recv("images")["images"]]
        assert files == [v["current"]["file"]]               # скрытый вариант с карт убирается
    finally:
        close_agents()
        c.__exit__(None, None, None)


def test_start_batch_with_os(tmp_path, web):
    url = web("Armbian_2026.09.img.xz", disk_image())
    c = make_client(tmp_path, os_images=[{"key": "armbian", "name": "Armbian", "url": url}])
    try:
        cur = wait_ready(c, "armbian")["current"]
        a = Agent(c, "CPU1")
        dev = a.ack["device_id"]
        body = {"mode": "flash_only", "device_ids": [dev], "confirm": True, "options": {"os": "armbian"}}
        r = c.post("/api/v1/batches", json=body)
        assert r.status_code == 409 and "ещё не на картах" in r.json()["detail"]
        a.ws.send_json({"type": "images_status", "files": {cur["file"]: {"state": "ready"}}})
        wait_for(lambda: variant(c, "armbian")["cards"]["ready"] == 1)
        r = c.post("/api/v1/batches", json=body)
        assert r.status_code == 200, r.text
        while True:                                          # партия идёт: карты не качают
            msg = a.recv()
            if msg["type"] == "job":
                job = msg
                break
            if msg["type"] == "images":
                sync = msg["sync"]
        assert sync is False
        fl = job["params"]["flash"]
        assert fl["manifest_file"] == f"catalog/{cur['file'][:-8]}.manifest.json"
        assert fl["os"] == "armbian" and fl["os_name"] == "Armbian 2026.09" and fl["image_version"] == "2026.09"
        detail = c.get(f"/api/v1/jobs/{job['job_id']}").json()
        assert detail["os_name"] == "Armbian 2026.09" and detail["image_url"] == url
        assert detail["image_file"] == "Armbian_2026.09.img.xz"
        csv = c.get(f"/api/v1/batches/{r.json()['batch_id']}/export.csv").text
        assert "Armbian 2026.09" in csv and url in csv
    finally:
        close_agents()
        c.__exit__(None, None, None)


def test_zeroimage_params(tmp_path):
    c = make_client(tmp_path)
    try:
        a = Agent(c, "CPU1")
        r = c.post("/api/v1/batches", json={"mode": "test_flash", "device_ids": [a.ack["device_id"]],
                                            "confirm": True, "options": {"os": "zero", "zero": "full"}})
        assert r.status_code == 200, r.text
        p = a.recv("job")["params"]
        assert p["flash"]["method"] == "zero" and p["flash"]["zero_full"] is True
        assert p["stages"]["boot"] is False and p["sdnand"]["pattern"] == "quick"
        assert p["flash"]["os_name"] == "Zeroimage (полная)"
    finally:
        close_agents()
        c.__exit__(None, None, None)


def test_default_os_is_sd_image_for_api_clients(client):
    a = Agent(client, "CPU1")
    r = client.post("/api/v1/batches", json={"mode": "flash_only", "device_ids": [a.ack["device_id"]],
                                             "confirm": True})
    assert r.status_code == 200
    fl = a.recv("job")["params"]["flash"]
    assert fl["os"] == "sd" and fl["manifest_file"].endswith(".manifest.json") and "/" not in fl["manifest_file"]
    assert json.dumps(fl, ensure_ascii=False).count("Образ на microSD") == 1
