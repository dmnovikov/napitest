#!/usr/bin/env python3
"""База образа тестовой microSD из проверенной карты (без root и без монтирования).

Снимок карты (на плате с этой картой, только чтение):
    ssh root@<карта> 'dd if=/dev/mmcblk1 bs=512 count=32768' > head.bin      # MBR + U-Boot (16 МиБ)
    ssh root@<карта> 'cd / && tar --one-file-system --numeric-owner --xattrs --xattrs-include="*" --acls -cpf - \\
        --exclude="./opt/napi-factory/images/*" --exclude="./opt/napi-factory/agent" \\
        --exclude="./var/lib/napi-factory/*" --exclude="./var/cache/apt/archives/*.deb" \\
        --exclude="./etc/ssh/ssh_host_*" --exclude="./root/.bash_history" --exclude="./var/log.hdd/*" \\
        --exclude="./tmp/*" --exclude="./lost+found/*" .' > rootfs.tar

Затем:
    scripts/make-sd-base.py head.bin rootfs.tar napi-sd-base-<версия>.img.zst --uuid <UUID rootfs> \\
        --label armbi_root --features "<Filesystem features из tune2fs -l>"

Что делается с rootfs: убираются агент, его настройки, VERSION и образы (их кладёт сервер при сборке
образа microSD); /etc/machine-id пустой (systemd создаст свой на каждой карте); ключи SSH создаются
при первом запуске (napi-sd-firstboot.service); включается расширение раздела на всю карту
(armbian-resize-filesystem). armbian-firstrun не включается: он вписывает случайный MAC в armbianEnv.txt,
а карта переходит с платы на плату.
"""
from __future__ import annotations

import argparse
import io
import os
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

DROP_PREFIX = ("opt/napi-factory/agent", "opt/napi-factory/VERSION", "opt/napi-factory/images/",
               "etc/napi-factory/agent.yaml", "etc/napi-factory/firstboot.conf", "var/lib/napi-factory/",
               "etc/systemd/system/napi-factory-agent.service",
               "etc/systemd/system/multi-user.target.wants/napi-factory-agent.service",
               "etc/ssh/ssh_host_")

FIRSTBOOT_UNIT = """[Unit]
Description=NAPI test microSD: SSH host keys for this card
After=local-fs.target
Before=ssh.service ssh.socket
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
ExecStart=/usr/bin/sync

[Install]
WantedBy=multi-user.target
"""

WANTS = "etc/systemd/system/multi-user.target.wants"
ADD_LINKS = {
    f"{WANTS}/armbian-resize-filesystem.service": "/usr/lib/systemd/system/armbian-resize-filesystem.service",
    f"{WANTS}/napi-sd-firstboot.service": "/etc/systemd/system/napi-sd-firstboot.service",
}


def norm(name: str) -> str:
    return name[2:] if name.startswith("./") else name.lstrip("/")


def filter_tar(src: Path, dst: Path) -> tuple[int, int]:
    dropped = kept = 0
    seen: set[str] = set()
    with tarfile.open(src) as tin, tarfile.open(dst, "w", format=tarfile.PAX_FORMAT) as tout:
        for m in tin:
            n = norm(m.name)
            if n and n.startswith(DROP_PREFIX):
                dropped += 1
                continue
            if n in ADD_LINKS:
                continue
            seen.add(n)
            if n == "etc/machine-id":
                m.size = 0
                tout.addfile(m, io.BytesIO(b""))
            elif m.isfile():
                tout.addfile(m, tin.extractfile(m))
            else:
                tout.addfile(m)
            kept += 1

        def add(name: str, data: bytes | None = None, link: str | None = None, mode: int = 0o644):
            ti = tarfile.TarInfo("./" + name)
            ti.uid = ti.gid = 0
            ti.mtime = int(os.path.getmtime(src))
            if link:
                ti.type, ti.linkname, ti.mode = tarfile.SYMTYPE, link, 0o777
                tout.addfile(ti)
            else:
                ti.size, ti.mode = len(data or b""), mode
                tout.addfile(ti, io.BytesIO(data or b""))

        add("etc/systemd/system/napi-sd-firstboot.service", FIRSTBOOT_UNIT.encode())
        for name, target in ADD_LINKS.items():
            add(name, link=target)
        if "etc/machine-id" not in seen:
            add("etc/machine-id", b"", mode=0o444)
    return kept, dropped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("head", help="первые 16 МиБ карты: MBR и U-Boot")
    ap.add_argument("rootfs", help="tar корневой ФС карты")
    ap.add_argument("out", help="результат: *.img.zst")
    ap.add_argument("--uuid", required=True, help="UUID корневой ФС (rootdev в armbianEnv.txt)")
    ap.add_argument("--label", default="armbi_root")
    ap.add_argument("--features", required=True, help="признаки ext4 исходной ФС (tune2fs -l)")
    ap.add_argument("--free-mib", type=int, default=768, help="свободное место в ФС до расширения раздела")
    a = ap.parse_args()

    head = Path(a.head).read_bytes()
    if len(head) != 16 << 20 or head[510:512] != b"\x55\xaa":
        sys.exit("head: ожидается 16 МиБ с MBR (0x55AA)")
    start = struct.unpack_from("<I", head, 446 + 8)[0]
    if head[446 + 4] != 0x83 or start * 512 != len(head):
        sys.exit(f"head: раздел 1 должен быть Linux и начинаться с {len(head) // 512} сектора (сейчас {start})")

    with tempfile.TemporaryDirectory(dir=Path(a.out).parent) as tmp:
        tmp = Path(tmp)
        ftar = tmp / "rootfs.tar"
        kept, dropped = filter_tar(Path(a.rootfs), ftar)
        print(f"rootfs: {kept} записей, убрано {dropped}")
        used = sum(m.size for m in tarfile.open(ftar))
        fs_mib = ((used >> 20) * 115 // 100 + a.free_mib + 15) // 16 * 16
        feats = [f for f in a.features.split() if f not in ("needs_recovery",)]
        conf = tmp / "mke2fs.conf"
        conf.write_text("[defaults]\n\tbase_features = " + ",".join(feats) + "\n"
                        "\tinode_size = 256\n[fs_types]\n\text4 = {\n\t\tfeatures = " + ",".join(feats) + "\n\t}\n")
        fs = tmp / "fs.img"
        env = dict(os.environ, MKE2FS_CONFIG=str(conf))
        subprocess.run(["mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096", "-U", a.uuid, "-L", a.label,
                        "-E", "root_owner=0:0", "-d", str(ftar), str(fs), f"{fs_mib}M"], check=True, env=env)
        subprocess.run(["e2fsck", "-fn", str(fs)], check=True, stdout=subprocess.DEVNULL)
        got = subprocess.run(["dumpe2fs", "-h", str(fs)], capture_output=True, text=True).stdout
        line = next(ln for ln in got.splitlines() if ln.startswith("Filesystem features"))
        print("ФС:", fs_mib, "МиБ;", line)
        sectors = fs_mib << 11
        mbr = bytearray(head)
        struct.pack_into("<I", mbr, 446 + 12, sectors)
        disk = tmp / "disk.img"
        with open(disk, "wb") as out, open(fs, "rb") as f:
            out.write(mbr)
            while buf := f.read(16 << 20):
                out.write(buf)
        subprocess.run(["zstd", "-q", "-f", "-3", "-T0", str(disk), "-o", a.out], check=True)
    print("готово:", a.out, os.path.getsize(a.out) >> 20, "МиБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
