"""Sampled integrity verification.

DESIGN.md sec 5 (``verify``): size on every record -- we already have both stats -- plus
a full BLAKE2b on a random sample (default 1%) and on 100% of anything that errored or
size-mismatched.  Full hashing would roughly double total I/O on 3 TB; at 1% the extra
read is ~60 GB, about 10 minutes on an HDD.

The sample is seeded and the seed is recorded in the ``runs`` table, so a verification
is reproducible and a resumed verify checks the same files.
"""

from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from pids import copier, db
from pids.paths import long_path
from pids.progress import Progress, log


@dataclass
class VerifyResult:
    checked: int = 0
    size_ok: int = 0
    size_mismatch: int = 0
    missing: int = 0
    hashed: int = 0
    hash_mismatch: int = 0
    errors: int = 0
    elapsed: float = 0.0
    seed: str = ""
    sample: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "lock"}

    @property
    def clean(self) -> bool:
        return not (self.size_mismatch or self.hash_mismatch or self.missing or self.errors)


def in_sample(src: str, seed: str, fraction: float) -> bool:
    """Deterministic per-file sampling.

    A hash of ``seed + src`` rather than an RNG sequence, so selection does not depend
    on iteration order and a resumed verify picks exactly the same files.
    """
    if fraction <= 0:
        return False
    if fraction >= 1:
        return True
    digest = hashlib.blake2b((seed + "\x00" + src).encode("utf-8", "replace"), digest_size=8)
    value = int.from_bytes(digest.digest(), "big") / float(1 << 64)
    return value < fraction


def verify(
    store,
    sample: float = 0.01,
    seed: str | None = None,
    workers: int = 4,
    progress: bool = True,
) -> VerifyResult:
    """Verify copied files; update the state DB in place for anything wrong."""
    seed = seed or time.strftime("%Y%m%d%H%M%S")
    result = VerifyResult(seed=seed, sample=sample)
    started = time.monotonic()
    bar = Progress(label="verify", enabled=progress)
    work: queue.Queue = queue.Queue(maxsize=max(4, workers * 4))

    def worker() -> None:
        while True:
            record = work.get()
            if record is None:
                return
            try:
                _check(store, record, sample, seed, result, bar)
            except Exception as exc:  # one bad file never stops a verify
                log.warning("verify error on %s: %s", record.dest, exc)
                with result.lock:
                    result.errors += 1

    threads = [threading.Thread(target=worker, name=f"pids-v{i}") for i in range(workers)]
    for thread in threads:
        thread.start()
    try:
        # `ok` and `quarantined` rows both have real copies on disk, so both are
        # checked; `failed` rows are force-hashed so a suspect file is always verified
        # at 100%, whatever the sample rate.
        for record in store.iter_records(statuses=[db.OK, db.QUARANTINED, db.FAILED]):
            if record.dest is None:
                continue
            work.put(record)
    finally:
        for _ in threads:
            work.put(None)
        for thread in threads:
            thread.join()
        store.flush()
        bar.finish()
    result.elapsed = time.monotonic() - started
    return result


def _check(store, record, sample: float, seed: str, result: VerifyResult, bar: Progress) -> None:
    dest = record.dest
    forced = record.status == db.FAILED
    try:
        stat = os.stat(long_path(dest))
    except OSError:
        record.status = db.FAILED
        record.reason = "dest_missing"
        store.write(record)
        with result.lock:
            result.checked += 1
            result.missing += 1
        bar.update(files=1, missing=1)
        return

    with result.lock:
        result.checked += 1

    if stat.st_size != record.size:
        record.status = db.FAILED
        record.reason = f"size_mismatch: source {record.size}, dest {stat.st_size}"
        store.write(record)
        with result.lock:
            result.size_mismatch += 1
        bar.update(files=1, size_mismatch=1)
        log.error("size mismatch: %s", dest)
        return

    with result.lock:
        result.size_ok += 1

    if not (forced or in_sample(record.src, seed, sample)):
        bar.update(files=1)
        return

    src_hash = copier.hash_file(record.src)
    dest_hash = copier.hash_file(dest)
    record.hash = dest_hash
    with result.lock:
        result.hashed += 1
    if src_hash != dest_hash:
        record.status = db.FAILED
        record.reason = "hash_mismatch"
        store.write(record)
        with result.lock:
            result.hash_mismatch += 1
        bar.update(files=1, hashed=1, hash_mismatch=1)
        log.error("hash mismatch: %s", dest)
        return
    if forced:
        # A previously failed row that now verifies byte-for-byte is genuinely fine.
        record.status = db.OK
        record.reason = "revalidated"
    store.write(record)
    bar.update(files=1, hashed=1)
