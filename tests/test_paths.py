"""Path handling edge cases (DESIGN.md sec 7.1)."""

from __future__ import annotations

import os
from pathlib import PurePath, PureWindowsPath

import pytest

from pids import paths


def test_dest_key_is_case_insensitive_where_the_filesystem_is():
    a = paths.dest_key("/dest/Check/CA101/071424/IMG_1.JPG")
    b = paths.dest_key("/dest/Check/CA101/071424/img_1.jpg")
    if paths.CASE_INSENSITIVE_FS:
        assert a == b, "IMG_1.JPG and img_1.jpg must be treated as colliding"
    else:
        assert a != b


def test_dest_key_normalises_separators():
    assert paths.dest_key("a\\b\\c.jpg") == paths.dest_key("a/b/c.jpg")


def test_relative_parts_handles_a_trailing_slash_on_the_root():
    """The R version produced a drive letter as the camera folder in this case."""
    root = PurePath("/src/")
    parts = paths.relative_parts(PurePath("/src/CAM101/100EK113/IMG_1.JPG"), root)
    assert parts == ("CAM101", "100EK113", "IMG_1.JPG")


def test_normalise_root_strips_trailing_separator():
    assert str(paths.normalise_root("/tmp/foo/")) == os.path.abspath("/tmp/foo")


def test_is_within():
    assert paths.is_within("/a/b/c", "/a/b")
    assert paths.is_within("/a/b", "/a/b")
    assert not paths.is_within("/a/bc", "/a/b")
    assert not paths.is_within("/a", "/a/b")


def test_is_jpeg_name():
    assert paths.is_jpeg_name("IMG_1.JPG")
    assert paths.is_jpeg_name("IMG_1.jpeg")
    assert not paths.is_jpeg_name("IMG_1.png")
    assert not paths.is_jpeg_name("jpg")


def test_safe_component_never_returns_empty():
    assert paths.safe_component("   ") == "_"
    assert paths.safe_component(" CAM101 ") == "CAM101"


def test_long_path_is_a_noop_off_windows():
    if not paths.IS_WINDOWS:
        assert paths.long_path("/tmp/x") == "/tmp/x"


# -- Windows path handling (sec 6). These run everywhere: the transforms are pure
# -- string logic, so the Windows behaviour is pinned without a Windows machine.


def test_strip_long_prefix_reverses_long_path():
    assert paths.strip_long_prefix("\\\\?\\C:\\src\\CAM101\\IMG_1.JPG") == "C:\\src\\CAM101\\IMG_1.JPG"
    assert paths.strip_long_prefix("\\\\?\\UNC\\nas\\share\\x.jpg") == "\\\\nas\\share\\x.jpg"
    assert paths.strip_long_prefix("/plain/path") == "/plain/path"


def test_strip_long_prefix_keeps_paths_relative_to_the_source_root():
    """A prefixed path does not match the un-prefixed root, which would break sec 6.

    Shown with PureWindowsPath so the failure mode is reproduced off Windows: the
    prefixed form cannot be made relative, the stripped form can.
    """
    root = PureWindowsPath("C:/src")
    prefixed = PureWindowsPath("\\\\?\\C:\\src\\NorthRange\\CAM101\\100EK113\\IMG_1.JPG")
    with pytest.raises(ValueError):
        prefixed.relative_to(root)
    stripped = PureWindowsPath(paths.strip_long_prefix(str(prefixed)))
    assert stripped.relative_to(root).parts == (
        "NorthRange",
        "CAM101",
        "100EK113",
        "IMG_1.JPG",
    )


def test_long_path_round_trips_for_unc_and_drive_paths():
    if not paths.IS_WINDOWS:
        pytest.skip("long_path is a no-op off Windows")
    for original in ("C:\\src\\a.jpg", "\\\\nas\\share\\a.jpg"):
        assert paths.strip_long_prefix(paths.long_path(original)) == original


def test_walker_yields_plain_paths(tmp_path):
    """The walk must never emit a `\\\\?\\` path, whatever it handed to scandir."""
    from pids import walker

    src = tmp_path / "CAM101"
    src.mkdir()
    (src / "IMG_1.JPG").write_bytes(b"\xff\xd8\xff\xd9")
    items = list(walker.iter_images([str(tmp_path)]))
    assert len(items) == 1
    assert "?" not in items[0].path
    assert items[0].path.startswith(str(tmp_path))
