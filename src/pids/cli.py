"""The ``pids`` command line (DESIGN.md sec 9).

    pids scan    --source SRC [--source SRC2 ...] [--state pids.sqlite] [--workers auto]
    pids run     --source SRC --dest DST [--state pids.sqlite]
                 [--workers auto|N] [--on-collision keep-first|hash-dedupe|suffix]
                 [--resume] [--retry-failed] [--verify-sample 0.01]
                 [--limit N] [--dry-run] [--journal sqlite|jsonl]
    pids verify  [--state pids.sqlite] [--sample 0.01]
    pids report  [--state pids.sqlite] --out reports/
    pids calibrate --source SRC --dest DST
"""

from __future__ import annotations

import os
import sys

import click

from pids import __version__, calibrate as calibrate_mod, db, report as report_mod, verify as verify_mod
from pids.paths import is_network_path, normalise_root
from pids.pipeline import (
    COLLISION_POLICIES,
    KEEP_FIRST,
    Options,
    PidsError,
    Summary,
    check_sources,
    run_pipeline,
    survey_cameras,
)
from pids.progress import human_bytes, human_time, log, setup_logging

DEFAULT_STATE = os.path.join("state", "pids.sqlite")

state_option = click.option(
    "--state",
    default=DEFAULT_STATE,
    show_default=True,
    help="State DB path. Keep this on LOCAL disk even when the data is on a share.",
)
journal_option = click.option(
    "--journal",
    type=click.Choice(["sqlite", "jsonl"]),
    default="sqlite",
    show_default=True,
    help="State backend. 'jsonl' is the network-share fallback (sec 8.5).",
)
log_option = click.option(
    "--log",
    "logfile",
    default=None,
    help="Detail log file [default: <state dir>/pids.log].",
)
verbose_option = click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
quiet_option = click.option("-q", "--quiet", is_flag=True, help="No progress line.")


def _default_log(state: str, logfile: str | None) -> str:
    if logfile:
        return logfile
    parent = os.path.dirname(os.path.abspath(state)) or "."
    return os.path.join(parent, "pids.log")


def _warn_state_on_share(state: str) -> None:
    """SQLite must not live on SMB/NFS: network locking can corrupt it (sec 8.5)."""
    if is_network_path(state):
        raise PidsError(
            f"the state DB would live on a network share ({state}). SQLite locking over "
            "SMB/NFS is unreliable and can corrupt the database. Put --state on local "
            "disk, or use --journal jsonl if the state file must be on the share."
        )


def _print_summary(summary: Summary, plan_only: bool) -> None:
    label = "Planned" if plan_only else "Copied"
    click.echo("")
    click.echo(f"  seen          {summary.seen:,}")
    if plan_only:
        click.echo(f"  planned       {summary.planned:,}")
    else:
        click.echo(f"  {label.lower():<13} {summary.copied:,}  ({human_bytes(summary.copied_bytes)})")
    if summary.skipped:
        click.echo(f"  skipped       {summary.skipped:,}  (already ok, --resume)")
    click.echo(f"  quarantined   {summary.quarantined:,}  -> Unsorted/")
    click.echo(f"  conflicts     {summary.conflicts:,}")
    if summary.duplicates:
        click.echo(f"  duplicates    {summary.duplicates:,}  (identical bytes)")
    click.echo(f"  failed        {summary.failed:,}")
    click.echo(f"  elapsed       {human_time(summary.elapsed)}")
    if summary.stopped_early:
        click.echo(f"  STOPPED EARLY ({summary.stop_reason or 'interrupted'}) -- rerun with --resume")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="pids")
def main() -> None:
    """Restructure camera-trap images into PantheraIDS Check/<CAxxx>/<MMDDYY>/."""


