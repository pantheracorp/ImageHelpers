"""Crash injection: kill -9 mid-run, then resume (deliverable 6, DESIGN.md sec 8.4).

The guarantee under test: a killed process loses at most the last uncommitted batch,
those files are simply recopied, and a file can never be recorded ``ok`` while its
destination is half-written.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from pids import db  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="SIGKILL/SIGTERM semantics differ on Windows"
)


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    return env


def build_tree(out: str, cameras: int = 4, per_camera: int = 300) -> None:
    subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "tools", "make_tree.py"),
            "--out", out,
            "--cameras", str(cameras),
            "--per-camera", str(per_camera),
            "--size", "3072",
            "--bad-exif", "0",
            "--collisions", "0",
            "--layout", "nested",
            "--quiet",
        ],
        check=True,
        env=child_env(),
        capture_output=True,
    )


def run_cmd(*args: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pids", *args],
        env=env or child_env(),
        capture_output=True,
        text=True,
    )


def jpegs(root: str) -> set[str]:
    out = set()
    for directory, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".jpg"):
                out.add(os.path.relpath(os.path.join(directory, name), root))
    return out


def part_files(root: str) -> list[str]:
    return [
        os.path.join(directory, name)
        for directory, _dirs, files in os.walk(root)
        for name in files
        if name.endswith(".part")
    ]


def wait_for_progress(dest: str, minimum: int, timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        count = len(jpegs(dest)) if os.path.isdir(dest) else 0
        if count >= minimum:
            return count
        time.sleep(0.05)
    raise AssertionError(f"copy never reached {minimum} files")


@pytest.fixture(scope="module")
def big_tree(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("crash") / "src")
    build_tree(path)
    return path


def test_kill_9_midrun_then_resume_matches_a_clean_run(big_tree, tmp_path):
    reference = str(tmp_path / "reference")
    clean = run_cmd(
        "run", "--source", big_tree, "--dest", reference,
        "--state", str(tmp_path / "ref.sqlite"), "--workers", "4", "--quiet",
    )
    assert clean.returncode == 0, clean.stderr
    expected = jpegs(reference)
    assert len(expected) > 100

    dest = str(tmp_path / "dest")
    state = str(tmp_path / "state.sqlite")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "pids", "run",
            "--source", big_tree, "--dest", dest, "--state", state,
            "--workers", "4", "--quiet",
        ],
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_progress(dest, 40)
    finally:
        proc.kill()
    proc.wait(timeout=30)
    assert len(jpegs(dest)) < len(expected), "the kill must land mid-run"

    resumed = run_cmd(
        "run", "--source", big_tree, "--dest", dest, "--state", state,
        "--resume", "--workers", "4", "--quiet",
    )
    assert resumed.returncode == 0, resumed.stderr

    assert jpegs(dest) == expected, "resume must land exactly the same tree"
    assert part_files(dest) == [], "no half-written files may survive"

    with db.open_store(state, "sqlite") as store:
        counts = store.counts()
        assert counts.get(db.CLAIMED, 0) == 0, "no row may be left mid-copy"
        assert counts.get(db.FAILED, 0) == 0
        # The generator plants one stray with no camera folder, which lands in Unsorted/.
        assert counts.get(db.OK, 0) + counts.get(db.QUARANTINED, 0) == len(expected)


def test_every_copied_file_is_byte_correct_after_a_crash(big_tree, tmp_path):
    dest = str(tmp_path / "dest")
    state = str(tmp_path / "state.sqlite")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "pids", "run",
            "--source", big_tree, "--dest", dest, "--state", state,
            "--workers", "4", "--quiet",
        ],
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_progress(dest, 30)
    finally:
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)

    # Whatever survived the kill must be complete: never a truncated destination
    # claiming to be a finished copy.
    with db.open_store(state, "sqlite") as store:
        for record in store.iter_records(statuses=[db.OK]):
            assert os.path.exists(record.dest)
            assert os.path.getsize(record.dest) == record.size
            with open(record.src, "rb") as src, open(record.dest, "rb") as dst:
                assert src.read() == dst.read()


def test_sigterm_stops_cleanly_and_stays_resumable(big_tree, tmp_path):
    dest = str(tmp_path / "dest")
    state = str(tmp_path / "state.sqlite")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "pids", "run",
            "--source", big_tree, "--dest", dest, "--state", state,
            "--workers", "2", "--quiet",
        ],
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_progress(dest, 20)
    finally:
        proc.terminate()
    stdout, _stderr = proc.communicate(timeout=30)
    assert "STOPPED EARLY" in stdout
    assert part_files(dest) == []

    resumed = run_cmd(
        "run", "--source", big_tree, "--dest", dest, "--state", state,
        "--resume", "--workers", "2", "--quiet",
    )
    assert resumed.returncode == 0, resumed.stderr
    with db.open_store(state, "sqlite") as store:
        assert store.counts().get(db.CLAIMED, 0) == 0
