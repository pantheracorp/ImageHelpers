"""The scan/run pipeline: one scandir generator, a bounded queue, N worker threads.

DESIGN.md sec 4 and sec 5::

    scandir generator --> bounded queue (maxsize = workers x 4) --> worker threads
       (main thread)        back-pressure, never a 600k list        (N threads)
                                                                        |
                                                      per-thread: open -> header -> copy
                                                                        |
                                                          single SQLite writer thread

Memory target: < 300 MB RSS regardless of file count.  Nothing here materialises the
file list, and every index lives in the state DB rather than a Python set, so memory is
flat in the number of files -- bounded by ``workers x copy buffer``.
"""

from __future__ import annotations

import os
import queue
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Iterator, Sequence

from pids import copier, db, exif
from pids.db import Record
from pids.devices import Probe, resolve_workers, same_physical_device
from pids.paths import (
    dest_key,
    free_bytes,
    is_within,
    long_path,
    normalise_root,
    relative_parts,
    safe_component,
)
from pids.progress import Progress, human_bytes, log
from pids.resolve import NO_CAMERA_ID, padding_conflicts, resolve_camera
from pids.walker import FileItem, WalkStats, iter_dirs, iter_images

CHECK_DIR = "Check"
UNSORTED_DIR = "Unsorted"

#: Stands in for the destination root when ``scan`` is run without ``--dest``.  The
#: planned paths below it are still exactly right relative to the output root, so the
#: camera x date summary and the collision count are accurate; only the prefix is
#: unknown, and it says so rather than quietly using the current directory.
DEST_PLACEHOLDER = "<DEST>"

KEEP_FIRST = "keep-first"
HASH_DEDUPE = "hash-dedupe"
SUFFIX = "suffix"
COLLISION_POLICIES = (KEEP_FIRST, HASH_DEDUPE, SUFFIX)

MAX_SUFFIX = 999


class PidsError(Exception):
    """An operator-facing error: bad arguments or a failed pre-flight check."""


@dataclass
class Options:
    """Everything a scan or run needs, resolved from the CLI."""

    sources: tuple[str, ...]
    dest: str | None = None
    state: str = "state/pids.sqlite"
    journal: str = "sqlite"
    workers: str | int | None = "auto"
    on_collision: str = KEEP_FIRST
    resume: bool = False
    retry_failed: bool = False
    limit: int | None = None
    plan_only: bool = False  # `scan`, and `run --dry-run`
    prescan: bool = True
    reuse_plan: bool = True
    allow_cross_site_merge: bool = False
    allow_padding_mix: bool = False
    follow_symlinks: bool = False
    progress: bool = True


@dataclass
class Summary:
    """Run counters.  Mutated under ``lock`` by the worker threads."""

    seen: int = 0
    copied: int = 0
    copied_bytes: int = 0
    planned: int = 0
    skipped: int = 0
    quarantined: int = 0
    conflicts: int = 0
    duplicates: int = 0
    failed: int = 0
    total_files: int | None = None
    total_bytes: int | None = None
    elapsed: float = 0.0
    stopped_early: bool = False
    stop_reason: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        return {
            key: value
            for key, value in self.__dict__.items()
            if key != "lock" and value is not None
        }


@dataclass
class CameraPreflight:
    """Result of the directory-only camera survey."""

    cameras: dict[str, set[str]] = field(default_factory=dict)  # camera_id -> sites
    folders: dict[str, str] = field(default_factory=dict)  # camera_id -> example folder
    dirs: int = 0

    @property
    def cross_site(self) -> dict[str, set[str]]:
        return {cam: sites for cam, sites in self.cameras.items() if len(sites) > 1}

    @property
    def padding(self) -> dict[int, list[str]]:
        return padding_conflicts(self.cameras)


# ---------------------------------------------------------------------------- #
# Pre-flight
# ---------------------------------------------------------------------------- #


def survey_cameras(sources: Sequence[str], exclude: Sequence[str] = ()) -> CameraPreflight:
    """Resolve camera folders from the directory tree alone -- no file stats.

    A 3 TB tree has a few thousand directories but 600k files, so this answers the
    zero-padding and cross-site questions in seconds, before any copying (sec 11).
    """
    result = CameraPreflight()
    for directory, root in iter_dirs(sources, exclude=exclude):
        result.dirs += 1
        parts = relative_parts(PurePath(directory), PurePath(root))
        camera = resolve_camera(parts)
        if camera is None:
            continue
        result.cameras.setdefault(camera.camera_id, set())
        if camera.site:
            result.cameras[camera.camera_id].add(camera.site)
        result.folders.setdefault(camera.camera_id, camera.folder)
    return result


