#!/usr/bin/env python3
"""Перенос provision-записи (серийник, партия, дата) с «хвоста» SD-NAND в файл.

FLASH пишет JSON по смещению align_up(image_size, 1 MiB) — сразу за концом образа.
При первой загрузке armbian-resize-filesystem расширяет rootfs на эту область, поэтому
юнит выполняется раньше него и сохраняет запись в /etc/napi-factory/provision.json.
Только стандартная библиотека Python.
"""

import argparse
import json
import os
import sys

PROVISION_FILE = "/etc/napi-factory/provision.json"
SIZE = 1024 * 1024


def load_conf(path):
    conf = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


def root_disk():
    """Диск, на котором смонтирован / (через major:minor, без угадывания имён)."""
    st = os.stat("/")
    link = os.path.realpath(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
    if os.path.exists(os.path.join(link, "partition")):
        link = os.path.dirname(link)
    return "/dev/" + os.path.basename(link)


def read_record(device, offset):
    with open(device, "rb") as f:
        f.seek(offset)
        raw = f.read(SIZE).rstrip(b"\0")
    if not raw:
        return None
    return json.loads(raw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/napi-factory/firstboot.conf")
    ap.add_argument("--device", help="по умолчанию диск корневой ФС")
    ap.add_argument("--out", default=PROVISION_FILE)
    args = ap.parse_args()
    if os.path.exists(args.out):
        return 0
    conf = load_conf(args.config)
    if not conf.get("provision_offset"):
        print("provision_offset не задан", file=sys.stderr)
        return 0
    device = args.device or root_disk()
    try:
        rec = read_record(device, int(conf["provision_offset"]))
    except (OSError, ValueError) as e:
        print(f"provision-запись не прочитана с {device}: {e}", file=sys.stderr)
        return 0
    if not isinstance(rec, dict) or not rec.get("serial"):
        print(f"provision-записи нет на {device}", file=sys.stderr)
        return 0
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, args.out)
    print(f"provision: {rec.get('serial')} ({rec.get('batch')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
