"""Shared fixtures.  ``src`` is put on the path so the suite runs without an install."""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from pids import synth  # noqa: E402


def write_jpeg(path: str, dt: str | None = synth.DEFAULT_DT, **kwargs) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(synth.jpeg(dt, **kwargs))
    return path


@pytest.fixture
def tree(tmp_path):
    """A small tree covering both confirmed layouts and every edge case at once.

    flat:    ROOT/CAM101/IMG_0001.JPG, IMG_0002.JPG
    nested:  ROOT/NorthRange/CAM102/100EK113/IMG_0001.JPG
             ROOT/NorthRange/CAM102/101EK113/IMG_0001.JPG  <- filename collision
    notes:   ROOT/CAM105 (Stolen)/IMG_0001.JPG
    bad:     no EXIF / zero date / no camera folder
    """
    src = tmp_path / "src"
    write_jpeg(str(src / "CAM101" / "IMG_0001.JPG"), "2024:07:14 06:31:02")
    write_jpeg(str(src / "CAM101" / "IMG_0002.JPG"), "2024:07:15 07:00:00")
    write_jpeg(
        str(src / "NorthRange" / "CAM102" / "100EK113" / "IMG_0001.JPG"),
        "2024:07:14 08:00:00",
    )
    write_jpeg(
        str(src / "NorthRange" / "CAM102" / "101EK113" / "IMG_0001.JPG"),
        "2024:07:14 09:00:00",
        size=2048,
    )
    write_jpeg(str(src / "CAM105 (Stolen)" / "IMG_0001.JPG"), "2024:07:20 10:00:00")
    write_jpeg(str(src / "CAM101" / "NOEXIF.JPG"), None, include_exif=False)
    write_jpeg(str(src / "CAM101" / "ZERO.JPG"), "0000:00:00 00:00:00")
    write_jpeg(str(src / "Loose Images" / "IMG_9999.JPG"), "2024:07:14 06:31:02")
    (src / "CAM101" / "notes.txt").write_text("not an image", encoding="utf-8")
    return src


@pytest.fixture
def dest(tmp_path):
    path = tmp_path / "dest"
    path.mkdir()
    return path


@pytest.fixture
def state(tmp_path):
    return str(tmp_path / "state" / "pids.sqlite")
