# PantheraIDS Restructure — Design Document

**Status:** Draft for review
**Target scale:** ~600,000 JPEGs, ~3 TB, single run
**Replaces:** `Final_restruture_script.R` / `SD_pantheraIDS_restructure_fixed_2.R`

---

## 1. Why replace the R script

The two existing R files are the same script saved twice (the only differences are the
exiftool path, the source/output paths, `DRY_RUN`, and indentation). It works on one
camera folder and cannot survive 600k files. Four structural problems:


| Problem                                                                                        | Consequence at 600k files                                                                                                    |
| ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Whole job held in RAM (`exif_list` → `exif_data` → `good_data` → `problematic`, nothing freed) | 4 coexisting copies × 1–2 GB each → swap death. This is what crashed the machine.                                            |
| `write.csv` on the full frame                                                                  | Formats a second complete character copy of the data before writing a byte, at peak memory. The observed crash point.        |
| `print()` of every failed copy                                                                 | `file.copy(overwrite = FALSE)` returns `FALSE` for files that already exist, so any re-run prints ~600k rows to the console. |
| ~6,000 exiftool process spawns (chunk size 100)                                                | ExifTool is a Perl script; ~0.3–1 s startup each → up to an hour of pure process launch.                                     |


Plus a correctness bug that matters more than the crash: **Section 6B unconditionally
overwrites Section 6**, setting `camera_id` from `basename(SOURCE_DIR)` for every row.
Pointed at a multi-camera root, `basename()` has no trailing digits, every row becomes
`NA`, and **zero files are copied** — with only a warning.

---



## 2. Locked decisions


| Decision           | Choice                                       | Rationale                                                                                                                                             |
| ------------------ | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Language           | Python CLI. maybe 3.12                       | Streaming generators, bounded memory, resumable, no per-chunk process spawn.                                                                          |
| Copy vs move       | **Copy**, source untouched                   | Requires ~2× free space on the destination volume.                                                                                                    |
| File types         | **JPG/JPEG only**                            | Enables header-only EXIF parse; exiftool not needed at all.                                                                                           |
| Camera ID source   | Camera folder name, at any depth             | Supports both `ROOT/CAM101/*.JPG` and `ROOT/Site/CAM101/100EK113/*.JPG`.                                                                              |
| Run scope          | **One site per run**                         | Output tree is unambiguous without a site level above `Check`; enforced by a pre-flight assertion (§11.1).                                            |
| Bad/missing EXIF   | **Quarantine** to `Unsorted/`, logged        | Never files an image under a guessed date.                                                                                                            |
| Filename collision | **Keep first**, log conflict (configurable)  | Made visible instead of silent. See §7.3 for the volume warning.                                                                                      |
| Verification       | Size on all, **sampled hash** (default 1%)   | Full hashing would roughly double total I/O on 3 TB.                                                                                                  |
| Manifest           | **SQLite** state DB + **CSV** reports        | Race-proof collision detection via a UNIQUE index, O(1) resume lookups, zero-memory indexes. JSONL journal as fallback for network-share runs (§8.5). |
| Packaging          | Native install (`uv`), Docker for tests only | See §10.                                                                                                                                              |


---



## 3. Performance model

This is the part worth reading before any code. **The copy is bandwidth-bound. No
worker count fixes that.** Ranked by actual impact:

1. **Put source and destination on different physical drives.** Reading and writing
  3 TB through one USB HDD makes read/write contend for the same head — roughly halves
   effective throughput and adds heavy seeking. This single choice is worth more than
   every other optimisation combined.
2. **Read each file exactly once.** Open the file, read the first 64 KB to get
  `DateTimeOriginal`, then issue the kernel copy for the whole file. The header bytes
   are already in the page cache, so the kernel copy re-reads them from RAM at zero disk
   cost. One seek per file instead of two. (§5)
3. **Drop exiftool.** `DateTimeOriginal` lives in the JPEG APP1/TIFF IFD in the first
  few KB. Parsing it directly removes ~6,000 process spawns and the field-staff
   exiftool install.
4. **Size worker count to the device, not the CPU.** (§4)



