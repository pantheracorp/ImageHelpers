"""End-to-end pipeline behaviour: scan, run, resume, collisions, quarantine.

Build-plan deliverables 3-6 (DESIGN.md sec 12).
"""

from __future__ import annotations

import os
import shutil

import pytest
from conftest import write_jpeg

from pids import db
from pids.pipeline import KEEP_FIRST, Options, PidsError, Pipeline, run_pipeline


def do_run(src, dest, state, **kwargs):
    options = Options(
        sources=(str(src),),
        dest=str(dest) if dest else None,
        state=str(state),
        workers=kwargs.pop("workers", 2),
        progress=False,
        prescan=kwargs.pop("prescan", True),
        **kwargs,
    )
    return run_pipeline(options, cmd=kwargs.get("cmd", "run"))


def tree_files(root) -> set[str]:
    out = set()
    for directory, _dirs, files in os.walk(root):
        for name in files:
            out.add(os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/"))
    return out


def statuses(state) -> dict[str, int]:
    with db.open_store(str(state), "sqlite") as store:
        return store.counts()


# ---------------------------------------------------------------------------- #
# scan
# ---------------------------------------------------------------------------- #


def test_scan_plans_without_copying_anything(tree, dest, state):
    summary, _probe, _workers = do_run(tree, dest, state, plan_only=True)
    assert summary.seen == 8  # notes.txt is not an image
    assert summary.planned == 4
    assert summary.quarantined == 3
    assert summary.conflicts == 1
    assert tree_files(dest) == set(), "scan must not write to the destination"
    assert statuses(state)[db.PLANNED] == 4


def test_scan_then_run_reuses_the_planned_rows(tree, dest, state):
    do_run(tree, dest, state, plan_only=True)
    summary, _p, _w = do_run(tree, dest, state)
    assert summary.copied == 4
    assert "Check/CA101/071424/IMG_0001.JPG" in tree_files(dest)


# ---------------------------------------------------------------------------- #
# run
# ---------------------------------------------------------------------------- #


def test_run_builds_the_pantheraids_structure(tree, dest, state):
    summary, _p, _w = do_run(tree, dest, state)
    files = tree_files(dest)
    assert "Check/CA101/071424/IMG_0001.JPG" in files
    assert "Check/CA101/071524/IMG_0002.JPG" in files
    assert "Check/CA102/071424/IMG_0001.JPG" in files
    assert "Check/CA105/072024/IMG_0001.JPG" in files
    assert summary.copied == 4
    assert summary.failed == 0


def test_run_leaves_the_source_untouched(tree, dest, state):
    before = tree_files(tree)
    do_run(tree, dest, state)
    assert tree_files(tree) == before


def test_non_images_are_ignored(tree, dest, state):
    do_run(tree, dest, state)
    assert not any(name.endswith("notes.txt") for name in tree_files(dest))


def test_mtime_is_preserved(tree, dest, state):
    """The R version silently dropped timestamps."""
    source = tree / "CAM101" / "IMG_0001.JPG"
    os.utime(source, (1_600_000_000, 1_600_000_000))
    do_run(tree, dest, state)
    copied = dest / "Check" / "CA101" / "071424" / "IMG_0001.JPG"
    assert int(os.stat(copied).st_mtime) == 1_600_000_000


def test_no_part_files_are_left_behind(tree, dest, state):
    do_run(tree, dest, state)
    assert not [name for name in tree_files(dest) if name.endswith(".part")]


def test_bytes_are_identical(tree, dest, state):
    do_run(tree, dest, state)
    source = (tree / "CAM101" / "IMG_0001.JPG").read_bytes()
    copied = (dest / "Check" / "CA101" / "071424" / "IMG_0001.JPG").read_bytes()
    assert source == copied


def test_limit_stops_after_n_files(tree, dest, state):
    summary, _p, _w = do_run(tree, dest, state, limit=3)
    assert summary.seen == 3
    assert summary.copied + summary.quarantined + summary.conflicts == 3


def test_dest_nested_in_source_is_not_re_ingested(tmp_path):
    """Without the exclusion the walk would eat its own output forever."""
    src = tmp_path / "src"
    write_jpeg(str(src / "CAM101" / "IMG_0001.JPG"))
    inner_dest = src / "restructured"
    inner_dest.mkdir()
    summary, _p, _w = do_run(src, inner_dest, tmp_path / "state.sqlite")
    assert summary.seen == 1
    assert summary.copied == 1


# ---------------------------------------------------------------------------- #
# quarantine
# ---------------------------------------------------------------------------- #


def test_quarantine_preserves_the_source_structure_and_reasons(tree, dest, state):
    do_run(tree, dest, state)
    files = tree_files(dest)
    assert "Unsorted/CAM101/NOEXIF.JPG" in files
    assert "Unsorted/CAM101/ZERO.JPG" in files
    assert "Unsorted/Loose Images/IMG_9999.JPG" in files
    with db.open_store(str(state), "sqlite") as store:
        reasons = {
            os.path.basename(r.src): r.reason
            for r in store.iter_records(statuses=[db.QUARANTINED])
        }
    assert reasons["NOEXIF.JPG"] == "no_exif"
    assert reasons["ZERO.JPG"] == "zero_date"
    assert reasons["IMG_9999.JPG"] == "no_camera_id"


def test_nothing_is_filed_under_a_guessed_date(tree, dest, state):
    do_run(tree, dest, state)
    assert not any(
        name.endswith(("NOEXIF.JPG", "ZERO.JPG")) and name.startswith("Check/")
        for name in tree_files(dest)
    )


def test_quarantined_files_never_collide_away(tmp_path):
    """Unsorted always suffixes: a quarantined file must not be silently dropped."""
    src = tmp_path / "src"
    write_jpeg(str(src / "CAM101" / "A" / "BAD.JPG"), None, include_exif=False)
    write_jpeg(str(src / "CAM101" / "A" / "BAD2.JPG"), None, include_exif=False)
    dest = tmp_path / "dest"
    do_run(src, dest, tmp_path / "state.sqlite", on_collision=KEEP_FIRST)
    assert tree_files(dest) == {"Unsorted/CAM101/A/BAD.JPG", "Unsorted/CAM101/A/BAD2.JPG"}


# ---------------------------------------------------------------------------- #
# collisions (sec 7.3)
# ---------------------------------------------------------------------------- #


def test_keep_first_copies_one_and_logs_the_other(tree, dest, state):
    summary, _p, _w = do_run(tree, dest, state, on_collision="keep-first")
    assert summary.conflicts == 1
    ca102 = [name for name in tree_files(dest) if name.startswith("Check/CA102/")]
    assert ca102 == ["Check/CA102/071424/IMG_0001.JPG"]
    with db.open_store(str(state), "sqlite") as store:
        losers = list(store.iter_records(statuses=[db.CONFLICT]))
    assert len(losers) == 1
    assert losers[0].conflict_with is not None
    assert losers[0].dest is None


def test_suffix_keeps_both(tree, dest, state):
    summary, _p, _w = do_run(tree, dest, state, on_collision="suffix")
    ca102 = sorted(name for name in tree_files(dest) if name.startswith("Check/CA102/"))
    assert ca102 == [
        "Check/CA102/071424/IMG_0001.JPG",
        "Check/CA102/071424/IMG_0001_001.JPG",
    ]
    assert summary.copied == 5


def test_hash_dedupe_drops_true_duplicates_only(tmp_path):
    src = tmp_path / "src"
    payload = str(src / "S" / "CAM101" / "100EK113" / "IMG_0001.JPG")
    write_jpeg(payload, "2024:07:14 06:31:02")
    twin = src / "S" / "CAM101" / "101EK113" / "IMG_0001.JPG"
    twin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(payload, twin)  # byte-identical
    write_jpeg(
        str(src / "S" / "CAM101" / "102EK113" / "IMG_0001.JPG"),
        "2024:07:14 06:31:02",
        size=4096,  # same name, same date, different bytes
    )
    dest = tmp_path / "dest"
    summary, _p, _w = do_run(src, dest, tmp_path / "state.sqlite", on_collision="hash-dedupe")
    files = sorted(tree_files(dest))
    assert files == [
        "Check/CA101/071424/IMG_0001.JPG",
        "Check/CA101/071424/IMG_0001_001.JPG",
    ]
    assert summary.duplicates == 1
    assert summary.copied == 2


def test_case_only_filename_differences_are_treated_as_collisions(tmp_path):
    from pids.paths import CASE_INSENSITIVE_FS

    if not CASE_INSENSITIVE_FS:
        pytest.skip("case-sensitive filesystem")
    src = tmp_path / "src"
    write_jpeg(str(src / "S" / "CAM101" / "100EK113" / "IMG_1.JPG"), "2024:07:14 06:31:02")
    write_jpeg(str(src / "S" / "CAM101" / "101EK113" / "img_1.jpg"), "2024:07:14 07:31:02")
    dest = tmp_path / "dest"
    summary, _p, _w = do_run(src, dest, tmp_path / "state.sqlite")
    assert summary.conflicts == 1


# ---------------------------------------------------------------------------- #
# resume and retry (sec 8.4)
# ---------------------------------------------------------------------------- #


def test_resume_is_idempotent_and_reports_no_failures(tree, dest, state):
    """The R script's re-run printed ~600k 'failures' because copy(overwrite=FALSE)
    returns FALSE for files that already exist.  A resume must skip by lookup."""
    first, _p, _w = do_run(tree, dest, state)
    before = tree_files(dest)
    second, _p, _w = do_run(tree, dest, state, resume=True)
    # Every decided file is skipped: copied, quarantined and logged collision losers.
    assert second.skipped == first.copied + first.quarantined + first.conflicts
    assert second.skipped == second.seen
    assert second.copied == 0
    assert second.failed == 0
    assert tree_files(dest) == before


def test_rerun_without_resume_recopies_without_duplicating(tree, dest, state):
    do_run(tree, dest, state)
    before = tree_files(dest)
    summary, _p, _w = do_run(tree, dest, state)
    assert summary.failed == 0
    assert tree_files(dest) == before


def test_retry_failed_reruns_only_the_failures(tree, dest, state):
    do_run(tree, dest, state)
    target = str(tree / "CAM101" / "IMG_0001.JPG")
    copied = dest / "Check" / "CA101" / "071424" / "IMG_0001.JPG"
    os.remove(copied)
    with db.open_store(str(state), "sqlite") as store:
        record = store.get(target)
        record.status = db.FAILED
        record.reason = "injected"
        store.write(record)
        store.flush()
    summary, _p, _w = do_run(tree, dest, state, retry_failed=True, prescan=False)
    assert summary.seen == 1
    assert summary.copied == 1
    assert copied.exists()


def test_retry_failed_records_a_vanished_source(tree, dest, state):
    do_run(tree, dest, state)
    target = str(tree / "CAM101" / "IMG_0001.JPG")
    with db.open_store(str(state), "sqlite") as store:
        record = store.get(target)
        record.status = db.FAILED
        store.write(record)
        store.flush()
    os.remove(target)
    summary, _p, _w = do_run(tree, dest, state, retry_failed=True, prescan=False)
    assert summary.failed == 1
    with db.open_store(str(state), "sqlite") as store:
        assert "source_missing" in (store.get(target).reason or "")


# ---------------------------------------------------------------------------- #
# pre-flight (sec 11)
# ---------------------------------------------------------------------------- #


def test_cross_site_camera_blocks_a_run(tmp_path):
    src = tmp_path / "src"
    write_jpeg(str(src / "North" / "CAM01" / "IMG_1.JPG"))
    write_jpeg(str(src / "South" / "CAM01" / "IMG_2.JPG"))
    with pytest.raises(PidsError, match="more than one site"):
        do_run(src, tmp_path / "dest", tmp_path / "s.sqlite")


def test_cross_site_camera_is_allowed_with_the_flag(tmp_path):
    src = tmp_path / "src"
    write_jpeg(str(src / "North" / "CAM01" / "IMG_1.JPG"))
    write_jpeg(str(src / "South" / "CAM01" / "IMG_2.JPG"))
    summary, _p, _w = do_run(
        src, tmp_path / "dest", tmp_path / "s.sqlite", allow_cross_site_merge=True
    )
    assert summary.copied == 2


def test_cross_site_camera_only_warns_during_scan(tmp_path):
    src = tmp_path / "src"
    write_jpeg(str(src / "North" / "CAM01" / "IMG_1.JPG"))
    write_jpeg(str(src / "South" / "CAM01" / "IMG_2.JPG"))
    summary, _p, _w = do_run(src, None, tmp_path / "s.sqlite", plan_only=True)
    assert summary.planned == 2


def test_zero_padding_mix_is_a_hard_error(tmp_path):
    src = tmp_path / "src"
    write_jpeg(str(src / "CAM5" / "IMG_1.JPG"))
    write_jpeg(str(src / "CAM05" / "IMG_2.JPG"))
    with pytest.raises(PidsError, match="zero-padding"):
        do_run(src, tmp_path / "dest", tmp_path / "s.sqlite")


def test_overlapping_source_roots_are_rejected(tmp_path):
    src = tmp_path / "src"
    write_jpeg(str(src / "CAM101" / "IMG_1.JPG"))
    options = Options(
        sources=(str(src), str(src / "CAM101")),
        dest=str(tmp_path / "dest"),
        state=str(tmp_path / "s.sqlite"),
        progress=False,
    )
    with pytest.raises(PidsError, match="overlap"):
        run_pipeline(options, cmd="run")


def test_dest_is_required_for_a_copy(tmp_path):
    src = tmp_path / "src"
    write_jpeg(str(src / "CAM101" / "IMG_1.JPG"))
    options = Options(sources=(str(src),), state=str(tmp_path / "s.sqlite"), progress=False)
    with pytest.raises(PidsError, match="--dest is required"):
        run_pipeline(options, cmd="run")


def test_missing_source_is_rejected(tmp_path):
    options = Options(sources=(str(tmp_path / "nope"),), dest=str(tmp_path), progress=False)
    with pytest.raises(PidsError, match="not a directory"):
        run_pipeline(options, cmd="run")


def test_insufficient_free_space_refuses_to_start(tree, dest, state, monkeypatch):
    monkeypatch.setattr("pids.pipeline.free_bytes", lambda _path: 10)
    with pytest.raises(PidsError, match="free"):
        do_run(tree, dest, state)


# ---------------------------------------------------------------------------- #
# failure handling
# ---------------------------------------------------------------------------- #


def test_an_unreadable_file_is_recorded_not_fatal(tree, dest, state, monkeypatch):
    real_open = open
    target = os.path.join(str(tree), "CAM101", "IMG_0001.JPG")

    def flaky(path, *args, **kwargs):
        if str(path) == target:
            raise OSError(5, "Input/output error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky)
    summary, _p, _w = do_run(tree, dest, state, workers=1)
    assert summary.failed == 1
    assert summary.copied == 3
    with db.open_store(str(state), "sqlite") as store:
        assert "Input/output error" in (store.get(target).reason or "")


def test_a_size_mismatch_fails_hard(tree, dest, state, monkeypatch):
    import pids.pipeline as pipeline_mod

    real_copy = pipeline_mod.copier.copy_file

    def short_copy(fh, src, dest_path, size):
        written = real_copy(fh, src, dest_path, size)
        with open(dest_path, "wb") as out:
            out.write(b"truncated")
        return written

    monkeypatch.setattr(pipeline_mod.copier, "copy_file", short_copy)
    summary, _p, _w = do_run(tree, dest, state, workers=1)
    # Every copy is checked, quarantined ones included: 4 to Check/ + 3 to Unsorted/.
    assert summary.failed == 7
    with db.open_store(str(state), "sqlite") as store:
        failures = list(store.iter_records(statuses=[db.FAILED]))
    assert all("size_mismatch" in (r.reason or "") for r in failures)
    assert all(r.hash for r in failures), "both sides are hashed on a mismatch"


def test_disk_full_stops_cleanly_and_stays_resumable(tree, dest, state, monkeypatch):
    import errno

    import pids.pipeline as pipeline_mod

    calls = {"n": 0}
    real_copy = pipeline_mod.copier.copy_file

    def fail_after_one(fh, src, dest_path, size):
        calls["n"] += 1
        if calls["n"] > 1:
            raise pipeline_mod.copier.DiskFull(errno.ENOSPC, "No space left on device")
        return real_copy(fh, src, dest_path, size)

    monkeypatch.setattr(pipeline_mod.copier, "copy_file", fail_after_one)
    summary, _p, _w = do_run(tree, dest, state, workers=1)
    assert summary.stopped_early
    assert summary.stop_reason == "destination full"
    # The manifest is intact, so the run can be finished later.
    with db.open_store(str(state), "sqlite") as store:
        assert store.counts().get(db.OK, 0) >= 1


# ---------------------------------------------------------------------------- #
# the JSONL fallback (sec 8.5)
# ---------------------------------------------------------------------------- #


def test_jsonl_journal_runs_end_to_end(tree, dest, tmp_path):
    state = tmp_path / "state.jsonl"
    summary, _p, _w = do_run(tree, dest, state, journal="jsonl")
    assert summary.copied == 4
    assert "Check/CA101/071424/IMG_0001.JPG" in tree_files(dest)
    with db.open_store(str(state), "jsonl") as store:
        assert store.counts()[db.OK] == 4


def test_jsonl_journal_resumes(tree, dest, tmp_path):
    state = tmp_path / "state.jsonl"
    do_run(tree, dest, state, journal="jsonl")
    summary, _p, _w = do_run(tree, dest, state, journal="jsonl", resume=True)
    assert summary.copied == 0 and summary.skipped == 8


# ---------------------------------------------------------------------------- #
# concurrency and memory (sec 4)
# ---------------------------------------------------------------------------- #


def test_queue_is_bounded_by_worker_count(tree, dest, state):
    options = Options(sources=(str(tree),), dest=str(dest), state=str(state), workers=4)
    with db.open_store(str(state), "sqlite") as store:
        pipeline = Pipeline(options, store, workers=4)
        assert pipeline.queue.maxsize == 16, "back-pressure, never a 600k list"


@pytest.mark.parametrize("workers", [1, 2, 8])
def test_results_are_identical_at_every_worker_count(tree, tmp_path, workers):
    dest = tmp_path / f"dest{workers}"
    state = tmp_path / f"state{workers}.sqlite"
    summary, _p, _w = do_run(tree, dest, state, workers=workers)
    assert (summary.copied, summary.quarantined, summary.conflicts, summary.failed) == (
        4,
        3,
        1,
        0,
    )
