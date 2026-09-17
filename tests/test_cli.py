"""CLI surface (DESIGN.md sec 9)."""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner
from conftest import write_jpeg

from pids import db
from pids.cli import main


@pytest.fixture
def runner():
    return CliRunner()


def invoke(runner, *args):
    result = runner.invoke(main, list(args), catch_exceptions=False)
    return result


def test_help_lists_every_command(runner):
    result = invoke(runner, "--help")
    for command in ("scan", "run", "verify", "report", "calibrate", "cameras"):
        assert command in result.output


def test_scan_reports_the_plan_and_copies_nothing(runner, tree, dest, tmp_path):
    result = invoke(
        runner, "scan", "--source", str(tree), "--state", str(tmp_path / "s.sqlite"), "-q"
    )
    assert result.exit_code == 0, result.output
    assert "planned       4" in result.output
    assert "quarantined   3" in result.output
    assert "Nothing was copied" in result.output
    assert os.listdir(dest) == []


def test_scan_writes_preview_csvs_with_out(runner, tree, tmp_path):
    out = tmp_path / "preview"
    result = invoke(
        runner, "scan", "--source", str(tree), "--state", str(tmp_path / "s.sqlite"),
        "--out", str(out), "-q",
    )
    assert result.exit_code == 0, result.output
    assert (out / "qc_summary.csv").exists()
    assert (out / "unsorted.csv").exists()


def test_run_copies_and_prints_a_summary(runner, tree, dest, tmp_path):
    result = invoke(
        runner, "run", "--source", str(tree), "--dest", str(dest),
        "--state", str(tmp_path / "s.sqlite"), "-q",
    )
    assert result.exit_code == 0, result.output
    assert "copied" in result.output
    assert (dest / "Check" / "CA101" / "071424" / "IMG_0001.JPG").exists()
    assert "QC:" in result.output


def test_run_dry_run_is_planning_only(runner, tree, dest, tmp_path):
    result = invoke(
        runner, "run", "--source", str(tree), "--dest", str(dest),
        "--state", str(tmp_path / "s.sqlite"), "--dry-run", "-q",
    )
    assert result.exit_code == 0, result.output
    assert "planning only" in result.output
    assert os.listdir(dest) == []


def test_run_with_verify_sample(runner, tree, dest, tmp_path):
    result = invoke(
        runner, "run", "--source", str(tree), "--dest", str(dest),
        "--state", str(tmp_path / "s.sqlite"), "--verify-sample", "1.0", "-q",
    )
    assert result.exit_code == 0, result.output
    assert "VERIFY PASS" in result.output


def test_run_rejects_a_state_db_on_a_network_share(runner, tree, dest, tmp_path, monkeypatch):
    """SQLite over SMB/NFS can corrupt the database (sec 8.5)."""
    monkeypatch.setattr("pids.cli.is_network_path", lambda _path: True)
    result = runner.invoke(
        main,
        ["run", "--source", str(tree), "--dest", str(dest), "--state", "/mnt/share/s.sqlite"],
    )
    assert result.exit_code != 0
    assert "network share" in result.output
    assert "--journal jsonl" in result.output


