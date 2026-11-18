"""Починка системы тестовой microSD силами агента (карты обновляют агента сами, систему — нет).

Юнит первого запуска карт до 2026.11 стоял «до ssh.socket» и при этом «после basic.target»: получалось
кольцо зависимостей, systemd при каждой загрузке выбрасывал из него случайное задание, и на части загрузок
SSH не поднимался. Агент заменяет юнит исправленным и поднимает SSH, если тот не запущен.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("napi.cardfix")

UNIT_PATH = "etc/systemd/system/napi-sd-firstboot.service"
FIRSTBOOT_UNIT = """[Unit]
Description=NAPI test microSD: SSH host keys for this card
DefaultDependencies=no
After=local-fs.target
Before=ssh.service ssh.socket sockets.target shutdown.target
Conflicts=shutdown.target
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
ExecStart=/usr/bin/sync

[Install]
WantedBy=multi-user.target
"""


def _run(*cmd: str, timeout: float = 60) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def fix_unit(root: str | Path = "/") -> bool:
    """Заменить юнит первого запуска исправленным. True, если файл изменён."""
    path = Path(root) / UNIT_PATH
    try:
        if path.read_text() == FIRSTBOOT_UNIT:
            return False
    except OSError:
        return False                      # не карта стенда (или юнита нет): ничего не трогаем
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(FIRSTBOOT_UNIT)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def repair() -> list[str]:
    """На живой карте: исправить юнит и поднять SSH. Возвращает список сделанного (для журнала)."""
    done: list[str] = []
    try:
        if fix_unit("/"):
            _run("systemctl", "daemon-reload")
            done.append("юнит napi-sd-firstboot исправлен")
    except OSError as e:
        log.warning("юнит napi-sd-firstboot: %s", e)
    if not Path("/" + UNIT_PATH).is_file():
        return done
    if not Path("/etc/ssh/ssh_host_ed25519_key").is_file() and _run("ssh-keygen", "-A"):
        done.append("созданы ключи SSH")
    if not (_run("systemctl", "is-active", "--quiet", "ssh.socket")
            or _run("systemctl", "is-active", "--quiet", "ssh.service")):
        if _run("systemctl", "start", "ssh.socket") or _run("systemctl", "start", "ssh.service"):
            done.append("SSH запущен")
        else:
            log.warning("SSH не запускается")
    return done
