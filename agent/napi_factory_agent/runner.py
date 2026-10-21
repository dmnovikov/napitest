"""Агент: состояние платы, исполнение заданий, связь этапов с сервером.

Задание выполняется локально до конца независимо от связи с сервером. Состояние
(state.json) и очередь событий лежат на microSD.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import PROTO, STAGES, __version__
from .config import AgentConfig
from .identity import read_cpuid, read_mac, uplink
from .images import find_manifest, selfcheck
from .imagesync import ImageSync
from .selfupdate import Updater
from .queue import EventQueue
from .stages import StageCancelled, StageCtx, StageResult, StageTimeout
from .stages import flash as st_flash
from .stages import net as st_net
from .stages import ram as st_ram
from .stages import sdnand as st_sdnand
from .util import atomic_write_json, read_json, read_text

log = logging.getLogger("napi.agent")

STAGE_FUNCS: dict[str, Callable[[StageCtx], StageResult]] = {
    "ram": st_ram.run, "net": st_net.run, "sdnand": st_sdnand.run, "flash": st_flash.run,
}
STAGE_LABEL = {"ram": "RAM", "net": "NET", "sdnand": "NAND", "flash": "FLASH", "boot": "BOOT"}
TEST_STAGES = ("ram", "net", "sdnand")
MODE_STAGES = {
    "test_only": {"ram", "net", "sdnand"},
    "test_flash": {"ram", "net", "sdnand", "flash", "boot"},
    "flash_only": {"flash", "boot"},
}


def initial_stages(mode: str, params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Начальные состояния этапов по режиму и профилю."""
    run_set = MODE_STAGES[mode]
    enabled = params.get("stages") or {}
    out = {}
    for s in STAGES:
        if s not in run_set:
            out[s] = {"state": "skipped", "detail": "по режиму"}
        elif not enabled.get(s, True) or (s == "boot" and not (params.get("boot") or {}).get("enabled", True)):
            # Причина от сервера (например, «NAPI-P: сеть не проверяется»), иначе — профиль
            out[s] = {"state": "skipped", "detail": (params.get("skip_reason") or {}).get(s) or "отключено в профиле"}
        else:
            out[s] = {"state": "pending"}
    return out


