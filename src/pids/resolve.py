"""Camera ID and site resolution from the image's path.

One rule replaces the R script's dead Section 6 / broken Section 6B pair, where 6B
unconditionally overwrote 6 and set ``camera_id`` from ``basename(SOURCE_DIR)`` for
every row -- pointed at a multi-camera root that produced ``NA`` everywhere and copied
zero files with only a warning (DESIGN.md sec 1, sec 6).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

#: Camera folder names: ``CAM101``, ``CA 101``, ``CT_101``, ``C101``.  Anchored at the
#: start so trailing field notes survive: ``CAM105 (Stolen)`` -> ``105``.
CAMERA_RE = re.compile(r"^(?:CAM|CA|CT|C)[\s_-]*(\d+)", re.IGNORECASE)

#: DCIM-style auto-numbered folders written by the camera itself: ``100EK113``,
#: ``100MEDIA``, ``101RECNX``.  Discarded before camera matching so a deepest-first
#: walk does not mistake them for anything meaningful.
DCIM_RE = re.compile(r"^\d{3}[A-Z0-9_]+$", re.IGNORECASE)

NO_CAMERA_ID = "no_camera_id"


@dataclass(frozen=True)
class Camera:
    """A resolved camera folder."""

    camera_id: str  # output folder name, e.g. "CA101"
    digits: str  # digits exactly as found, e.g. "101" or "05"
    folder: str  # the source folder that matched, e.g. "CAM105 (Stolen)"
    site: str | None  # the component directly above it, recorded not used in the path


def resolve_camera(parts: Sequence[str]) -> Camera | None:
    """Find the camera folder in ``parts`` (path components below the source root).

    ``parts`` must not include the filename.  Walks from the *deepest* component
    upward and takes the first match, so ``Site/CAM101/100EK113/img.jpg`` and
    ``ROOT/CAM101/img.jpg`` both yield ``CA101``, and a site folder that happens to
    contain digits cannot win over the real camera folder (sec 6).
    """
    for i in range(len(parts) - 1, -1, -1):
        name = parts[i].strip()
        if not name or DCIM_RE.match(name):
            continue
        match = CAMERA_RE.match(name)
        if not match:
            continue
        digits = match.group(1)
        site = None
        for j in range(i - 1, -1, -1):
            candidate = parts[j].strip()
            if candidate and not DCIM_RE.match(candidate):
                site = candidate
                break
        # Output ID is "CA" + the digits *as found*: CAM05 -> CA05, CAM5 -> CA5.  The R
        # header comment promised zero-padding but the code never did it, so padding
        # here would silently rename every camera relative to existing PantheraIDS
        # data.  A run containing both forms is a pre-flight error instead (sec 11.2).
        return Camera(camera_id="CA" + digits, digits=digits, folder=name, site=site)
    return None


def padding_conflicts(camera_ids: Iterable[str]) -> dict[int, list[str]]:
    """Group camera IDs that differ only by zero-padding.

    ``["CA5", "CA05", "CA6"]`` -> ``{5: ["CA05", "CA5"]}``.  Returned groups are a hard
    pre-flight error rather than a silent merge of two cameras (sec 6, sec 11.2).
    """
    groups: dict[int, set[str]] = defaultdict(set)
    for cam in camera_ids:
        if not cam:
            continue
        digits = cam[2:] if cam.upper().startswith("CA") else cam
        if not digits.isdigit():
            continue
        groups[int(digits)].add(cam)
    return {key: sorted(values) for key, values in groups.items() if len(values) > 1}
