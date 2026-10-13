"""Тестовая microSD: сборка образа для скачивания, пакет агента для автообновления, меню без встроенного образа."""
import hashlib
import io
import lzma
import struct
import subprocess
import tarfile

from conftest import TOKEN, Agent, close_agents, wait_for
from test_images import make_client

from app.services import sdcard


def make_base(tmp_path) -> str:
    """Маленькая «Armbian»: MBR + раздел ext4 с 1 МиБ, как у настоящей базы (там 16 МиБ)."""
    fs = tmp_path / "fs.img"
    subprocess.run(["truncate", "-s", "48M", str(fs)], check=True)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-L", "armbi_root", str(fs)], check=True)
    head = bytearray(1 << 20)
    head[446 + 4] = 0x83
    struct.pack_into("<II", head, 446 + 8, 2048, fs.stat().st_size // 512)
    head[510:512] = b"\x55\xaa"
    disk = tmp_path / "base.img"
    disk.write_bytes(bytes(head) + fs.read_bytes())
    out = tmp_path / "napi-sd-base-1.img.zst"
    subprocess.run(["zstd", "-q", "-f", str(disk), "-o", str(out)], check=True)
    return str(out)


def sd_view(c):
    return c.get("/api/v1/sdcard").json()


def unpacked(c, tmp_path):
    r = c.get("/api/v1/sdcard/download")
    assert r.status_code == 200
    disk = tmp_path / "got.img"
    disk.write_bytes(lzma.decompress(r.content))
    return r, sdcard.Debugfs(disk, sdcard.partition(disk))


def test_sd_image_build_download_and_rebuild(tmp_path):
    c = make_client(tmp_path, sd_base=make_base(tmp_path), public_url="http://192.168.16.100:8080")
    try:
        v = wait_for(lambda: (sd_view(c)["image"] or None) and sd_view(c), timeout=60)
        img = v["image"]
        assert img["agent_version"] == v["agent_version"] and img["server_url"] == "ws://192.168.16.100:8080/ws/agent"
        r, fs = unpacked(c, tmp_path)
        assert hashlib.sha256(r.content).hexdigest() == img["sha256"]
        assert img["file"] in r.headers["content-disposition"]
        yaml_text = fs.cat("/etc/napi-factory/agent.yaml").decode()
        assert f'token: "{TOKEN}"' in yaml_text and 'server_url: "ws://192.168.16.100:8080/ws/agent"' in yaml_text
        assert fs.cat("/opt/napi-factory/VERSION").decode().strip() == img["version"]
        assert b"def tree_hash" in fs.cat("/opt/napi-factory/agent/napi_factory_agent/selfupdate.py")
        unit = fs.run(["stat /etc/systemd/system/multi-user.target.wants/napi-factory-agent.service"])
        assert "Type: symlink" in unit
        mode = fs.run(["stat /etc/napi-factory/agent.yaml"])
        assert "Mode:  0600" in mode and "User:     0" in mode
        fs.fsck()
        # повторный запуск: входы те же — образ не пересобирается
        assert sd_view(c)["stale"] is False
        # «Обновить образ microSD»: только с паролем; собирается новый, прежний удаляется
        assert c.post("/api/v1/sdcard", json={"password": "wrong"}).status_code == 403
        assert c.post("/api/v1/sdcard", json={"password": "secret-pass"}).status_code == 200
        wait_for(lambda: sd_view(c)["image"]["built_at"] != img["built_at"] and not sd_view(c)["progress"],
                 timeout=60)
        files = sorted(p.name for p in (tmp_path / "images" / "sd").glob("napi-test-sd-*"))
        assert len(files) == 1
        audit = c.get("/api/v1/audit").json()
        assert any(a["action"] == "sd_rebuild" for a in audit)
        # плохая ссылка на базу — 400, текущий образ остаётся
        assert c.post("/api/v1/sdcard", json={"password": "secret-pass", "url": "ftp://x/y.img"}).status_code == 400
    finally:
        c.__exit__(None, None, None)


def test_no_base_is_reported(tmp_path):
    c = make_client(tmp_path)
    try:
        v = wait_for(lambda: sd_view(c)["error"] and sd_view(c))
        assert "не задана база" in v["error"] and v["image"] is None
        assert c.get("/api/v1/sdcard/download").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_agent_update_package(tmp_path):
    c = make_client(tmp_path)
    try:
        a = Agent(c, "CPU1")
        cat = a.recv("images")
        info = cat["agent"]
        sd = c.app.state.core.factory.sdcard
        assert info["version"] == sd.agent_version and len(info["version"]) == 12
        url = f"/api/v1/agent/update/{info['version']}.tar.gz"
        assert c.get(url).status_code == 401
        r = c.get(url, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == info["sha256"]
        names = tarfile.open(fileobj=io.BytesIO(r.content)).getnames()
        assert "napi_factory_agent/runner.py" in names and not any("__pycache__" in n for n in names)
        assert c.get("/api/v1/agent/update/000000000000.tar.gz",
                     headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404
        # карта со старым агентом видна в сводке
        ag = c.get("/api/v1/images").json()["agent"]
        assert ag["online"] == 1 and ag["current"] == 0
    finally:
        close_agents()
        c.__exit__(None, None, None)


def test_menu_without_builtin_sd_image(tmp_path):
    c = make_client(tmp_path)
    try:
        a = Agent(c, "CPU1", sd_selfcheck={"state": "n/a", "detail": "нет образа на microSD"})
        wait_for(lambda: "sd" not in [m["key"] for m in c.get("/api/v1/images").json()["menu"]])
        r = c.post("/api/v1/batches", json={"mode": "flash_only", "device_ids": [a.ack["device_id"]],
                                            "confirm": True, "options": {"os": "sd"}})
        assert r.status_code == 409 and "нет встроенного образа" in r.json()["detail"]
    finally:
        close_agents()
        c.__exit__(None, None, None)
