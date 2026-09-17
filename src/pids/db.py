"""State store: SQLite state DB (default) and an append-only JSONL fallback.

DESIGN.md sec 8.  All writes go through **one writer thread** with batched transactions:
SQLite allows a single writer, so funnelling writes avoids ``SQLITE_BUSY`` retries
entirely and keeps worker threads purely on I/O (sec 4).

The writer thread is also what makes collision handling *exact* rather than
best-effort.  It is the only thing that inserts destination keys, so its
select-then-insert is race-free by construction, and the ``UNIQUE`` index on
``dest_key`` is a hard backstop underneath it.  Two worker threads cannot both believe
they won a destination (sec 8.2).
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Iterator, Sequence

from pids import __version__

SCHEMA_VERSION = 1

#: Rows per transaction.  Claims are answered from the writer's own uncommitted
#: transaction, so batching costs no correctness: a kill loses at most the last batch,
#: and those files are simply recopied on resume (sec 8.4).
BATCH_ROWS = 2000

#: Commit at least this often even when idle, so a long slow run keeps its state fresh.
COMMIT_INTERVAL = 1.0

# Status vocabulary.  ``claimed`` is an in-flight marker not in the DESIGN.md list: a
# row left ``claimed`` by a killed process means the copy was in progress, and resume
# recopies it because only ``ok`` is skipped.
PLANNED = "planned"
CLAIMED = "claimed"
OK = "ok"
CONFLICT = "conflict"
QUARANTINED = "quarantined"
FAILED = "failed"
SKIPPED = "skipped"

#: Statuses that represent a decided file: copied, filed to Unsorted, or a logged
#: collision loser.  ``--resume`` skips these so a resumed run costs only the work
#: that is actually left.  ``planned``/``claimed``/``failed`` are *not* terminal:
#: planned rows still need copying and claimed rows were interrupted mid-copy.
TERMINAL = (OK, QUARANTINED, CONFLICT)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  src        TEXT PRIMARY KEY,   -- absolute source path
  size       INTEGER NOT NULL,
  mtime      REAL    NOT NULL,
  dt         TEXT,               -- raw EXIF DateTimeOriginal, NULL if unreadable
  site       TEXT,               -- component above the camera folder
  cam        TEXT,               -- CA116
  date       TEXT,               -- 071424
  dest       TEXT,               -- NULL for quarantined/conflict rows
  dest_key   TEXT,               -- case-folded dest, for collision arbitration
  status     TEXT NOT NULL,      -- planned|claimed|ok|conflict|quarantined|failed|skipped
  reason     TEXT,               -- no_exif | zero_date | no_camera_id | OS error
  conflict_with TEXT,            -- src that won the destination
  hash       TEXT,               -- populated only for sampled/suspect files
  ts         REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS files_dest_key ON files(dest_key);
CREATE INDEX IF NOT EXISTS files_status   ON files(status);
CREATE INDEX IF NOT EXISTS files_cam_date ON files(cam, date);

CREATE TABLE IF NOT EXISTS runs (
  run_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  cmd      TEXT NOT NULL,
  sources  TEXT,
  dest     TEXT,
  flags    TEXT,
  workers  INTEGER,
  version  TEXT,
  started  REAL,
  ended    REAL,
  totals   TEXT
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""

_FIELDS = (
    "src",
    "size",
    "mtime",
    "dt",
    "site",
    "cam",
    "date",
    "dest",
    "dest_key",
    "status",
    "reason",
    "conflict_with",
    "hash",
    "ts",
)

_UPSERT = (
    "INSERT INTO files (" + ",".join(_FIELDS) + ") "
    "VALUES (" + ",".join("?" * len(_FIELDS)) + ") "
    "ON CONFLICT(src) DO UPDATE SET "
    + ",".join(f"{name}=excluded.{name}" for name in _FIELDS if name != "src")
)


@dataclass
class Record:
    """One source file's row.  Mutated in place by the worker, then written once."""

    src: str
    size: int
    mtime: float
    dt: str | None = None
    site: str | None = None
    cam: str | None = None
    date: str | None = None
    dest: str | None = None
    dest_key: str | None = None
    status: str = PLANNED
    reason: str | None = None
    conflict_with: str | None = None
    hash: str | None = None
    ts: float = field(default_factory=time.time)

    def values(self) -> tuple:
        return tuple(getattr(self, name) for name in _FIELDS)

    @classmethod
    def from_row(cls, row: Sequence) -> "Record":
        return cls(**{name: row[i] for i, name in enumerate(_FIELDS)})


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of claiming a destination path."""

    won: bool
    winner: str | None = None  # the src that owns it, when we lost


class _Reply:
    """One-shot cross-thread result slot for a claim."""

    __slots__ = ("event", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: ClaimResult | None = None

    def set(self, result: ClaimResult) -> None:
        self.result = result
        self.event.set()

    def wait(self) -> ClaimResult:
        self.event.wait()
        assert self.result is not None
        return self.result


class SqliteStore:
    """The default state store: one SQLite file in WAL mode."""

    kind = "sqlite"

    def __init__(self, path: str | os.PathLike[str], queue_size: int = 4096):
        self.path = os.fspath(path)
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._read_lock = threading.Lock()
        self._read_conn: sqlite3.Connection | None = None
        self._flush_done = threading.Event()

    # -- lifecycle ---------------------------------------------------------------

    def _connect(self, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            uri = "file:" + self.path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        else:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        # WAL gives concurrent readers alongside the single writer; NORMAL is durable
        # across a process kill -- the failure mode that matters here -- without an
        # fsync per transaction (sec 8.1).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def start(self) -> None:
        conn = self._connect()
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.close()
        self._read_conn = self._connect(readonly=True)
        self._thread = threading.Thread(target=self._writer, name="pids-writer", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self._queue.put(("stop",))
            self._thread.join()
            self._thread = None
        if self._read_conn is not None:
            self._read_conn.close()
            self._read_conn = None
        self._raise_if_failed()

    def __enter__(self) -> "SqliteStore":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _raise_if_failed(self) -> None:
        if self._error is not None:
            raise self._error

    def _put(self, op: tuple) -> None:
        """Enqueue an op, but never block forever if the writer thread has died."""
        while True:
            self._raise_if_failed()
            try:
                self._queue.put(op, timeout=0.5)
                return
            except queue.Full:
                if self._thread is None or not self._thread.is_alive():
                    self._raise_if_failed()
                    raise RuntimeError("state DB writer thread is not running")

    # -- writer thread -----------------------------------------------------------

    def _writer(self) -> None:
        conn = self._connect()
        pending = 0
        last_commit = time.monotonic()
        in_txn = False
        try:
            while True:
                try:
                    op = self._queue.get(timeout=0.2)
                except queue.Empty:
                    op = None
                if op is not None and op[0] == "stop":
                    break

                if op is not None:
                    if not in_txn:
                        conn.execute("BEGIN")
                        in_txn = True
                    pending += self._apply(conn, op)

                elapsed = time.monotonic() - last_commit
                flushing = op is not None and op[0] == "flush"
                if in_txn and (pending >= BATCH_ROWS or elapsed >= COMMIT_INTERVAL or flushing):
                    conn.execute("COMMIT")
                    in_txn = False
                    pending = 0
                    last_commit = time.monotonic()
                if flushing:
                    self._flush_done.set()
        except BaseException as exc:  # surfaced to the caller on close()
            self._error = exc
            self._drain_replies_on_error()
        finally:
            try:
                if in_txn:
                    conn.execute("COMMIT")
            except sqlite3.Error:
                pass
            conn.close()
            self._flush_done.set()

    def _drain_replies_on_error(self) -> None:
        """Unblock any worker waiting on a claim so the run can shut down."""
        while True:
            try:
                op = self._queue.get_nowait()
            except queue.Empty:
                return
            if op[0] == "claim":
                op[2].set(ClaimResult(won=False, winner=None))
            elif op[0] == "flush":
                self._flush_done.set()

    def _apply(self, conn: sqlite3.Connection, op: tuple) -> int:
        kind = op[0]
        if kind == "write":
            conn.execute(_UPSERT, op[1].values())
            return 1
        if kind == "claim":
            record: Record = op[1]
            reply: _Reply = op[2]
            row = conn.execute(
                "SELECT src FROM files WHERE dest_key=?", (record.dest_key,)
            ).fetchone()
            if row is not None and row[0] != record.src:
                reply.set(ClaimResult(won=False, winner=row[0]))
                return 0
            try:
                conn.execute(_UPSERT, record.values())
            except sqlite3.IntegrityError:
                # Backstop: the UNIQUE index caught something the SELECT did not.
                row = conn.execute(
                    "SELECT src FROM files WHERE dest_key=?", (record.dest_key,)
                ).fetchone()
                reply.set(ClaimResult(won=False, winner=row[0] if row else None))
                return 0
            reply.set(ClaimResult(won=True))
            return 1
        if kind == "sql":
            conn.execute(op[1], op[2])
            return 1
        if kind == "flush":
            return 0
        raise AssertionError(f"unknown op {kind!r}")

    # -- write API ---------------------------------------------------------------

    def write(self, record: Record) -> None:
        """Queue a full-row upsert.  Asynchronous and batched."""
        record.ts = time.time()
        self._put(("write", record))

    def claim(self, record: Record) -> ClaimResult:
        """Claim ``record.dest_key``, blocking until the writer thread arbitrates.

        A win inserts the row; a loss returns the winning src and writes nothing, so
        the caller can apply ``--on-collision`` (sec 5 step 6).
        """
        record.ts = time.time()
        reply = _Reply()
        self._put(("claim", record, reply))
        result = reply.wait()
        self._raise_if_failed()
        return result

    def flush(self) -> None:
        """Block until everything queued so far is committed."""
        self._flush_done.clear()
        self._put(("flush",))
        self._flush_done.wait()
        self._raise_if_failed()

    # -- read API ----------------------------------------------------------------

    def status_of(self, src: str) -> str | None:
        """Indexed primary-key lookup used by resume -- not a scan (sec 8.4)."""
        assert self._read_conn is not None
        with self._read_lock:
            row = self._read_conn.execute(
                "SELECT status FROM files WHERE src=?", (src,)
            ).fetchone()
        return row[0] if row else None

    def get(self, src: str) -> Record | None:
        assert self._read_conn is not None
        with self._read_lock:
            row = self._read_conn.execute(
                "SELECT " + ",".join(_FIELDS) + " FROM files WHERE src=?", (src,)
            ).fetchone()
        return Record.from_row(row) if row else None

    def iter_records(
        self, statuses: Iterable[str] | None = None, order: str = "src"
    ) -> Iterator[Record]:
        """Stream rows one at a time; nothing is ever loaded into memory (sec 8.3)."""
        sql = "SELECT " + ",".join(_FIELDS) + " FROM files"
        params: tuple = ()
        if statuses is not None:
            statuses = tuple(statuses)
            sql += " WHERE status IN (" + ",".join("?" * len(statuses)) + ")"
            params = statuses
        sql += f" ORDER BY {order}"
        conn = self._connect(readonly=True)
        try:
            cursor = conn.execute(sql, params)
            for row in cursor:
                yield Record.from_row(row)
        finally:
            conn.close()

    def query(self, sql: str, params: Sequence = ()) -> list[tuple]:
        assert self._read_conn is not None
        with self._read_lock:
            return self._read_conn.execute(sql, tuple(params)).fetchall()

    def stream(self, sql: str, params: Sequence = ()) -> Iterator[tuple]:
        conn = self._connect(readonly=True)
        try:
            for row in conn.execute(sql, tuple(params)):
                yield row
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        return {
            status: n
            for status, n in self.query("SELECT status, COUNT(*) FROM files GROUP BY status")
        }

    def totals(self) -> tuple[int, int]:
        row = self.query("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE status='ok'")
        return (row[0][0], row[0][1]) if row else (0, 0)

    def cameras(self) -> list[str]:
        return [
            cam
            for (cam,) in self.query(
                "SELECT DISTINCT cam FROM files WHERE cam IS NOT NULL ORDER BY cam"
            )
        ]

    def cross_site_cameras(self) -> list[tuple[str, int, str]]:
        """Cameras whose images come from more than one site (sec 11.1)."""
        return self.query(
            "SELECT cam, COUNT(DISTINCT site) AS n, "
            "       GROUP_CONCAT(DISTINCT site) AS sites "
            "FROM files WHERE cam IS NOT NULL AND site IS NOT NULL "
            "GROUP BY cam HAVING n > 1 ORDER BY cam"
        )

    # -- run bookkeeping ---------------------------------------------------------

    def begin_run(
        self,
        cmd: str,
        sources: Sequence[str],
        dest: str | None,
        flags: dict,
        workers: int,
    ) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO runs (cmd, sources, dest, flags, workers, version, started) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    cmd,
                    json.dumps(list(sources)),
                    dest,
                    json.dumps(flags, default=str, sort_keys=True),
                    workers,
                    __version__,
                    time.time(),
                ),
            )
            return int(cursor.lastrowid or 0)
        finally:
            conn.close()

    def end_run(self, run_id: int, totals: dict) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE runs SET ended=?, totals=? WHERE run_id=?",
                (time.time(), json.dumps(totals, default=str, sort_keys=True), run_id),
            )
        finally:
            conn.close()

    def runs(self) -> list[tuple]:
        return self.query(
            "SELECT run_id, cmd, workers, started, ended, totals FROM runs ORDER BY run_id"
        )


class JsonlStore:
    """Append-only JSONL fallback for state files that must live on a network share.

    SQLite must not live on SMB/NFS -- file locking over network filesystems is
    unreliable and can corrupt the database (sec 8.5).  This store is safe to append
    over SMB, at the documented cost of in-memory indexes and a startup scan, and is
    selected only by ``--journal jsonl``.
    """

    kind = "jsonl"

    def __init__(self, path: str | os.PathLike[str]):
        self.path = os.fspath(path)
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = None
        self._offsets: dict[str, int] = {}  # src -> byte offset of its latest line
        self._status: dict[str, str] = {}  # src -> latest status
        self._owners: dict[str, str] = {}  # dest_key -> owning src
        self._runs_path = self.path + ".runs"

    def start(self) -> None:
        if os.path.exists(self.path):
            self._load()
        # Binary mode: byte offsets from tell() are only meaningful for seek() here,
        # and text-mode offsets are opaque.
        self._fh = open(self.path, "ab+")

    def _load(self) -> None:
        with open(self.path, "rb") as fh:
            offset = 0
            for line in fh:
                stripped = line.strip()
                if stripped:
                    try:
                        row = json.loads(stripped.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        offset += len(line)
                        continue  # torn final line from a killed process
                    src = row.get("src")
                    if src:
                        self._offsets[src] = offset
                        self._status[src] = row.get("status", PLANNED)
                        key = row.get("dest_key")
                        if key:
                            self._owners[key] = src
                offset += len(line)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "JsonlStore":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _append(self, record: Record) -> None:
        assert self._fh is not None
        record.ts = time.time()
        payload = json.dumps(asdict(record), separators=(",", ":")).encode("utf-8")
        self._fh.seek(0, os.SEEK_END)
        offset = self._fh.tell()
        self._fh.write(payload + b"\n")
        self._offsets[record.src] = offset
        self._status[record.src] = record.status

    def write(self, record: Record) -> None:
        with self._lock:
            self._append(record)

    def claim(self, record: Record) -> ClaimResult:
        with self._lock:
            owner = self._owners.get(record.dest_key or "")
            if owner is not None and owner != record.src:
                return ClaimResult(won=False, winner=owner)
            if record.dest_key:
                self._owners[record.dest_key] = record.src
            self._append(record)
            return ClaimResult(won=True)

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()

    def status_of(self, src: str) -> str | None:
        return self._status.get(src)

    def get(self, src: str) -> Record | None:
        offset = self._offsets.get(src)
        if offset is None:
            return None
        with self._lock:
            assert self._fh is not None
            self._fh.flush()
            here = self._fh.tell()
            self._fh.seek(offset)
            line = self._fh.readline()
            self._fh.seek(here)
        try:
            return Record(**json.loads(line.decode("utf-8")))
        except (ValueError, TypeError, UnicodeDecodeError):
            return None

    def iter_records(
        self, statuses: Iterable[str] | None = None, order: str = "src"
    ) -> Iterator[Record]:
        wanted = set(statuses) if statuses is not None else None
        keys = sorted(self._offsets) if order == "src" else list(self._offsets)
        for src in keys:
            if wanted is not None and self._status.get(src) not in wanted:
                continue
            record = self.get(src)
            if record is not None:
                yield record

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for status in self._status.values():
            out[status] = out.get(status, 0) + 1
        return out

    def totals(self) -> tuple[int, int]:
        count = size = 0
        for record in self.iter_records(statuses=[OK]):
            count += 1
            size += record.size
        return count, size

    def cameras(self) -> list[str]:
        return sorted({r.cam for r in self.iter_records() if r.cam})

    def cross_site_cameras(self) -> list[tuple[str, int, str]]:
        sites: dict[str, set[str]] = {}
        for record in self.iter_records():
            if record.cam and record.site:
                sites.setdefault(record.cam, set()).add(record.site)
        return [
            (cam, len(values), ",".join(sorted(values)))
            for cam, values in sorted(sites.items())
            if len(values) > 1
        ]

    def begin_run(
        self, cmd: str, sources: Sequence[str], dest: str | None, flags: dict, workers: int
    ) -> int:
        entry = {
            "cmd": cmd,
            "sources": list(sources),
            "dest": dest,
            "flags": flags,
            "workers": workers,
            "version": __version__,
            "started": time.time(),
        }
        with open(self._runs_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        return 0

    def end_run(self, run_id: int, totals: dict) -> None:
        with open(self._runs_path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"ended": time.time(), "totals": totals}, default=str) + "\n"
            )

    def runs(self) -> list[tuple]:
        return []


Store = SqliteStore  # default


def open_store(path: str | os.PathLike[str], journal: str = "sqlite"):
    """Open the state store named by ``--journal``."""
    if journal == "jsonl":
        return JsonlStore(path)
    if journal == "sqlite":
        return SqliteStore(path)
    raise ValueError(f"unknown journal type {journal!r}")


def copy_record(record: Record, **changes) -> Record:
    return replace(record, **changes)
