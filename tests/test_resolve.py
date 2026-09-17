"""Camera/site resolution against both confirmed layouts (DESIGN.md sec 6, sec 12.3)."""

from __future__ import annotations

import pytest

from pids.resolve import padding_conflicts, resolve_camera


@pytest.mark.parametrize(
    "parts,camera_id,site",
    [
        # Layout A: ROOT/CAM101/*.JPG
        (("CAM101",), "CA101", None),
        # Layout B: ROOT/Site/CAM101/100EK113/*.JPG -- deepest-first still finds it
        (("NorthRange", "CAM101", "100EK113"), "CA101", "NorthRange"),
        (("Survey2024", "NorthRange", "CAM101", "100MEDIA"), "CA101", "NorthRange"),
        # Prefix variants
        (("CA101",), "CA101", None),
        (("CT101",), "CA101", None),
        (("C101",), "CA101", None),
        (("CAM 101",), "CA101", None),
        (("CAM_101",), "CA101", None),
        (("CAM-101",), "CA101", None),
        (("cam101",), "CA101", None),
        # Trailing field notes survive because the match anchors at the start
        (("CAM105 (Stolen)",), "CA105", None),
        (("CAM105 - battery dead",), "CA105", None),
        # Digits as found: no invented zero-padding
        (("CAM05",), "CA05", None),
        (("CAM5",), "CA5", None),
    ],
)
def test_resolves(parts, camera_id, site):
    camera = resolve_camera(parts)
    assert camera is not None
    assert camera.camera_id == camera_id
    assert camera.site == site


@pytest.mark.parametrize(
    "parts",
    [
        (),
        ("Loose Images",),
        ("Photos", "Random"),
        ("100EK113",),  # DCIM folder alone is not a camera
        ("100MEDIA",),
        ("101RECNX",),
        ("Camp3",),  # 'Ca' followed by letters is not a camera
        ("Site2024",),
    ],
)
def test_unresolvable(parts):
    assert resolve_camera(parts) is None


def test_dcim_folder_does_not_win_over_the_camera_folder():
    camera = resolve_camera(("CAM101", "100EK113"))
    assert camera is not None and camera.camera_id == "CA101"


def test_deepest_camera_folder_wins_over_a_numeric_site():
    """A site folder that happens to look camera-ish must not beat the real one."""
    camera = resolve_camera(("CT12", "CAM101", "100EK113"))
    assert camera is not None
    assert camera.camera_id == "CA101"
    assert camera.site == "CT12"


def test_site_skips_dcim_folders_when_looking_upward():
    camera = resolve_camera(("NorthRange", "100EK113", "CAM101"))
    assert camera is not None and camera.site == "NorthRange"


def test_padding_conflicts_detects_ca5_and_ca05():
    assert padding_conflicts(["CA5", "CA05", "CA6"]) == {5: ["CA05", "CA5"]}


def test_padding_conflicts_ignores_distinct_cameras():
    assert padding_conflicts(["CA5", "CA50", "CA500"]) == {}
