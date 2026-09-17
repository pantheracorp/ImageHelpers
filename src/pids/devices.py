"""Best-effort destination device detection, for choosing a worker count.

DESIGN.md sec 4: worker count is sized to the *device*, not the CPU.  More threads on a
spinning disk cause seek thrash and go slower; a network share is latency-bound and
wants queue depth.  Detection is imperfect across Windows and macOS, so ``auto`` always
prints what it chose and how to override it -- and ``pids calibrate`` measures it.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass

HDD = "hdd"
SATA_SSD = "sata_ssd"
NVME = "nvme"
NETWORK = "network"
UNKNOWN = "unknown"

#: Worker defaults per device class (sec 4).
WORKERS = {HDD: 2, SATA_SSD: 4, NVME: 8, NETWORK: 16, UNKNOWN: 2}

NETWORK_FSTYPES = {
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


@dataclass(frozen=True)
class Probe:
    """What we think the destination volume is."""

    device_class: str
    workers: int
    detail: str

    @property
    def confident(self) -> bool:
        return self.device_class != UNKNOWN

    def describe(self) -> str:
        return f"{self.device_class} ({self.detail}) -> {self.workers} workers"


def _mount_table() -> list[tuple[str, str]]:
    """``[(mountpoint, fstype)]``, longest mountpoint first."""
    entries: list[tuple[str, str]] = []
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 3:
                        entries.append((parts[1].replace("\\040", " "), parts[2]))
        except OSError:
            pass
    elif sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["/sbin/mount"], capture_output=True, text=True, timeout=10
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        # e.g. "//guest@nas/photos on /Volumes/photos (smbfs, nodev, nosuid)"
        for line in out.splitlines():
            match = re.match(r"^\S+ on (.+?) \(([^,)]+)", line)
            if match:
                entries.append((match.group(1), match.group(2)))
    entries.sort(key=lambda item: len(item[0]), reverse=True)
    return entries


def mount_fstype(path: str) -> str | None:
    """Filesystem type of the volume holding ``path``, where the OS exposes it."""
    target = os.path.abspath(path)
    while not os.path.exists(target) and target != os.path.dirname(target):
        target = os.path.dirname(target)
    for mountpoint, fstype in _mount_table():
        if target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/"):
            return fstype
    return None


def _probe_linux(path: str) -> Probe | None:
    fstype = mount_fstype(path)
    if fstype and fstype.lower() in NETWORK_FSTYPES:
        return Probe(NETWORK, WORKERS[NETWORK], f"fstype {fstype}")
    try:
        st = os.stat(path)
        major, minor = os.major(st.st_dev), os.minor(st.st_dev)
        dev_link = f"/sys/dev/block/{major}:{minor}"
        real = os.path.realpath(dev_link)
        name = os.path.basename(real)
        # Walk up from a partition (sda1) to its disk (sda).
        queue = f"/sys/class/block/{name}/queue/rotational"
        if not os.path.exists(queue):
            parent = os.path.basename(os.path.dirname(real))
            queue = f"/sys/class/block/{parent}/queue/rotational"
            name = parent
        with open(queue, "r", encoding="ascii") as fh:
            rotational = fh.read().strip() == "1"
        if rotational:
            return Probe(HDD, WORKERS[HDD], f"/sys rotational=1 ({name})")
        if name.startswith("nvme"):
            return Probe(NVME, WORKERS[NVME], f"/sys {name}")
        return Probe(SATA_SSD, WORKERS[SATA_SSD], f"/sys rotational=0 ({name})")
    except (OSError, ValueError):
        return None


def _probe_macos(path: str) -> Probe | None:
    fstype = mount_fstype(path)
    if fstype and fstype.lower() in NETWORK_FSTYPES:
        return Probe(NETWORK, WORKERS[NETWORK], f"fstype {fstype}")
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", path],
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        info = plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return None
    protocol = str(info.get("BusProtocol") or "")
    solid = info.get("SolidState")
    if solid is False:
        return Probe(HDD, WORKERS[HDD], f"diskutil SolidState=No, bus {protocol or '?'}")
    if solid is True:
        if protocol.upper() in {"PCI-EXPRESS", "PCI", "APPLE FABRIC", "NVME"}:
            return Probe(NVME, WORKERS[NVME], f"diskutil SolidState=Yes, bus {protocol}")
        return Probe(SATA_SSD, WORKERS[SATA_SSD], f"diskutil SolidState=Yes, bus {protocol}")
    return None


def _probe_windows(path: str) -> Probe | None:  # pragma: no cover - platform dependent
    if path.startswith("\\\\") or path.startswith("//"):
        return Probe(NETWORK, WORKERS[NETWORK], "UNC path")
    try:
        import ctypes

        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if drive:
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
            if drive_type == 4:  # DRIVE_REMOTE
                return Probe(NETWORK, WORKERS[NETWORK], "mapped network drive")
    except Exception:
        pass
    # MediaType via PowerShell is slow and often reports "Unspecified" for USB
    # enclosures, so a wrong guess would be worse than admitting we do not know.
    return None


def probe(path: str) -> Probe:
    """Classify the volume holding ``path``.

    Falls back to assuming a spinning disk: two workers is the safe answer, because
    over-threading an HDD actively hurts while under-threading an SSD only leaves
    throughput on the table.
    """
    if path.startswith("\\\\") or path.startswith("//"):
        return Probe(NETWORK, WORKERS[NETWORK], "UNC path")
    result: Probe | None = None
    if sys.platform.startswith("linux"):
        result = _probe_linux(path)
    elif sys.platform == "darwin":
        result = _probe_macos(path)
    elif sys.platform == "win32":  # pragma: no cover - platform dependent
        result = _probe_windows(path)
    if result is not None:
        return result
    return Probe(UNKNOWN, WORKERS[UNKNOWN], "not detected, assuming spinning disk")


def resolve_workers(spec: str | int | None, dest: str) -> tuple[int, Probe]:
    """Turn ``--workers auto|N`` into a number, with the probe for reporting."""
    detected = probe(dest)
    if spec is None or spec == "auto" or spec == "":
        return detected.workers, detected
    try:
        count = int(spec)
    except (TypeError, ValueError):
        raise ValueError(f"--workers must be 'auto' or an integer, got {spec!r}") from None
    if count < 1:
        raise ValueError("--workers must be at least 1")
    return count, detected


def same_physical_device(a: str, b: str) -> bool | None:
    """Whether two paths sit on the same device.

    Returns ``None`` when it cannot be determined.  Used only to warn: reading and
    writing 3 TB through one USB HDD roughly halves effective throughput and is worth
    more than every other optimisation combined (sec 3.1).
    """
    try:
        return os.stat(_existing(a)).st_dev == os.stat(_existing(b)).st_dev
    except OSError:
        return None


def _existing(path: str) -> str:
    target = os.path.abspath(path)
    while not os.path.exists(target) and target != os.path.dirname(target):
        target = os.path.dirname(target)
    return target