@main.command()
@click.option("--source", "sources", multiple=True, required=True, help="Source root (repeatable).")
@click.option("--dest", default=None, help="Destination root; only recorded, never written.")
@state_option
@journal_option
@click.option("--workers", default="auto", show_default=True, help="'auto' or an integer.")
@click.option("--limit", type=int, default=None, help="Only the first N images.")
@click.option("--out", default=None, help="Also write the preview CSVs to this directory.")
@click.option(
    "--on-collision",
    type=click.Choice(list(COLLISION_POLICIES)),
    default=KEEP_FIRST,
    show_default=True,
)
@click.option("--prescan/--no-prescan", default=True, help="Count files first for an exact ETA.")
@log_option
@verbose_option
@quiet_option
def scan(sources, dest, state, journal, workers, limit, out, on_collision, prescan, logfile, verbose, quiet):
    """Plan only: parse headers, populate the state DB, write preview CSVs.

    Replaces the R script's DRY_RUN.  Gives an accurate count, the camera x date
    summary and the conflict/quarantine list *before* touching 3 TB (sec 5).
    """
    try:
        # Checked before logging is set up: a share path we must not touch is also a
        # place we must not try to create a log directory.
        _warn_state_on_share(state)
        setup_logging(_default_log(state, logfile), verbose)
        options = Options(
            sources=tuple(sources),
            dest=dest,
            state=state,
            journal=journal,
            workers=workers,
            on_collision=on_collision,
            limit=limit,
            plan_only=True,
            prescan=prescan,
            progress=not quiet,
        )
        summary, probe, worker_count = run_pipeline(options, cmd="scan")
        click.echo(f"\ndevice: {probe.describe()}  (override with --workers N)")
        _print_summary(summary, plan_only=True)
        with db.open_store(state, journal) as store:
            _echo_qc(store)
            if out:
                written = report_mod.write_reports(store, out)
                click.echo("")
                for name, rows in written.items():
                    click.echo(f"  {name:<24} {rows:,} rows")
                click.echo(f"\npreview CSVs in {out}")
        click.echo("\nNothing was copied. Review the numbers, then run `pids run`.")
    except PidsError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--source", "sources", multiple=True, required=True, help="Source root (repeatable).")
@click.option("--dest", required=True, help="Destination root; Check/ is created inside it.")
@state_option
@journal_option
@click.option("--workers", default="auto", show_default=True, help="'auto' or an integer.")
@click.option(
    "--on-collision",
    type=click.Choice(list(COLLISION_POLICIES)),
    default=KEEP_FIRST,
    show_default=True,
    help="What to do when two files want the same destination name (sec 7.3).",
)
@click.option("--resume", is_flag=True, help="Skip sources already recorded as ok.")
@click.option("--retry-failed", is_flag=True, help="Re-run only failed/interrupted rows.")
@click.option("--verify-sample", type=float, default=0.0, help="Verify this fraction after copying.")
@click.option("--limit", type=int, default=None, help="Only the first N images (trial runs).")
@click.option("--dry-run", is_flag=True, help="Plan without copying (same as `scan`).")
@click.option("--prescan/--no-prescan", default=True, help="Count files first for an exact ETA.")
@click.option("--allow-cross-site-merge", is_flag=True, help="Permit one camera ID across sites.")
@click.option("--allow-padding-mix", is_flag=True, help="Permit CA5 and CA05 side by side.")
@click.option("--out", default=None, help="Write the operator CSVs here when the run finishes.")
@log_option
@verbose_option
@quiet_option
def run(
    sources,
    dest,
    state,
    journal,
    workers,
    on_collision,
    resume,
    retry_failed,
    verify_sample,
    limit,
    dry_run,
    prescan,
    allow_cross_site_merge,
    allow_padding_mix,
    out,
    logfile,
    verbose,
    quiet,
):
    """Fused single pass: header parse and copy share one open handle and one seek."""
    try:
        _warn_state_on_share(state)
        setup_logging(_default_log(state, logfile), verbose)
        options = Options(
            sources=tuple(sources),
            dest=dest,
            state=state,
            journal=journal,
            workers=workers,
            on_collision=on_collision,
            resume=resume,
            retry_failed=retry_failed,
            limit=limit,
            plan_only=dry_run,
            prescan=prescan and not retry_failed,
            allow_cross_site_merge=allow_cross_site_merge,
            allow_padding_mix=allow_padding_mix,
            progress=not quiet,
        )
        if dry_run:
            click.echo("--dry-run: planning only, nothing will be copied.")
        summary, probe, worker_count = run_pipeline(options, cmd="run")
        click.echo(f"\ndevice: {probe.describe()}  (override with --workers N)")
        click.echo(f"workers: {worker_count}")
        _print_summary(summary, plan_only=dry_run)

        with db.open_store(state, journal) as store:
            if verify_sample and not dry_run:
                click.echo(f"\nverifying (sample {verify_sample:.1%})...")
                result = verify_mod.verify(
                    store, sample=verify_sample, workers=worker_count, progress=not quiet
                )
                _echo_verify(result)
            _echo_qc(store)
            if out:
                written = report_mod.write_reports(store, out)
                click.echo("")
                for name, rows in written.items():
                    click.echo(f"  {name:<24} {rows:,} rows")
        if summary.failed:
            click.echo("\nSome files failed. Re-run just those with:")
            click.echo(f"  pids run --source ... --dest {dest} --state {state} --retry-failed")
        if summary.stopped_early:
            sys.exit(2)
    except PidsError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@state_option
