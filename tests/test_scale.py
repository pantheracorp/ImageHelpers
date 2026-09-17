"""Scale behaviour: bounded memory under load (deliverable 5, DESIGN.md sec 4).

    Memory target: < 300 MB RSS regardless of file count.

Marked ``slow``; run with ``pytest -m slow``.  The synthetic tree keeps the files tiny,
so this exercises the *per-file* costs -- the walk, the queue, the state DB and the
indexes -- which are what actually scale with 600k files.
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from pids import db  # noqa: E402

pytestmark = pytest.mark.slow

FILES = 20_000
RSS_BUDGET_MB = 300


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    return env


def peak_child_rss_mb() -> float:
    """``ru_maxrss`` is bytes on macOS and kilobytes on Linux."""
    raw = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


@pytest.fixture(scope="module")
def big_tree(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("scale") / "src")
    subprocess.run(
        [
            sys.executable, os.path.join(ROOT, "tools", "make_tree.py"),
            "--out", out,
            "--cameras", "20",
            "--per-camera", str(FILES // 20),
            "--size", "1024",
            "--bad-exif", "0.01",
            "--collisions", "0.1",
            "--layout", "mixed",
            "--quiet",
        ],
        check=True,
        env=child_env(),
        capture_output=True,
    )
    return out


def test_memory_stays_flat_in_file_count(big_tree, tmp_path):
    dest = str(tmp_path / "dest")
    state = str(tmp_path / "state.sqlite")
    before = peak_child_rss_mb()
    result = subprocess.run(
        [
            sys.executable, "-m", "pids", "run",
            "--source", big_tree, "--dest", dest, "--state", state,
            "--workers", "4", "--quiet",
        ],
        env=child_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    after = peak_child_rss_mb()
    assert after < RSS_BUDGET_MB, (
        f"peak child RSS {after:.0f} MB exceeds the {RSS_BUDGET_MB} MB budget "
        f"(baseline before this run: {before:.0f} MB)"
    )

    with db.open_store(state, "sqlite") as store:
        counts = store.counts()
    assert sum(counts.values()) == FILES + 1  # plus the planted stray
    assert counts.get(db.FAILED, 0) == 0


def test_reports_over_a_large_manifest_stream(big_tree, tmp_path):
    """Reporting 20k rows must not need the table in memory."""
    dest = str(tmp_path / "dest")
    state = str(tmp_path / "state.sqlite")
    subprocess.run(
        [
            sys.executable, "-m", "pids", "run",
            "--source", big_tree, "--dest", dest, "--state", state,
            "--workers", "4", "--quiet",
        ],
        check=True,
        env=child_env(),
        capture_output=True,
    )
    before = peak_child_rss_mb()
    out = str(tmp_path / "reports")
    result = subprocess.run(
        [sys.executable, "-m", "pids", "report", "--state", state, "--out", out],
        env=child_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert peak_child_rss_mb() < max(before, RSS_BUDGET_MB)
    with open(os.path.join(out, "restructuring_log.csv"), encoding="utf-8-sig") as fh:
        assert sum(1 for _ in fh) > 1000