def check_sources(sources: Sequence[str]) -> tuple[str, ...]:
    """Normalise source roots and reject the ways they can be wrong."""
    if not sources:
        raise PidsError("at least one --source is required")
    roots = [str(normalise_root(s)) for s in sources]
    for root in roots:
        if not os.path.isdir(root):
            raise PidsError(f"source is not a directory: {root}")
    for i, a in enumerate(roots):
        for b in roots[i + 1 :]:
            if is_within(a, b) or is_within(b, a):
                raise PidsError(
                    f"source roots overlap, which would process files twice:\n  {a}\n  {b}"
                )
    return tuple(roots)


def preflight(options: Options) -> CameraPreflight:
    """All the checks worth doing before touching 3 TB.

    Camera resolution problems, cross-site merges, zero-padding mixes and same-device
    source/destination are all reported here rather than discovered 6 hours in.
    """
    survey = survey_cameras(options.sources, exclude=_excluded(options))
    if not survey.cameras:
        log.warning(
            "no camera folders matched %s under the source root(s); "
            "every file will be quarantined to %s/",
            "CAM|CA|CT|C + digits",
            UNSORTED_DIR,
        )

    padding = survey.padding
    if padding:
        detail = "; ".join(
            f"{'/'.join(ids)} (camera {number})" for number, ids in sorted(padding.items())
        )
        message = (
            "camera IDs differ only by zero-padding, so two folder names mean the same "
            f"camera: {detail}. The R script's header promised zero-padding but never "
            "applied it, so guessing here would silently rename cameras. Rename the "
            "source folders, or pass --allow-padding-mix to keep them separate."
        )
        if options.allow_padding_mix:
            log.warning(message)
        else:
            raise PidsError(message)

    cross = survey.cross_site
    if cross:
        detail = "; ".join(f"{cam}: {', '.join(sorted(sites))}" for cam, sites in sorted(cross.items()))
        message = (
            f"the same camera ID appears under more than one site: {detail}. "
            f"Their images would merge into one {CHECK_DIR}/<CAxxx>/ folder. "
            "Pass --allow-cross-site-merge if that is correct for this survey."
        )
        if options.plan_only or options.allow_cross_site_merge:
            log.warning(message)
        else:
            raise PidsError(message)

    if options.dest:
        shared = same_physical_device(options.sources[0], options.dest)
        if shared:
            log.warning(
                "source and destination are on the same device: reading and writing "
                "through one disk makes the heads contend and roughly halves throughput. "
                "A different physical drive is worth more than every other optimisation "
                "combined (sec 3.1)."
            )
    return survey


def prescan(options: Options, stats: WalkStats) -> tuple[int, int]:
    """Count files and bytes up front: exact ETA and an exact free-space check.

    This is a metadata-only walk (no file contents), which is cheap next to the copy
    itself and buys the pre-flight disk-space check promised in sec 7.5.
    """
    files = 0
    total = 0
    for item in iter_images(
        options.sources,
        exclude=_excluded(options),
        stats=stats,
        follow_symlinks=options.follow_symlinks,
    ):
        files += 1
        total += item.size
        if options.limit and files >= options.limit:
            break
    return files, total


def check_space(dest: str, needed: int) -> None:
    """Refuse to start a copy that cannot fit (sec 7.5)."""
    free = free_bytes(dest)
    if needed > free:
        raise PidsError(
            f"destination has {human_bytes(free)} free but the source is "
            f"{human_bytes(needed)}. Copy leaves the source intact, so the destination "
            "needs room for the whole set."
        )
    if needed > free * 0.95:
        log.warning(
            "destination will be over 95%% full after the copy (%s free, %s needed)",
            human_bytes(free),
            human_bytes(needed),
        )