### Projected wall-clock (3 TB / 600k files)


| Configuration                 | Bound by               | Estimate          |
| ----------------------------- | ---------------------- | ----------------- |
| USB HDD → *different* USB HDD | ~120 MB/s sequential   | **7–9 h**         |
| USB HDD → *same* HDD          | ~60 MB/s + seek thrash | 15–20 h           |
| NVMe → NVMe                   | syscall / queue depth  | 40–60 min         |
| NAS over 1 GbE                | ~110 MB/s wire         | 7–8 h             |
| NAS over 10 GbE               | array throughput       | ~1 h              |
| Current R script              | —                      | does not complete |


The header-parse pass alone (if run separately via `scan`) is seek-dominated: ~600k
seeks at ~10 ms on an HDD ≈ 1.5–2 h. That is why `run` fuses parse and copy by default.

### CPU is not the bottleneck

Parsing a TIFF IFD is a few hundred bytes of pure-Python work — microseconds per file,
~10 minutes of CPU total across 600k files. **No process pool is needed.** A single
`ThreadPoolExecutor` with a bounded queue is the entire concurrency design; threads are
correct here because the GIL is released during file I/O and inside `hashlib` for large
buffers. Multiprocessing would add pickling overhead and duplicate memory for zero gain.

---



## 4. Concurrency

```
scandir generator ──▶ bounded queue (maxsize = workers × 4) ──▶ ThreadPoolExecutor
   (one thread)          back-pressure, never a 600k list        (N threads)
                                                                      │
                                                    per-thread: open → header → copy
                                                                      │
                                                        single SQLite writer thread
                                                       (one transaction per 2k records)
```

All database writes go through **one writer thread**. SQLite allows only one writer at a
time, so funnelling writes through a single thread with batched transactions avoids
`SQLITE_BUSY` retries entirely and keeps the worker threads purely on I/O. Worker threads
read from the DB (claim/collision checks) on their own read-only connections under WAL,
which permits concurrent readers alongside the writer.

Worker count is chosen per destination volume class, overridable with `--workers`:


| Device class                   | Default workers | Why                                                                  |
| ------------------------------ | --------------- | -------------------------------------------------------------------- |
| Spinning HDD (USB or internal) | **2**           | More threads cause seek thrash and go *slower*.                      |
| SATA SSD                       | 4               |                                                                      |
| NVMe                           | 8               |                                                                      |
| Network share (SMB/NFS)        | 16              | Latency-bound; high queue depth is the win.                          |
| `auto` (default)               | probed          | Detect rotational flag where the OS exposes it, else ask/assume HDD. |


Detection is best-effort and imperfect across Windows/macOS, so `auto` prints what it  
chose and how to override it. A short `--calibrate` mode copies ~200 files at 1, 2, 4, 8 and 16 workers and reports measured MB/s, so the right value is measured rather than guessed.   

**Memory target: < 300 MB RSS regardless of file count.** Achieved by the generator +
bounded queue (never materialise the file list) and by keeping every index on disk in
SQLite rather than in Python sets. Moving to SQLite removes both the ~50 MB resume set
and the ~80 MB destination-key set the JSONL design would have needed, so memory is now
flat in the number of files: bounded by `workers × copy buffer`, not by 600k.

---



## 5. Pipeline



### `scan` — plan only, no writes (replaces DRY_RUN)

Walks the source, parses headers, populates the state DB with planned rows (`status = planned`) and writes the preview CSVs. Gives an accurate count, the camera×date summary,
and the conflict/quarantine list **before** touching 3 TB. A later `run` against the
same state DB reuses those rows instead of re-parsing headers.

### `run` — fused single pass (default)

Per file, in one worker thread:

1. `open()` the source.
2. Read first 64 KB → locate `APP1`/`Exif\0\0` → parse TIFF IFD → `DateTimeOriginal`
  (tag `0x9003`), falling back to `SubSecDateTimeOriginal`/`DateTimeDigitized`.
   If the tag is absent, unparseable, or `0000:00:00` → **quarantine** (§7.2).
