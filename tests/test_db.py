"""State store: race-proof collision arbitration, resume lookups, run history.

DESIGN.md sec 8.1/8.2.  The point of moving to SQLite was that a 600k-entry Python set
could be read as empty by two threads at once; these tests are the proof.
"""

from __future__ import annotations

import threading

import pytest

from pids import db
from pids.db import Record


def record(src: str, dest: str = "/dest/Check/CA101/071424/IMG_1.JPG", **kwargs) -> Record:
    return Record(src=src, size=100, mtime=1.0, dest=dest, dest_key=dest.casefold(), **kwargs)


@pytest.fixture(params=["sqlite", "jsonl"])
def store(request, tmp_path):
    """Every guarantee is tested against both journals, so the fallback is real."""
    suffix = ".sqlite" if request.param == "sqlite" else ".jsonl"
    with db.open_store(str(tmp_path / ("state" + suffix)), request.param) as opened:
        yield opened


def test_first_claim_wins_second_loses(store):
    assert store.claim(record("/src/a.jpg")).won
    result = store.claim(record("/src/b.jpg"))
    assert not result.won
    assert result.winner == "/src/a.jpg"


def test_reclaiming_the_same_destination_from_the_same_source_is_allowed(store):
    """A `run` after a `scan` re-claims its own planned destination."""
    assert store.claim(record("/src/a.jpg")).won
    assert store.claim(record("/src/a.jpg")).won


def test_distinct_destinations_both_win(store):
    assert store.claim(record("/src/a.jpg", "/dest/1.jpg")).won
    assert store.claim(record("/src/b.jpg", "/dest/2.jpg")).won


def test_exactly_one_thread_wins_a_contested_destination(store):
    """32 threads, one destination: precisely one claim may succeed."""
    winners: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(32)

    def contend(index: int) -> None:
        barrier.wait()
        if store.claim(record(f"/src/{index}.jpg")).won:
            with lock:
                winners.append(f"/src/{index}.jpg")

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1


def test_status_lookup_is_by_source_path(store):
    store.write(record("/src/a.jpg", status=db.OK))
    store.flush()
    assert store.status_of("/src/a.jpg") == db.OK
    assert store.status_of("/src/missing.jpg") is None


def test_write_upserts_rather_than_duplicating(store):
    store.write(record("/src/a.jpg", status=db.CLAIMED))
    store.write(record("/src/a.jpg", status=db.OK))
    store.flush()
    assert store.counts() == {db.OK: 1}


def test_records_round_trip(store):
    store.write(
        record("/src/a.jpg", status=db.OK, cam="CA101", date="071424", site="NorthRange", dt="2024:07:14 06:31:02")
    )
    store.flush()
    got = store.get("/src/a.jpg")
    assert got is not None
    assert (got.cam, got.date, got.site, got.dt) == (
        "CA101",
        "071424",
        "NorthRange",
        "2024:07:14 06:31:02",
    )


def test_iter_records_filters_by_status(store):
    store.write(record("/src/a.jpg", "/dest/1.jpg", status=db.OK))
    store.write(record("/src/b.jpg", "/dest/2.jpg", status=db.FAILED))
    store.flush()
    assert [r.src for r in store.iter_records(statuses=[db.FAILED])] == ["/src/b.jpg"]


def test_cross_site_cameras_is_detected(store):
    store.write(record("/src/a.jpg", "/dest/1.jpg", status=db.OK, cam="CA01", site="North"))
    store.write(record("/src/b.jpg", "/dest/2.jpg", status=db.OK, cam="CA01", site="South"))
    store.write(record("/src/c.jpg", "/dest/3.jpg", status=db.OK, cam="CA02", site="North"))
    store.flush()
    cross = store.cross_site_cameras()
    assert [row[0] for row in cross] == ["CA01"]
    assert row_sites(cross[0][2]) == {"North", "South"}


def row_sites(value: str) -> set[str]:
    return set(value.split(","))


def test_run_history_is_recorded(tmp_path):
    with db.open_store(str(tmp_path / "s.sqlite"), "sqlite") as store:
        run_id = store.begin_run("run", ["/src"], "/dest", {"resume": True}, 4)
        store.end_run(run_id, {"copied": 7})
        runs = store.runs()
    assert len(runs) == 1
    assert runs[0][1] == "run" and runs[0][2] == 4


def test_unique_index_exists_as_a_backstop(tmp_path):
    """Even if the writer's select-then-insert were wrong, the index would catch it."""
    import sqlite3

    path = str(tmp_path / "s.sqlite")
    with db.open_store(path, "sqlite") as store:
        store.write(record("/src/a.jpg"))
        store.flush()
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO files (src,size,mtime,dest_key,status,ts) VALUES (?,?,?,?,?,?)",
            ("/src/other.jpg", 1, 1.0, "/dest/check/ca101/071424/img_1.jpg", "ok", 1.0),
        )
    conn.close()


def test_jsonl_survives_a_torn_final_line(tmp_path):
    """A killed process can leave half a line; the next run must still load."""
    path = tmp_path / "state.jsonl"
    with db.open_store(str(path), "jsonl") as store:
        store.write(record("/src/a.jpg", status=db.OK))
    with open(path, "ab") as fh:
        fh.write(b'{"src": "/src/b.jpg", "siz')
    with db.open_store(str(path), "jsonl") as store:
        assert store.status_of("/src/a.jpg") == db.OK
        assert store.status_of("/src/b.jpg") is None


def test_jsonl_reload_keeps_destination_ownership(tmp_path):
    path = tmp_path / "state.jsonl"
    with db.open_store(str(path), "jsonl") as store:
        assert store.claim(record("/src/a.jpg")).won
    with db.open_store(str(path), "jsonl") as store:
        result = store.claim(record("/src/b.jpg"))
        assert not result.won and result.winner == "/src/a.jpg"
