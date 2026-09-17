"""Source tree walk: a generator, never a 600k-element list.

DESIGN.md sec 4.  ``os.scandir`` in one thread feeds a bounded queue, so memory is flat
in the number of files -- the R script's crash started with holding the whole job in
RAM (sec 1).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

from pids.paths import SKIP_DIRS, is_jpeg_name, is_within, long_path


@dataclass
class WalkStats:
    """Counters filled in as the walk proceeds; cheap to print in the summary."""

    dirs: int = 0
    images: int = 0
    other_files: int = 0
    image_bytes: int = 0
    unreadable_dirs: list[str] = field(default_factory=list)
    unreadable_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FileItem:
    """One candidate source image."""

    path: str
    size: int
    mtime: float
    root: str


def _should_skip_dir(name: str) -> bool:
    return name.casefold() in SKIP_DIRS


def iter_images(
    roots: Sequence[str],
    exclude: Iterable[str] = (),
    stats: WalkStats | None = None,
    follow_symlinks: bool = False,
) -> Iterator[FileItem]:
    """Yield every JPEG under ``roots``, depth-first, skipping ``exclude`` subtrees.

    ``exclude`` normally holds the destination root and the state directory: when the
    destination is nested inside the source, walking it would re-ingest our own output.
    """
    stats = stats if stats is not None else WalkStats()
    excluded = [os.path.abspath(p) for p in exclude]
    for root in roots:
        root = os.path.abspath(root)
        stack = [root]
        while stack:
            current = stack.pop()
            stats.dirs += 1
            try:
                with os.scandir(long_path(current)) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=follow_symlinks):
                                if _should_skip_dir(entry.name):
                                    continue
                                if any(is_within(entry.path, ex) for ex in excluded):
                                    continue
                                stack.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=follow_symlinks):
                                continue
                            if not is_jpeg_name(entry.name):
                                stats.other_files += 1
                                continue
                            info = entry.stat(follow_symlinks=follow_symlinks)
                            stats.images += 1
                            stats.image_bytes += info.st_size
                            yield FileItem(
                                path=entry.path,
                                size=info.st_size,
                                mtime=info.st_mtime,
                                root=root,
                            )
                        except OSError:
                            stats.unreadable_files.append(entry.path)
            except OSError:
                stats.unreadable_dirs.append(current)


def iter_dirs(roots: Sequence[str], exclude: Iterable[str] = ()) -> Iterator[tuple[str, str]]:
    """Yield ``(directory_path, root)`` for every directory under ``roots``.

    Directories only: no per-file stat.  This is what makes the camera pre-flight
    checks (zero-padding, cross-site merges) affordable before any copying, because a
    3 TB tree has a few thousand directories but 600k files (sec 11).
    """
    excluded = [os.path.abspath(p) for p in exclude]
    for root in roots:
        root = os.path.abspath(root)
        stack = [root]
        while stack:
            current = stack.pop()
            yield current, root
            try:
                with os.scandir(long_path(current)) as entries:
                    for entry in entries:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if _should_skip_dir(entry.name):
                            continue
                        if any(is_within(entry.path, ex) for ex in excluded):
                            continue
                        stack.append(entry.path)
            except OSError:
                continue
