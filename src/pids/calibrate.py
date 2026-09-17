"""Measure the right worker count instead of guessing it.

DESIGN.md sec 4: device detection is best-effort and imperfect across Windows and
macOS, so ``pids calibrate`` copies a few hundred real files at 1, 2, 4, 8 and 16
workers and reports measured MB/s.

Each round gets its *own* disjoint slice of files.  Re-copying the same files would
find them in the page cache and make every later round look faster, which would bias
the result towards more workers -- exactly the wrong answer on a spinning disk.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass
from typing import Sequence

from pids import copier
from pids.paths import long_path, normalise_root
from pids.walker import FileItem, iter_images

DEFAULT_LADDER = (1, 2, 4, 8, 16)
CALIBRATE_DIR = ".pids-calibrate"


@dataclass
class Round:
    workers: int
    files: int
    bytes: int
    seconds: float
    errors: int = 0

    @property
    def mbps(self) -> float:
        return self.bytes / self.seconds / (1024 * 1024) if self.seconds else 0.0

    @property
    def files_per_sec(self) -> float:
        return self.files / self.seconds if self.seconds else 0.0


def collect_sample(sources: Sequence[str], count: int) -> list[FileItem]:
    """Take the first ``count`` images from the walk."""
    items: list[FileItem] = []
    for item in iter_images(sources):
        items.append(item)
        if len(items) >= count:
            break
    return items


def calibrate(
    sources: Sequence[str],
    dest: str,
    per_round: int = 200,
    ladder: Sequence[int] = DEFAULT_LADDER,
) -> list[Round]:
    """Copy disjoint slices at each worker count and time them."""
    ladder = [w for w in ladder if w >= 1]
    needed = per_round * len(ladder)
    items = collect_sample(sources, needed)
    if len(items) < len(ladder) * 2:
        raise ValueError(
            f"need at least {len(ladder) * 2} images to calibrate, found {len(items)}"
        )
    slice_size = len(items) // len(ladder)
    scratch = os.path.join(str(normalise_root(dest)), CALIBRATE_DIR)
    results: list[Round] = []
    try:
        for index, workers in enumerate(ladder):
            batch = items[index * slice_size : (index + 1) * slice_size]
            target = os.path.join(scratch, f"w{workers}")
            os.makedirs(long_path(target), exist_ok=True)
            results.append(_time_round(batch, target, workers))
    finally:
        shutil.rmtree(long_path(scratch), ignore_errors=True)
    return results


def _time_round(batch: Sequence[FileItem], target: str, workers: int) -> Round:
    lock = threading.Lock()
    state = {"bytes": 0, "files": 0, "errors": 0}
    work = list(batch)
    cursor = [0]
    cursor_lock = threading.Lock()

    def next_item() -> tuple[int, FileItem] | None:
        with cursor_lock:
            if cursor[0] >= len(work):
                return None
            position = cursor[0]
            cursor[0] += 1
            return position, work[position]

    def worker() -> None:
        while True:
            task = next_item()
            if task is None:
                return
            position, item = task
            dest_path = os.path.join(target, f"{position:06d}_{os.path.basename(item.path)}")
            try:
                with open(long_path(item.path), "rb") as fh:
                    written = copier.copy_file(fh, item.path, dest_path, item.size)
                with lock:
                    state["bytes"] += written
                    state["files"] += 1
            except OSError:
                with lock:
                    state["errors"] += 1

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started
    return Round(
        workers=workers,
        files=state["files"],
        bytes=state["bytes"],
        seconds=elapsed,
        errors=state["errors"],
    )


def format_rounds(rounds: Sequence[Round]) -> str:
    lines = [f"  {'workers':>7}  {'files':>6}  {'MB/s':>8}  {'files/s':>8}  errors"]
    best = max(rounds, key=lambda r: r.mbps) if rounds else None
    for item in rounds:
        marker = "  <- fastest" if best is item else ""
        lines.append(
            f"  {item.workers:>7}  {item.files:>6}  {item.mbps:>8.1f}  "
            f"{item.files_per_sec:>8.1f}  {item.errors:>6}{marker}"
        )
    if best:
        lines.append("")
        lines.append(f"  Recommended: --workers {best.workers}")
    return "\n".join(lines)