3. Resolve `camera_id` from the path (§6). Unresolvable → quarantine.
4. Compute `dest = DEST/Check/<CAxxx>/<MMDDYY>/<filename>`.
5. `mkdir` the destination directory — guarded by an in-process set of already-created
  directories, so this is ~a few thousand syscalls, not 600k.
6. **Claim the destination** — `INSERT` the destination key into `files` against a
  `UNIQUE` index. Success means this file owns that path. `IntegrityError` means
   another file already claimed it → apply `--on-collision`. This is what makes
   collision handling exact rather than best-effort: the database, not a Python set,
   arbitrates, so two worker threads cannot both believe they won. Claims are issued in
   batches through the writer thread (one transaction per batch), so the added latency
   is a few ms against a ~40 ms copy.
7. Kernel copy of the whole file: `CopyFileW` via ctypes on Windows,
  `fcopyfile` on macOS, `os.sendfile` on Linux. Preserve mtime (`copystat`) — the R
   version silently dropped timestamps.
8. `stat` the destination; size mismatch → immediate full hash of both sides and a
  hard failure record.
9. Update the row's `status` (batched through the writer thread).

Steps 2 and 7 share a single open handle and a single disk seek.

### `verify` — sampled integrity (post-run, re-runnable)

Size on every record (already have both stats). Full BLAKE2b on a random sample
(`--sample 0.01`, seeded and recorded so it's reproducible), plus 100% of anything that
errored or size-mismatched. At 1%: ~6,000 files × 5 MB × 2 sides ≈ 60 GB extra read,
~10 min on an HDD.

### `report` — CSVs from the state DB

Each report is a single SQL query streamed straight to a CSV writer, one row at a time —
nothing is ever loaded into memory (§8.3). `qc_summary.csv`, which the R script produced
with a `group_by`/`summarise` over the full in-memory frame, becomes a
`GROUP BY cam, date` that the database answers from an index. This is also the QC step:
unlike the R version, it never re-walks the destination tree (which it did four separate
times, at peak memory).

---



## 6. Camera ID and date resolution

Handles both confirmed layouts with one rule, replacing the dead Section 6 / broken 6B pair.

**Camera folder:** walk the image's path components from the *deepest* upward and take
the first that matches `^(?:CAM|CA|CT|C)[\s_-]*(\d+)` (case-insensitive), after
discarding DCIM-style auto-numbered folders (`^\d{3}[A-Z0-9_]+$` — `100EK113`,
`100MEDIA`, `101RECNX`). Deepest-first means `Site/CAM101/100EK113/img.jpg` and
`ROOT/CAM101/img.jpg` both yield `101`, and a site folder that happens to contain digits
can't win over the real camera folder.

- Trailing notes survive: `CAM105 (Stolen)` → `105`, because the match anchors at the start.
- Output ID is `CA` + the digits **as found** (`CAM05` → `CA05`, `CAM5` → `CA5`).
The R header comment promised zero-padding but the code never did it. If both `CA5`
and `CA05` appear in one run, that's a hard pre-flight error, not a silent merge. See §11.
- The component *above* the camera folder is recorded as `site` in the manifest even
though it is not part of the output path. With one site per run this is what the
pre-flight assertion checks against (§11.1), and it keeps provenance in the reports.

**Date:** `DateTimeOriginal` → `MMDDYY`. Parsed once with a direct string slice, not
600k individual `as.POSIXct` calls. No timezone conversion is applied: EXIF
`DateTimeOriginal` is already local camera time, and converting it would shift images
across date boundaries.

**Windows specifics:** `\\?\` long-path prefix (paths over 260 chars are a real risk
with `Site/Camera/Subfolder/` nesting), and folder names with trailing dots or spaces
are normalised before use.

---



## 7. Edge cases



### 7.1 Path handling

Destination keys are compared case-insensitively on Windows and macOS so that
`IMG_1.JPG` and `img_1.jpg` are correctly treated as colliding. Source path prefix
stripping uses real path operations, not `str_replace` on a string — the R version
silently produced a drive letter as the camera folder if `SOURCE_DIR` had a trailing
slash or differing case.

### 7.2 Quarantine

Bad or missing EXIF → copied to `DEST/Unsorted/<path relative to source root>`,
preserving the original structure so provenance is obvious, and recorded in
`unsorted.csv` with the specific reason (`no_exif`, `zero_date`, `unparseable`,
`no_camera_id`, `truncated_header`). Nothing is filed on a guessed date; nothing is
left behind unrecorded.

### 7.3 Collisions — read this

Default is your choice, **keep-first**: the first file wins, later ones are copied
nowhere and recorded in `conflicts.csv`.

Be aware of the expected volume. Reconyx/Bushnell cameras restart numbering at
`IMG_0001.JPG` inside every `100EK113`-style subfolder, so within a single
camera + date, distinct images sharing a filename are likely **common**, not rare.
Under keep-first those images never reach the output tree. Nothing is lost (the source
is intact), but the tree is incomplete and `dest_count` will legitimately be far below
`source_count`.

`scan` reports the exact collision count before any copying, so this is a decision you
can make on real numbers. The flag supports:

- `keep-first` *(default)* — first wins, rest logged.
- `hash-dedupe` — hash both; identical bytes → single copy logged as duplicate;
different bytes → suffix `_001`. Lossless and non-inflating. Costs a hash only on
collisions.
- `suffix` — always `_001`, `_002`. Lossless but duplicates true duplicates.



### 7.4 Interrupted writes

Copy to `<dest>.part` then atomic `os.replace`. A killed process can never leave a
half-written file that a later run mistakes for complete.

### 7.5 Disk full

Pre-flight compares total source bytes against destination free space and refuses to
start if short. Mid-run `ENOSPC` stops cleanly with the manifest intact and resumable.

---



## 8. Manifest, reports and resume



### 8.1 State database

A single file, `state/pids.sqlite`, in WAL mode.

```sql
CREATE TABLE files (
  src        TEXT PRIMARY KEY,   -- absolute source path
  size       INTEGER NOT NULL,
  mtime      REAL    NOT NULL,
  dt         TEXT,               -- raw EXIF DateTimeOriginal, NULL if unreadable
  site       TEXT,               -- component above the camera folder (§6)
  cam        TEXT,               -- CA116
  date       TEXT,               -- 071424
  dest       TEXT,               -- NULL for quarantined/conflict rows
  dest_key   TEXT,               -- case-folded dest, for collision arbitration
  status     TEXT NOT NULL,      -- planned|ok|conflict|quarantined|failed
  reason     TEXT,               -- no_exif | zero_date | no_camera_id | OS error
  conflict_with TEXT,            -- src that won the destination
  hash       TEXT,               -- populated only for sampled/suspect files
  ts         REAL NOT NULL
);
CREATE UNIQUE INDEX files_dest_key ON files(dest_key);   -- arbitrates collisions
CREATE INDEX files_status         ON files(status);      -- resume + failure re-runs
CREATE INDEX files_cam_date       ON files(cam, date);   -- qc_summary in one query
```

`PRAGMA journal_mode=WAL; synchronous=NORMAL` — WAL gives concurrent readers alongside
the single writer; `NORMAL` is durable across a process kill (which is the failure mode
that matters here) without an fsync per transaction. Batched at 2,000 rows per
transaction, the DB is a negligible fraction of runtime. Final size for 600k rows is
roughly 250–350 MB, dominated by the path strings.

A `runs` table records each invocation (paths, flags, worker count, tool version,
start/end, totals) so a multi-day, multi-resume job has a single auditable history.

### 8.2 What SQLite buys over the JSONL design

- **Collision detection is exact and race-proof** — a `UNIQUE` index instead of a
600k-entry Python set that two threads could both read as empty.
- **Memory is flat in file count** — no resume set, no destination-key set (§4).
- **Resume is an indexed lookup**, not a startup scan of the whole log.
- **Reports are SQL**, so `qc_summary` and cross-site collision detection (§11) are
queries rather than passes over the data.
- **Targeted re-runs** — `WHERE status='failed'` re-runs only the failures, without
re-walking 3 TB.



### 8.3 Operator CSVs (written by `report`, streamed from SQL)

- `restructuring_log.csv` — full src → dest mapping (same shape as the R script's output).
- `conflicts.csv` — filename collisions and how each was resolved.
- `unsorted.csv` — quarantined files with reasons.
- `qc_summary.csv` — camera × date × image count.
- `failures.csv` — errors with the OS error, for re-run targeting.

CSV remains the operator-facing format, as you wanted — the DB is the internal state
store, not something anyone is expected to open by hand.

### 8.4 Resume

`run --resume` skips any source path already present with `status='ok'` — an index
lookup, not a scan. Resuming is idempotent: an already-complete file is skipped by
database lookup, not by an `overwrite=FALSE` copy attempt that then reports itself as a
failure (the bug that made the R script's re-runs print 600k "failures").

Crash safety comes from the combination of WAL and the `.part`-then-`os.replace` copy
(§7.4): a killed process loses at most the last uncommitted batch, and those files are
simply recopied. A file can never be recorded `ok` while its destination is half-written.

### 8.5 Network-share caveat

**SQLite must not live on an SMB/NFS share** — file locking over network filesystems is
unreliable and can corrupt the database. When source or destination is a network share,
the state DB still goes on local disk (the default is alongside the tool, not the data).
If a workflow genuinely requires the state file on a share, `--journal jsonl` writes an
append-only JSONL log instead, which is safe to append over SMB at the cost of the
in-memory indexes and a startup scan. This is a fallback, not the default.

### 8.6 Progress output

One rewriting status line: files/s, MB/s, % done, ETA, and **current destination
throughput** so you can see whether the disk is saturated (and therefore whether more
workers would help). Full detail goes to a log file. Never 600k lines to a console —
that alone killed the R version.

---



## 9. CLI

```
pids scan    --source SRC [--source SRC2 ...] [--state pids.sqlite] [--workers auto]
pids run     --source SRC --dest DST [--state pids.sqlite]
             [--workers auto|N] [--on-collision keep-first|hash-dedupe|suffix]
             [--resume] [--retry-failed] [--verify-sample 0.01]
             [--limit N] [--dry-run] [--journal sqlite|jsonl] [--allow-multi-site]
pids verify  [--state pids.sqlite] [--sample 0.01]
pids report  [--state pids.sqlite] --out reports/
pids calibrate --source SRC --dest DST
```

- `--limit N` processes only the first N files — for a 5,000-image trial run before
committing 9 hours.
- `--retry-failed` re-runs only `status='failed'` rows, so a transient USB disconnect
costs minutes rather than a full re-walk.
- `--state` defaults to a local path even when the data is on a network share (§8.5).
- `--allow-multi-site` overrides the single-site pre-flight assertion (§11.1). Needed
  only when the component above the camera folders isn't actually a site.

---



## 10. Packaging and deployment

**Recommendation: native install, not Docker.** This workload is 600k small-file
syscalls, which is the one case where containers are genuinely bad: Docker Desktop bind
mounts cost 5–20× on Mac (VirtioFS/gRPC-FUSE) and Windows (WSL2 9p/virtiofs). On Windows
it's worse — passing an external USB drive through to the WSL2 VM is fiddly and
sometimes doesn't work at all. Turning a 9-hour job into a 30-hour job to gain
reproducibility is the wrong trade.

Instead:

- **Primary, both platforms:** a `uv`-managed project — `uv tool install pids`, then
  `pids run ...`. One command, identical on Mac and Windows, pinned dependencies, and
  updates are a single command rather than redistributing a binary.
  *Note: the Mac here has Python 3.8; the tool targets 3.12, which* `uv` *installs itself.*
- **Fallback for locked-down Windows machines:** a frozen `.exe` (§10.1).
- **Docker:** provided for CI and reproducible testing against synthetic trees only,
clearly marked as not for production runs.

Dependencies are deliberately minimal — the standard library plus `click` for the CLI.
`sqlite3`, `csv`, `json`, `hashlib` and `concurrent.futures` are all stdlib, so choosing
SQLite adds nothing to install. No Pillow, no exiftool, no pandas. Fewer moving parts on
a field laptop, and nothing that pulls the whole dataset into memory.

### 10.1 Building the Windows `.exe`

**PyInstaller cannot cross-compile.** It bundles the host platform's CPython interpreter
and compiled extension modules, so running it on macOS yields a Mach-O binary and
nothing else. The same is true of cx_Freeze, py2exe and Nuitka. A Windows `.exe` must be
built on Windows.

Build it in **CI on a `windows-latest` GitHub Actions runner**, triggered on tag, with
the binary attached to the release — implemented in `.github/workflows/release.yml`,
using `packaging/pids.spec` and `packaging/entry.py`. That needs no Windows machine and doesn't depend on
the state of whoever's laptop did the build. Building on one of the field Windows PCs
works for a one-off but isn't reproducible. A Windows VM on Apple Silicon has a specific
trap: ARM Windows produces an **ARM64** binary by default, which will not run on x64
field laptops — it needs x64 Python under emulation.

Two operational caveats that argue for `uv` as the default:

- **Unsigned one-file exes are routinely flagged** by SmartScreen and corporate AV.
  "Windows protected your PC", or outright quarantine, is a support call. A code-signing
  certificate resolves it at an annual cost.
- A one-file exe unpacks to a temp directory on each launch (~1–2 s). Irrelevant for a
  9-hour job, but it also means the AV scans the unpacked payload every run.

---



## 11. Open decisions

1. ~~**Cross-site camera collisions.**~~ **Resolved: one site per run.** The output
   tree is therefore unambiguous — every `Check/CAxxx/` belongs to a single site — and
   no site level above `Check` is needed. Two consequences, both now designed in:
   - The pre-flight **asserts** a single site rather than reporting on it. If `scan`
     finds more than one distinct site component under the source root, it **stops**
     with the list of sites found. This is a guardrail, not an inconvenience: pointing
     the tool one level too high is the exact mistake that would silently merge two
     sites into one camera folder. Overridable with `--allow-multi-site` for the case
     where the extra component isn't really a site.
   - Each run gets its own state DB and its own `reports/` directory, keyed by site, so
     per-site QC stays separate and a re-run of one site can't disturb another.
2. **Zero-padding.** Should `CA5` and `CA05` be the same camera? Proposal: treat a run
  containing both as a hard pre-flight error rather than guessing.
3. **Bad-EXIF fraction.** Unknown. If quarantine turns out to catch a large share,
  revisit the mtime-fallback option.
4. ~~**Multiple source roots in one run.**~~ **Resolved: one site per run**, so the
   multi-`--source` form is only for a single site split across several drives. Kept for
   that case; the single-site assertion in (1) still applies across all roots given.

---



## 12. Build plan


| #   | Deliverable                                                                               | Validates                                                                     |
| --- | ----------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| 1   | Header EXIF parser + unit tests                                                           | Correct dates, including corrupt/truncated/`0000:00:00` files                 |
| 2   | Synthetic tree generator (600k tiny JPEGs, crafted EXIF, both layouts, seeded collisions) | Scale testing without 3 TB                                                    |
| 3   | SQLite schema + `scan` + camera/date resolver + CSV reports                               | Resolution rules against both real layouts; collision and quarantine counts   |
| 4   | `run` fused copy, single-threaded                                                         | Correctness before concurrency                                                |
| 5   | Threaded executor + bounded queue + `calibrate`                                           | Bounded memory under load; measured worker tuning                             |
| 6   | Single writer thread, `--resume`/`--retry-failed`, crash injection (`kill -9` mid-run)    | Resume is exact and idempotent; collision arbitration holds under concurrency |
| 7   | `verify` + `report`                                                                       | Sampled integrity; QC parity with the R script's output                       |
| 8   | Real trial: `--limit 5000` on one camera, then one full drive                             | Real-world throughput and edge cases                                          |
| 9   | **Done** — `uv tool install`, `README.md` runbook, `ci.yml`, `release.yml` exe build (§10.1) | Field-staff install on both platforms; exe built on Windows, never cross-compiled |


We will trial on a windows using the frozen uv+ exe

