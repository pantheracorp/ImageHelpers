"""Sampled integrity verification (deliverable 7, DESIGN.md sec 5)."""

from __future__ import annotations

import os

import pytest

from pids import db, verify
from pids.pipeline import Options, run_pipeline


@pytest.fixture
def finished(tree, dest, state):
    options = Options(
        sources=(str(tree)), dest=str(dest), state=str(state), workers=2, progress=False
    )
    options.sources = (str(tree),)
    run_pipeline(options, cmd="run")
    return state, dest


def test_clean_run_verifies(finished):
    state, _dest = finished
    with db.open_store(str(state), "sqlite") as store:
        result = verify.verify(store, sample=1.0, seed="t", workers=2, progress=False)
    assert result.clean
    assert result.checked == 7  # 4 copied + 3 quarantined copies
    assert result.hashed == 7
    assert result.size_mismatch == 0


def test_sampling_is_a_fraction_not_everything(finished):
    state, _dest = finished
    with db.open_store(str(state), "sqlite") as store:
        result = verify.verify(store, sample=0.0, seed="t", workers=1, progress=False)
    assert result.checked == 7
    assert result.hashed == 0
    assert result.size_ok == 7


def test_sampling_is_reproducible_for_a_seed():
    srcs = [f"/src/{index}.jpg" for index in range(2000)]
    first = [s for s in srcs if verify.in_sample(s, "seed-a", 0.1)]
    second = [s for s in srcs if verify.in_sample(s, "seed-a", 0.1)]
    third = [s for s in srcs if verify.in_sample(s, "seed-b", 0.1)]
    assert first == second, "same seed must pick the same files"
    assert first != third, "a different seed must pick different files"
    assert 0.05 < len(first) / len(srcs) < 0.15, "roughly the requested fraction"


def test_a_truncated_destination_is_caught_by_size(finished):
    state, dest = finished
    victim = dest / "Check" / "CA101" / "071424" / "IMG_0001.JPG"
    victim.write_bytes(b"short")
    with db.open_store(str(state), "sqlite") as store:
        result = verify.verify(store, sample=0.0, seed="t", workers=1, progress=False)
        assert result.size_mismatch == 1
        assert not result.clean
        record = store.get(str(dest).replace("dest", "src"))  # unrelated lookup is None
    assert record is None


def test_a_silently_altered_destination_is_caught_by_hash(finished):
    """Same size, different bytes: only hashing finds this."""
    state, dest = finished
    victim = dest / "Check" / "CA101" / "071424" / "IMG_0001.JPG"
    data = bytearray(victim.read_bytes())
    data[-10] ^= 0xFF
    victim.write_bytes(bytes(data))
    with db.open_store(str(state), "sqlite") as store:
        result = verify.verify(store, sample=1.0, seed="t", workers=1, progress=False)
    assert result.hash_mismatch == 1
    assert not result.clean


def test_a_missing_destination_is_reported(finished):
    state, dest = finished
    os.remove(dest / "Check" / "CA101" / "071424" / "IMG_0001.JPG")
    with db.open_store(str(state), "sqlite") as store:
        result = verify.verify(store, sample=0.0, seed="t", workers=1, progress=False)
        assert result.missing == 1
        failures = list(store.iter_records(statuses=[db.FAILED]))
    assert [r.reason for r in failures] == ["dest_missing"]


def test_failed_rows_are_always_hashed_whatever_the_sample(finished):
    state, _dest = finished
    with db.open_store(str(state), "sqlite") as store:
        record = next(store.iter_records(statuses=[db.OK]))
        record.status = db.FAILED
        record.reason = "injected"
        store.write(record)
        store.flush()
        result = verify.verify(store, sample=0.0, seed="t", workers=1, progress=False)
        assert result.hashed == 1
        # A row that now verifies byte-for-byte is genuinely fine again.
        assert store.get(record.src).status == db.OK
    assert result.clean
