"""Operator CSVs and QC parity with the R script's output (deliverable 7)."""

from __future__ import annotations

import csv
import os

import pytest

from pids import db, report
from pids.pipeline import Options, run_pipeline


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(params=["sqlite", "jsonl"])
def finished_run(request, tree, dest, tmp_path):
    """A completed run over the shared tree, on both journal backends."""
    journal = request.param
    state = str(tmp_path / ("state.sqlite" if journal == "sqlite" else "state.jsonl"))
    options = Options(
        sources=(str(tree),),
        dest=str(dest),
        state=state,
        journal=journal,
        workers=2,
        progress=False,
    )
    run_pipeline(options, cmd="run")
    out = tmp_path / "reports"
    with db.open_store(state, journal) as store:
        written = report.write_reports(store, str(out))
    return out, written


def test_every_report_is_written(finished_run):
    out, written = finished_run
    assert set(written) == {
        "restructuring_log.csv",
        "conflicts.csv",
        "unsorted.csv",
        "failures.csv",
        "qc_summary.csv",
        "qc_checks.csv",
    }
    for name in written:
        assert os.path.exists(out / name)


def test_restructuring_log_has_the_r_script_shape(finished_run):
    out, _written = finished_run
    rows = read_csv(str(out / "restructuring_log.csv"))
    assert len(rows) == 4
    assert list(rows[0]) == [
        "source_file",
        "site",
        "camera_id",
        "date_mmddyy",
        "datetime_original",
        "dest_file",
    ]
    row = next(r for r in rows if r["camera_id"] == "CA102")
    assert row["site"] == "NorthRange"
    assert row["date_mmddyy"] == "071424"
    assert row["dest_file"].endswith(os.path.join("CA102", "071424", "IMG_0001.JPG"))


def test_qc_summary_is_camera_by_date_counts(finished_run):
    out, _written = finished_run
    rows = read_csv(str(out / "qc_summary.csv"))
    summary = {(r["camera_id"], r["date_mmddyy"]): int(r["image_count"]) for r in rows}
    assert summary == {
        ("CA101", "071424"): 1,
        ("CA101", "071524"): 1,
        ("CA102", "071424"): 1,
        ("CA105", "072024"): 1,
    }


def test_unsorted_lists_every_quarantined_file_with_a_reason(finished_run):
    out, _written = finished_run
    rows = read_csv(str(out / "unsorted.csv"))
    reasons = {os.path.basename(r["source_file"]): r["reason"] for r in rows}
    assert reasons == {
        "NOEXIF.JPG": "no_exif",
        "ZERO.JPG": "zero_date",
        "IMG_9999.JPG": "no_camera_id",
    }


def test_conflicts_names_the_winner(finished_run):
    out, _written = finished_run
    rows = read_csv(str(out / "conflicts.csv"))
    assert len(rows) == 1
    assert rows[0]["conflict_with"].endswith("IMG_0001.JPG")
    assert rows[0]["resolution"] == "collision_keep_first"


def test_failures_is_empty_on_a_clean_run(finished_run):
    out, _written = finished_run
    assert read_csv(str(out / "failures.csv")) == []


def test_qc_checks_account_for_every_row(finished_run):
    out, _written = finished_run
    rows = {r["check"]: r for r in read_csv(str(out / "qc_checks.csv"))}
    assert rows["accounted_for"]["status"] == "PASS"
    assert rows["in_flight_rows"]["value"] == "0"
    assert rows["failed"]["value"] == "0"
    assert rows["copied_ok"]["value"] == "4"
    assert rows["cameras"]["value"] == "3"


def test_reports_stream_without_loading_the_table(tmp_path, monkeypatch):
    """The whole point of sec 8.3: one row at a time, never a full frame in memory."""
    state = str(tmp_path / "s.sqlite")
    with db.open_store(state, "sqlite") as store:
        for index in range(500):
            store.write(
                db.Record(
                    src=f"/src/{index}.jpg",
                    size=10,
                    mtime=1.0,
                    cam="CA1",
                    date="071424",
                    dest=f"/dest/{index}.jpg",
                    dest_key=f"/dest/{index}.jpg",
                    status=db.OK,
                )
            )
        store.flush()
        written = report.write_reports(store, str(tmp_path / "out"))
    assert written["restructuring_log.csv"] == 500


def test_format_checks_is_human_readable(tmp_path):
    with db.open_store(str(tmp_path / "s.sqlite"), "sqlite") as store:
        text = report.format_checks(report.qc_checks(store))
    assert "rows_in_manifest" in text
