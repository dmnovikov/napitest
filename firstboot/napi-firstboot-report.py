#!/usr/bin/env python3
"""Отчёт первой загрузки с SD-NAND (этап BOOT, раздел 5.5 ТЗ).

Встраивается в финальный образ. При первой загрузке шлёт серверу boot_ok с cpuid,
версией образа и uptime; после подтверждения (ok: true) отключает свой юнит.
Только стандартная библиотека Python.
"""

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

UNIT = "napi-firstboot-report.service"
PROVISION_FILE = "/etc/napi-factory/provision.json"


def load_conf(path):
    conf = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


def rockchip_serial(cpuid):
    """serial# U-Boot Rockchip из cpuid в OTP (как агент: identity.rockchip_serial)."""
    import zlib

    def crc32_no_comp(crc, data):
        return (~zlib.crc32(data, ~crc & 0xFFFFFFFF)) & 0xFFFFFFFF

    half = len(cpuid) // 2
    low = bytes(cpuid[1 + (i << 1)] for i in range(half))
    high = bytes(cpuid[i << 1] for i in range(half))
    serial = crc32_no_comp(0, low)
    serial |= crc32_no_comp(serial, high) << 32
    return "%016x" % serial


def read_cpuid(conf):
    """Тот же порядок, что у агента: OTP (если задан) с приоритетом над device-tree."""
    dt = None
    try:
        with open("/proc/device-tree/serial-number", "rb") as f:
            s = f.read().replace(b"\x00", b"").decode("ascii", "replace").strip().lower()
        if s and set(s) - {"0"} and set(s) - {"f"}:
            dt = s
    except OSError:
        pass
    off, ln = conf.get("otp_offset"), conf.get("otp_length")
    if off and ln:
        try:
            with open("/sys/bus/nvmem/devices/rockchip-otp0/nvmem", "rb") as f:
                f.seek(int(off))
                b = f.read(int(ln))
            if len(b) == int(ln) and set(b) - {0, 0xFF}:
                return rockchip_serial(b)
        except OSError:
            pass
    return dt


def discover(port, timeout=1.5):
    """Адрес сервера широковещательным UDP-запросом (как у агента). None, если не ответил."""
    req = json.dumps({"type": "napi_discover", "v": 1}).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(timeout)
        try:
            s.sendto(req, ("255.255.255.255", port))
            msg = json.loads(s.recvfrom(2048)[0])
        except (OSError, ValueError):
            return None
    http = msg.get("http") if isinstance(msg, dict) and msg.get("type") == "napi_server" else None
    return http if isinstance(http, str) and http.startswith(("http://", "https://")) else None


def uptime():
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/etc/napi-factory/firstboot.conf")
    args = ap.parse_args()
    conf = load_conf(args.config)
    cpuid = read_cpuid(conf)
    serial = None
    try:
        with open(PROVISION_FILE) as f:
            serial = json.load(f).get("serial")
    except (OSError, ValueError):
        pass
    if not cpuid:
        print("cpuid не найден: отчёт невозможен", file=sys.stderr)
        return 1
    server = conf.get("server", "").rstrip("/")
    port = int(conf.get("discovery_port", 18089))
    attempts = int(conf.get("max_attempts", 60))
    for i in range(attempts):
        base = server or discover(port)
        if not base:
            print(f"попытка {i + 1}/{attempts}: сервер не найден (UDP {port})", file=sys.stderr)
            time.sleep(float(conf.get("retry_s", 10)))
            continue
        url = base + "/api/v1/agent/boot_ok"
        payload = {"cpuid": cpuid, "image_version": conf.get("image_version"),
                   "uptime": round(uptime(), 1), "serial": serial}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + conf.get("token", "")})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.loads(r.read() or b"{}")
            if resp.get("ok"):
                print(f"boot_ok подтверждён: job {resp.get('job_id')}")
                subprocess.run(["systemctl", "disable", UNIT], check=False)
                return 0
            print(f"сервер ответил {resp}", file=sys.stderr)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"попытка {i + 1}/{attempts}: {e}", file=sys.stderr)
        time.sleep(float(conf.get("retry_s", 10)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
