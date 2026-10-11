"""Конфигурация агента (YAML на microSD + переменные окружения)."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    # ws://host:port/ws/agent; пусто: поиск сервера широковещательным UDP, затем mDNS
    "server_url": "",
    "token": "",
    "discovery_port": 18089,   # 0: не искать широковещательным UDP
    "mdns": True,
    "state_dir": "/var/lib/napi-factory",
    "queue_max_bytes": 32 * 1024 * 1024,
    "factory_dir": "/opt/napi-factory",
    "heartbeat_s": 3.0,
    "identity": {
        # VALIDATE ON NAPI-C: смещение и длина cpuid в OTP RK3308
        "nvmem_path": "/sys/bus/nvmem/devices/rockchip-otp0/nvmem",
        "otp_offset": None,
        "otp_length": None,
        "known_clone_macs": [],
    },
    "net": {"iface": "end0"},
    # Правила распознавания для inventory в hello (до получения задания).
    # Перед разрушающими операциями используются правила из задания (профиль сервера).
    "detect": {
        "expected_controller": "ff490000.mmc",
        "require_controller_match": True,
        "size_min_bytes": 3500000000,
        "size_max_bytes": 4400000000,
        "allowed_types": ["MMC", "SD"],
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class AgentConfig:
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))

    def __getitem__(self, k: str) -> Any:
        return self.data[k]

    def get(self, k: str, default: Any = None) -> Any:
        return self.data.get(k, default)

    @property
    def images_dir(self) -> str:
        return os.path.join(self.data["factory_dir"], "images")

    @property
    def version_file(self) -> str:
        return os.path.join(self.data["factory_dir"], "VERSION")


def load_config(path: str | None, overrides: dict | None = None) -> AgentConfig:
    data = copy.deepcopy(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as f:
            data = _merge(data, yaml.safe_load(f) or {})
    env = {
        "NAPI_SERVER_URL": "server_url",
        "NAPI_AGENT_TOKEN": "token",
        "NAPI_STATE_DIR": "state_dir",
    }
    for ev, key in env.items():
        if os.environ.get(ev):
            data[key] = os.environ[ev]
    if overrides:
        data = _merge(data, overrides)
    return AgentConfig(data)