@journal_option
@click.option("--sample", type=float, default=0.01, show_default=True, help="Fraction to hash.")
@click.option("--seed", default=None, help="Reproduce an earlier sample.")
@click.option("--workers", type=int, default=4, show_default=True)
@quiet_option
@log_option
@verbose_option
def verify(state, journal, sample, seed, workers, quiet, logfile, verbose):
    """Size-check every copy and hash a seeded random sample (sec 5)."""
    setup_logging(_default_log(state, logfile), verbose)
    with db.open_store(state, journal) as store:
        result = verify_mod.verify(
            store, sample=sample, seed=seed, workers=workers, progress=not quiet
        )
        _echo_verify(result)
    if not result.clean:
        sys.exit(1)


@main.command()
@state_option
@journal_option
@click.option("--out", required=True, help="Directory for the CSVs.")
@log_option
@verbose_option
def report(state, journal, out, logfile, verbose):
    """Write the operator CSVs from the state DB, streamed row by row."""
    setup_logging(_default_log(state, logfile), verbose)
    with db.open_store(state, journal) as store:
        written = report_mod.write_reports(store, out)
        for name, rows in written.items():
            click.echo(f"  {name:<24} {rows:,} rows")
        _echo_qc(store)
    click.echo(f"\nCSVs in {os.path.abspath(out)}")


@main.command()
@click.option("--source", "sources", multiple=True, required=True)
@click.option("--dest", required=True)
@click.option("--per-round", type=int, default=200, show_default=True, help="Files per worker count.")
@click.option(
    "--ladder",
    default="1,2,4,8,16",
    show_default=True,
    help="Worker counts to measure.",
)
@log_option
@verbose_option
def calibrate(sources, dest, per_round, ladder, logfile, verbose):
    """Measure MB/s at several worker counts on the real hardware (sec 4)."""
    setup_logging(logfile, verbose)
    try:
        roots = check_sources(sources)
        counts = tuple(int(value) for value in ladder.split(",") if value.strip())
        click.echo(
            f"copying {per_round} files per worker count "
            f"({per_round * len(counts)} total, disjoint slices)...\n"
        )
        rounds = calibrate_mod.calibrate(roots, dest, per_round=per_round, ladder=counts)
    except (PidsError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(calibrate_mod.format_rounds(rounds))


@main.command()
@click.option("--source", "sources", multiple=True, required=True)
def cameras(sources):
    """List the camera folders resolved from the directory tree alone (no file reads)."""
    try:
        roots = check_sources(sources)
    except PidsError as exc:
        raise click.ClickException(str(exc)) from exc
    survey = survey_cameras(roots)
    click.echo(f"{survey.dirs:,} directories scanned, {len(survey.cameras)} cameras:\n")
    for camera_id in sorted(survey.cameras):
        sites = ", ".join(sorted(survey.cameras[camera_id])) or "-"
        click.echo(f"  {camera_id:<8} folder={survey.folders.get(camera_id, ''):<20} sites={sites}")
    if survey.padding:
        click.echo("\nZERO-PADDING CONFLICT (hard error on scan/run):")
        for number, ids in sorted(survey.padding.items()):
            click.echo(f"  camera {number}: {' / '.join(ids)}")
    if survey.cross_site:
        click.echo("\nCROSS-SITE CAMERAS (blocking for a multi-site run):")
        for camera_id, sites in sorted(survey.cross_site.items()):
            click.echo(f"  {camera_id}: {', '.join(sorted(sites))}")


def _echo_qc(store) -> None:
    checks = report_mod.qc_checks(store)
    click.echo("\nQC:")
    click.echo(report_mod.format_checks(checks))


def _echo_verify(result) -> None:
    click.echo("")
    click.echo(f"  checked        {result.checked:,}")
    click.echo(f"  size ok        {result.size_ok:,}")
    click.echo(f"  size mismatch  {result.size_mismatch:,}")
    click.echo(f"  missing        {result.missing:,}")
    click.echo(f"  hashed         {result.hashed:,}  (sample {result.sample:.1%}, seed {result.seed})")
    click.echo(f"  hash mismatch  {result.hash_mismatch:,}")
    click.echo(f"  elapsed        {human_time(result.elapsed)}")
    click.echo("  VERIFY PASS" if result.clean else "  VERIFY FAIL -- see failures.csv")


if __name__ == "__main__":
    main()