def test_report_writes_csvs_after_a_run(runner, tree, dest, tmp_path):
    state = str(tmp_path / "s.sqlite")
    invoke(runner, "run", "--source", str(tree), "--dest", str(dest), "--state", state, "-q")
    out = tmp_path / "reports"
    result = invoke(runner, "report", "--state", state, "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "restructuring_log.csv" in result.output
    assert (out / "restructuring_log.csv").exists()


def test_verify_exits_nonzero_when_something_is_wrong(runner, tree, dest, tmp_path):
    state = str(tmp_path / "s.sqlite")
    invoke(runner, "run", "--source", str(tree), "--dest", str(dest), "--state", state, "-q")
    os.remove(dest / "Check" / "CA101" / "071424" / "IMG_0001.JPG")
    result = runner.invoke(main, ["verify", "--state", state, "--sample", "0", "-q"])
    assert result.exit_code == 1
    assert "VERIFY FAIL" in result.output


def test_cameras_lists_resolved_folders(runner, tree):
    result = invoke(runner, "cameras", "--source", str(tree))
    assert result.exit_code == 0
    for camera in ("CA101", "CA102", "CA105"):
        assert camera in result.output


def test_cameras_flags_padding_and_cross_site(runner, tmp_path):
    src = tmp_path / "src"
    write_jpeg(str(src / "North" / "CAM5" / "IMG_1.JPG"))
    write_jpeg(str(src / "South" / "CAM5" / "IMG_2.JPG"))
    write_jpeg(str(src / "North" / "CAM05" / "IMG_3.JPG"))
    result = invoke(runner, "cameras", "--source", str(src))
    assert "ZERO-PADDING CONFLICT" in result.output
    assert "CROSS-SITE CAMERAS" in result.output


def test_limit_enables_a_small_trial_run(runner, tree, dest, tmp_path):
    result = invoke(
        runner, "run", "--source", str(tree), "--dest", str(dest),
        "--state", str(tmp_path / "s.sqlite"), "--limit", "2", "-q",
    )
    assert result.exit_code == 0, result.output
    assert "seen          2" in result.output


def test_retry_failed_hint_is_printed_when_something_fails(runner, tree, dest, tmp_path, monkeypatch):
    import pids.pipeline as pipeline_mod

    monkeypatch.setattr(
        pipeline_mod.copier,
        "copy_file",
        lambda *args: (_ for _ in ()).throw(OSError(5, "Input/output error")),
    )
    result = invoke(
        runner, "run", "--source", str(tree), "--dest", str(dest),
        "--state", str(tmp_path / "s.sqlite"), "-q",
    )
    assert "--retry-failed" in result.output


def test_calibrate_measures_a_ladder(runner, tmp_path):
    src = tmp_path / "src"
    for index in range(24):
        write_jpeg(str(src / "CAM101" / f"IMG_{index:04d}.JPG"), size=4096)
    result = invoke(
        runner, "calibrate", "--source", str(src), "--dest", str(tmp_path / "dest"),
        "--per-round", "4", "--ladder", "1,2",
    )
    assert result.exit_code == 0, result.output
    assert "Recommended: --workers" in result.output
    assert not os.path.exists(tmp_path / "dest" / ".pids-calibrate")


def test_multiple_source_roots_in_one_run(runner, tmp_path):
    """Sec 11.4: several drives in one invocation."""
    src_a = tmp_path / "a"
    src_b = tmp_path / "b"
    write_jpeg(str(src_a / "CAM101" / "IMG_1.JPG"), "2024:07:14 06:31:02")
    write_jpeg(str(src_b / "CAM102" / "IMG_1.JPG"), "2024:07:14 06:31:02")
    dest = tmp_path / "dest"
    result = invoke(
        runner, "run", "--source", str(src_a), "--source", str(src_b),
        "--dest", str(dest), "--state", str(tmp_path / "s.sqlite"), "-q",
    )
    assert result.exit_code == 0, result.output
    assert (dest / "Check" / "CA101" / "071424" / "IMG_1.JPG").exists()
    assert (dest / "Check" / "CA102" / "071424" / "IMG_1.JPG").exists()


def test_a_log_file_is_written(runner, tree, dest, tmp_path):
    logfile = tmp_path / "pids.log"
    invoke(
        runner, "run", "--source", str(tree), "--dest", str(dest),
        "--state", str(tmp_path / "s.sqlite"), "--log", str(logfile), "-q",
    )
    assert logfile.exists()
    assert "collision" in logfile.read_text(encoding="utf-8")


def test_run_history_is_auditable_across_invocations(runner, tree, dest, tmp_path):
    state = str(tmp_path / "s.sqlite")
    invoke(runner, "scan", "--source", str(tree), "--state", state, "-q")
    invoke(runner, "run", "--source", str(tree), "--dest", str(dest), "--state", state, "-q")
    invoke(
        runner, "run", "--source", str(tree), "--dest", str(dest), "--state", state,
        "--resume", "-q",
    )
    with db.open_store(state, "sqlite") as store:
        runs = store.runs()
    assert [row[1] for row in runs] == ["scan", "run", "run"]
    assert all(row[4] is not None for row in runs), "every run records an end time"
