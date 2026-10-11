#!/usr/bin/env bash
# Установка собранного веб-интерфейса: web/dist-next → web/dist (раздаётся сервером).
# Сборка (npm run build) пишет в dist-next и на работающий сервер не влияет.
set -euo pipefail
web="$(cd "$(dirname "$0")/../web" && pwd)"
[[ -f "$web/dist-next/index.html" ]] || { echo "нет сборки: cd web && npm run build" >&2; exit 1; }
rm -rf "$web/dist.new"
cp -a "$web/dist-next" "$web/dist.new"
rm -rf "$web/dist.old"
[[ -d "$web/dist" ]] && mv "$web/dist" "$web/dist.old"
mv "$web/dist.new" "$web/dist"
rm -rf "$web/dist.old"
echo "веб установлен: $web/dist"