def _excluded(options: Options) -> tuple[str, ...]:
    """Subtrees the walk must never enter.

    Only the destination: when it is nested inside the source, walking it would
    re-ingest our own output.  The state directory is deliberately *not* excluded --
    the state DB and log are never JPEGs, and excluding their parent would silently
    skip the source tree whenever the two sit side by side.
    """
    if options.dest:
        return (str(normalise_root(options.dest)),)
    return ()


# ---------------------------------------------------------------------------- #
# Pipeline
# ---------------------------------------------------------------------------- #


class Pipeline:
    """Drives one ``scan`` or ``run``."""

    def __init__(self, options: Options, store, workers: int, probe: Probe | None = None):
        self.options = options
        self.store = store
        self.workers = workers
        self.probe = probe
        self.summary = Summary()
        self.stop = threading.Event()
        self.queue: queue.Queue = queue.Queue(maxsize=max(4, workers * 4))
        self.walk_stats = WalkStats()
        self._made_dirs: set[str] = set()
        self._mkdir_lock = threading.Lock()
        self.dest_root = (
            str(normalise_root(options.dest)) if options.dest else DEST_PLACEHOLDER
        )
        self.progress: Progress | None = None

    # -- public entry point ------------------------------------------------------

    def execute(self) -> Summary:
        started = time.monotonic()
        label = "scan" if self.options.plan_only else "copy"
        self.progress = Progress(
            label=label,
            total_files=self.summary.total_files,
            total_bytes=self.summary.total_bytes,
            enabled=self.options.progress,
        )
        threads = [
            threading.Thread(target=self._worker, name=f"pids-w{i}", daemon=True)
            for i in range(self.workers)
        ]
        for thread in threads:
            thread.start()

        previous_term = _install_sigterm(self.stop)
        try:
            self._feed()
        except KeyboardInterrupt:
            self.stop.set()
            self.summary.stopped_early = True
            self.summary.stop_reason = "interrupted"
            if self.progress:
                self.progress.note("\ninterrupted; finishing in-flight files...")
        finally:
            for _ in threads:
                self.queue.put(None)
            for thread in threads:
                thread.join()
            _restore_sigterm(previous_term)
            self.store.flush()
            if self.progress:
                self.progress.finish()
        self.summary.elapsed = time.monotonic() - started
        return self.summary

    # -- feeder (main thread) ----------------------------------------------------

    def _feed(self) -> None:
        for item in self._source_items():
            if self.stop.is_set():
                self.summary.stopped_early = True
                break
            with self.summary.lock:
                self.summary.seen += 1
            existing = None
            if self.options.resume or self.options.reuse_plan:
                existing = self.store.get(item.path)
                if (
                    existing is not None
                    and self.options.resume
                    and existing.status in db.TERMINAL
                ):
                    with self.summary.lock:
                        self.summary.skipped += 1
                    if self.progress:
                        self.progress.update(files=1, skipped=1)
                    continue
            # Blocking put: this is the back-pressure that keeps memory flat.
            while not self.stop.is_set():
                try:
                    self.queue.put((item, existing), timeout=0.5)
                    break
                except queue.Full:
                    continue
            if self.options.limit and self.summary.seen >= self.options.limit:
                break

    def _source_items(self) -> Iterator[FileItem]:
        if self.options.retry_failed:
            return self._retry_items()
        return iter_images(
            self.options.sources,
            exclude=_excluded(self.options),
            stats=self.walk_stats,
            follow_symlinks=self.options.follow_symlinks,
        )

    def _retry_items(self) -> Iterator[FileItem]:
        """Re-feed only the rows that failed or were interrupted mid-copy.

        A transient USB disconnect costs minutes rather than a full re-walk of 3 TB
        (sec 9), and ``claimed`` rows are files a killed process was mid-copy on.
        """
        roots = self.options.sources or ()
        for record in self.store.iter_records(statuses=[db.FAILED, db.CLAIMED]):
            try:
                info = os.stat(long_path(record.src))
            except OSError as exc:
                record.status = db.FAILED
                record.reason = f"source_missing: {exc.strerror or exc}"
                self.store.write(record)
                with self.summary.lock:
                    self.summary.failed += 1
                continue
            root = _root_of(record.src, roots) or os.path.dirname(record.src)
            yield FileItem(
                path=record.src, size=info.st_size, mtime=info.st_mtime, root=root
            )

    # -- workers -----------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            task = self.queue.get()
            if task is None:
                return
            if self.stop.is_set():
                continue  # drain without work so the feeder's sentinels still arrive
            item, existing = task
            try:
                self._process(item, existing)
            except copier.DiskFull as exc:
                self._fail(item, f"disk_full: {exc.strerror or exc}")
                self.summary.stop_reason = "destination full"
                self.summary.stopped_early = True
                self.stop.set()
                log.error("destination out of space; stopping cleanly. Run is resumable.")
            except OSError as exc:
                self._fail(item, _oserror_reason(exc))
            except Exception as exc:  # never let one bad file kill the run
                log.exception("unexpected error on %s", item.path)
                self._fail(item, f"internal_error: {exc!r}")

    def _process(self, item: FileItem, existing: Record | None) -> None:
        record = Record(src=item.path, size=item.size, mtime=item.mtime)
        parts = relative_parts(PurePath(item.path), PurePath(item.root))
        dir_parts, filename = parts[:-1], parts[-1]

        reuse = (
            self.options.reuse_plan
            and existing is not None
            and existing.dt is not None
            and existing.cam is not None
            and existing.date is not None
            and existing.size == item.size
        )

        # Buffered, not raw: the header parser needs full reads, and buffering costs
        # nothing because the kernel copy below works on the file descriptor.
        handle = None if self.options.plan_only else open(long_path(item.path), "rb")
        try:
            if reuse and existing is not None:
                record.dt, record.cam, record.date, record.site = (
                    existing.dt,
                    existing.cam,
                    existing.date,
                    existing.site,
                )
                date = existing.date
                reason = None
            else:
                if handle is None:
                    with open(long_path(item.path), "rb") as header_fh:
                        found = exif.read_datetime(header_fh)
                else:
                    found = exif.read_datetime(handle)
                record.dt = found.raw
                date, reason = exif.parse_exif_datetime(found.raw)
                if found.reason and not found.raw:
                    reason = found.reason

            camera = resolve_camera(dir_parts)
            if camera is not None:
                record.cam, record.site = camera.camera_id, camera.site

            if date is None:
                return self._quarantine(record, item, dir_parts, filename, reason or exif.NO_EXIF, handle)
            if camera is None and not record.cam:
                return self._quarantine(record, item, dir_parts, filename, NO_CAMERA_ID, handle)
            record.date = date

            dest_dir = os.path.join(
                self.dest_root,
                CHECK_DIR,
                safe_component(record.cam or ""),
                safe_component(record.date),
            )
            self._place(record, item, dest_dir, safe_component(filename), handle)
        finally:
            if handle is not None:
                handle.close()

    # -- destination placement ---------------------------------------------------

    def _place(
        self,
        record: Record,
        item: FileItem,
        dest_dir: str,
        filename: str,
        handle,
        policy: str | None = None,
        quarantined: bool = False,
    ) -> None:
        """Claim a destination, apply the collision policy, then copy."""
        policy = policy or self.options.on_collision
        stem, ext = os.path.splitext(filename)
        candidate = os.path.join(dest_dir, filename)
        record.dest = candidate
        record.dest_key = dest_key(candidate)
        record.status = db.PLANNED if self.options.plan_only else db.CLAIMED

        claim = self.store.claim(record)
        if not claim.won:
            resolved = self._on_collision(
                record, item, dest_dir, stem, ext, claim.winner, policy
            )
            if resolved is None:
                return
            candidate = resolved

        if self.options.plan_only:
            with self.summary.lock:
                self.summary.planned += 1
            record.status = db.PLANNED
            self.store.write(record)
            if self.progress:
                self.progress.update(files=1, planned=1)
            return

        self._ensure_dir(dest_dir)
        written = copier.copy_file(handle, item.path, candidate, item.size)
        actual = os.stat(long_path(candidate)).st_size
        if actual != item.size:
            # Size mismatch: hash both sides immediately and fail hard (sec 5.8).
            record.status = db.FAILED
            record.reason = f"size_mismatch: expected {item.size}, wrote {actual}"
            record.hash = copier.hash_file(item.path)
            self.store.write(record)
            with self.summary.lock:
                self.summary.failed += 1
            if self.progress:
                self.progress.update(files=1, failed=1)
            log.error("size mismatch copying %s -> %s", item.path, candidate)
            return

        record.status = db.QUARANTINED if quarantined else db.OK
        record.dest = candidate
        self.store.write(record)
        with self.summary.lock:
            if quarantined:
                self.summary.quarantined += 1
            else:
                self.summary.copied += 1
            self.summary.copied_bytes += written
        if self.progress:
            self.progress.update(
                files=1,
                nbytes=written,
                **({"quarantined": 1} if quarantined else {}),
            )

    def _on_collision(
        self,
        record: Record,
        item: FileItem,
        dest_dir: str,
        stem: str,
        ext: str,
        winner: str | None,
        policy: str,
    ) -> str | None:
        """Apply ``--on-collision``.  Returns the destination to use, or None.

        Reconyx/Bushnell cameras restart numbering at ``IMG_0001.JPG`` in every
        ``100EK113``-style subfolder, so collisions within one camera+date are common,
        not rare (sec 7.3).  Whatever the policy, the outcome is logged.
        """
        if policy == KEEP_FIRST:
            record.status = db.CONFLICT
            record.reason = "collision_keep_first"
            record.conflict_with = winner
            record.dest = None
            record.dest_key = None
            self.store.write(record)
            with self.summary.lock:
                self.summary.conflicts += 1
            if self.progress:
                self.progress.update(files=1, conflicts=1)
            log.info("collision: %s not copied; %s owns the destination", item.path, winner)
            return None

        if policy == HASH_DEDUPE and winner and self._is_duplicate(record, item, winner):
            return self._record_duplicate(record, winner)

        for index in range(1, MAX_SUFFIX + 1):
            candidate = os.path.join(dest_dir, f"{stem}_{index:03d}{ext}")
            record.dest = candidate
            record.dest_key = dest_key(candidate)
            result = self.store.claim(record)
            if result.won:
                record.conflict_with = winner
                if not record.reason:
                    record.reason = (
                        "differing_bytes" if policy == HASH_DEDUPE else "collision_suffixed"
                    )
                with self.summary.lock:
                    self.summary.conflicts += 1
                return candidate
            # Under hash-dedupe the twin may already own a *suffixed* name rather than
            # the base one, so every level is compared -- otherwise a three-way
            # collision can copy a true duplicate as _002.
            if policy == HASH_DEDUPE and result.winner:
                if self._is_duplicate(record, item, result.winner):
                    return self._record_duplicate(record, result.winner)
        record.status = db.FAILED
        record.reason = f"collision_unresolved: >{MAX_SUFFIX} files named {stem}{ext}"
        record.dest = None
        record.dest_key = None
        self.store.write(record)
        with self.summary.lock:
            self.summary.failed += 1
        return None

    def _is_duplicate(self, record: Record, item: FileItem, owner: str) -> bool:
        """Byte-compare this file against the source that owns a destination.

        The source hash is computed at most once per file; the owner's is read per
        comparison.  Cheap, because it only ever runs on a collision (sec 7.3).
        """
        try:
            if os.stat(long_path(owner)).st_size != item.size:
                return False
        except OSError:
            return False
        if record.hash is None:
            record.hash = copier.hash_file(item.path)
        try:
            return copier.hash_file(owner) == record.hash
        except OSError:
            return False

    def _record_duplicate(self, record: Record, winner: str) -> None:
        """Identical bytes: one copy on disk, the duplicate logged (never lost)."""
        record.status = db.CONFLICT
        record.reason = "duplicate_bytes"
        record.conflict_with = winner
        record.dest = None
        record.dest_key = None
        self.store.write(record)
        with self.summary.lock:
            self.summary.duplicates += 1
        if self.progress:
            self.progress.update(files=1, duplicates=1)
        return None

    def _quarantine(
        self,
        record: Record,
        item: FileItem,
        dir_parts: Sequence[str],
        filename: str,
        reason: str,
        handle,
    ) -> None:
        """Copy to ``DEST/Unsorted/<path relative to source root>`` (sec 7.2).

        Nothing is filed on a guessed date; nothing is left behind unrecorded.  The
        original structure is preserved so provenance is obvious.  Unsorted always uses
        the suffix policy: a quarantined file must never be silently dropped.
        """
        record.reason = reason
        record.date = None
        unsorted_dir = os.path.join(
            self.dest_root, UNSORTED_DIR, *[safe_component(part) for part in dir_parts]
        )
        if self.options.plan_only or not self.options.dest:
            record.status = db.QUARANTINED
            # Recorded, not claimed: nothing is copied during a plan, but the preview
            # still shows where this file would land.
            record.dest = os.path.join(unsorted_dir, safe_component(filename))
            record.dest_key = None
            self.store.write(record)
            with self.summary.lock:
                self.summary.quarantined += 1
            if self.progress:
                self.progress.update(files=1, quarantined=1)
            return
        record.status = db.QUARANTINED
        self._place(
            record,
            item,
            unsorted_dir,
            safe_component(filename),
            handle,
            policy=SUFFIX,
            quarantined=True,
        )

    # -- helpers -----------------------------------------------------------------

    def _ensure_dir(self, path: str) -> None:
        """``mkdir`` guarded by an in-process set: a few thousand syscalls, not 600k."""
        if path in self._made_dirs:
            return
        with self._mkdir_lock:
            if path in self._made_dirs:
                return
            os.makedirs(long_path(path), exist_ok=True)
            self._made_dirs.add(path)

    def _fail(self, item: FileItem, reason: str) -> None:
        record = Record(
            src=item.path,
            size=item.size,
            mtime=item.mtime,
            status=db.FAILED,
            reason=reason,
        )
        try:
            self.store.write(record)
        except Exception:  # pragma: no cover - store already failing
            log.error("could not record failure for %s: %s", item.path, reason)
        with self.summary.lock:
            self.summary.failed += 1
        if self.progress:
            self.progress.update(files=1, failed=1)
        log.warning("failed: %s (%s)", item.path, reason)


