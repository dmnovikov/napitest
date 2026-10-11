#!/usr/bin/env bash
# Подготовка тестовой microSD для NAPI Factory Test.
#
# Скрипт НЕ выбирает и НЕ пишет блочные устройства сам. Он работает с файлами:
#   manifest  <image.img.zst> <version> [profile]
#       Генерирует <name>.manifest.json рядом с образом: размер и SHA-256 сжатого файла,
#       размер и SHA-256 распакованного потока, размер из zstd frame header (сверяется).
#   stage     <staging_dir> --image <img.zst> --agent-config <agent.yaml> --test-sd-version <ver>
#       Собирает дерево файлов для тестовой Armbian: /opt/napi-factory/{images,agent,VERSION},
#       /etc/napi-factory/agent.yaml, systemd-юнит агента.
#   install   <mounted_rootfs> <staging_dir>
#       Копирует подготовленное дерево в ЯВНО указанный смонтированный rootfs тестовой
#       Armbian (каталог, не устройство) после подтверждения оператора.
#
# Запись готовой эталонной карты на остальные 49 карт делается вручную по docs/provisioning-sd.md
# с явной проверкой устройства (lsblk), не этим скриптом.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "ошибка: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "нужна утилита $1"; }

cmd_manifest() {
  [[ $# -ge 2 ]] || die "usage: $0 manifest <image.img.zst> <version> [profile]"
  local img="$1" ver="$2" profile="${3:-napi-c-rk3308}"
  need zstd; need sha256sum
  [[ -f "$img" ]] || die "нет файла $img"
  [[ "$img" == *.img.zst ]] || die "ожидается *.img.zst"
  local name; name="$(basename "$img" .img.zst)"
  local out; out="$(dirname "$img")/${name}.manifest.json"
  echo "сжатый файл: SHA-256..."
  local csize csha; csize=$(stat -c %s "$img"); csha=$(sha256sum "$img" | cut -d' ' -f1)
  echo "распакованный поток: размер и SHA-256 (zstd -d)..."
  # Два прохода распаковки вместо tee: без гонок с process substitution
  local isize isha
  isize=$(zstd -d -c -q "$img" | wc -c | tr -d ' ')
  isha=$(zstd -d -c -q "$img" | sha256sum | cut -d' ' -f1)
  local fsize; fsize=$(zstd -l -v "$img" 2>&1 | sed -n 's/.*Decompressed Size:.*(\([0-9]*\) B).*/\1/p' | head -1)
  if [[ -n "$fsize" && "$fsize" != "$isize" ]]; then
    die "zstd frame header $fsize ≠ фактический размер $isize"
  fi
  [[ -n "$fsize" ]] || echo "предупреждение: в zstd frame header нет размера (сжимать с --content-size)" >&2
  cat > "$out" <<EOF
{
 "version": "$ver",
 "profile": "$profile",
 "file": "$(basename "$img")",
 "compressed_size": $csize,
 "compressed_sha256": "$csha",
 "image_size": $isize,
 "image_sha256": "$isha"
}
EOF
  echo "манифест: $out (образ $isize байт)"
}

cmd_stage() {
  [[ $# -ge 1 ]] || die "usage: $0 stage <staging_dir> --image X.img.zst --agent-config agent.yaml --test-sd-version V"
  local dir="$1"; shift
  local image="" acfg="" sdver=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --image) image="$2"; shift 2 ;;
      --agent-config) acfg="$2"; shift 2 ;;
      --test-sd-version) sdver="$2"; shift 2 ;;
      *) die "неизвестный аргумент $1" ;;
    esac
  done
  [[ -n "$image" && -n "$acfg" && -n "$sdver" ]] || die "нужны --image, --agent-config, --test-sd-version"
  [[ -f "$image" ]] || die "нет образа $image"
  [[ -f "$acfg" ]] || die "нет конфига агента $acfg"
  grep -q 'CHANGE-ME' "$acfg" && die "в $acfg не заменён token"
  local man="${image%.img.zst}.manifest.json"
  [[ -f "$man" ]] || die "нет манифеста $man (сначала: $0 manifest ...)"
  if [[ -e "$dir" && -n "$(ls -A "$dir" 2>/dev/null)" ]]; then die "$dir не пуст"; fi
  mkdir -p "$dir/opt/napi-factory/images" "$dir/opt/napi-factory/agent" "$dir/etc/napi-factory" \
           "$dir/etc/systemd/system/multi-user.target.wants" "$dir/var/lib/napi-factory"
  cp "$image" "$man" "$dir/opt/napi-factory/images/"
  cp -r "$REPO/agent/napi_factory_agent" "$dir/opt/napi-factory/agent/"
  find "$dir/opt/napi-factory/agent" -name '__pycache__' -prune -exec rm -rf {} +
  echo "$sdver" > "$dir/opt/napi-factory/VERSION"
  install -m 0600 "$acfg" "$dir/etc/napi-factory/agent.yaml"
  cp "$REPO/agent/napi_factory_agent/systemd/napi-factory-agent.service" "$dir/etc/systemd/system/"
  ln -sf ../napi-factory-agent.service "$dir/etc/systemd/system/multi-user.target.wants/napi-factory-agent.service"
  echo "дерево тестовой microSD: $dir"
  echo "зависимости на тестовой Armbian: python3 python3-websockets python3-yaml memtester iperf3 zstd"
}

cmd_install() {
  [[ $# -eq 2 ]] || die "usage: $0 install <mounted_rootfs_dir> <staging_dir>"
  local root="$1" dir="$2"
  [[ -d "$root" ]] || die "$root не каталог (нужна точка монтирования rootfs, не /dev/...)"
  [[ "$root" != "/" ]] || die "отказ: / это система, на которой запущен скрипт"
  [[ -f "$root/etc/os-release" ]] || die "$root не похож на rootfs (нет etc/os-release)"
  grep -qi armbian "$root/etc/os-release" || echo "предупреждение: в $root/etc/os-release нет Armbian" >&2
  [[ -d "$dir/opt/napi-factory" ]] || die "$dir не результат stage"
  echo "Будет скопировано дерево $dir в rootfs $root:"
  findmnt -T "$root" -o SOURCE,TARGET,FSTYPE || true
  read -r -p "Введите YES для продолжения: " ans
  [[ "$ans" == "YES" ]] || die "отменено"
  cp -a "$dir/." "$root/"
  sync
  echo "готово. Проверьте memtest=N в cmdline тестового ядра (docs/provisioning-sd.md)."
}

case "${1:-}" in
  manifest) shift; cmd_manifest "$@" ;;
  stage) shift; cmd_stage "$@" ;;
  install) shift; cmd_install "$@" ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
