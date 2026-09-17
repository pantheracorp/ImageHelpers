"""Path handling that survives Windows, macOS and network shares.

DESIGN.md sec 6 (Windows specifics) and sec 7.1 (path handling).  Everything here is
deliberately built on real path operations rather than string replacement -- the R
version's ``str_replace(SourceFile, SOURCE_DIR, "")`` produced a drive letter as the
camera folder whenever ``SOURCE_DIR`` had a trailing slash or different case.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePath

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

#: Destination paths are compared case-insensitively on Windows and macOS, so
#: ``IMG_1.JPG`` and ``img_1.jpg`` are correctly treated as colliding (sec 7.1).
CASE_INSENSITIVE_FS = IS_WINDOWS or IS_MACOS

#: Directories that are never image sources: OS bookkeeping and NAS metadata.
SKIP_DIRS = frozenset(
    {
        "$recycle.bin",
        ".ds_store",
        ".spotlight-v100",
        ".trash",
        ".trashes",
        ".fseventsd",
        ".documentrevisions-v100",
        ".temporaryitems",
        "system volume information",
        "@eadir",
        "#recycle",
        "lost+found",
    }
)

JPEG_SUFFIXES = frozenset({".jpg", ".jpeg"})


def normalise_root(p: str | os.PathLike[str]) -> Path:
    """Absolute, symlink-resolved root with any trailing separator removed."""
    return Path(os.path.abspath(os.path.expanduser(str(p))))


def long_path(p: str | os.PathLike[str]) -> str:
    r"""Add the Windows ``\\?\`` prefix so paths over 260 chars work.

    ``Site/Camera/Subfolder/`` nesting plus a long destination root makes MAX_PATH a
    real risk, not a theoretical one.  No-op off Windows and for UNC paths already
    prefixed.
    """
    s = os.fspath(p)
    if not IS_WINDOWS or s.startswith("\\\\?\\"):
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def strip_long_prefix(p: str) -> str:
    r"""Remove a ``\\?\`` prefix, returning an ordinary path.

    ``long_path()`` is applied only at syscall boundaries, so nothing else in the tool
    ever stores a prefixed path.  ``os.scandir`` is the exception: hand it a prefixed
    directory and every ``DirEntry.path`` it returns carries the prefix forward, which
    would end up in the manifest, in the reports, and -- worse -- would stop
    ``relative_to(source_root)`` matching, so camera resolution would silently fail on
    exactly the deep trees the prefix exists to support.
    """
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def safe_component(name: str) -> str:
    """Normalise a single path component for use in an output path.

    Windows silently strips trailing dots and spaces from directory names, which turns
    a camera folder called ``"CAM105 "`` into a different directory than the one we
    recorded.  Normalise up front so the manifest and the filesystem agree.
    """
    cleaned = name.strip()
    if IS_WINDOWS:
        cleaned = cleaned.rstrip(". ")
    return cleaned or "_"


def dest_key(path: str | os.PathLike[str]) -> str:
    """Collision-arbitration key for a destination path.

    Case-folded on case-insensitive filesystems and separator-normalised, so the
    UNIQUE index in the state DB matches what the filesystem will actually do.
    """
    s = os.fspath(path).replace("\\", "/")
    return s.casefold() if CASE_INSENSITIVE_FS else s


def is_network_path(p: str | os.PathLike[str]) -> bool:
    """True for UNC paths and for mount points whose filesystem is a network one."""
    s = os.fspath(p)
    from pids.devices import is_network_drive, mount_fstype  # local import: avoids a cycle

    if is_network_drive(s):
        return True
    fstype = mount_fstype(s)
    if not fstype:
        return False
    return fstype.lower() in {
        "smbfs",
        "cifs",
        "smb2",
        "nfs",
        "nfs4",
        "afpfs",
        "webdav",
        "fuse.sshfs",
        "9p",
    }


def relative_parts(path: PurePath, root: PurePath) -> tuple[str, ...]:
    """Path components of ``path`` below ``root``, filename included.

    Uses real path relativity (``os.path.relpath`` semantics via ``PurePath``), and
    falls back to the whole path if ``path`` is somehow not under ``root``.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        try:
            rel = PurePath(os.path.relpath(str(path), str(root)))
        except ValueError:  # different drives on Windows
            return path.parts
    return rel.parts


def is_within(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    """True if ``child`` is ``parent`` or lives underneath it.

    Used to stop ``run`` from walking its own output when the destination is nested
    inside the source tree, which would otherwise re-ingest copied files forever.
    """
    c = os.path.normcase(os.path.abspath(os.fspath(child)))
    p = os.path.normcase(os.path.abspath(os.fspath(parent)))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def is_jpeg_name(name: str) -> bool:
    dot = name.rfind(".")
    return dot != -1 and name[dot:].lower() in JPEG_SUFFIXES


def free_bytes(path: str | os.PathLike[str]) -> int:
    """Free space on the volume holding ``path`` (walking up to an existing dir)."""
    p = Path(os.path.abspath(os.fspath(path)))
    while not p.exists() and p != p.parent:
        p = p.parent
    usage = os.statvfs(str(p)) if not IS_WINDOWS else None
    if usage is not None:
        return usage.f_frsize * usage.f_bavail
    import shutil

    return shutil.disk_usage(str(p)).free