def compute_result(mode: str, stages: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Итог задания со стороны агента (BOOT закрывает сервер)."""
    detail = next((stages[s].get("detail", "") for s in STAGES
                   if stages[s]["state"] in ("fail", "error", "interrupted")), "")
    if any(stages[s]["state"] == "interrupted" and s == "flash" for s in STAGES):
        return "interrupted", detail
    need = [s for s in STAGES if s != "boot" and s in MODE_STAGES[mode] and stages[s]["state"] != "skipped"]
    if any(stages[s]["state"] in ("fail", "error", "interrupted") for s in STAGES if s != "boot"):
        return "fail", detail
    blocked = [s for s in STAGES if stages[s].get("blocked")]
    if blocked:
        # Этап не выполнялся из-за предохранителя: это не PASS и не дефект платы
        return "incomplete", stages[blocked[0]].get("detail", "")
    if mode != "test_only" and stages["flash"]["state"] != "pass":
        return "fail", detail or "FLASH не выполнена"
    if not need:
        return "fail", "нет выполненных этапов"
    if all(stages[s]["state"] == "pass" for s in need):
        return "pass", ""
    return "fail", detail


class Agent:
    def __init__(self, cfg: AgentConfig, backend: Any, state_dir: str | None = None):
        self.cfg = cfg
        self.backend = backend
        self.state_dir = Path(state_dir or cfg["state_dir"])
        self.state_dir.mkdir(parents=True, exist_ok=True)
        backend.state_dir = str(self.state_dir)
        self.state_path = self.state_dir / "state.json"
        self.state: dict[str, Any] = read_json(self.state_path, {}) or {}
        self.queue = EventQueue(self.state_dir / "queue", int(cfg.get("queue_max_bytes", 32 << 20)))
        self.lock = threading.RLock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.link: Any = None                      # ws_client.WSLink
        self.server_addr: tuple[str, int] | None = None
        self.serial: str | None = self.state.get("serial")
        self._waiters: dict[tuple[str, str], concurrent.futures.Future] = {}
        self._outstanding: dict[tuple[str, str], dict] = {}
        self._cancel = threading.Event()
        self._task: asyncio.Task | None = None
        self._stage_t0: float | None = None
        self.sd_selfcheck: dict[str, Any] = {"state": "n/a"}
        self.images = ImageSync(self, cfg.images_dir, str(self.state_dir))
        self.updater = Updater(self)
        self.cpuid, self.id_source = read_cpuid(backend.root, cfg["identity"], self.state.get("assigned_cpuid"))
        self._recover()

    # ------------------------------------------------------------------ состояние
    @property
    def job(self) -> dict[str, Any] | None:
        return self.state.get("job")

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.state_path, self.state)

    def emit(self, kind: str, data: dict | None = None, stage: str | None = None,
             job_id: str | None = None) -> None:
        jid = job_id if job_id is not None else (self.job or {}).get("job_id")
        self.queue.put(kind, jid, data, stage)
        if self.link is not None:
            self.link.notify_events()

    def _recover(self) -> None:
        """После рестарта: незавершённое задание помечается interrupted (если карта на той же плате)."""
        job = self.job
        if not job or job.get("finished"):
            return
        if job.get("cpuid") != self.cpuid:
            log.warning("state.json от другой платы (cpuid %s ≠ %s): задание отброшено", job.get("cpuid"), self.cpuid)
            self.state.pop("job", None)
            self.save()
            return
        for s, st in job["stages"].items():
            if st["state"] == "running":
                st.update(state="interrupted", detail=f"{STAGE_LABEL[s]}: агент перезапущен")
                st.pop("pct", None)
            elif st["state"] == "pending":
                st.update(state="skipped", detail="задание прервано")
        job.update(finished=True, result="interrupted", finished_ts=time.time(),
                   detail=next((st["detail"] for st in job["stages"].values()
                                if st["state"] == "interrupted"), "агент перезапущен"))
        self.save()
        self.emit("job_finished", {"result": "interrupted", "stages": job["stages"], "detail": job["detail"]},
                  job_id=job["job_id"])

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            job = self.job
            if not job:
                return {"type": "heartbeat", "job_id": None, "mode": None, "elapsed": 0,
                        "finished": False, "result": None, "stages": {}}
            end = job.get("finished_ts") or time.time()
            stages = {k: dict(v) for k, v in job["stages"].items()}
            for k, v in stages.items():
                if v["state"] == "running" and self._stage_t0:
                    v["t"] = int(time.time() - self._stage_t0)
            return {"type": "heartbeat", "job_id": job["job_id"], "mode": job["mode"],
                    "elapsed": int(end - job["started_ts"]), "finished": bool(job.get("finished")),
                    "result": job.get("result"), "stages": stages}

    def hello(self) -> dict[str, Any]:
        be = self.backend
        iface = (self.cfg.get("net") or {}).get("iface", "end0")
        mac = read_mac(be.root, iface, (self.cfg["identity"] or {}).get("known_clone_macs") or [])
        try:
            from .stages.ram import parse_meminfo
            ram = parse_meminfo(be.read_meminfo()).get("MemTotal", 0)
        except OSError:
            ram = 0
        try:
            inv = be.detect(self.cfg["detect"])
        except OSError as e:
            inv = {"ok": False, "reasons": [f"inventory: {e}"], "devices": [], "target": None}
        cur = self.snapshot() if self.job else None
        if cur:
            cur.pop("type", None)
        up = uplink(be.root, self._server_ip())
        return {
            "type": "hello", "proto": PROTO, "cpuid": self.cpuid, "id_source": self.id_source,
            **mac, "ip": be.iface_ip(up["iface"] if up else iface) or be.iface_ip(iface), "uplink": up, "hostname": be.hostname(), "agent_version": f"{__version__}+{self.updater.version}",
            "test_sd_version": read_text(self.cfg.version_file, "unknown"), "kernel": be.kernel(),
            "ram_bytes": ram, "has_led": be.has_led(), "simulated": bool(be.simulated), "inventory": inv,
            "sd_selfcheck": self.sd_selfcheck, "stream": self.queue.stream, "current_job": cur,
            "boot_id": be.boot_id(), "images": dict(self.images.status), "agent_update": self.updater.info(),
        }

    def _server_ip(self) -> str | None:
        host = (self.server_addr or (None,))[0]
        try:
            return str(ipaddress.ip_address(host)) if host else None
        except ValueError:
            return None                   # имя хоста: берётся маршрут по умолчанию

    def run_selfcheck(self) -> None:
        images = self.cfg.images_dir
        man = find_manifest(images)
        if man:
            self.sd_selfcheck = selfcheck(images, man, str(self.state_dir / "selfcheck.json"))
        else:
            self.sd_selfcheck = {"state": "n/a", "detail": "нет образа на microSD"}

    # ------------------------------------------------------------------ сообщения сервера
    async def handle(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "hello_ack":
            if msg.get("cpuid") and not self.cpuid:
                self.cpuid, self.id_source = msg["cpuid"], "assigned"
                self.state["assigned_cpuid"] = msg["cpuid"]
                self.save()
            if msg.get("serial"):
                self.serial = msg["serial"]
                self.state["serial"] = msg["serial"]
                self.save()
            self.updater.confirm()
        elif t == "job":
            await self.submit(msg)
        elif t == "cancel":
            self.cancel(msg.get("job_id"))
        elif t == "ack":
            self.queue.ack(int(msg.get("seq", 0)), msg.get("stream"))
        elif t in ("net_grant", "serial"):
            key = (t, msg.get("job_id") or "")
            if t == "serial" and msg.get("serial"):
                self.serial = msg["serial"]
                self.state["serial"] = msg["serial"]
            self._outstanding.pop(key, None)
            fut = self._waiters.pop(key, None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif t == "images":
            self.updater.on_catalog(msg)
            self.images.on_catalog(msg)
        elif t == "identify":
            asyncio.get_running_loop().run_in_executor(None, self.backend.identify, int(msg.get("seconds", 10)))
        elif t == "error":
            log.warning("сервер: %s", msg.get("detail"))

    async def submit(self, msg: dict[str, Any]) -> bool:
        self.loop = self.loop or asyncio.get_running_loop()
        job_id = msg.get("job_id")
        mode = msg.get("mode")
        if not job_id or mode not in MODE_STAGES:
            self.emit("log", {"lines": [f"задание отклонено: неверный режим {mode!r}"]}, job_id=job_id or "")
            return False
        cur = self.job
        if cur and cur["job_id"] == job_id:
            log.info("повтор job %s: не перезапускается", job_id)
            if self.link:
                self.link.send_soon(self.snapshot())
            return False
        if cur and not cur.get("finished"):
            self.emit("log", {"lines": [f"задание {job_id} отклонено: активно {cur['job_id']}"]}, job_id=job_id)
            return False
        params = msg.get("params") or {}
        self.images.pause()
        with self.lock:
            self.state["job"] = {
                "job_id": job_id, "mode": mode, "params": params, "cpuid": self.cpuid,
                "stages": initial_stages(mode, params), "started_ts": time.time(),
                "finished": False, "result": None, "finished_ts": None,
            }
            self.save()
        self._cancel = threading.Event()
        self._task = asyncio.get_running_loop().create_task(self._run_job())
        return True

    def cancel(self, job_id: str | None) -> None:
        job = self.job
        if job and not job.get("finished") and (job_id is None or job_id == job["job_id"]):
            log.info("отмена %s", job["job_id"])
            self._cancel.set()

    # ------------------------------------------------------------------ запросы из потоков этапов
    def _request_sync(self, job_id: str, cancel: threading.Event):
        def request(msg: dict, resp_type: str, timeout: float) -> dict | None:
            loop = self.loop
            if not resp_type:
                if loop and self.link:
                    loop.call_soon_threadsafe(self.link.send_soon, msg)
                return None
            key = (resp_type, job_id)
            fut: concurrent.futures.Future = concurrent.futures.Future()
            self._waiters[key] = fut
            self._outstanding[key] = msg
            if loop and self.link:
                loop.call_soon_threadsafe(self.link.send_soon, msg)
            end = time.monotonic() + timeout
            try:
                while True:
                    if cancel.is_set():
                        raise StageCancelled()
                    left = end - time.monotonic()
                    if left <= 0:
                        return None
                    try:
                        return fut.result(timeout=min(left, 0.5))
                    except concurrent.futures.TimeoutError:
                        continue
            finally:
                self._waiters.pop(key, None)
                self._outstanding.pop(key, None)
        return request

    def outstanding_requests(self) -> list[dict]:
        return list(self._outstanding.values())

    # ------------------------------------------------------------------ исполнение
    def _set_stage(self, name: str, **kw: Any) -> None:
        with self.lock:
            st = self.job["stages"][name]
            st.clear()
            st.update({k: v for k, v in kw.items() if v is not None})

    async def _run_job(self) -> None:
        job = self.job
        job_id, mode, params = job["job_id"], job["mode"], job["params"]
        log.info("задание %s режим %s", job_id, mode)
        self.backend.on_job_start(job_id, mode)
        self.backend.led_mode("normal")
        self.emit("job_started", {"mode": mode})
        shared: dict[str, Any] = {}
        cancelled = False
        for name in STAGES:
            st = job["stages"][name]
            if st["state"] != "pending" or name == "boot":
                continue
            if cancelled or self._cancel.is_set():
                cancelled = True
                self._set_stage(name, state="skipped", detail="отменено")
                continue
            if name == "flash" and mode == "test_flash":
                # FAIL сети прошивке не мешает (итог всё равно FAIL): сеть часто подводит стенд, а не плата
                bad = [s for s in TEST_STAGES if job["stages"][s]["state"] not in ("pass", "skipped")
                       and not (s == "net" and job["stages"][s]["state"] == "fail")]
                if bad:
                    self._set_stage(name, state="skipped", detail="тесты не пройдены")
                    continue
            await self._run_stage(name, job_id, mode, params, shared)
            if job["stages"][name]["state"] == "interrupted" or self._cancel.is_set():
                cancelled = True
        if cancelled:
            for name in STAGES:
                if job["stages"][name]["state"] == "pending":
                    self._set_stage(name, state="skipped", detail="отменено")
            result, detail = compute_result(mode, job["stages"])
            if result != "interrupted":
                result, detail = "cancelled", "остановлено оператором"
        else:
            result, detail = compute_result(mode, job["stages"])
        with self.lock:
            job.update(finished=True, result=result, detail=detail, finished_ts=time.time())
            self.save()
        log.info("задание %s: %s %s", job_id, result, detail)
        # Брак виден на стенде без экрана: светодиод часто мигает до выключения платы или нового задания
        self.backend.led_mode("fail" if result == "fail" else "normal")
        self.emit("job_finished", {"result": result, "stages": job["stages"], "detail": detail})
        if self.link:
            self.link.send_soon(self.snapshot())
        self.backend.on_job_finished(self.snapshot())
        self.images.kick()

    async def _run_stage(self, name: str, job_id: str, mode: str, params: dict, shared: dict) -> None:
        timeout = float((params.get(name) or {}).get("timeout_s", 900))
        label = STAGE_LABEL[name]

        def on_progress(pct: float | None, detail: str | None) -> None:
            with self.lock:
                st = self.job["stages"][name]
                if pct is not None:
                    st["pct"] = int(pct)
                if detail is not None:
                    st["detail"] = detail

        ctx = StageCtx(name, job_id, mode, params, self.backend, self._cancel, timeout, on_progress,
                       self._request_sync(job_id, self._cancel), shared, self.cfg.images_dir,
                       self.server_addr, self.cpuid)
        self._stage_t0 = time.time()
        self._set_stage(name, state="running", pct=0)
        self.save()
        self.emit("stage_started", {}, stage=name)

        def call() -> StageResult:
            try:
                return STAGE_FUNCS[name](ctx)
            except StageCancelled:
                if name == "flash" and shared.get("flash_write_started"):
                    return StageResult("interrupted", "FLASH: interrupted, SD-NAND непригодна до перепрошивки")
                if name == "sdnand":
                    return StageResult("interrupted", "NAND: остановлено")
                return StageResult("skipped", "отменено")
            except StageTimeout:
                return StageResult("fail", f"{label}: timeout")
            except Exception as e:  # noqa: BLE001 — ошибка агента не должна ронять задание
                log.exception("этап %s", name)
                return StageResult("error", f"{label}: {type(e).__name__}: {e}"[:120])

        res = await asyncio.to_thread(call)
        t = int(time.time() - self._stage_t0)
        self._stage_t0 = None
        self._set_stage(name, state=res.state, t=t, detail=res.detail or None, warn=True if res.warnings else None,
                        blocked=True if res.measurements.get("blocked") else None)
        if res.warnings:
            for w in res.warnings:
                ctx.log(f"ЗАМЕЧАНИЕ: {w}")
        self.save()
        logtail = "\n".join(list(ctx.log_lines)[-50:])
        self.emit("stage_finished", {"state": res.state, "t": t, "detail": res.detail,
                                     "measurements": res.measurements, "warnings": res.warnings,
                                     "log": logtail}, stage=name)
        log.info("%s: %s %s", name, res.state, res.detail)

    async def wait_job(self) -> None:
        if self._task:
            await self._task
