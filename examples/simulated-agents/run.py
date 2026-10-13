#!/usr/bin/env python3
"""Симулятор N плат NAPI-C: настоящий код агента с MockBackend в одном процессе.

Пример:
  python examples/simulated-agents/run.py --server ws://127.0.0.1:8080/ws/agent --token dev-token -n 50

Каждая плата: свой каталог (поддельные sysfs/proc, sparse-«SD-NAND», state.json, queue/),
уникальный cpuid, simulated=true в hello. Инъекция ошибок по этапам (--fail-rate),
обрывы связи (--drop-rate, вероятность на тик heartbeat), эмуляция BOOT: после FLASH PASS
плата «перезагружается с SD-NAND» (связь пропадает) и шлёт POST /api/v1/agent/boot_ok.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import random
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))

from napi_factory_agent.backends import MockBackend  # noqa: E402
from napi_factory_agent.config import load_config  # noqa: E402
from napi_factory_agent.main import run_agent  # noqa: E402
from napi_factory_agent.runner import Agent  # noqa: E402
from napi_factory_agent.testing import make_test_image  # noqa: E402

log = logging.getLogger("napi.sim")


def boot_ok_url(ws_url: str) -> str:
    u = urlparse(ws_url)
    scheme = "https" if u.scheme == "wss" else "http"
    return f"{scheme}://{u.netloc}/api/v1/agent/boot_ok"


def post_boot_ok(url: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def cpuid_for(i: int, seed: str) -> str:
    return "5a4e" + hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()[:12]


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="ws://127.0.0.1:8080/ws/agent")
    ap.add_argument("--token", default="dev-token")
    ap.add_argument("-n", "--count", type=int, default=50)
    ap.add_argument("--dir", default="/tmp/napi-sim", help="каталог состояния симулятора")
    ap.add_argument("--clean", action="store_true", help="удалить состояние предыдущего запуска")
    ap.add_argument("--fail-rate", type=float, default=0.1, help="доля заданий с ошибкой на случайном этапе")
    ap.add_argument("--drop-rate", type=float, default=0.002, help="вероятность обрыва связи на тик heartbeat")
    ap.add_argument("--boot-fail-rate", type=float, default=0.05, help="доля плат без отчёта первой загрузки")
    ap.add_argument("--napi-p", type=int, default=0, help="сколько плат подключены через USB-адаптер (NAPI-P)")
    ap.add_argument("--speed", type=float, default=1.0, help="множитель скорости (2 = вдвое быстрее)")
    ap.add_argument("--image-version", default="2026.10.0")
    ap.add_argument("--test-sd-version", default="2026.10.1-sim")
    ap.add_argument("--heartbeat", type=float, default=3.0)
    ap.add_argument("--seed", default="sim")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.setLevel(logging.INFO)

    base = Path(args.dir)
    if args.clean and base.exists():
        shutil.rmtree(base)
    factory = base / "factory"
    images = factory / "images"
    if not (images / f"napi-c-armbian-{args.image_version}.manifest.json").exists():
        make_test_image(images, args.image_version, size=2 * 1024 * 1024)
    (factory / "VERSION").write_text(args.test_sd_version + "\n")

    rnd = random.Random(args.seed)
    boot_url = boot_ok_url(args.server)
    agents: list[Agent] = []
    links = []

    for i in range(args.count):
        cpuid = cpuid_for(i, args.seed)
        be = MockBackend(str(base / f"board-{i:02d}"), cpuid, speed=args.speed, fail_rate=args.fail_rate,
                         drop_rate=args.drop_rate, seed=hash((args.seed, i)) & 0xFFFFFFFF,
                         uplink="usb" if i < args.napi_p else "end0")
        cfg = load_config(None, {"token": args.token, "server_url": args.server, "mdns": False,
                                 "factory_dir": str(factory), "heartbeat_s": args.heartbeat})
        agent = Agent(cfg, be, be.state_dir)

        def make_hook(agent=agent, be=be):
            def hook(snap: dict) -> None:
                st = snap.get("stages", {})
                if not (snap.get("result") == "pass" and st.get("flash", {}).get("state") == "pass"
                        and st.get("boot", {}).get("state") == "pending"):
                    return
                asyncio.ensure_future(simulate_boot(agent, be, snap))
            return hook

        async def simulate_boot(agent: Agent, be: MockBackend, snap: dict) -> None:
            # Оператор вынимает microSD и перезагружает плату: агент пропадает со связи
            delay = rnd.uniform(5, 20) / args.speed
            agent.link.pause(delay + 3)
            await asyncio.sleep(delay)
            if rnd.random() < args.boot_fail_rate:
                log.info("%s: BOOT без отчёта (симуляция)", agent.cpuid)
                return
            payload = {"cpuid": agent.cpuid, "image_version": args.image_version,
                       "uptime": round(rnd.uniform(15, 40), 1), "serial": agent.serial}
            try:
                r = await asyncio.to_thread(post_boot_ok, boot_url, args.token, payload)
                log.info("%s: boot_ok → %s", agent.cpuid, r)
            except (urllib.error.URLError, OSError, ValueError) as e:
                log.warning("%s: boot_ok не доставлен: %s", agent.cpuid, e)

        be.boot_hook = make_hook()
        agents.append(agent)

    for agent in agents:
        links.append(await run_agent(agent, args.server, args.token, args.heartbeat, mdns=False))
        await asyncio.sleep(0.02)   # не все разом
    log.info("запущено %d симулированных плат → %s", len(agents), args.server)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        import signal
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    except (NotImplementedError, RuntimeError):
        pass
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), 30)
        except asyncio.TimeoutError:
            online = sum(1 for lk in links if lk.ws is not None)
            busy = sum(1 for a in agents if a.job and not a.job.get("finished"))
            log.info("online %d/%d, в работе %d", online, len(links), busy)
    for lk in links:
        lk.stop()


if __name__ == "__main__":
    asyncio.run(main())
