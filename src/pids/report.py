"""Operator CSVs, streamed straight from the state DB.

DESIGN.md sec 8.3.  Each report is a single SQL query streamed to a CSV writer one row
at a time -- nothing is ever loaded into memory.  ``qc_summary.csv``, which the R script
produced with a ``group_by``/``summarise`` over the full in-memory frame at peak memory,
is a ``GROUP BY cam, date`` the database answers from an index.

This is also the QC step: unlike the R version it never re-walks the destination tree,
which it did four separate times.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

from pids import db
from pids.progress import log


@dataclass(frozen=True)
class ReportSpec:
    """One CSV: its filename, header row, SQL, and a store-agnostic fallback."""

    name: str
    header: tuple[str, ...]
    sql: str
    statuses: tuple[str, ...]
    columns: tuple[str, ...]


LOG_COLUMNS = ("src", "site", "cam", "date", "dt", "dest")


def placed_status(store) -> str:
    """Which status represents "has a destination" for reporting purposes.

    After ``run`` that is ``ok``.  After ``scan`` nothing has been copied yet, so the
    same reports are generated from ``planned`` rows -- which is what makes them the
    preview that sec 5 promises *before* touching 3 TB.
    """
    return db.OK if store.counts().get(db.OK, 0) else db.PLANNED


def specs(placed: str = db.OK) -> tuple[ReportSpec, ...]:
    """The report set, resolved against the status that carries destinations."""
    return (
    ReportSpec(
        name="restructuring_log.csv",
        header=(
            "source_file",
            "site",
            "camera_id",
            "date_mmddyy",
            "datetime_original",
            "dest_file",
        ),
        sql=(
            "SELECT src, site, cam, date, dt, dest FROM files "
            f"WHERE status='{placed}' ORDER BY cam, date, src"
        ),
        statuses=(placed,),
        columns=LOG_COLUMNS,
    ),
    ReportSpec(
        name="conflicts.csv",
        header=(
            "source_file",
            "camera_id",
            "date_mmddyy",
            "dest_file",
            "conflict_with",
            "resolution",
        ),
        sql=(
            "SELECT src, cam, date, dest, conflict_with, reason FROM files "
            "WHERE conflict_with IS NOT NULL ORDER BY cam, date, src"
        ),
        statuses=(),  # selected by conflict_with, not by status
        columns=("src", "cam", "date", "dest", "conflict_with", "reason"),
    ),
    ReportSpec(
        name="unsorted.csv",
        header=("source_file", "reason", "datetime_original", "dest_file", "camera_id"),
        sql=(
            "SELECT src, reason, dt, dest, cam FROM files "
            "WHERE status='quarantined' ORDER BY reason, src"
        ),
        statuses=(db.QUARANTINED,),
        columns=("src", "reason", "dt", "dest", "cam"),
    ),
    ReportSpec(
        name="failures.csv",
        header=("source_file", "reason", "camera_id", "date_mmddyy", "dest_file"),
        sql=(
            "SELECT src, reason, cam, date, dest FROM files "
            "WHERE status='failed' ORDER BY src"
        ),
        statuses=(db.FAILED,),
        columns=("src", "reason", "cam", "date", "dest"),
    ),
    )


def qc_summary_sql(placed: str = db.OK) -> str:
    """``qc_summary`` is a GROUP BY the database answers from files_cam_date."""
    return (
        f"SELECT cam, date, COUNT(*) FROM files WHERE status='{placed}' "
        "GROUP BY cam, date ORDER BY cam, date"
    )


def _stream_sql(store, sql: str) -> Iterator[tuple]:
    if hasattr(store, "stream"):
        return store.stream(sql)
    raise NotImplementedError


def _stream_records(store, spec: ReportSpec) -> Iterator[tuple]:
    """Fallback for the JSONL journal, which has no SQL engine."""
    for record in store.iter_records(statuses=spec.statuses or None):
        if spec.name == "conflicts.csv" and not record.conflict_with:
            continue
        yield tuple(getattr(record, column) for column in spec.columns)


def _write_csv(path: str, header: Sequence[str], rows: Iterable[Sequence]) -> int:
    count = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])
            count += 1
    return count


def write_reports(store, out_dir: str) -> dict[str, int]:
    """Write every operator CSV.  Returns ``{filename: row count}``."""
    os.makedirs(out_dir, exist_ok=True)
    written: dict[str, int] = {}
    use_sql = store.kind == "sqlite"
    placed = placed_status(store)

    for spec in specs(placed):
        rows = _stream_sql(store, spec.sql) if use_sql else _stream_records(store, spec)
        path = os.path.join(out_dir, spec.name)
        written[spec.name] = _write_csv(path, spec.header, rows)

    qc_rows = (
        _stream_sql(store, qc_summary_sql(placed))
        if use_sql
        else _qc_summary_records(store, placed)
    )
    written["qc_summary.csv"] = _write_csv(
        os.path.join(out_dir, "qc_summary.csv"),
        ("camera_id", "date_mmddyy", "image_count"),
        qc_rows,
    )

    checks = qc_checks(store)
    written["qc_checks.csv"] = _write_csv(
        os.path.join(out_dir, "qc_checks.csv"),
        ("check", "value", "status", "note"),
        [(c.name, c.value, "PASS" if c.passed else "REVIEW", c.note) for c in checks],
    )
    log.info("wrote %d report files to %s", len(written), out_dir)
    return written


def _qc_summary_records(store, placed: str = db.OK) -> Iterator[tuple]:
    """Aggregate in Python for the JSONL fallback; bounded by camera x date, not files."""
    counts: dict[tuple[str, str], int] = {}
    for record in store.iter_records(statuses=[placed]):
        key = (record.cam or "", record.date or "")
        counts[key] = counts.get(key, 0) + 1
    for (cam, date), count in sorted(counts.items()):
        yield cam, date, count


@dataclass(frozen=True)
class Check:
    name: str
    value: str
    passed: bool
    note: str = ""


def qc_checks(store) -> list[Check]:
    """The R script's QC-1..QC-4, answered from the manifest instead of four re-walks."""
    counts = store.counts()
    total = sum(counts.values())
    copied = counts.get(db.OK, 0)
    conflicts = counts.get(db.CONFLICT, 0)
    quarantined = counts.get(db.QUARANTINED, 0)
    failed = counts.get(db.FAILED, 0)
    in_flight = counts.get(db.CLAIMED, 0)
    planned = counts.get(db.PLANNED, 0)

    placed = placed_status(store)
    cameras = len(store.cameras())
    if store.kind == "sqlite":
        date_folders = store.query(
            "SELECT COUNT(*) FROM (SELECT DISTINCT cam, date FROM files "
            f"WHERE status='{placed}')"
        )[0][0]
    else:
        date_folders = len({(r.cam, r.date) for r in store.iter_records(statuses=[placed])})
    cross = store.cross_site_cameras()

    accounted = copied + conflicts + quarantined + failed + in_flight + planned
    checks = [
        Check("rows_in_manifest", str(total), True),
        Check("copied_ok", str(copied), True),
    ]
    if planned:
        checks.append(
            Check("planned_not_yet_copied", str(planned), True, "from `pids scan`")
        )
    checks += [
        Check(
            "accounted_for",
            f"{accounted}/{total}",
            accounted == total,
            "every source row has a terminal status",
        ),
        Check(
            "in_flight_rows",
            str(in_flight),
            in_flight == 0,
            "non-zero means a run was interrupted; use --retry-failed",
        ),
        Check("failed", str(failed), failed == 0, "see failures.csv"),
        Check(
            "quarantined",
            str(quarantined),
            True,
            "bad or missing EXIF; see unsorted.csv (expected to be non-zero)",
        ),
        Check(
            "conflicts",
            str(conflicts),
            True,
            "filename collisions; see conflicts.csv (common with DCIM subfolders)",
        ),
        Check("cameras", str(cameras), cameras > 0),
        Check("camera_date_folders", str(date_folders), date_folders > 0),
        Check(
            "cross_site_cameras",
            str(len(cross)),
            len(cross) == 0,
            "; ".join(f"{cam}: {sites}" for cam, _, sites in cross),
        ),
    ]
    return checks


def format_checks(checks: Sequence[Check]) -> str:
    width = max(len(check.name) for check in checks)
    lines = []
    for check in checks:
        mark = "ok  " if check.passed else "note"
        note = f"  -- {check.note}" if check.note else ""
        lines.append(f"  [{mark}] {check.name:<{width}}  {check.value}{note}")
    return "\n".join(lines)
