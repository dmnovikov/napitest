"""Автообновление агента с сервера: пакет, проверка, переключение, откат."""
import hashlib
import http.server
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from napi_factory_agent import selfupdate
from napi_factory_agent.selfupdate import PKG, Updater, guard, pack, tree_hash

from .conftest import make_agent
from .test_runner import run

SRC = Path(selfupdate.__file__).resolve().parent


class Handler(http.server.BaseHTTPRequestHandler):
    files: dict = {}
    hits: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1]
        self.hits.append((self.path, self.headers.get("Authorization")))
        data = self.files.get(name)
        self.send_response(200 if data is not None else 404)
        self.send_header("Content-Length", str(len(data or b"")))
        self.end_headers()
        self.wfile.write(data or b"")


@pytest.fixture
def server():
    Handler.files, Handler.hits = {}, []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def card_layout(tmp_path) -> Path:
    """/opt/napi-factory/agent как на карте: каталог пакета (до первого обновления)."""
    root = tmp_path / "card-agent"
    shutil.copytree(SRC, root / PKG, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def new_version(tmp_path, extra="# новая версия\n", broken=False):
    d = tmp_path / "new" / PKG
    shutil.rmtree(d.parent, ignore_errors=True)
    shutil.copytree(SRC, d, ignore=shutil.ignore_patterns("__pycache__"))
    with open(d / "util.py", "a") as f:
        f.write(extra if not broken else "def (:\n")
    return tree_hash(d), pack(d)


def updating_agent(tmp_path, server, root):
    a = make_agent(tmp_path)
    a.link.http_base = lambda: f"http://127.0.0.1:{server.server_port}"
    a.updater = Updater(a, root=root)
    a.updater.version = tree_hash(root / PKG)
    a.restarted = 0

    def restart():
        a.restarted += 1
    a.updater.restart = restart
    return a


def offer(a, ver, data):
    Handler.files[f"{ver}.tar.gz"] = data
    msg = {"type": "images", "sync": True, "images": [],
           "agent": {"version": ver, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}
    a.updater.on_catalog(msg)
    a.images.on_catalog(msg)


def test_pack_is_deterministic_and_hash_matches():
    data = pack(SRC)
    assert data == pack(SRC)
    assert len(tree_hash(SRC)) == 12


def test_update_switches_and_confirms(tmp_path, server):
    root = card_layout(tmp_path)
    old = tree_hash(root / PKG)
    a = updating_agent(tmp_path, server, root)
    ver, data = new_version(tmp_path)
    offer(a, ver, data)
    run(a.images._sync_once())
    assert a.restarted == 1
    assert os.readlink(root / PKG) == f"versions/{ver}"
    assert tree_hash(root / PKG) == ver and (root / "versions" / old).is_dir()
    assert Handler.hits[0] == (f"/api/v1/agent/update/{ver}.tar.gz", "Bearer t")
    st = json.loads((root / "update.json").read_text())
    assert st["pending"] == {"version": ver, "prev": old, "tries": 0}
    # новая версия запустилась (guard) и получила hello_ack — откат больше не нужен
    assert guard(root) is None
    b = updating_agent(tmp_path, server, root)
    b.updater.version = ver
    run(b.handle({"type": "hello_ack", "device_id": 1}))
    assert "pending" not in json.loads((root / "update.json").read_text())
    offer(b, ver, data)
    assert b.updater.needed() is False                 # уже стоит нужная версия


def test_crash_loop_rolls_back(tmp_path, server):
    root = card_layout(tmp_path)
    old = tree_hash(root / PKG)
    a = updating_agent(tmp_path, server, root)
    ver, data = new_version(tmp_path)
    offer(a, ver, data)
    run(a.images._sync_once())
    for _ in range(selfupdate.MAX_TRIES):
        assert guard(root) is None                     # новая версия падает, не дойдя до hello_ack
    assert guard(root) == old
    assert os.readlink(root / PKG) == f"versions/{old}"
    st = json.loads((root / "update.json").read_text())
    assert st["bad"] == [ver] and st["rolled_back"] == ver
    # прежняя версия снова на связи: откатившуюся не ставит
    b = updating_agent(tmp_path, server, root)
    offer(b, ver, data)
    assert b.updater.needed() is False and "откатилась" in b.updater.status["detail"]
    assert b.hello()["agent_update"]["rolled_back"] == ver


@pytest.mark.parametrize("case", ["broken", "checksum"])
def test_bad_package_is_not_installed(tmp_path, server, case):
    root = card_layout(tmp_path)
    a = updating_agent(tmp_path, server, root)
    ver, data = new_version(tmp_path, broken=case == "broken")
    offer(a, ver, data)
    if case == "checksum":
        Handler.files[f"{ver}.tar.gz"] = data + b"x"
    run(a.images._sync_once())
    assert a.restarted == 0 and not (root / PKG).is_symlink()
    st = a.updater.status
    assert st["state"] == "error"
    assert ("не запускается" in st["detail"]) if case == "broken" else ("контрольная сумма" in st["detail"])
    assert a.updater.needed() is False                 # повтор не раньше чем через RETRY_S
    hello = [m for m in a.link.sent if m.get("type") == "hello"]
    assert hello and hello[-1]["agent_update"]["state"] == "error"


def test_no_update_during_job_or_from_repo_checkout(tmp_path, server):
    root = card_layout(tmp_path)
    a = updating_agent(tmp_path, server, root)
    ver, data = new_version(tmp_path)
    offer(a, ver, data)
    a.state["job"] = {"job_id": "j", "finished": False, "stages": {}}
    run(a.images._sync_once())
    assert a.restarted == 0 and not Handler.hits
    # агент из репозитория (не factory_dir/agent) не обновляет сам себя
    plain = make_agent(tmp_path, cpuid="5a4e00000000a002")
    assert plain.updater.enabled is False
    assert plain.hello()["agent_version"].endswith("+" + tree_hash(SRC))