def _oserror_reason(exc: OSError) -> str:
    name = getattr(exc, "strerror", None) or str(exc)
    return f"oserror[{exc.errno}]: {name}"


def _root_of(path: str, roots: Sequence[str]) -> str | None:
    for root in roots:
        if is_within(path, root):
            return str(normalise_root(root))
    return None


def _install_sigterm(stop: threading.Event):
    """Turn SIGTERM into a clean stop, so a kill leaves a resumable manifest."""
    if threading.current_thread() is not threading.main_thread():
        return None
    try:
        return signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        return None


def _restore_sigterm(previous) -> None:
    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        pass


# ---------------------------------------------------------------------------- #
# Orchestration
# ---------------------------------------------------------------------------- #


def run_pipeline(options: Options, cmd: str) -> tuple[Summary, Probe, int]:
    """Validate, pre-flight, then execute a scan or a run."""
    options.sources = check_sources(options.sources)
    if options.on_collision not in COLLISION_POLICIES:
        raise PidsError(f"--on-collision must be one of {', '.join(COLLISION_POLICIES)}")
    if not options.plan_only and not options.dest:
        raise PidsError("--dest is required for a copy; use `pids scan` to plan only")

    target = options.dest or options.sources[0]
    try:
        workers, probe = resolve_workers(options.workers, target)
    except ValueError as exc:
        raise PidsError(str(exc)) from exc

    if not options.retry_failed:
        preflight(options)

    store = db.open_store(options.state, options.journal)
    store.start()
    try:
        if options.reuse_plan and not options.resume and not store.counts():
            # Nothing to reuse or skip: don't pay for a per-file lookup on a fresh DB.
            options.reuse_plan = False
        run_id = store.begin_run(
            cmd=cmd,
            sources=options.sources,
            dest=options.dest,
            flags={
                "on_collision": options.on_collision,
                "resume": options.resume,
                "retry_failed": options.retry_failed,
                "limit": options.limit,
                "plan_only": options.plan_only,
                "journal": options.journal,
                "prescan": options.prescan,
            },
            workers=workers,
        )
        pipeline = Pipeline(options, store, workers, probe)

        if options.prescan and not options.retry_failed:
            files, total = prescan(options, pipeline.walk_stats)
            pipeline.summary.total_files = files
            pipeline.summary.total_bytes = total
            log.info("pre-scan: %d images, %s", files, human_bytes(total))
            if options.dest and not options.plan_only:
                check_space(options.dest, total)
            pipeline.walk_stats = WalkStats()

        summary = pipeline.execute()
        store.flush()
        store.end_run(run_id, summary.as_dict())
        return summary, probe, workers
    finally:
        store.close()
