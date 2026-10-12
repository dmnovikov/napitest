"""Точка входа агента: napi-factory-agent --config /etc/napi-factory/agent.yaml."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .backends import MockBackend, RealBackend
from .config import load_config
from .identity import regenerate_machine_id
from .runner import Agent
from .ws_client import WSLink

log = logging.getLogger("napi.main")


async def run_agent(agent: Agent, url: str, token: str, heartbeat_s: float = 3.0, mdns: bool = True,
                    selfcheck: bool = True, discovery_port: int = 0) -> WSLink:
    """Запуск агента в текущем event loop. Возвращает WSLink (run_forever уже запущен задачей)."""
    agent.loop = asyncio.get_running_loop()
    link = WSLink(agent, url, token, heartbeat_s=heartbeat_s, mdns=mdns, discovery_port=discovery_port)
    agent.link = link
    if selfcheck:
        # Самопроверка образа на новой карте занимает минуты (SHA-256 3 ГБ на RK3308):
        # плата подключается сразу, итог проверки уходит повторным hello.
        agent.sd_selfcheck = {"state": "running", "detail": "проверка хешей образа"}

        async def _selfcheck() -> None:
            await asyncio.to_thread(agent.run_selfcheck)
            log.info("самопроверка microSD: %s", agent.sd_selfcheck)
            link.send_soon(agent.hello())

        link.selfcheck_task = asyncio.create_task(_selfcheck())
    link.task = asyncio.create_task(link.run_forever())
    link.images_task = asyncio.create_task(agent.images.run())
    return link


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NAPI Factory Test agent")
    ap.add_argument("--config", default="/etc/napi-factory/agent.yaml")
    ap.add_argument("--backend", choices=["real", "mock"], default="real")
    ap.add_argument("--state-dir")
    ap.add_argument("--mock-dir", help="каталог поддельного оборудования (только --backend mock)")
    ap.add_argument("--mock-cpuid", default="5a4e0000000000aa")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    state_dir = args.state_dir or cfg["state_dir"]

    if args.backend == "real":
        backend = RealBackend(state_dir, led_name=cfg.get("led"))
    else:
        if not args.mock_dir:
            ap.error("--backend mock требует --mock-dir")
        log.warning("MOCK-бэкенд: аппаратные тесты симулируются, simulated=true")
        backend = MockBackend(args.mock_dir, args.mock_cpuid)
        state_dir = args.state_dir or backend.state_dir

    if not cfg["token"]:
        log.error("не задан token агента")
        return 2

    agent = Agent(cfg, backend, state_dir)
    if args.backend == "real" and agent.cpuid and agent.state.get("machine_id_for") != agent.cpuid:
        # Клоны карт имеют одинаковый machine-id: регенерируем один раз на плату
        if regenerate_machine_id("/"):
            agent.state["machine_id_for"] = agent.cpuid
            agent.save()

    async def amain() -> None:
        link = await run_agent(agent, cfg["server_url"], cfg["token"], float(cfg["heartbeat_s"]),
                               bool(cfg["mdns"]), discovery_port=int(cfg["discovery_port"] or 0))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        link.stop()
        # Задание не прерываем намеренно: при остановке сервиса systemd оно станет interrupted при рестарте

    asyncio.run(amain())
    return 0


if __name__ == "__main__":
    sys.exit(main())
