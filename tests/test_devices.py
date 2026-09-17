"""Device detection and worker sizing (DESIGN.md sec 4)."""

from __future__ import annotations

import os
import sys

import pytest

from pids import devices


def test_probe_resolves_a_plain_directory_not_just_a_mount_point(tmp_path):
    """`diskutil info` only accepts mount points; a dest directory must still probe."""
    target = tmp_path / "dest" / "nested"
    target.mkdir(parents=True)
    probe = devices.probe(str(target))
    assert probe.workers >= 1
    if sys.platform in ("darwin",) or sys.platform.startswith("linux"):
        assert probe.confident, f"detection failed on a plain directory: {probe.detail}"


def test_probe_of_a_nonexistent_path_walks_up_to_something_real(tmp_path):
    probe = devices.probe(str(tmp_path / "does" / "not" / "exist"))
    assert probe.workers >= 1


def test_unc_paths_are_treated_as_network():
    probe = devices.probe("\\\\nas\\photos\\dest")
    assert probe.device_class == devices.NETWORK
    assert probe.workers == devices.WORKERS[devices.NETWORK]


def test_worker_defaults_match_the_design_table():
    assert devices.WORKERS[devices.HDD] == 2
    assert devices.WORKERS[devices.SATA_SSD] == 4
    assert devices.WORKERS[devices.NVME] == 8
    assert devices.WORKERS[devices.NETWORK] == 16


def test_unknown_devices_assume_a_spinning_disk(monkeypatch):
    """Over-threading an HDD actively hurts; under-threading an SSD only costs speed."""
    monkeypatch.setattr(devices, "_probe_macos", lambda _p: None)
    monkeypatch.setattr(devices, "_probe_linux", lambda _p: None)
    monkeypatch.setattr(devices, "_probe_windows", lambda _p: None)
    probe = devices.probe(os.getcwd())
    assert probe.device_class == devices.UNKNOWN
    assert probe.workers == 2
    assert not probe.confident
    assert "assuming spinning disk" in probe.detail


@pytest.mark.parametrize("spec,expected", [("auto", None), ("1", 1), (8, 8), ("", None)])
def test_resolve_workers_accepts_auto_or_a_number(spec, expected, tmp_path):
    workers, probe = devices.resolve_workers(spec, str(tmp_path))
    assert workers == (probe.workers if expected is None else expected)


@pytest.mark.parametrize("bad", ["lots", "0", "-3", "4.5"])
def test_resolve_workers_rejects_nonsense(bad, tmp_path):
    with pytest.raises(ValueError):
        devices.resolve_workers(bad, str(tmp_path))


def test_same_physical_device_detects_a_shared_disk(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert devices.same_physical_device(str(a), str(b)) is True


def test_network_fstypes_are_classified_as_network(monkeypatch, tmp_path):
    monkeypatch.setattr(devices, "mount_fstype", lambda _p: "smbfs")
    probe = devices.probe(str(tmp_path))
    assert probe.device_class == devices.NETWORK
