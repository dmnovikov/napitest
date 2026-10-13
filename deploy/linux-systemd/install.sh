#!/usr/bin/env bash
# Установка сервера копированием: каталог + venv + systemd-юнит. Запускать от root
# из корня репозитория после сборки и установки веба (cd web && npm ci && npm run build && cd .. && scripts/deploy-web.sh).
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DST=/opt/napi-factory-test

[[ $EUID -eq 0 ]] || { echo "нужен root" >&2; exit 1; }
[[ -f "$SRC/web/dist/index.html" ]] || { echo "веб не установлен: cd web && npm ci && npm run build && cd .. && scripts/deploy-web.sh" >&2; exit 1; }
command -v python3 >/dev/null || { echo "нужен python3 >= 3.11" >&2; exit 1; }
command -v iperf3 >/dev/null || echo "предупреждение: iperf3 не установлен, NET-этап реальных плат не пройдёт"
for t in zstd xz debugfs e2fsck; do command -v $t >/dev/null || echo "предупреждение: нет $t (образы ОС и microSD)"; done

id napi-factory >/dev/null 2>&1 || useradd --system --home /var/lib/napi-factory --shell /usr/sbin/nologin napi-factory
mkdir -p "$DST" /etc/napi-factory /var/lib/napi-factory
rsync -a --delete --exclude .venv --exclude node_modules --exclude __pycache__ --exclude .pytest_cache \
  "$SRC/server" "$SRC/config" "$SRC/firstboot" "$SRC/docs" "$DST/"
# Агент: из него сервер собирает образ microSD и пакет автообновления карт
mkdir -p "$DST/agent" && rsync -a --delete --exclude __pycache__ "$SRC/agent/napi_factory_agent" "$DST/agent/"
mkdir -p "$DST/web" && rsync -a --delete "$SRC/web/dist" "$DST/web/"
python3 -m venv "$DST/server/.venv"
"$DST/server/.venv/bin/pip" install --quiet "$DST/server"
[[ -f /etc/napi-factory/server.env ]] || install -m 600 "$SRC/config/server.example.env" /etc/napi-factory/server.env
chown -R napi-factory:napi-factory /var/lib/napi-factory
install -m 644 "$SRC/deploy/linux-systemd/napi-factory-server.service" /etc/systemd/system/
if [[ -d /etc/avahi/services ]]; then
  install -m 644 "$SRC/deploy/linux-systemd/napi-factory.avahi.service" /etc/avahi/services/napi-factory.service
fi
systemctl daemon-reload
echo "Отредактируйте /etc/napi-factory/server.env (токен, пароль), затем:"
echo "  systemctl enable --now napi-factory-server"
