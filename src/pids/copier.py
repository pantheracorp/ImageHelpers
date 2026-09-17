"""Platform kernel copies, reusing the handle the header was read from.

DESIGN.md sec 3.2 and sec 5: open the file once, read the first 64 KB for
``DateTimeOriginal``, then issue the kernel copy for the whole file.  The header bytes
are already in the page cache, so the kernel copy re-reads them from RAM at zero disk
cost -- one seek per file instead of two.

Every copy goes to ``<dest>.part`` and is then atomically ``os.replace``d, so a killed
process can never leave a half-written file that a later run mistakes for complete
(sec 7.4).
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sys
from typing import BinaryIO

from pids.paths import IS_MACOS, IS_WINDOWS, long_path

#: Buffer for the portable fallback path, and for hashing.
COPY_BUFSIZE = 1024 * 1024

PART_SUFFIX = ".part"

_HAS_SENDFILE = hasattr(os, "sendfile") and sys.platform.startswith("linux")
try:  # pragma: no cover - platform dependent
    import posix

    _HAS_FCOPYFILE = IS_MACOS and hasattr(posix, "_fcopyfile")
except ImportError:  # pragma: no cover - Windows
    posix = None  # type: ignore[assignment]
    _HAS_FCOPYFILE = False

if IS_WINDOWS:  # pragma: no cover - platform dependent
    import ctypes
    from ctypes import wintypes

    # use_last_error is required for ctypes.get_last_error() to hold the real
    # GetLastError value: without it the errno we report on a failed copy is whatever
    # unrelated call ran last.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CopyFileW = _kernel32.CopyFileW
    _CopyFileW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.BOOL)
    _CopyFileW.restype = wintypes.BOOL


class DiskFull(OSError):
    """Raised when the destination volume runs out of space (sec 7.5)."""


def _is_disk_full(exc: OSError) -> bool:
    return exc.errno in (errno.ENOSPC, errno.EDQUOT) or getattr(exc, "winerror", None) in (
        39,
        112,
    )


def _copy_bytes(fh: BinaryIO, out_fd: int, size: int) -> int:
    """Portable fallback: userspace read/write loop over the open source handle."""
    fh.seek(0)
    written = 0
    view = memoryview(bytearray(COPY_BUFSIZE))
    while True:
        read = fh.readinto(view)  # type: ignore[attr-defined]
        if not read:
            break
        os.write(out_fd, view[:read])
        written += read
    return written


def _copy_kernel(fh: BinaryIO, src_path: str, out_fd: int, size: int) -> int:
    """Kernel-side copy from the already-open handle where the platform allows it."""
    fh.seek(0)
    in_fd = fh.fileno()
    if _HAS_SENDFILE:
        offset = 0
        while offset < size:
            sent = os.sendfile(out_fd, in_fd, offset, size - offset)
            if sent == 0:
                break
            offset += sent
        return offset
    if _HAS_FCOPYFILE:
        try:
            posix._fcopyfile(in_fd, out_fd, posix._COPYFILE_DATA)  # type: ignore[attr-defined]
            return os.fstat(out_fd).st_size
        except OSError as exc:
            if _is_disk_full(exc):
                raise
            # Cross-device or unsupported filesystem: fall back to the byte loop.
    return _copy_bytes(fh, out_fd, size)


def copy_file(fh: BinaryIO, src_path: str, dest_path: str, size: int) -> int:
    """Copy the open source to ``dest_path`` and return the bytes written.

    Writes ``dest_path.part`` first, then ``os.replace``s it into place, then copies
    mtime/permissions across -- the R version silently dropped timestamps (sec 5.7).
    """
    tmp = dest_path + PART_SUFFIX
    tmp_os = long_path(tmp)
    dest_os = long_path(dest_path)
    written = 0
    try:
        if IS_WINDOWS:  # pragma: no cover - platform dependent
            # CopyFileW is path-based, but the source header is in the page cache, so
            # this is still a single physical read of the file.
            if not _CopyFileW(long_path(src_path), tmp_os, False):
                raise ctypes.WinError(ctypes.get_last_error())
            written = os.stat(tmp_os).st_size
        else:
            out_fd = os.open(tmp_os, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
            try:
                written = _copy_kernel(fh, src_path, out_fd, size)
            finally:
                os.close(out_fd)
        os.replace(tmp_os, dest_os)
    except OSError as exc:
        _unlink_quiet(tmp_os)
        if _is_disk_full(exc):
            raise DiskFull(exc.errno, str(exc), dest_path) from exc
        raise
    try:
        shutil.copystat(long_path(src_path), dest_os)
    except OSError:
        pass  # timestamps are best-effort on exFAT/SMB; never fail a copy for them
    return written


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def clear_partial(dest_path: str) -> None:
    """Remove a leftover ``.part`` for this destination."""
    _unlink_quiet(long_path(dest_path + PART_SUFFIX))


def hash_file(path: str, algo: str = "blake2b", digest_size: int = 16) -> str:
    """Stream a file through BLAKE2b.

    ``hashlib`` releases the GIL for large buffers, so this parallelises across the
    worker threads without a process pool (sec 3).
    """
    if algo == "blake2b":
        digest = hashlib.blake2b(digest_size=digest_size)
    else:
        digest = hashlib.new(algo)
    with open(long_path(path), "rb", buffering=0) as fh:
        view = memoryview(bytearray(COPY_BUFSIZE))
        while True:
            read = fh.readinto(view)  # type: ignore[attr-defined]
            if not read:
                break
            digest.update(view[:read])
    return digest.hexdigest()


def same_bytes(a: str, b: str) -> bool:
    """True if two files are byte-identical (size first, then hash)."""
    try:
        if os.stat(long_path(a)).st_size != os.stat(long_path(b)).st_size:
            return False
    except OSError:
        return False
    return hash_file(a) == hash_file(b)
