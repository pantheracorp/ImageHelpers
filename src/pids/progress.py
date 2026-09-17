"""One rewriting status line, plus file logging.

DESIGN.md sec 8.6.  Never 600k lines to a console -- ``print()`` of every failed copy
is what killed the R version on a re-run.  Full detail goes to a log file; the terminal
gets a single line that includes *current* destination throughput, so an operator can
see whether the disk is saturated and therefore whether more workers would help.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import deque
from typing import TextIO

log = logging.getLogger("pids")


def setup_logging(logfile: str | os.PathLike[str] | None, verbose: bool = False) -> None:
    """Send detail to ``logfile`` and warnings upward to stderr."""
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    log.propagate = False
    if logfile:
        parent = os.path.dirname(os.path.abspath(os.fspath(logfile)))
        if parent:
            os.makedirs(parent, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(logfile, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        log.addHandler(handler)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.DEBUG if verbose else logging.WARNING)
    stderr.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(stderr)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def human_time(seconds: float) -> str:
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes:02d}:{secs:02d}"


class Progress:
    """Throttled single-line progress with a rolling throughput window."""

    def __init__(
        self,
        label: str = "run",
        total_files: int | None = None,
        total_bytes: int | None = None,
        stream: TextIO | None = None,
        enabled: bool = True,
        interval: float = 0.25,
        window: float = 5.0,
    ):
        self.label = label
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.stream = stream if stream is not None else sys.stderr
        self.interval = interval
        self.window = window
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.files = 0
        self.bytes = 0
        self.counters: dict[str, int] = {}
        self._last_render = 0.0
        self._last_width = 0
        self._samples: deque[tuple[float, int]] = deque()
        self.tty = enabled and hasattr(self.stream, "isatty") and self.stream.isatty()
        self.enabled = enabled
        self._heartbeat_every = 5000  # non-tty: one line per N files, not per file

    def update(self, files: int = 1, nbytes: int = 0, **counters: int) -> None:
        now = time.monotonic()
        with self.lock:
            self.files += files
            self.bytes += nbytes
            for key, value in counters.items():
                if value:
                    self.counters[key] = self.counters.get(key, 0) + value
            self._samples.append((now, nbytes))
            cutoff = now - self.window
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            if not self.enabled:
                return
            if self.tty:
                if now - self._last_render >= self.interval:
                    self._render(now)
            elif self.files % self._heartbeat_every == 0:
                self._heartbeat(now)

    def _recent_mbps(self, now: float) -> float:
        if len(self._samples) < 2:
            return 0.0
        span = now - self._samples[0][0]
        if span <= 0:
            return 0.0
        recent = sum(nbytes for _, nbytes in self._samples)
        return recent / span / (1024 * 1024)

    def _line(self, now: float) -> str:
        elapsed = max(now - self.started, 1e-6)
        rate = self.files / elapsed
        parts = [self.label]
        if self.total_files:
            pct = 100.0 * self.files / self.total_files
            parts.append(f"{self.files:,}/{self.total_files:,} ({pct:4.1f}%)")
            remaining = (self.total_files - self.files) / rate if rate else float("inf")
            parts.append(f"eta {human_time(remaining)}")
        else:
            parts.append(f"{self.files:,} files")
        parts.append(f"{rate:,.0f} f/s")
        parts.append(f"{self._recent_mbps(now):,.1f} MB/s now")
        parts.append(f"{human_bytes(self.bytes)} copied")
        extra = " ".join(f"{k}={v:,}" for k, v in sorted(self.counters.items()) if v)
        if extra:
            parts.append(extra)
        return " | ".join(parts)

    def _render(self, now: float) -> None:
        line = self._line(now)
        pad = max(0, self._last_width - len(line))
        self.stream.write("\r" + line + " " * pad)
        self.stream.flush()
        self._last_width = len(line)
        self._last_render = now

    def _heartbeat(self, now: float) -> None:
        self.stream.write(self._line(now) + "\n")
        self.stream.flush()
        self._last_render = now

    def finish(self) -> None:
        with self.lock:
            if not self.enabled:
                return
            now = time.monotonic()
            if self.tty:
                self._render(now)
                self.stream.write("\n")
            else:
                self.stream.write(self._line(now) + "\n")
            self.stream.flush()

    def note(self, message: str) -> None:
        """Print a one-off message without corrupting the status line."""
        with self.lock:
            if self.tty and self._last_width:
                self.stream.write("\r" + " " * self._last_width + "\r")
                self._last_width = 0
            self.stream.write(message + "\n")
            self.stream.flush()
