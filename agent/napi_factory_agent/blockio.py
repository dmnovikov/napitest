"""Доступ к целевому блочному устройству с проверкой идентичности.

Перед записью: открытый fd должен быть блочным устройством с тем же major:minor, что
распознанная в sysfs SD-NAND, и того же размера. Иначе запись не начинается.
"""

from __future__ import annotations

import fcntl
import mmap
import os
import stat

BLKFLSBUF = 0x1261
BLKGETSIZE64 = 0x80081272
ALIGN = 4096


class TargetMismatch(Exception):
    pass


# CHECKED NAPI-C: BLKFLSBUF и O_DIRECT на mmc-контроллере RK3308 (прошивка со сверкой проходит) (readback идёт с носителя)
class BlockTarget:
    """Открытое целевое устройство. Писать через write_at, читать через read_at."""

    def __init__(self, path: str, expected_dev: str | None, expected_size: int, require_blockdev: bool = True):
        self.path = path
        self.size = expected_size
        self.fd = os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        try:
            st = os.fstat(self.fd)
            if require_blockdev:
                if not stat.S_ISBLK(st.st_mode):
                    raise TargetMismatch(f"{path}: не блочное устройство")
                got = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
                if got != expected_dev:
                    raise TargetMismatch(f"{path}: major:minor {got} ≠ {expected_dev} из sysfs")
                buf = bytearray(8)
                fcntl.ioctl(self.fd, BLKGETSIZE64, buf)
                real_size = int.from_bytes(buf, "little")
            else:
                real_size = os.lseek(self.fd, 0, os.SEEK_END)
            if real_size != expected_size:
                raise TargetMismatch(f"{path}: размер {real_size} ≠ {expected_size} из sysfs")
        except BaseException:
            os.close(self.fd)
            raise
        self._rfd: int | None = None
        self.direct = False

    # --- запись -------------------------------------------------------
    def write_at(self, offset: int, data: bytes | memoryview) -> None:
        if offset < 0 or offset + len(data) > self.size:
            raise ValueError(f"запись за пределами ёмкости: {offset}+{len(data)} > {self.size}")
        view = memoryview(data)
        while view:
            n = os.pwrite(self.fd, view, offset)
            if n <= 0:
                raise OSError("pwrite вернул 0")
            view = view[n:]
            offset += n

    def sync(self) -> None:
        os.fsync(self.fd)

    def flush_cache(self) -> None:
        """Сброс кеша страниц, чтобы readback шёл с носителя."""
        try:
            fcntl.ioctl(self.fd, BLKFLSBUF, 0)
        except OSError:
            pass
        if self._rfd is not None:
            os.close(self._rfd)
            self._rfd = None

    # --- чтение -------------------------------------------------------
    def _reader(self) -> int:
        if self._rfd is None:
            try:
                self._rfd = os.open(self.path, os.O_RDONLY | os.O_DIRECT)
                self.direct = True
            except (OSError, AttributeError):
                self._rfd = os.open(self.path, os.O_RDONLY)
                self.direct = False
        return self._rfd

    def read_at(self, offset: int, n: int) -> bytes:
        if offset < 0 or offset + n > self.size:
            raise ValueError(f"чтение за пределами ёмкости: {offset}+{n} > {self.size}")
        fd = self._reader()
        if self.direct:
            start = offset - offset % ALIGN
            end = min(-(-(offset + n) // ALIGN) * ALIGN, self.size)
            length = end - start
            buf = mmap.mmap(-1, -(-length // ALIGN) * ALIGN)
            try:
                try:
                    got = os.preadv(fd, [buf], start)
                except OSError as e:
                    if e.errno == 22:  # EINVAL: O_DIRECT не поддерживается здесь
                        os.close(fd)
                        self._rfd = os.open(self.path, os.O_RDONLY)
                        self.direct = False
                        return self.read_at(offset, n)
                    raise
                if got < offset - start + n:
                    raise OSError(f"короткое чтение {got} на {start}")
                return bytes(buf[offset - start: offset - start + n])
            finally:
                buf.close()
        out = bytearray()
        while len(out) < n:
            chunk = os.pread(fd, n - len(out), offset + len(out))
            if not chunk:
                raise OSError(f"короткое чтение на {offset + len(out)}")
            out += chunk
        return bytes(out)

    def close(self) -> None:
        for fd in (self._rfd, self.fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._rfd = None

    def __enter__(self) -> "BlockTarget":
        return self

    def __exit__(self, *a) -> None:
        self.close()
