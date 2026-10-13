"""Настройки сервера из переменных окружения и профиль платы из YAML."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGES = ("ram", "net", "sdnand", "flash", "boot")
TEST_STAGES = ("ram", "net", "sdnand")
MODES = ("test_only", "test_flash", "flash_only")
DESTRUCTIVE_MODES = ("test_flash", "flash_only")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    db_path: str = "/var/lib/napi-factory/factory.db"
    profile_path: str = str(REPO_ROOT / "config" / "napi-c-rk3308.yaml")
    web_dist: str = str(REPO_ROOT / "web" / "dist")
    host: str = "0.0.0.0"
    port: int = 8080
    agent_token: str = ""
    admin_user: str = "admin"
    admin_password: str = ""
    allow_simulated: bool = False
    iperf_enable: bool = True
    iperf_bin: str = "iperf3"
    iperf_host: str = ""          # пусто: адрес сервера, к которому подключился агент
    grid_window_h: float = 12.0   # сколько часов показывать плату в сетке после последней связи
    session_hours: float = 12.0
    # UDP-порт обнаружения сервера платами (0: выключено); public_url: что отвечать, если
    # сервер за NAT/прокси (пусто: свой IP на интерфейсе запрашивающего)
    discovery_port: int = 18089
    public_url: str = ""
    # Образы ОС, подготовленные по ссылкам (меню «ОС на SD-NAND»); отсюда их скачивают карты
    images_dir: str = "/var/lib/napi-factory/images"
    # База образа тестовой microSD (*.img.zst или ссылка), берётся при первом запуске; дальше —
    # кнопка «Обновить образ microSD» на странице «microSD»
    sd_base: str = ""
    # Местные поправки профиля (YAML, сливается поверх профиля): например, временные пороги сети
    profile_overrides: str = ""
    # Только для разработки: разрешить реальному агенту разрушающие операции, даже если
    # профиль не помечен hardware_validated. В production не задавать.
    destructive_override: bool = False
    tick_s: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        s = cls()
        s.db_path = env.get("NAPI_DB_PATH", s.db_path)
        s.profile_path = env.get("NAPI_PROFILE", s.profile_path)
        s.web_dist = env.get("NAPI_WEB_DIST", s.web_dist)
        s.host = env.get("NAPI_HOST", s.host)
        s.port = int(env.get("NAPI_PORT", s.port))
        s.agent_token = env.get("NAPI_AGENT_TOKEN", "")
        s.admin_user = env.get("NAPI_ADMIN_USER", s.admin_user)
        s.admin_password = env.get("NAPI_ADMIN_PASSWORD", "")
        s.allow_simulated = _bool(env.get("NAPI_ALLOW_SIMULATED"))
        s.iperf_enable = _bool(env.get("NAPI_IPERF_ENABLE"), True)
        s.iperf_bin = env.get("NAPI_IPERF_BIN", s.iperf_bin)
        s.iperf_host = env.get("NAPI_IPERF_HOST", "")
        s.grid_window_h = float(env.get("NAPI_GRID_WINDOW_H", s.grid_window_h))
        s.session_hours = float(env.get("NAPI_SESSION_HOURS", s.session_hours))
        s.discovery_port = int(env.get("NAPI_DISCOVERY_PORT", s.discovery_port))
        s.public_url = env.get("NAPI_PUBLIC_URL", "")
        s.images_dir = env.get("NAPI_IMAGES_DIR", s.images_dir)
        s.sd_base = env.get("NAPI_SD_BASE", "")
        s.profile_overrides = env.get("NAPI_PROFILE_OVERRIDES", "")
        s.destructive_override = _bool(env.get("NAPI_DESTRUCTIVE_OVERRIDE"))
        return s

    def validate(self) -> None:
        if not self.agent_token or len(self.agent_token) < 16:
            raise SystemExit("NAPI_AGENT_TOKEN обязателен (не короче 16 символов)")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Profile:
    """Профиль платы. Сервер формирует из него параметры задания; агент профиль не читает."""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.name: str = data.get("name", "unknown")
        self.hardware_validated: bool = bool(data.get("hardware_validated", False))

    @classmethod
    def load(cls, path: str | Path, overrides: str | Path | None = None) -> "Profile":
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if overrides:
            with open(overrides, encoding="utf-8") as fh:
                data = _merge(data, yaml.safe_load(fh) or {})
        return cls(data)

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.data.get(name) or {})

    @property
    def heartbeat_offline_s(self) -> float:
        return float(self.section("timeouts").get("heartbeat_offline_s", 10))

    @property
    def job_timeout_s(self) -> float:
        return float(self.section("timeouts").get("job_s", 7200))

    @property
    def boot_enabled(self) -> bool:
        return bool(self.section("boot").get("enabled", True))

    @property
    def boot_timeout_s(self) -> float:
        return float(self.section("boot").get("timeout_s", 600))

    @property
    def image_version(self) -> str | None:
        return self.section("flash").get("image_version")

    @property
    def net_parallel(self) -> int:
        return int(self.section("net").get("max_parallel", 4))

    @property
    def iperf_base_port(self) -> int:
        return int(self.section("net").get("iperf_base_port", 5201))

    def stages_for_mode(self, mode: str) -> dict[str, bool]:
        if mode == "test_only":
            return {"ram": True, "net": True, "sdnand": True, "flash": False, "boot": False}
        if mode == "test_flash":
            return {"ram": True, "net": True, "sdnand": True, "flash": True, "boot": self.boot_enabled}
        if mode == "flash_only":
            return {"ram": False, "net": False, "sdnand": False, "flash": True, "boot": self.boot_enabled}
        raise ValueError(f"неизвестный режим {mode}")

    def job_params(self, mode: str, options: dict[str, Any] | None, destructive_override: bool,
                   os_flash: dict[str, Any] | None = None) -> dict[str, Any]:
        """os_flash: что писать на SD-NAND (выбор меню «ОС на SD-NAND», см. Factory.resolve_os):
        {"os", "os_name"} и либо "manifest_file"/"image_version" образа, либо "method": "zero"."""
        options = options or {}
        os_flash = os_flash or {}
        ram = self.section("ram")
        net = self.section("net")
        sd = self.section("sdnand")
        flash = self.section("flash")

        boot_opt = options.get("boot") or "check"
        if boot_opt not in ("check", "skip"):
            raise ValueError("boot: check | skip")
        stages = self.stages_for_mode(mode)
        zero = os_flash.get("method") == "zero"
        if boot_opt == "skip" or zero:
            stages["boot"] = False   # BOOTCHECK не нужен (или грузиться нечему): итог сразу после FLASH
        net_check = options.get("net") or "quick"
        if net_check not in ("quick", "full"):
            raise ValueError("net: quick | full")
        memtester = options.get("memtester") or ram.get("memtester_default", "quick")
        if memtester not in ("quick", "full"):
            raise ValueError("memtester: quick | full")
        ram_params = {k: ram[k] for k in (
            "expected_mib", "tolerance_mib", "memtest_required", "memtester_reserve_mib",
            "memtester_max_mib", "memtester_passes", "memtester_s_per_mib", "timeout_s") if k in ram}
        if memtester == "quick" and "memtester_quick_mib" in ram:
            ram_params["memtester_max_mib"] = ram["memtester_quick_mib"]

        if mode == "test_only":
            pattern = options.get("sdnand_pattern") or sd.get("pattern_test_only", "quick")
            if pattern not in ("quick", "full"):
                raise ValueError("sdnand_pattern: quick | full")
        elif mode == "test_flash":
            # SD-NAND за концом образа; под Zeroimage образа нет — быстрый тест по всей ёмкости
            pattern = "quick" if zero else "beyond_image"
        else:
            pattern = "skip"

        return {
            "profile": self.name,
            "destructive_enabled": self.hardware_validated or destructive_override,
            "stages": stages,
            "ram": ram_params,
            "net": {**{k: net[k] for k in (
                "iface", "expect_speed", "expect_duplex", "iperf_seconds", "min_mbps",
                "grant_timeout_s", "timeout_s", "max_rx_errors", "max_tx_errors",
                "udp_seconds", "udp_rate_mbps", "udp_max_loss_tx_pct", "udp_max_loss_rx_pct",
                "udp_retry") if k in net},
                "method": "tcp" if net_check == "full" else "udp"},
            "sdnand": {
                "pattern": pattern,
                **{k: sd[k] for k in ("quick_ranges", "range_mib", "block_kib", "timeout_s") if k in sd},
                "detect": dict(sd.get("detect") or {}),
            },
            "flash": self._flash_params(flash, os_flash),
            "boot": {"enabled": stages["boot"]},
        }

    def _flash_params(self, flash: dict[str, Any], os_flash: dict[str, Any]) -> dict[str, Any]:
        out = {k: flash[k] for k in (
            "image_version", "image_file", "manifest_file", "block_kib", "timeout_s", "provision") if k in flash}
        if os_flash.get("method") == "zero":
            zero = self.section("zeroimage")
            out = {"method": "zero", "zero_full": bool(os_flash.get("full")),
                   "zero_mib": int(zero.get("quick_mib", 64)),
                   **{k: flash[k] for k in ("block_kib", "timeout_s") if k in flash}}
        elif os_flash.get("manifest_file"):
            out.update(manifest_file=os_flash["manifest_file"], image_version=os_flash.get("image_version"),
                       image_file=os_flash.get("image_file"))
        out.update(os=os_flash.get("os", "sd"), os_name=os_flash.get("os_name", "Образ на microSD"))
        return out
