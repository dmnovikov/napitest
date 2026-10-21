"""Ядро сервера: платы, задания, события агентов, этап BOOT, таймауты и рассылка в браузер.

Сервер единственный источник правды. Heartbeat живёт в памяти; в БД пишутся регистрация,
события, результаты этапов и итоги заданий и партий.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import DESTRUCTIVE_MODES, MODES, STAGES, TEST_STAGES, Profile, Settings
from ..db.database import Database, dumps, loads, now_iso, parse_iso, to_iso
from ..scheduler.bootnet import BootNet, subnet_hosts
from ..scheduler.net import Grant, NetScheduler
from .catalog import CUSTOM, SD, ZERO, Catalog
from .sdcard import SdCard
from ..version import __version__

log = logging.getLogger("napi.factory")

ACTIVE_STATUSES = ("queued", "running", "awaiting_boot")
FINAL_STATUSES = ("pass", "fail", "interrupted", "incomplete", "cancelled", "offline")
# Прежние тексты таймаута BOOT (итог был FAIL) — в старых записях БД; пересчитываются при старте
BOOT_TIMEOUT_TEXTS = ("BOOTCHECK: нет отчёта загрузки", "BOOTCHECK: плата не появилась в сети",
                      "BOOT: no report", "BOOT: плата не появилась в сети")
# Загрузка не подтверждена за время ожидания: это не дефект (оператор мог не успеть) — PASS с замечанием
BOOT_UNCONFIRMED = "BOOTCHECK: загрузка не подтверждена"
BOOT_UNCONFIRMED_SD = "BOOTCHECK: не подтверждена — плата загрузилась с microSD"
BOOT_NAT = "BOOTCHECK: плата за роутером (NAT), сервер её не видит — проверьте загрузку вручную"
BOOT_BY_IP = "MAC не проверен: плата в другой сети, найдена по прежнему IP"
BOOT_UNCONFIRMED_TEXTS = (BOOT_UNCONFIRMED, BOOT_UNCONFIRMED_SD, BOOT_NAT)
FINAL_STATUSES = ("pass", "fail", "interrupted", "cancelled", "offline", "incomplete")
# Итог задания от агента может заменить эти статусы (например, после долгого офлайна).
OVERRIDABLE_STATUSES = ("queued", "running", "offline")


BOARDS = {"napi-c": "NAPI-C", "napi-p": "NAPI-P"}
NO_NET = {"napi-p": "NAPI-P: сеть не проверяется (Ethernet на штырях, связь через USB-адаптер)"}


def detect_board(uplink: dict | None) -> str | None:
    """Тип платы по интерфейсу связи с сервером: USB-Ethernet адаптер → NAPI-P, встроенный → NAPI-C."""
    if not isinstance(uplink, dict) or not uplink.get("iface"):
        return None
    return "napi-p" if uplink.get("bus") == "usb" else "napi-c"


def board_of(dev) -> str:
    """Тип, по которому проверяется плата: заданный вручную, иначе автоопределённый (NAPI-C по умолчанию)."""
    return dev["board_override"] or dev["board_detected"] or "napi-c"


class FactoryError(Exception):
    def __init__(self, message: str, status: int = 400, extra: Any = None):
        super().__init__(message)
        self.status = status
        self.extra = extra


@dataclass
class AgentConn:
    ws: Any
    server_host: str
    device_id: int | None = None
    cpuid: str | None = None
    mac: str | None = None
    stream: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False

    async def send(self, msg: dict) -> bool:
        if self.closed:
            return False
        try:
            async with self.lock:
                await self.ws.send_json(msg)
            return True
        except Exception:  # соединение уже закрыто
            self.closed = True
            return False


def verdict(enabled: dict[str, bool], stages: dict[str, dict], include_boot: bool = True) -> str | None:
    """Итог по включённым этапам: pass, fail, interrupted или None, если ещё не всё завершено."""
    required = [s for s in STAGES if enabled.get(s) and (include_boot or s != "boot")]
    states = [(stages.get(s) or {}).get("state", "pending") for s in required]
    if any(st == "interrupted" for st in states):
        return "interrupted"
    if any(st in ("fail", "error") for st in states):
        return "fail"
    if all(st == "pass" for st in states):
        return "pass"
    return None


def net_only_fail(enabled: dict[str, bool], stages: dict[str, dict]) -> bool:
    """«Тесты + прошивка»: не прошла только сеть, а прошивка прошла — плату ведут дальше, на BOOTCHECK
    (сеть часто подводит стенд, а не плата); итог задания всё равно FAIL."""
    st = {s: (stages.get(s) or {}).get("state", "pending") for s in STAGES if enabled.get(s) and s != "boot"}
    return (st.get("net") in ("fail", "error") and st.get("flash") == "pass"
            and all(v == "pass" for k, v in st.items() if k != "net"))


def boot_ready(enabled: dict[str, bool], stages: dict[str, dict]) -> bool:
    """Этапы до BOOT позволяют проверять загрузку: все пройдены или не прошла только сеть."""
    return verdict(enabled, stages, include_boot=False) == "pass" or net_only_fail(enabled, stages)


NET_STAND = "похоже на сеть стенда: NET не прошли почти все платы партии"


class Factory:
    def __init__(self, settings: Settings, profile: Profile, db: Database):
        self.settings = settings
        self.profile = profile
        self.db = db
        self.agents: dict[int, AgentConn] = {}
        self.live: dict[int, dict] = {}          # device_id -> последний heartbeat
        self.last_hb: dict[int, float] = {}      # device_id -> time.monotonic()
        self.online_cache: dict[int, bool] = {}
        self.cancel_requested: set[str] = set()
        self.ui_clients: set[Any] = set()
        self.early_boot: dict[str, dict] = {}    # job_id -> отчёт первой загрузки, пришедший раньше итога FLASH
        self.bootnet = BootNet(port=int(profile.section("boot").get("probe_port", 22)))
        self.boot_state: dict[str, dict] = {}    # job_id -> состояние сетевой проверки BOOT
        self.peer_ip: dict[int, str | None] = {}  # device_id -> адрес, с которого пришёл агент (NAT: не его IP)
        self.dirty: set[int] = set()
        self.summary_dirty = False
        self.grid_reset = False    # сетка поменялась целиком («Скрыть офлайн»): браузерам — новый snapshot
        ports = [profile.iperf_base_port + i for i in range(profile.net_parallel)]
        lease = float(profile.section("net").get("iperf_seconds", 10)) * 2 + 120
        self.net = NetScheduler(ports, lease_s=lease)
        self._bg: list[asyncio.Task] = []
        self._last_seen_flush = time.monotonic()
        # Образы ОС для SD-NAND и их наличие на тестовых microSD (по отчётам агентов)
        self.catalog = Catalog(settings, profile, db, self._catalog_changed)
        self.card_images: dict[int, dict[str, dict]] = {}   # device_id -> {file: {state, pct, detail}}
        # Образ тестовой microSD для скачивания и пакет агента для автообновления карт
        self.sdcard = SdCard(settings, profile, self._sd_changed)
        self.card_agent: dict[int, dict] = {}                # device_id -> {agent_version, update, sd_image}
        self._catalog_dirty = False
        self._sync_allowed: bool | None = None

    def _sd_changed(self) -> None:
        self.summary_dirty = True

    def _catalog_changed(self) -> None:
        # Вызывается и из потока подготовки образа: только флаги, рассылка — в tick
        self._catalog_dirty = True
        self.summary_dirty = True

    # ------------------------------------------------------------------ служебное

    def audit(self, operator: str | None, action: str, detail: Any = None) -> None:
        self.db.execute(
            "INSERT INTO audit_log (ts, operator, action, detail_json) VALUES (?, ?, ?, ?)",
            (now_iso(), operator, action, dumps(detail)),
        )

    def mark(self, device_id: int | None) -> None:
        if device_id is not None:
            self.dirty.add(device_id)
        self.summary_dirty = True

    def is_online(self, device_id: int) -> bool:
        conn = self.agents.get(device_id)
        if conn is None or conn.closed:
            return False
        last = self.last_hb.get(device_id)
        return last is not None and time.monotonic() - last <= self.profile.heartbeat_offline_s

    def _meta_next(self, key: str) -> int:
        with self.db.transaction() as tx:
            row = tx.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            value = int(row["value"]) + 1 if row else 1
            tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
        return value

    def expected_boards(self) -> int:
        row = self.db.one("SELECT value FROM meta WHERE key = 'expected_boards'")
        return int(row["value"]) if row else 0

    def set_expected_boards(self, operator: dict, count: int) -> None:
        if not 0 <= count <= 500:
            raise FactoryError("число плат: от 0 до 500")
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('expected_boards', ?)", (str(count),))
        self.audit(operator["username"], "expected_boards", {"count": count})
        self.summary_dirty = True

    def _job(self, job_id: str | None):
        if not job_id:
            return None
        return self.db.one("SELECT * FROM job WHERE id = ?", (job_id,))

    def _latest_job(self, device_id: int):
        return self.db.one(
            "SELECT * FROM job WHERE device_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (device_id,)
        )

    def _active_job(self, device_id: int):
        return self.db.one(
            "SELECT * FROM job WHERE device_id = ? AND status IN ('queued','running','awaiting_boot')"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (device_id,),
        )

    def _set_stage(self, job_id: str, stage: str, state: str, t: float | None = None, detail: str | None = None,
                   measurements: dict | None = None, log_text: str | None = None,
                   warnings: list[str] | None = None) -> None:
        self.db.execute(
            "INSERT INTO stage_result (job_id, stage, state, t, detail, measurements_json, log, warnings_json,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (job_id, stage) DO UPDATE SET state = excluded.state,"
            " t = COALESCE(excluded.t, t), detail = COALESCE(excluded.detail, detail),"
            " measurements_json = COALESCE(excluded.measurements_json, measurements_json),"
            " log = COALESCE(excluded.log, log), warnings_json = COALESCE(excluded.warnings_json, warnings_json),"
            " updated_at = excluded.updated_at",
            (job_id, stage, state, t, detail, dumps(measurements), log_text, dumps(warnings or None), now_iso()),
        )

    def _stage_rows(self, job_id: str) -> dict[str, Any]:
        return {r["stage"]: r for r in self.db.all("SELECT * FROM stage_result WHERE job_id = ?", (job_id,))}

    def job_stages(self, job) -> dict[str, dict]:
        """Снимок этапов: база по режиму → сохранённый снимок → результаты этапов → живой heartbeat."""
        params = loads(job["params_json"], {})
        enabled = params.get("stages", {})
        stages: dict[str, dict] = {
            s: {"state": "pending" if enabled.get(s) else "skipped"} for s in STAGES
        }
        for name, st in (loads(job["snapshot_json"], {}) or {}).items():
            if name in stages and name != "boot":
                stages[name] = dict(st)
        live = self.live.get(job["device_id"])
        if live and live.get("job_id") == job["id"] and job["status"] in ("queued", "running"):
            for name, st in (live.get("stages") or {}).items():
                if name in stages and name != "boot":
                    stages[name] = {k: v for k, v in st.items() if k in ("state", "t", "pct", "detail", "warn")}
        # Завершённый этап из события важнее heartbeat, который мог устареть.
        rows = self._stage_rows(job["id"])
        for name, row in rows.items():
            if name in stages and name != "boot":
                if row["state"] not in ("pending", "running") or stages[name].get("state") in ("pending", None):
                    if row["state"] == "running" and stages[name].get("state") == "running":
                        continue
                    stages[name] = {"state": row["state"], "t": row["t"], "detail": row["detail"]}
                warnings = loads(row["warnings_json"])
                if warnings:
                    stages[name]["warnings"] = warnings
        if "boot" in rows:
            row = rows["boot"]
            stages["boot"] = {"state": row["state"], "t": row["t"], "detail": row["detail"]}
            warnings = loads(row["warnings_json"])
            if warnings:
                stages["boot"]["warnings"] = warnings
        if job["status"] in FINAL_STATUSES:
            # Этап, до которого задание не дошло, показывается пропущенным, а не «ещё не запускался».
            for st in stages.values():
                if st.get("state") == "pending":
                    st["state"] = "skipped"
        return stages

    # ------------------------------------------------------------------ представления

    def job_view(self, job) -> dict:
        started = parse_iso(job["started_at"])
        finished = parse_iso(job["finished_at"])
        elapsed = None
        live = self.live.get(job["device_id"])
        if job["status"] == "running" and live and live.get("job_id") == job["id"]:
            elapsed = live.get("elapsed")
        if elapsed is None and started:
            end = finished or datetime.now(timezone.utc)
            elapsed = int((end - started).total_seconds())
        stages = self.job_stages(job)
        return {
            "job_id": job["id"],
            "batch_id": job["batch_id"],
            "mode": job["mode"],
            "status": job["status"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "elapsed": elapsed,
            "boot_deadline": job["boot_deadline"],
            "stages": stages,
            "warnings_count": sum(len(s.get("warnings") or []) for s in stages.values()),
            "result": job["result"],
            "detail": job["detail"],
            "os_name": job["os_name"],
            "board_type": job["board_type"],
            **self.job_eta(job, stages),
        }

    # ------------------------------------------------------------------ прогноз времени

    @staticmethod
    def _eta_key(params: dict, stage: str) -> str:
        """Варианты этапа, у которых разная длительность: сравниваются только задания с тем же вариантом."""
        if stage == "ram":
            return str((params.get("ram") or {}).get("memtester_max_mib"))
        if stage == "net":
            return str((params.get("net") or {}).get("method"))
        if stage == "sdnand":
            return str((params.get("sdnand") or {}).get("pattern"))
        if stage == "flash":
            f = params.get("flash") or {}
            return f"{f.get('os')}:{f.get('image_version')}:{f.get('zero_full')}"
        return ""

    def stage_estimate(self, stage: str, key: str) -> float | None:
        """Медиана длительности этапа по последним успешным заданиям с тем же вариантом (кэш на минуту)."""
        cache = self.__dict__.setdefault("_eta_cache", {})
        hit = cache.get((stage, key))
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        rows = self.db.all(
            "SELECT s.t, j.params_json FROM stage_result s JOIN job j ON j.id = s.job_id"
            " WHERE s.stage = ? AND s.state = 'pass' AND s.t > 0 ORDER BY s.id DESC LIMIT 200", (stage,))
        ts = sorted([r["t"] for r in rows if self._eta_key(loads(r["params_json"], {}) or {}, stage) == key][:30])
        value = ts[len(ts) // 2] if ts else None
        cache[(stage, key)] = (time.monotonic(), value)
        return value

    def job_eta(self, job, stages: dict) -> dict:
        """Сколько ещё идти заданию: текущий этап — по проценту или по медиане, остальные — по медиане.
        eta_partial: для части этапов истории ещё нет, прогноз занижен."""
        if job["status"] not in ACTIVE_STATUSES:
            return {}
        params = loads(job["params_json"], {}) or {}
        total, partial, before_boot = 0.0, False, None
        for name in STAGES:
            if name == "boot":
                before_boot = total
            st = stages.get(name) or {}
            state = st.get("state")
            if state not in ("pending", "running", "waiting"):
                continue
            est = self.stage_estimate(name, self._eta_key(params, name))
            t = float(st.get("t") or 0)
            if name == "boot" and state == "running":
                # Ожидание загрузки: отсчёт от начала ожидания (оператор выключает плату, вынимает microSD)
                deadline = parse_iso(job["boot_deadline"])
                if deadline:
                    t = self.profile.boot_timeout_s - (deadline - datetime.now(timezone.utc)).total_seconds()
            elif state == "running" and not t:
                # heartbeat не несёт время этапа: отсчёт от события stage_started
                row = self.db.one("SELECT updated_at FROM stage_result WHERE job_id = ? AND stage = ?"
                                  " AND state = 'running'", (job["id"], name))
                since = parse_iso(row["updated_at"]) if row else None
                t = (datetime.now(timezone.utc) - since).total_seconds() if since else 0
            pct = st.get("pct")
            if state == "running" and isinstance(pct, (int, float)) and pct >= 5 and t > 0:
                left = t * (100 - pct) / pct
            elif est is not None:
                left = max(est - t, 0) if state != "pending" else est
            else:
                partial = True
                continue
            total += left
        # Когда понадобится оператор: с BOOTCHECK — к началу ожидания загрузки (вынуть microSD), иначе к концу
        boot = (params.get("stages") or {}).get("boot")
        op = before_boot if boot and before_boot is not None else total
        if job["status"] == "awaiting_boot":
            op = 0.0
        return {"eta_s": int(total), "eta_partial": partial, "eta_op_s": int(op)}

    def active_batches(self) -> list[dict]:
        """Партии с незавершёнными заданиями: кто и когда запустил, сколько плат, когда закончится."""
        rows = self.db.all(
            "SELECT b.id, b.name, b.mode, b.kind, b.started_at, o.username, COUNT(j.id) AS boards"
            " FROM job j JOIN batch b ON b.id = j.batch_id LEFT JOIN operator o ON o.id = b.operator_id"
            " WHERE j.status IN ('queued','running','awaiting_boot') GROUP BY b.id ORDER BY b.id")
        out = []
        for r in rows:
            eta, partial, op = 0, False, None
            for job in self.db.all("SELECT * FROM job WHERE batch_id = ? AND status IN"
                                   " ('queued','running','awaiting_boot')", (r["id"],)):
                e = self.job_eta(job, self.job_stages(job))
                eta = max(eta, e.get("eta_s") or 0)
                partial = partial or bool(e.get("eta_partial"))
                if (loads(job["params_json"], {}).get("stages") or {}).get("boot"):
                    op = min(op, e.get("eta_op_s") or 0) if op is not None else e.get("eta_op_s") or 0
            # Первая плата, которой нужен оператор (BOOTCHECK), иначе конец партии
            op = min(op, eta) if op is not None else eta
            out.append({"batch_id": r["id"], "name": r["name"], "mode": r["mode"], "kind": r["kind"],
                        "started_at": r["started_at"], "operator": r["username"], "boards": r["boards"],
                        "eta_at": to_iso(datetime.now(timezone.utc) + timedelta(seconds=eta)),
                        "operator_at": to_iso(datetime.now(timezone.utc) + timedelta(seconds=op)),
                        "eta_partial": partial})
        return out

    def board_view(self, dev) -> dict:
        return {"type": board_of(dev), "detected": dev["board_detected"], "override": dev["board_override"],
                "uplink": {"iface": dev["uplink_iface"], "bus": dev["uplink_bus"], "driver": dev["uplink_driver"],
                           "mac": dev["uplink_mac"]} if dev["uplink_iface"] else None}

    def tile(self, device_id: int, job) -> str:
        if job is not None and job["status"] == "awaiting_boot":
            return "awaiting_boot"
        if not self.is_online(device_id):
            # Прошита и ушла со связи (загрузилась с SD-NAND, агента там нет) — показываем итог, а не «нет связи»
            if job is not None and job["status"] in ("pass", "fail") and (self.job_stages(job).get("flash") or {}).get(
                    "state") == "pass":
                return self._pass_tile(job) if job["status"] == "pass" else self._fail_tile(job)
            return "offline"
        if job is None:
            return "ready"
        if job["status"] == "pass":
            return self._pass_tile(job)
        if job["status"] == "fail":
            return self._fail_tile(job)
        return {
            "queued": "busy", "running": "busy", "pass": "pass", "fail": "fail",
            "interrupted": "interrupted", "offline": "interrupted", "incomplete": "incomplete",
        }.get(job["status"], "ready")

    def _pass_tile(self, job) -> str:
        """PASS, но загрузка с SD-NAND не подтверждена: жёлтая плитка, а не зелёная (дефект не доказан)."""
        boot = self.job_stages(job).get("boot") or {}
        return "boot_unconfirmed" if boot.get("detail") in BOOT_UNCONFIRMED_TEXTS else "pass"

    def _fail_tile(self, job) -> str:
        """FAIL только по сети, а сеть не прошли почти все платы партии: серая «сеть стенда?», не красная."""
        stages = self.job_stages(job)
        failed = {s for s, st in stages.items() if st.get("state") in ("fail", "error")}
        return "net_stand" if failed == {"net"} and self._net_stand_batch(job["batch_id"]) else "fail"

    def _net_stand_batch(self, batch_id: int | None) -> bool:
        """В партии NET не прошли не меньше 3 плат и больше половины плат, у которых сеть проверялась."""
        if batch_id is None:
            return False
        row = self.db.one(
            "SELECT SUM(s.state IN ('fail','error')) AS f, SUM(s.state = 'pass') AS p FROM stage_result s"
            " JOIN job j ON j.id = s.job_id WHERE j.batch_id = ? AND s.stage = 'net'", (batch_id,))
        f, p = (row["f"] or 0, row["p"] or 0) if row else (0, 0)
        return f >= 3 and f * 2 > f + p

    def device_view(self, dev) -> dict:
        job = self._latest_job(dev["id"])
        cpuid = dev["cpuid"] or ""
        last_seen = dev["last_seen"]
        if dev["id"] in self.last_hb and self.is_online(dev["id"]):
            last_seen = now_iso()
        previous = self._previous(dev, job)
        return {
            "device_id": dev["id"],
            "display_no": dev["display_no"],
            "cpuid": cpuid,
            "cpuid_tail": cpuid[-4:].upper(),
            "id_source": dev["id_source"],
            "serial": dev["serial"],
            "online": self.is_online(dev["id"]),
            "last_seen": last_seen,
            "ip": dev["ip"],
            "mac": dev["mac"],
            "mac_source": dev["mac_source"],
            "agent_version": dev["agent_version"],
            "test_sd_version": dev["test_sd_version"],
            "kernel": dev["kernel"],
            "ram_bytes": dev["ram_bytes"],
            "has_led": bool(dev["has_led"]),
            "simulated": bool(dev["simulated"]),
            "is_golden": bool(dev["is_golden"]),
            "board": self.board_view(dev),
            "tested_before": bool(dev["tested_pass"]) or self.db.one(
                "SELECT 1 FROM job j JOIN batch b ON b.id = j.batch_id WHERE j.device_id = ? AND b.kind = 'production'"
                " AND j.status IN ('pass','fail','interrupted','offline','incomplete') LIMIT 1", (dev["id"],)) is not None,
            "tile": "ready" if previous else self.tile(dev["id"], job),
            "job": self.job_view(job) if job is not None else None,
            "previous": previous,
        }

    def _previous(self, dev, job) -> dict | None:
        """Итог прошлой проверки, если плату перезагрузили после него (другой boot_id): оператор
        должен видеть, что подключил уже проверенную плату, а не принять старый итог за текущий."""
        if (job is None or job["status"] not in FINAL_STATUSES or not self.is_online(dev["id"])
                or not dev["boot_id"] or not job["boot_id"] or dev["boot_id"] == job["boot_id"]):
            return None
        detail = job["detail"]
        if job["status"] != "pass":
            for name, st in self.job_stages(job).items():
                if st.get("state") in ("fail", "error", "interrupted"):
                    detail = st.get("detail") or detail
                    break
        batch = self.db.one("SELECT name FROM batch WHERE id = ?", (job["batch_id"],))
        return {"job_id": job["id"], "status": job["status"], "mode": job["mode"],
                "finished_at": job["finished_at"], "batch": batch["name"] if batch else None,
                "detail": detail, "warnings_count": self.job_view(job).get("warnings_count", 0)}

    def _grid_since(self) -> str:
        """С какого момента плата без связи ещё видна в сетке."""
        return to_iso(datetime.now(timezone.utc) - timedelta(hours=self.settings.grid_window_h))

    def _grid_hidden(self) -> set[int]:
        """Платы, скрытые кнопкой «Скрыть офлайн» (до следующего подключения)."""
        row = self.db.one("SELECT value FROM meta WHERE key = 'grid_hidden'")
        return set(loads(row["value"], [])) if row else set()

    def _set_grid_hidden(self, ids: set[int]) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('grid_hidden', ?)", (dumps(sorted(ids)),))

    def _visible_devices(self) -> list:
        since = self._grid_since()
        hidden = self._grid_hidden()
        rows = [r for r in self.db.all(
            "SELECT *, id IN (SELECT device_id FROM job WHERE status IN ('queued','running','awaiting_boot'))"
            " AS has_active FROM device WHERE last_seen >= ? OR id IN"
            " (SELECT device_id FROM job WHERE status IN ('queued','running','awaiting_boot'))"
            " ORDER BY display_no, id",
            (since,),
        ) if r["has_active"] or r["id"] not in hidden]
        online_ids = {d for d in self.agents if self.is_online(d)}
        known = {r["id"] for r in rows}
        extra = [d for d in online_ids if d not in known]
        if extra:
            rows += self.db.all(
                f"SELECT * FROM device WHERE id IN ({','.join('?' * len(extra))})", extra
            )
        return rows

    def devices_view(self) -> list[dict]:
        return [self.device_view(d) for d in self._visible_devices()]

    def summary(self, devices: list[dict] | None = None, operator: str | None = None) -> dict:
        devices = devices if devices is not None else self.devices_view()
        counts = {"total": len(devices), "online": 0, "busy": 0, "pass": 0, "fail": 0, "offline": 0,
                  "interrupted": 0, "incomplete": 0, "pass_warn": 0, "boot_unconfirmed": 0,
                  "net_stand": 0}
        for d in devices:
            if d["online"]:
                counts["online"] += 1
            tile = d["tile"]
            if tile in ("busy", "awaiting_boot"):
                counts["busy"] += 1
            elif tile in ("pass", "fail", "offline", "interrupted", "incomplete", "boot_unconfirmed", "net_stand"):
                counts[tile] += 1
            if tile == "pass" and (d["job"] or {}).get("warnings_count"):
                counts["pass_warn"] += 1
        batch = self.db.one(
            "SELECT b.*, o.username FROM batch b LEFT JOIN operator o ON o.id = b.operator_id"
            " ORDER BY b.id DESC LIMIT 1"
        )
        batch_view = None
        if batch:
            batch_view = {
                "batch_id": batch["id"], "name": batch["name"], "mode": batch["mode"], "kind": batch["kind"],
                "operator": batch["username"], "started_at": batch["started_at"],
                "finished_at": batch["finished_at"], "golden": batch["golden_result"],
            }
        test_sd = sorted({d["test_sd_version"] for d in devices if d["online"] and d["test_sd_version"]})
        return {
            **counts,
            "batch": batch_view,
            "versions": {"test_sd": test_sd, "image": self.profile.image_version, "server": __version__},
            "operator": operator if operator is not None else (batch_view or {}).get("operator"),
            "server_time": now_iso(),
            "net": self.net.stats(),
            "hardware_validated": self.profile.hardware_validated,
            "expected": self.expected_boards(),
            "images": self.images_view(),
            "active": self.active_batches(),
        }

    def images_view(self) -> dict:
        online = [d for d in self.agents if self.is_online(d)]
        files: dict[str, int] = {}
        for d in online:
            for name, st in (self.card_images.get(d) or {}).items():
                if st.get("state") == "ready":
                    files[name] = files.get(name, 0) + 1
        variants = self.catalog.variants(files, len(online))
        menu = self.catalog.menu(variants)
        if online and not any((self.card_agent.get(d) or {}).get("sd_image") for d in online):
            # На новых картах (образ microSD со страницы «microSD») встроенного образа нет
            menu = [m for m in menu if m["key"] != SD]
        nos = {r["id"]: r["display_no"] for r in self.db.all(
            "SELECT id, display_no FROM device WHERE id IN (" + ",".join("?" * len(online)) + ")", online)
        } if online else {}
        cards = [{"no": nos.get(d), **(self.card_agent.get(d) or {})} for d in online]
        return {"menu": menu, "variants": variants, "sync": bool(self._sync_allowed),
                "agent": self.sdcard.agent_view(cards)}

    # ------------------------------------------------------------------ образы ОС на картах

    def _catalog_msg(self) -> dict:
        return {"type": "images", "sync": bool(self._sync_allowed), "images": self.catalog.agent_catalog(),
                "agent": self.sdcard.agent_info()}

    async def push_catalog(self) -> None:
        msg = self._catalog_msg()
        for conn in list(self.agents.values()):
            await conn.send(msg)

    def _sync_now(self) -> bool:
        """Карты обновляют образы только между партиями: скачивание не мешает тесту сети и записи."""
        return self.db.one("SELECT 1 FROM job WHERE status IN ('queued','running') LIMIT 1") is None

    def resolve_os(self, options: dict, device_ids: list[int]) -> tuple[dict, dict]:
        """Выбор меню «ОС для внутреннего накопителя» → параметры FLASH для агента и поля задания для отчёта."""
        key = options.get("os") or SD
        if key == SD:
            missing = [d for d in device_ids if not (self.card_agent.get(d) or {}).get("sd_image", True)]
            if missing:
                nos = [str((self.db.one("SELECT display_no FROM device WHERE id = ?", (d,)) or {"display_no": d})
                           ["display_no"]) for d in missing]
                raise FactoryError("на microSD плат " + ", ".join(nos) + " нет встроенного образа:"
                                   " выберите ОС из меню", 409)
            ver = self.profile.image_version
            return ({"os": SD, "os_name": "Образ на microSD"},
                    {"os_key": SD, "os_name": "Образ на microSD", "image_version": ver,
                     "image_file": self.profile.section("flash").get("image_file"), "image_url": None})
        if key == ZERO:
            zero = options.get("zero") or "quick"
            if zero not in ("quick", "full"):
                raise FactoryError("zero: quick | full")
            name = "Zeroimage (полная)" if zero == "full" else "Zeroimage"
            return ({"os": ZERO, "os_name": name, "method": "zero", "full": zero == "full"},
                    {"os_key": ZERO, "os_name": name, "image_version": "zeroimage", "image_file": None,
                     "image_url": None})
        img = self.catalog.current(key)
        if img is None:
            v = self.db.one("SELECT name FROM os_variant WHERE key = ?", (key,))
            if v is None:
                raise FactoryError(f"нет ОС «{key}» в меню")
            raise FactoryError(f"образ «{v['name']}» ещё не готов на сервере", 409)
        if key != CUSTOM and not img["visible"]:
            raise FactoryError(f"ОС «{img['vname']}» убрана из меню")
        not_ready = []
        for d in device_ids:
            st = (self.card_images.get(d) or {}).get(img["file"]) or {}
            if st.get("state") != "ready":
                dev = self.db.one("SELECT display_no FROM device WHERE id = ?", (d,))
                pct = f" {st['pct']:.0f} %" if st.get("state") == "downloading" and st.get("pct") is not None else ""
                not_ready.append(f"{dev['display_no'] if dev else d}{pct}")
        if not_ready:
            raise FactoryError(f"образ «{img['vname']}» ещё не на картах плат: {', '.join(not_ready)}", 409,
                               {"not_ready": not_ready})
        name = f"{img['vname']} {img['version']}" if img["version"] else img["vname"]
        return ({"os": key, "os_name": name, "manifest_file": f"catalog/{img['file'][:-len('.img.zst')]}.manifest.json",
                 "image_version": img["version"], "image_file": img["file"]},
                {"os_key": key, "os_name": name, "image_version": img["version"], "image_file": img["source_file"],
                 "image_url": img["url"]})

    def state(self, operator: str | None = None) -> dict:
        devices = self.devices_view()
        return {"devices": devices, "summary": self.summary(devices, operator)}

    # ------------------------------------------------------------------ агент: подключение

    def _assign_display_no(self, device_id: int) -> int:
        used = {r["display_no"] for r in self._visible_devices() if r["id"] != device_id and r["display_no"]}
        n = 1
        while n in used:
            n += 1
        return n

    async def agent_hello(self, conn: AgentConn, msg: dict) -> None:
        if msg.get("simulated") and not self.settings.allow_simulated:
            await conn.send({"type": "error", "detail": "simulated agents are not allowed on this server"})
            raise FactoryError("simulated agent rejected", 4403)

        cpuid = msg.get("cpuid")
        id_source = msg.get("id_source") or "none"
        if not cpuid:
            cpuid = "A" + secrets.token_hex(8).upper()
            id_source = "assigned"
        now = now_iso()
        dev = self.db.one("SELECT * FROM device WHERE cpuid = ?", (cpuid,))

        if dev is not None:
            other = self.agents.get(dev["id"])
            if other is not None and other is not conn and not other.closed and self.is_online(dev["id"]):
                if other.mac and msg.get("mac") and other.mac != msg.get("mac"):
                    self.audit(None, "duplicate_cpuid", {"cpuid": cpuid, "mac": msg.get("mac"), "other_mac": other.mac})
                    await conn.send({"type": "error", "detail": f"duplicate cpuid {cpuid}"})
                    raise FactoryError("duplicate cpuid", 4409)

        fields = {
            "id_source": id_source,
            "mac": msg.get("mac"),
            "mac_source": msg.get("mac_source"),
            "ip": msg.get("ip"),
            "hostname": msg.get("hostname"),
            "agent_version": msg.get("agent_version"),
            "test_sd_version": msg.get("test_sd_version"),
            "kernel": msg.get("kernel"),
            "ram_bytes": msg.get("ram_bytes"),
            "has_led": 1 if msg.get("has_led") else 0,
            "simulated": 1 if msg.get("simulated") else 0,
            "inventory_json": dumps(msg.get("inventory")),
            "selfcheck_json": dumps(msg.get("sd_selfcheck")),
            "boot_id": msg.get("boot_id"),
            "last_seen": now,
        }
        up = msg.get("uplink") if isinstance(msg.get("uplink"), dict) else None
        if up and up.get("iface"):
            fields.update(board_detected=detect_board(up), uplink_iface=str(up["iface"])[:32],
                          uplink_bus=up.get("bus"), uplink_driver=up.get("driver"), uplink_mac=up.get("mac"))
        if dev is None:
            cur = self.db.execute(
                "INSERT INTO device (cpuid, first_seen, " + ", ".join(fields) + ") VALUES (?, ?, "
                + ", ".join("?" * len(fields)) + ")",
                (cpuid, now, *fields.values()),
            )
            device_id = cur.lastrowid
            display_no = self._assign_display_no(device_id)
        else:
            device_id = dev["id"]
            hidden = self._grid_hidden()
            stale = dev["last_seen"] < self._grid_since() or device_id in hidden
            if device_id in hidden:
                self._set_grid_hidden(hidden - {device_id})
            display_no = dev["display_no"] if dev["display_no"] and not stale else self._assign_display_no(device_id)
            self.db.execute(
                "UPDATE device SET " + ", ".join(f"{k} = ?" for k in fields) + " WHERE id = ?",
                (*fields.values(), device_id),
            )
        self.db.execute("UPDATE device SET display_no = ? WHERE id = ?", (display_no, device_id))
        if msg.get("test_sd_version"):
            self.db.execute(
                "INSERT OR IGNORE INTO test_sd_version (version, first_seen) VALUES (?, ?)",
                (msg["test_sd_version"], now),
            )

        old = self.agents.get(device_id)
        if old is not None and old is not conn:
            old.closed = True
            try:
                await old.ws.close(code=4000)
            except Exception:
                pass
        conn.device_id = device_id
        conn.cpuid = cpuid
        client = getattr(conn.ws, "client", None)
        self.peer_ip[device_id] = getattr(client, "host", None)
        conn.mac = msg.get("mac")
        conn.stream = msg.get("stream")
        self.agents[device_id] = conn
        self.last_hb[device_id] = time.monotonic()

        serial = self.db.one("SELECT serial FROM device WHERE id = ?", (device_id,))["serial"]
        await conn.send({"type": "hello_ack", "device_id": device_id, "cpuid": cpuid, "serial": serial,
                         "server_time": time.time()})
        if isinstance(msg.get("images"), dict):
            self.card_images[device_id] = msg["images"]
        sc = msg.get("sd_selfcheck") or {}
        self.card_agent[device_id] = {"agent_version": msg.get("agent_version"), "update": msg.get("agent_update"),
                                      "sd_image": sc.get("state") not in ("n/a",)}
        if self._sync_allowed is None:
            self._sync_allowed = self._sync_now()
        await conn.send(self._catalog_msg())

        current = msg.get("current_job") or None
        if current and current.get("job_id"):
            self.live[device_id] = current
            await self._reconcile_current_job(conn, device_id, current)
        else:
            self.live.pop(device_id, None)
            active = self._active_job(device_id)
            if active is not None and active["status"] == "running":
                # Агент не знает о задании, которое сервер считает выполняющимся (стёрт state на microSD).
                await self._finalize(active, "interrupted", detail="агент потерял задание")
        await self._dispatch_queued(device_id)
        self.mark(device_id)

    async def _reconcile_current_job(self, conn: AgentConn, device_id: int, current: dict) -> None:
        job = self._job(current["job_id"])
        if job is None or job["device_id"] != device_id:
            return
        if current.get("stages"):
            self.db.execute("UPDATE job SET snapshot_json = ? WHERE id = ?", (dumps(current["stages"]), job["id"]))
        if job["status"] == "queued":
            self.db.execute("UPDATE job SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
                            (now_iso(), job["id"]))
        if job["id"] in self.cancel_requested and not current.get("finished"):
            await conn.send({"type": "cancel", "job_id": job["id"]})
        # Задание, которое сервер успел пометить offline, снова считается выполняющимся.
        if job["status"] == "offline" and not current.get("finished"):
            self.db.execute("UPDATE job SET status = 'running', result = NULL, finished_at = NULL WHERE id = ?",
                            (job["id"],))

    async def _dispatch_queued(self, device_id: int) -> None:
        conn = self.agents.get(device_id)
        if conn is None:
            return
        live = self.live.get(device_id) or {}
        if live.get("job_id") and not live.get("finished"):
            return  # у агента уже идёт задание; второе не выдаём
        job = self.db.one(
            "SELECT * FROM job WHERE device_id = ? AND status = 'queued' ORDER BY created_at, rowid LIMIT 1",
            (device_id,),
        )
        if job is None:
            return
        self.db.execute("UPDATE job SET boot_id = (SELECT boot_id FROM device WHERE id = ?) WHERE id = ?",
                        (device_id, job["id"]))
        await conn.send({"type": "job", "job_id": job["id"], "mode": job["mode"],
                         "params": loads(job["params_json"], {})})

    async def agent_disconnected(self, conn: AgentConn) -> None:
        device_id = conn.device_id
        conn.closed = True
        if device_id is None or self.agents.get(device_id) is not conn:
            return
        del self.agents[device_id]
        live = self.live.get(device_id)
        if live and live.get("job_id") and live.get("stages"):
            self.db.execute("UPDATE job SET snapshot_json = ? WHERE id = ? AND status IN ('queued','running')",
                            (dumps(live["stages"]), live["job_id"]))
        self.db.execute("UPDATE device SET last_seen = ? WHERE id = ?", (now_iso(), device_id))
        await self._send_grants(self.net.release(device_id))
        self.mark(device_id)

    # ------------------------------------------------------------------ агент: сообщения

    async def agent_message(self, conn: AgentConn, msg: dict) -> None:
        device_id = conn.device_id
        if device_id is None:
            raise FactoryError("hello required first", 4400)
        self.last_hb[device_id] = time.monotonic()
        kind = msg.get("type")
        if kind == "heartbeat":
            await self._heartbeat(device_id, msg)
        elif kind == "event":
            await self._event(conn, device_id, msg)
        elif kind == "net_request":
            grant = self.net.request(device_id, msg.get("job_id") or "")
            if grant is not None:
                await self._send_grants([grant])
            self.summary_dirty = True
        elif kind == "net_release":
            await self._send_grants(self.net.release(device_id))
            self.summary_dirty = True
        elif kind == "serial_request":
            await conn.send(self._serial_reply(device_id, msg.get("job_id")))
        elif kind == "images_status":
            self.card_images[device_id] = msg.get("files") or {}
            self.summary_dirty = True
        else:
            await conn.send({"type": "error", "detail": f"unknown message type {kind!r}"})

    async def _heartbeat(self, device_id: int, msg: dict) -> None:
        prev = self.live.get(device_id) or {}
        self.live[device_id] = msg
        job_id = msg.get("job_id")
        if job_id and prev.get("job_id") != job_id:
            job = self._job(job_id)
            if job is not None and job["device_id"] == device_id and job["status"] == "queued":
                self.db.execute(
                    "UPDATE job SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
                    (now_iso(), job_id),
                )
        if (not job_id or msg.get("finished")) and prev.get("job_id") != job_id:
            await self._dispatch_queued(device_id)
        self.mark(device_id)

    async def _event(self, conn: AgentConn, device_id: int, msg: dict) -> None:
        stream = msg.get("stream") or conn.stream or f"dev{device_id}"
        seq = int(msg.get("seq", -1))
        cur = self.db.execute(
            "INSERT OR IGNORE INTO event (device_id, job_id, stream, seq, ts, kind, stage, data_json, received_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, msg.get("job_id"), stream, seq, msg.get("ts"), msg.get("kind", "?"), msg.get("stage"),
             dumps(msg.get("data")), now_iso()),
        )
        if cur.rowcount:
            await self._apply_event(device_id, msg)
        await conn.send({"type": "ack", "stream": stream, "seq": seq})

    async def _apply_event(self, device_id: int, msg: dict) -> None:
        # Событие относится к плате своего задания: microSD с недосланной очередью
        # могли переставить в другую плату.
        job = self._job(msg.get("job_id"))
        if job is None:
            return
        device_id = job["device_id"]
        kind = msg.get("kind")
        stage = msg.get("stage")
        data = msg.get("data") or {}
        if kind == "job_started":
            if job["status"] == "queued":
                self.db.execute("UPDATE job SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
                                (now_iso(), job["id"]))
        elif kind == "stage_started" and stage in STAGES and stage != "boot":
            self._set_stage(job["id"], stage, "running")
        elif kind == "stage_finished" and stage in STAGES and stage != "boot":
            self._set_stage(job["id"], stage, data.get("state", "error"), data.get("t"), data.get("detail"),
                            data.get("measurements"), data.get("log"), data.get("warnings"))
            if stage == "flash":
                m = data.get("measurements") or {}
                self.db.execute(
                    "UPDATE job SET image_version = COALESCE(?, image_version),"
                    " image_sha256 = COALESCE(?, image_sha256) WHERE id = ?",
                    (m.get("image_version"), m.get("sha256_readback") or m.get("manifest_sha256"), job["id"]),
                )
                if m.get("image_version") and not m.get("zero"):
                    self.db.execute(
                        "INSERT OR IGNORE INTO image (version, sha256, size_bytes, first_seen) VALUES (?, ?, ?, ?)",
                        (m["image_version"], m.get("sha256_readback") or m.get("manifest_sha256"),
                         m.get("image_size"), now_iso()),
                    )
            self._check_tested(job)
        elif kind == "job_finished":
            if job["status"] in OVERRIDABLE_STATUSES:
                if data.get("stages"):
                    self.db.execute("UPDATE job SET snapshot_json = ? WHERE id = ?",
                                    (dumps(data["stages"]), job["id"]))
                await self._agent_finished(self._job(job["id"]), data)
        self.mark(device_id)

    def _check_tested(self, job) -> bool:
        """TEST_PASS: включённые тесты (RAM, NET, SD-NAND) пройдены в этом задании. Сеть, пропущенная
        по правилам (NAPI-P, «Без проверки сети»), отметке не мешает."""
        if job["mode"] not in ("test_only", "test_flash"):
            return False
        rows = self._stage_rows(job["id"])
        enabled = loads(job["params_json"], {}).get("stages", {})
        need = [s for s in TEST_STAGES if enabled.get(s, True)]
        if need and all(rows.get(s) is not None and rows[s]["state"] == "pass" for s in need):
            self.db.execute("UPDATE device SET tested_pass = 1 WHERE id = ?", (job["device_id"],))
            return True
        return False

    def _tests_ok_for_flash(self, job) -> bool:
        """«Тесты + прошивка»: можно шить — включённые тесты пройдены; FAIL сети прошивке не мешает."""
        enabled = loads(job["params_json"], {}).get("stages", {})
        rows = self._stage_rows(job["id"])
        for s in TEST_STAGES:
            if not enabled.get(s):
                continue
            state = rows[s]["state"] if s in rows else None
            if state != "pass" and not (s == "net" and state in ("fail", "error")):
                return False
        return True

    def _tested_except_net(self, device_id: int) -> bool:
        """«Только прошивка» после тестов, где не прошла только сеть: FAIL сети прошивке не мешает."""
        job = self.db.one("SELECT * FROM job WHERE device_id = ? AND mode IN ('test_only','test_flash')"
                          " AND status IN ('pass','fail') ORDER BY created_at DESC, rowid DESC LIMIT 1", (device_id,))
        return job is not None and self._tests_ok_for_flash(job)

    async def _agent_finished(self, job, data: dict) -> None:
        result = data.get("result")
        params = loads(job["params_json"], {})
        enabled = params.get("stages", {})
        stages = self.job_stages(job)
        if result == "cancelled":
            await self._finalize(job, "cancelled", detail=data.get("detail"))
            return
        if result == "interrupted":
            await self._finalize(job, "interrupted", detail=data.get("detail"))
            return
        if result == "incomplete" and not any(
                (stages.get(s) or {}).get("state") in ("fail", "error") for s in STAGES):
            # Этап заблокирован предохранителем: не PASS и не дефект платы (серым, не красным)
            await self._finalize(job, "incomplete", detail=data.get("detail"))
            return
        before_boot = verdict(enabled, stages, include_boot=False)
        if (before_boot == "pass" and result == "pass") or (net_only_fail(enabled, stages) and result == "fail"):
            if enabled.get("boot"):
                deadline = to_iso(datetime.now(timezone.utc) + timedelta(seconds=self.profile.boot_timeout_s))
                self.db.execute("UPDATE job SET status = 'awaiting_boot', boot_deadline = ? WHERE id = ?",
                                (deadline, job["id"]))
                self._set_stage(job["id"], "boot", "running", detail=self._boot_wait_text())
                self.mark(job["device_id"])
                early = self.early_boot.pop(job["id"], None)
                if early is not None:
                    await self._complete_boot(self._job(job["id"]), early)
            else:
                await self._finalize(job, "pass")
            return
        detail = data.get("detail")
        if before_boot == "pass" and result != "pass":
            detail = detail or f"агент сообщил {result}"
        await self._finalize(job, "fail" if before_boot != "interrupted" else "interrupted", detail=detail)

    async def _finalize(self, job, status: str, detail: str | None = None) -> None:
        if status == "pass":
            # Прошита после FAIL сети: BOOTCHECK пройден или не подтверждён, но итог — FAIL
            failed = [st for s, st in self.job_stages(job).items() if s != "boot" and st.get("state") in ("fail", "error")]
            if failed:
                status, detail = "fail", f"{failed[0].get('detail') or 'FAIL'} · прошита"
        result = status
        self.db.execute(
            "UPDATE job SET status = ?, result = ?, detail = NULLIF(COALESCE(?, detail), ''),"
            " finished_at = ?, started_at = COALESCE(started_at, ?) WHERE id = ?",
            (status, result, detail, now_iso(), now_iso(), job["id"]),
        )
        self.cancel_requested.discard(job["id"])
        self.early_boot.pop(job["id"], None)
        self.boot_state.pop(job["id"], None)
        if status == "pass" and job["mode"] == "test_only":
            self._assign_serial(job["device_id"], job["batch_id"])
        self._finish_batch_if_done(job["batch_id"])
        self.mark(job["device_id"])

    # ------------------------------------------------------------------ серийники

    def normalize_serials(self) -> None:
        """serial.source: cpuid — серийник любой платы равен ID чипа, в том числе выданный раньше
        по другой схеме (NC-…). Выполняется при старте сервера."""
        if self.profile.section("serial").get("source") != "cpuid":
            return
        rows = self.db.all("SELECT id, serial, cpuid FROM device WHERE serial IS NOT NULL AND serial != cpuid")
        for r in rows:
            self.db.execute("UPDATE device SET serial = ? WHERE id = ?", (r["cpuid"], r["id"]))
            self.audit(None, "serial_reassigned", {"device_id": r["id"], "old": r["serial"], "serial": r["cpuid"]})

    def _assign_serial(self, device_id: int, batch_id: int | None) -> str:
        dev = self.db.one("SELECT serial, cpuid FROM device WHERE id = ?", (device_id,))
        cfg = self.profile.section("serial")
        if dev["serial"] and (cfg.get("source") != "cpuid" or dev["serial"] == dev["cpuid"]):
            return dev["serial"]
        if cfg.get("source") == "cpuid":
            # Серийник = ID чипа (Rockchip serial# из OTP): его же видит ОС в /proc/device-tree/serial-number
            row = self.db.one("SELECT cpuid FROM device WHERE id = ?", (device_id,))
            serial = row["cpuid"]
            self.db.execute("UPDATE device SET serial = ? WHERE id = ?", (serial, device_id))
            self.audit(None, "serial_assigned", {"device_id": device_id, "serial": serial, "source": "cpuid"})
            return serial
        n = self._meta_next("serial_counter")
        fmt = cfg.get("format", "NC-{yy}{mm}-{n:04d}")
        now = datetime.now(timezone.utc)
        serial = fmt.format(n=n, yy=now.strftime("%y"), mm=now.strftime("%m"), yyyy=now.strftime("%Y"),
                            batch=batch_id or 0)
        self.db.execute("UPDATE device SET serial = ? WHERE id = ?", (serial, device_id))
        self.audit(None, "serial_assigned", {"device_id": device_id, "serial": serial})
        return serial

    def _serial_reply(self, device_id: int, job_id: str | None) -> dict:
        job = self._job(job_id)
        reply: dict[str, Any] = {"type": "serial", "job_id": job_id}
        if job is None or job["device_id"] != device_id or job["status"] not in ("queued", "running"):
            reply["error"] = "no active job"
            return reply
        dev = self.db.one("SELECT serial, tested_pass FROM device WHERE id = ?", (device_id,))
        if job["mode"] == "test_flash":
            self._check_tested(job)
            if not self._tests_ok_for_flash(job):
                reply["error"] = "tests not passed in this job"
                return reply
        elif job["mode"] == "flash_only":
            requires = bool(self.profile.section("flash").get("flash_only_requires_tested", True))
            if not dev["serial"] and not dev["tested_pass"] and requires and not self._tested_except_net(device_id):
                reply["error"] = "device has no TEST_PASS"
                return reply
        else:
            reply["error"] = "mode has no provisioning"
            return reply
        serial = self._assign_serial(device_id, job["batch_id"])
        batch = self.db.one("SELECT name FROM batch WHERE id = ?", (job["batch_id"],))
        reply.update(serial=serial, batch=batch["name"], date=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        self.mark(device_id)
        return reply

    async def _send_grants(self, grants: list[Grant]) -> None:
        for grant in grants:
            conn = self.agents.get(grant.device_id)
            if conn is None:
                self.net.release(grant.device_id)
                continue
            host = self.settings.iperf_host or conn.server_host
            await conn.send({"type": "net_grant", "job_id": grant.job_id, "host": host, "port": grant.port})

    # ------------------------------------------------------------------ оператор

    async def start_batch(self, operator: dict, mode: str, device_ids: list[int], kind: str = "production",
                          options: dict | None = None, confirm: bool = False) -> dict:
        if mode not in MODES:
            raise FactoryError(f"неизвестный режим {mode}")
        if kind not in ("production", "golden"):
            raise FactoryError("kind: production | golden")
        if kind == "golden" and mode != "test_only":
            raise FactoryError("проверка стенда выполняется в режиме «Только тесты»")
        if mode in DESTRUCTIVE_MODES and not confirm:
            raise FactoryError("разрушающий режим требует подтверждения (confirm: true)")
        device_ids = sorted(set(int(d) for d in device_ids))
        if not device_ids:
            raise FactoryError("не выбрано ни одной платы")
        rows = self.db.all(f"SELECT id FROM device WHERE id IN ({','.join('?' * len(device_ids))})", device_ids)
        missing = set(device_ids) - {r["id"] for r in rows}
        if missing:
            raise FactoryError("неизвестные платы", 404, sorted(missing))
        offline = [d for d in device_ids if not self.is_online(d)]
        if offline:
            raise FactoryError(f"нет связи с платами {self._nos(offline)}: снимите их с выбора", 409, offline)
        busy = [d for d in device_ids if self._active_job(d) is not None]
        if busy:
            raise FactoryError(f"у плат {self._nos(busy)} уже есть активное задание", 409, busy)
        if kind == "production" and not (options or {}).get("allow_retest"):
            seen = self.previously_tested(device_ids, mode)
            if seen:
                raise FactoryError("часть плат уже проверялась", 409, {"retest": seen})
        os_flash, os_job = ({}, {}) if mode not in DESTRUCTIVE_MODES else self.resolve_os(options or {}, device_ids)
        try:
            params = self.profile.job_params(mode, options, self.settings.destructive_override, os_flash)
        except ValueError as exc:
            raise FactoryError(str(exc)) from exc
        # Тип плат для всей партии: запоминается у плат, как «Тип платы» на панели выбора
        board_opt = (options or {}).get("board") or None
        warnings: list[str] = []
        if board_opt is not None:
            if board_opt not in BOARDS:
                raise FactoryError("тип плат: napi-c | napi-p | по плате")
            warnings = self.set_board(operator, device_ids, board_opt)

        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        prefix = "G" if kind == "golden" else "B"
        n = self._meta_next(f"batch_counter_{prefix}_{today}")
        name = f"{prefix}-{today}-{n:02d}"
        golden = None
        if kind == "production":
            golden = self.db.one(
                "SELECT id, golden_result FROM batch WHERE kind = 'golden' AND finished_at IS NOT NULL"
                " ORDER BY id DESC LIMIT 1"
            )
        now = now_iso()
        cur = self.db.execute(
            "INSERT INTO batch (name, kind, mode, operator_id, started_at, image_version, profile_name, options_json,"
            " golden_batch_id, golden_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, kind, mode, operator["id"], now,
             os_job.get("image_version") if mode in DESTRUCTIVE_MODES else None, self.profile.name,
             dumps(options or {}), golden["id"] if golden else None, golden["golden_result"] if golden else None),
        )
        batch_id = cur.lastrowid
        job_ids = []
        boards: dict[str, int] = {}
        for device_id in device_ids:
            job_id = "j_" + secrets.token_hex(8)
            dev = self.db.one("SELECT * FROM device WHERE id = ?", (device_id,))
            board = board_of(dev)
            boards[board] = boards.get(board, 0) + 1
            if board in NO_NET and params["stages"].get("net"):
                dev_params = {**params, "stages": {**params["stages"], "net": False},
                              "skip_reason": {**(params.get("skip_reason") or {}), "net": NO_NET[board]},
                              "board": board}
            else:
                dev_params = {**params, "board": board}
            self.db.execute(
                "INSERT INTO job (id, batch_id, device_id, mode, status, created_at, params_json, test_sd_version,"
                " agent_version, os_key, os_name, image_file, image_url, board_type, board_detected, uplink_iface)"
                " VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, batch_id, device_id, mode, now, dumps(dev_params), dev["test_sd_version"], dev["agent_version"],
                 os_job.get("os_key"), os_job.get("os_name"), os_job.get("image_file"), os_job.get("image_url"),
                 board, dev["board_detected"], dev["uplink_iface"]),
            )
            job_ids.append(job_id)
        self.audit(operator["username"], "batch_start",
                   {"batch": name, "mode": mode, "kind": kind, "devices": device_ids, "options": options or {},
                    "boards": boards,
                    **({"os": os_job.get("os_name"), "image": os_job.get("image_file")} if os_job else {})})
        self._sync_allowed = False       # партия идёт: карты не качают образы (см. tick)
        await self.push_catalog()
        if (options or {}).get("allow_retest"):
            self.audit(operator["username"], "retest_confirmed",
                       {"batch": name, "devices": [r["device_id"] for r in self.previously_tested(device_ids, mode)]})
        for device_id in device_ids:
            await self._dispatch_queued(device_id)
            self.mark(device_id)
        return {"batch_id": batch_id, "name": name, "job_ids": job_ids, "warnings": warnings}

    def _nos(self, device_ids: list[int]) -> str:
        """Номера плат на стенде для сообщений оператору."""
        rows = self.db.all(f"SELECT id, display_no FROM device WHERE id IN ({','.join('?' * len(device_ids))})",
                           device_ids)
        nos = {r["id"]: r["display_no"] for r in rows}
        return ", ".join(str(nos.get(d) or d) for d in device_ids)

    def _seen_since(self, device_id: int, since: str) -> bool:
        row = self.db.one("SELECT last_seen FROM device WHERE id = ?", (device_id,))
        return bool(row) and row["last_seen"] >= since

    async def refresh_grid(self, operator: dict) -> dict:
        """«Скрыть офлайн, обнаружить онлайн платы»: платы без связи и без активного задания уходят из сетки
        (история остаётся), свободные агенты переподключаются и заново присылают IP, MAC и тип платы."""
        visible = self._visible_devices()
        hidden, kept = [], []
        for dev in visible:
            if self.is_online(dev["id"]):
                continue
            (kept if self._active_job(dev["id"]) is not None else hidden).append(dev["id"])
        since = self._grid_since()
        self._set_grid_hidden({d for d in self._grid_hidden() if d not in hidden and self._seen_since(d, since)}
                              | set(hidden))
        reconnect = []
        idle = [d for d in self.agents if self.is_online(d) and self._active_job(d) is None
                and not ((self.live.get(d) or {}).get("job_id") and not (self.live.get(d) or {}).get("finished"))]
        for device_id, conn in list(self.agents.items()):
            if device_id in idle:
                conn.closed = True
                try:
                    await conn.ws.close(code=4000)
                except Exception:
                    pass
                reconnect.append(device_id)
        self.audit(operator["username"], "grid_refresh", {"hidden": hidden, "kept_busy": kept, "reconnect": reconnect})
        self.grid_reset = True
        return {"hidden": len(hidden), "kept_busy": self._nos(kept) if kept else "", "reconnect": len(reconnect)}

    def previously_tested(self, device_ids: list[int], mode: str) -> list[dict]:
        """Платы, которые уже проходили этот вид проверки (по cpuid в базе).

        test_only/test_flash: было завершённое задание с тестами; flash_only: SD-NAND уже прошивалась.
        """
        if mode == "flash_only":
            cond = "j.mode IN ('test_flash','flash_only') AND EXISTS (SELECT 1 FROM stage_result r" \
                   " WHERE r.job_id = j.id AND r.stage = 'flash' AND r.state = 'pass')"
        else:
            cond = "j.mode IN ('test_only','test_flash')"
        out = []
        for device_id in device_ids:
            row = self.db.one(
                "SELECT j.id, j.mode, j.status, j.finished_at, j.created_at, b.name AS batch, d.serial, d.cpuid,"
                " (SELECT COUNT(*) FROM job j2 WHERE j2.device_id = j.device_id"
                "  AND j2.status IN ('pass','fail','interrupted','offline','incomplete')) AS runs"
                " FROM job j JOIN batch b ON b.id = j.batch_id JOIN device d ON d.id = j.device_id"
                f" WHERE j.device_id = ? AND j.status IN ('pass','fail','interrupted','offline','incomplete') AND b.kind = 'production'"
                f" AND {cond} ORDER BY j.created_at DESC LIMIT 1",
                (device_id,),
            )
            if row is not None:
                out.append({"device_id": device_id, "serial": row["serial"], "cpuid": row["cpuid"],
                            "last_job_id": row["id"], "last_mode": row["mode"], "last_status": row["status"],
                            "last_batch": row["batch"], "last_date": row["finished_at"] or row["created_at"],
                            "runs": row["runs"]})
        return out

    async def stop(self, operator: dict, device_ids: list[int] | None = None, batch_id: int | None = None) -> dict:
        sql = "SELECT * FROM job WHERE status IN ('queued','running','awaiting_boot')"
        params: list[Any] = []
        if batch_id is not None:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        if device_ids:
            sql += f" AND device_id IN ({','.join('?' * len(device_ids))})"
            params += list(device_ids)
        if batch_id is None and not device_ids:
            raise FactoryError("не выбрано ни одной платы")
        jobs = self.db.all(sql, params)
        stopped = []
        for job in jobs:
            conn = self.agents.get(job["device_id"])
            if job["status"] == "queued":
                live = self.live.get(job["device_id"]) or {}
                if conn is not None:
                    await conn.send({"type": "cancel", "job_id": job["id"]})
                if live.get("job_id") == job["id"]:
                    self.cancel_requested.add(job["id"])
                else:
                    await self._finalize(job, "cancelled", detail="остановлено оператором до старта")
            elif job["status"] == "running":
                self.cancel_requested.add(job["id"])
                if conn is not None:
                    await conn.send({"type": "cancel", "job_id": job["id"]})
            else:  # awaiting_boot
                self._set_stage(job["id"], "boot", "skipped", detail="BOOTCHECK: остановлено оператором")
                await self._finalize(job, "cancelled", detail="ожидание BOOT остановлено оператором")
            stopped.append(job["id"])
        self.audit(operator["username"], "stop", {"batch_id": batch_id, "devices": device_ids, "jobs": stopped})
        return {"jobs": stopped}

    async def identify(self, operator: dict, device_id: int) -> None:
        dev = self.db.one("SELECT has_led FROM device WHERE id = ?", (device_id,))
        if dev is None:
            raise FactoryError("неизвестная плата", 404)
        if not dev["has_led"]:
            raise FactoryError("у платы нет управляемого LED")
        conn = self.agents.get(device_id)
        if conn is None or not self.is_online(device_id):
            raise FactoryError("плата не на связи", 409)
        await conn.send({"type": "identify", "seconds": 10})
        self.audit(operator["username"], "identify", {"device_id": device_id})

    async def retry_boot(self, operator: dict, device_ids: list[int]) -> dict:
        """«Только BOOTCHECK»: прошитые платы, у которых загрузка с SD-NAND не подтвердилась (оператор
        не успел вынуть microSD), снова ждут загрузку. Плата без связи — нормально: на ней прошитая ОС."""
        ids = sorted(set(int(d) for d in device_ids))
        if not ids:
            raise FactoryError("не выбрано ни одной платы")
        jobs, bad = [], []
        for device_id in ids:
            job = self._latest_job(device_id)
            reason = self._boot_retry_refusal(job)
            if reason:
                bad.append(f"{self._nos([device_id])} — {reason}")
            else:
                jobs.append(job)
        if bad:
            raise FactoryError("BOOTCHECK не повторить: плата " + "; плата ".join(bad), 409)
        deadline = to_iso(datetime.now(timezone.utc) + timedelta(seconds=self.profile.boot_timeout_s))
        for job in jobs:
            self.db.execute("UPDATE job SET status = 'awaiting_boot', result = NULL, finished_at = NULL,"
                            " boot_deadline = ? WHERE id = ?", (deadline, job["id"]))
            self.db.execute("UPDATE batch SET finished_at = NULL WHERE id = ?", (job["batch_id"],))
            self._clear_boot_warning(job["id"])
            self.boot_state.pop(job["id"], None)
            self._set_stage(job["id"], "boot", "running", detail=self._boot_wait_text())
            self.mark(job["device_id"])
        self.audit(operator["username"], "boot_retry", {"devices": ids, "jobs": [j["id"] for j in jobs]})
        return {"jobs": len(jobs)}

    def _boot_retry_refusal(self, job) -> str | None:
        if job is None:
            return "заданий не было"
        if job["status"] in ACTIVE_STATUSES:
            return "задание ещё идёт" if job["status"] != "awaiting_boot" else "BOOTCHECK уже идёт"
        if not loads(job["params_json"], {}).get("stages", {}).get("boot"):
            return "в задании загрузка не проверялась (Zeroimage или «Не проверять загрузку»)"
        rows = self._stage_rows(job["id"])
        if (rows.get("flash") or {"state": None})["state"] != "pass":
            return "сначала прошейте её"
        if (rows.get("boot") or {"state": None})["state"] == "pass":
            return "загрузка уже подтверждена"
        enabled = loads(job["params_json"], {}).get("stages", {})
        if not boot_ready(enabled, {s: {"state": r["state"]} for s, r in rows.items()}):
            return "есть FAIL до прошивки"
        return None

    def set_golden(self, operator: dict, device_id: int, is_golden: bool) -> None:
        cur = self.db.execute("UPDATE device SET is_golden = ? WHERE id = ?", (1 if is_golden else 0, device_id))
        if not cur.rowcount:
            raise FactoryError("неизвестная плата", 404)
        self.audit(operator["username"], "golden", {"device_id": device_id, "is_golden": is_golden})
        self.mark(device_id)

    def set_board(self, operator: dict, device_ids: list[int], board: str | None) -> list[str]:
        """Тип платы вручную (None — снова автоопределение). Возвращает предупреждения."""
        if board is not None and board not in BOARDS:
            raise FactoryError("тип платы: napi-c | napi-p | авто")
        ids = sorted(set(int(d) for d in device_ids))
        if not ids:
            raise FactoryError("не выбрано ни одной платы")
        rows = self.db.all(f"SELECT * FROM device WHERE id IN ({','.join('?' * len(ids))})", ids)
        if len(rows) != len(ids):
            raise FactoryError("неизвестные платы", 404)
        warnings = []
        for dev in rows:
            if board == "napi-c" and dev["uplink_bus"] == "usb":
                warnings.append(f"плата {dev['display_no']}: подключена через USB-адаптер — тест сети проверит"
                                " адаптер, а не плату")
            self.db.execute("UPDATE device SET board_override = ? WHERE id = ?", (board, dev["id"]))
            self.mark(dev["id"])
        self.audit(operator["username"], "board_type",
                   {"devices": ids, "board": board or "auto",
                    "detected": {r["id"]: r["board_detected"] for r in rows}})
        return warnings

    # ------------------------------------------------------------------ BOOT

    async def boot_ok(self, payload: dict) -> dict:
        cpuid = payload.get("cpuid")
        dev = self.db.one("SELECT * FROM device WHERE cpuid = ?", (cpuid,)) if cpuid else None
        if dev is None:
            self.audit(None, "boot_ok_unknown", payload)
            return {"ok": True, "job_id": None}
        job = self.db.one(
            "SELECT * FROM job WHERE device_id = ? AND status = 'awaiting_boot' ORDER BY created_at DESC LIMIT 1",
            (dev["id"],),
        )
        if job is None:
            return await self._boot_ok_early(dev, payload)
        await self._complete_boot(job, payload)
        self.audit(None, "boot_ok", {"device_id": dev["id"], "job_id": job["id"], **payload})
        return {"ok": True, "job_id": job["id"]}

    async def _boot_ok_early(self, dev, payload: dict) -> dict:
        """Отчёт пришёл раньше итога задания: агент не успел дослать очередь до выключения платы."""
        job = self.db.one(
            "SELECT * FROM job WHERE device_id = ? AND status IN ('running','offline')"
            " AND mode IN ('test_flash','flash_only') ORDER BY created_at DESC LIMIT 1",
            (dev["id"],),
        )
        if job is None or not loads(job["params_json"], {}).get("stages", {}).get("boot"):
            late = self._boot_timed_out_job(dev["id"])
            if late is not None:
                await self._complete_boot(late, payload, late=True)
                self.audit(None, "boot_ok_late", {"device_id": dev["id"], "job_id": late["id"], **payload})
                return {"ok": True, "job_id": late["id"]}
            self.audit(None, "boot_ok_no_job", {"device_id": dev["id"], **payload})
            return {"ok": True, "job_id": None}
        enabled = loads(job["params_json"], {}).get("stages", {})
        rows = self._stage_rows(job["id"])
        stages = {s: {"state": r["state"]} for s, r in rows.items()}
        if boot_ready(enabled, stages):
            # Все этапы до BOOT уже подтверждены событиями; итог задания можно не ждать.
            self.db.execute("UPDATE job SET status = 'awaiting_boot' WHERE id = ?", (job["id"],))
            await self._complete_boot(self._job(job["id"]), payload)
        else:
            self.early_boot[job["id"]] = payload
        self.audit(None, "boot_ok_early", {"device_id": dev["id"], "job_id": job["id"], **payload})
        return {"ok": True, "job_id": job["id"]}

    def _clear_boot_warning(self, job_id: str) -> None:
        """Поздняя загрузка подтверждена: замечание «не подтверждена» снимается."""
        self.db.execute("UPDATE stage_result SET warnings_json = NULL WHERE job_id = ? AND stage = 'boot'", (job_id,))

    def normalize_boot_timeouts(self) -> None:
        """Старые записи, где таймаут BOOT давал FAIL при пройденных тестах и прошивке, — в PASS с замечанием."""
        for job in self.db.all(
                "SELECT j.* FROM job j JOIN stage_result s ON s.job_id = j.id AND s.stage = 'boot'"
                f" WHERE j.status = 'fail' AND s.state = 'fail' AND s.detail IN ({', '.join('?' * len(BOOT_TIMEOUT_TEXTS))})",
                BOOT_TIMEOUT_TEXTS):
            rows = self._stage_rows(job["id"])
            enabled = loads(job["params_json"], {}).get("stages", {})
            if verdict(enabled, {s: {"state": r["state"]} for s, r in rows.items()}, include_boot=False) != "pass":
                continue
            self.db.execute("UPDATE stage_result SET state = 'skipped', detail = ?, warnings_json = ?"
                            " WHERE job_id = ? AND stage = 'boot'", (BOOT_UNCONFIRMED, dumps([BOOT_UNCONFIRMED]), job["id"]))
            self.db.execute("UPDATE job SET status = 'pass', result = 'pass', detail = NULL WHERE id = ?", (job["id"],))
            self.audit(None, "boot_timeout_reclassified", {"job_id": job["id"], "device_id": job["device_id"]})

    def _boot_timed_out_job(self, device_id: int):
        """Последнее задание платы, если BOOTCHECK в нём не подтверждён за время ожидания
        (оператор не успел снять microSD): PASS с замечанием, либо FAIL по таймауту в старых записях."""
        job = self.db.one("SELECT * FROM job WHERE device_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                          (device_id,))
        if job is None or job["status"] not in ("pass", "fail"):
            return None
        rows = self._stage_rows(job["id"])
        boot = rows.get("boot")
        if boot is None:
            return None
        # PASS с замечанием; FAIL — если до BOOT не прошла сеть (плата всё равно прошита)
        unconfirmed = boot["state"] == "skipped" and boot["detail"] in BOOT_UNCONFIRMED_TEXTS
        old_fail = job["status"] == "fail" and boot["state"] == "fail" and boot["detail"] in BOOT_TIMEOUT_TEXTS
        if not (unconfirmed or old_fail):
            return None
        enabled = loads(job["params_json"], {}).get("stages", {})
        stages = {s: {"state": r["state"]} for s, r in rows.items()}
        return job if boot_ready(enabled, stages) else None

    async def _complete_boot(self, job, payload: dict, late: bool = False) -> None:
        flash_row = self._stage_rows(job["id"]).get("flash")
        flashed_at = parse_iso(flash_row["updated_at"]) if flash_row else None
        t = int((datetime.now(timezone.utc) - flashed_at).total_seconds()) if flashed_at else None
        measurements = {"uptime": payload.get("uptime"), "image_version": payload.get("image_version"),
                        "serial": payload.get("serial")}
        expected = job["image_version"] or loads(job["params_json"], {}).get("flash", {}).get("image_version")
        if expected and payload.get("image_version") and payload["image_version"] != expected:
            self._set_stage(job["id"], "boot", "fail", t,
                            f"BOOTCHECK: образ {payload['image_version']}≠{expected}", measurements)
            await self._finalize(job, "fail", detail="BOOTCHECK: версия образа не совпала")
        else:
            uptime = payload.get("uptime")
            detail = f"загрузка с SD-NAND, uptime {uptime:.0f} с" if isinstance(uptime, (int, float)) else "загрузка с SD-NAND"
            self._set_stage(job["id"], "boot", "pass", t, detail, measurements)
            if late:
                self._clear_boot_warning(job["id"])
            await self._finalize(job, "pass", detail="" if late else None)

    # ------------------------------------------------------------------ BOOT по сети

    @property
    def boot_method(self) -> str:
        return str(self.profile.section("boot").get("method", "report"))

    def _boot_wait_text(self) -> str:
        if self.boot_method == "network":
            return "выключите плату, выньте microSD, включите"
        return "ожидание отчёта первой загрузки"

    def _boot_detail(self, job_id: str, state: dict, text: str) -> None:
        if state.get("detail") != text:
            state["detail"] = text
            self._set_stage(job_id, "boot", "running", detail=text)
            job = self._job(job_id)
            if job:
                self.mark(job["device_id"])

    def _boot_candidates(self) -> list[tuple[Any, bool]]:
        jobs = [(j, False) for j in self.db.all("SELECT * FROM job WHERE status = 'awaiting_boot'")]
        # Поздняя загрузка: BOOTCHECK последнего задания платы не подтверждён за последние 2 ч
        since = to_iso(datetime.now(timezone.utc) - timedelta(hours=2))
        texts = BOOT_UNCONFIRMED_TEXTS + BOOT_TIMEOUT_TEXTS
        for row in self.db.all(
                "SELECT j.* FROM job j JOIN stage_result s ON s.job_id = j.id AND s.stage = 'boot'"
                f" WHERE j.status IN ('pass', 'fail') AND s.detail IN ({', '.join('?' * len(texts))})"
                " AND j.finished_at >= ?",
                (*texts, since)):
            late = self._boot_timed_out_job(row["device_id"])
            if late is not None and late["id"] == row["id"]:
                jobs.append((late, True))
        return jobs

    async def boot_probe(self) -> None:
        """Плата загрузилась с SD-NAND: её MAC в сети, отвечает SSH, агента тестовой microSD нет."""
        cands = self._boot_candidates()
        if not cands:
            return
        confirm_s = float(self.profile.section("boot").get("confirm_s", 45))
        now = time.monotonic()
        todo = []
        hosts: set[str] = set()
        for job, late in cands:
            dev = self.db.one("SELECT id, COALESCE(uplink_mac, mac) AS mac, ip FROM device WHERE id = ?",
                              (job["device_id"],))
            st = self.boot_state.setdefault(job["id"], {"left": False, "up_since": None, "late": late})
            if self.is_online(dev["id"]):
                st["up_since"] = None
                if not late:
                    self._boot_detail(job["id"], st, "загрузилась с microSD: выньте карту и перезагрузите"
                                      if st["left"] else self._boot_wait_text())
                continue
            st["left"] = True
            routed = bool(dev["ip"]) and not self.bootnet.is_local(dev["ip"])
            if routed and self._behind_nat(dev["id"], dev["ip"]):
                if not late:
                    self._set_stage(job["id"], "boot", "skipped", None, BOOT_NAT, warnings=[BOOT_NAT])
                    await self._finalize(self._job(job["id"]), "pass")
                continue
            if not dev["mac"] and not routed:
                continue
            todo.append((job, late, dev, st, routed))
            if not routed:
                hosts.update(subnet_hosts(dev["ip"]))
        if not todo:
            return
        if hosts:
            await self.bootnet.sweep(sorted(hosts))
        arp = self.bootnet.arp()
        for job, late, dev, st, routed in todo:
            found = None
            for ip in [dev["ip"]] if routed else arp.get(dev["mac"].lower(), []):
                banner = await self.bootnet.ssh_banner(ip)
                if banner:
                    found = (ip, banner)
                    break
            if self.is_online(dev["id"]):
                continue
            if found is None:
                st["up_since"] = None
                if not late:
                    self._boot_detail(job["id"], st, "ждём загрузки с SD-NAND")
                continue
            if st["up_since"] is None or st.get("ip") != found[0]:
                st["up_since"], st["ip"] = now, found[0]
            if not late:
                self._boot_detail(job["id"], st, f"в сети {found[0]}, проверка")
            if now - st["up_since"] >= confirm_s:
                await self._complete_boot_net(self._job(job["id"]), dev["mac"], found[0], found[1], late,
                                              warnings=[BOOT_BY_IP] if routed else None)

    def _behind_nat(self, device_id: int, ip: str) -> bool:
        """Агент подключался не со своего IP: между платой и сервером NAT, по сети её не проверить."""
        peer = self.peer_ip.get(device_id)
        try:
            ipaddress.IPv4Address(peer or "")
        except ValueError:
            return False
        return peer != ip

    async def _complete_boot_net(self, job, mac: str, ip: str, banner: str, late: bool,
                                 warnings: list[str] | None = None) -> None:
        flash_row = self._stage_rows(job["id"]).get("flash")
        flashed_at = parse_iso(flash_row["updated_at"]) if flash_row else None
        t = int((datetime.now(timezone.utc) - flashed_at).total_seconds()) if flashed_at else None
        if late:
            self._clear_boot_warning(job["id"])
        self._set_stage(job["id"], "boot", "pass", t, f"загрузка с SD-NAND, в сети {ip}",
                        {"method": "network", "ip": ip, "mac": mac, "ssh_banner": banner}, warnings=warnings)
        await self._finalize(job, "pass", detail="" if late else None)
        self.audit(None, "boot_net", {"device_id": job["device_id"], "job_id": job["id"], "ip": ip, "late": late})

    # ------------------------------------------------------------------ таймеры

    async def tick(self) -> None:
        now = datetime.now(timezone.utc)
        for device_id in list(set(self.agents) | set(self.online_cache)):
            online = self.is_online(device_id)
            if self.online_cache.get(device_id) != online:
                self.online_cache[device_id] = online
                self.mark(device_id)

        for job in self.db.all("SELECT * FROM job WHERE status = 'awaiting_boot' AND boot_deadline < ?",
                               (to_iso(now),)):
            st = self.boot_state.get(job["id"]) or {}
            text = BOOT_UNCONFIRMED_SD if st.get("left") and self.is_online(job["device_id"]) else BOOT_UNCONFIRMED
            self._set_stage(job["id"], "boot", "skipped", self.profile.boot_timeout_s, text, warnings=[text])
            await self._finalize(job, "pass")

        limit = to_iso(now - timedelta(seconds=self.profile.job_timeout_s))
        for job in self.db.all(
            "SELECT j.* FROM job j JOIN device d ON d.id = j.device_id"
            " WHERE j.status IN ('queued','running') AND d.last_seen < ? AND j.created_at < ?",
            (limit, limit),
        ):
            if not self.is_online(job["device_id"]):
                await self._finalize(job, "offline", detail="нет связи с платой дольше таймаута задания")

        await self._send_grants(self.net.expire())
        sync = self._sync_now()
        if sync != self._sync_allowed or self._catalog_dirty:
            self._sync_allowed = sync
            self._catalog_dirty = False
            self.summary_dirty = True
            await self.push_catalog()
        # last_seen пишется раз в минуту, а не на каждый heartbeat
        if time.monotonic() - self._last_seen_flush >= 60:
            self._last_seen_flush = time.monotonic()
            online = [d for d in self.agents if self.is_online(d)]
            if online:
                self.db.execute(
                    f"UPDATE device SET last_seen = ? WHERE id IN ({','.join('?' * len(online))})",
                    (now_iso(), *online),
                )

    def _finish_batch_if_done(self, batch_id: int) -> None:
        batch = self.db.one("SELECT * FROM batch WHERE id = ?", (batch_id,))
        if batch is None or batch["finished_at"]:
            return
        jobs = self.db.all("SELECT status FROM job WHERE batch_id = ?", (batch_id,))
        if any(j["status"] in ACTIVE_STATUSES for j in jobs):
            return
        golden = None
        if batch["kind"] == "golden":
            golden = "pass" if jobs and all(j["status"] == "pass" for j in jobs) else "fail"
        self.db.execute("UPDATE batch SET finished_at = ?, golden_result = COALESCE(?, golden_result) WHERE id = ?",
                        (now_iso(), golden, batch_id))
        self.summary_dirty = True

    # ------------------------------------------------------------------ браузер

    async def broadcast_pending(self) -> None:
        if self.grid_reset:
            self.grid_reset = False
            self.dirty.clear()
            self.summary_dirty = False
            msg = {"type": "snapshot", **self.state()}
            for ws in list(self.ui_clients):
                try:
                    await ws.send_json(msg)
                except Exception:
                    self.ui_clients.discard(ws)
            return
        if not self.ui_clients or (not self.dirty and not self.summary_dirty):
            self.dirty.clear()
            self.summary_dirty = False
            return
        ids = list(self.dirty)
        self.dirty.clear()
        self.summary_dirty = False
        devices = []
        if ids:
            rows = self.db.all(f"SELECT * FROM device WHERE id IN ({','.join('?' * len(ids))})", ids)
            devices = [self.device_view(r) for r in rows]
        messages = []
        if devices:
            messages.append({"type": "devices", "devices": devices})
        messages.append({"type": "summary", "summary": self.summary()})
        for ws in list(self.ui_clients):
            try:
                for m in messages:
                    await ws.send_json(m)
            except Exception:
                self.ui_clients.discard(ws)

    async def run_background(self) -> None:
        async def ticker():
            while True:
                try:
                    await self.tick()
                except Exception:
                    log.exception("tick failed")
                await asyncio.sleep(self.settings.tick_s)

        async def broadcaster():
            while True:
                try:
                    await self.broadcast_pending()
                except Exception:
                    log.exception("broadcast failed")
                await asyncio.sleep(0.5)

        async def boot_watcher():
            while True:
                try:
                    await self.boot_probe()
                except Exception:
                    log.exception("boot probe failed")
                await asyncio.sleep(float(self.profile.section("boot").get("probe_s", 3)))

        self._bg = [asyncio.create_task(ticker()), asyncio.create_task(broadcaster())]
        self.catalog.start()
        self.sdcard.start()
        if self.boot_method == "network":
            self._bg.append(asyncio.create_task(boot_watcher()))

    async def stop_background(self) -> None:
        await self.catalog.stop()
        await self.sdcard.stop()
        for task in self._bg:
            task.cancel()
