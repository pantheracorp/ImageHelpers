# pids — PantheraIDS image restructuring

Restructures camera-trap images into the PantheraIDS layout:

```
DEST/Check/<CameraID>/<MMDDYY>/<image>.JPG
```

Built for one site at a time at a scale of ~600,000 images / ~3 TB per run: streaming,
resumable, bounded memory, and no ExifTool dependency. See [DESIGN.md](DESIGN.md) for
the reasoning behind every choice here.

- **Source is never modified.** Files are copied, not moved.
- **Memory is flat** in the number of images — it does not grow with the job.
- **Resumable.** A crash, a power cut or an unplugged drive costs you the last batch,
  not the run.
- **Nothing is filed on a guessed date.** Images with missing or corrupt EXIF go to
  `Unsorted/` and are listed in a CSV.

---

## Install on Windows

**1. Install `uv`** (a single self-contained binary; no admin rights needed):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Or, if your machine has winget: `winget install --id=astral-sh.uv -e`

If policy blocks the install script, download `uv.exe` from
<https://github.com/astral-sh/uv/releases>, drop it in a folder, and call it by path.

**2. Install `pids`:**

```powershell
uv tool install git+https://github.com/pantheracorp/ImageHelpers
```

**3. Open a new terminal.** PATH changes do not apply to a shell that is already
running — this is the single most common "command not found" cause.

**4. Check it works:**

```powershell
pids --version
pids --help
```

You do **not** need to install Python first. `uv` fetches the Python 3.12 this tool
needs and keeps it in its own isolated environment.

### Install on macOS / Linux

Identical, with a different first line:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/pantheracorp/ImageHelpers
```

### Keeping it up to date

| Task | Command |
|---|---|
| Upgrade | `uv tool upgrade pids` |
| Force reinstall from git | `uv tool install --force git+https://github.com/pantheracorp/ImageHelpers` |
| List installed tools | `uv tool list` |
| Uninstall | `uv tool uninstall pids` |
| Command not found after install | `uv tool update-shell`, then open a new terminal |

### Run once without installing

```powershell
uvx --from git+https://github.com/pantheracorp/ImageHelpers pids --help
```

---

## Running a site

Work through these in order. **Do not skip step 2** — it is what tells you whether the
numbers are sane before you commit several hours of copying.

### 1. Check the camera folders were understood

Reads directory names only, no image files, so it returns in seconds:

```powershell
pids cameras --source "E:\Raw\Site1"
```

Confirm the camera list matches your field records. This is also where zero-padding
conflicts (`CA5` vs `CA05`) and cross-site cameras surface.

### 2. Plan the run — nothing is copied

```powershell
pids scan --source "E:\Raw\Site1" `
          --state C:\pids\site1.sqlite `
          --out   C:\pids\site1-preview
```

This replaces the old R script's `DRY_RUN`. Read the summary before going further:

- **quarantined** — images with no usable EXIF date. Expect a few; investigate a lot.
- **conflicts** — two different images wanting the same destination filename. With
  `100EK113`-style camera subfolders this is often a large number. Read
  [Collisions](#collisions) before you accept it.
- **QC block** — per-camera and per-date counts to cross-check against field sheets.

### 3. Trial run on a small slice

```powershell
pids run --source "E:\Raw\Site1" --dest "F:\PantheraIDS" `
         --state C:\pids\site1.sqlite --limit 5000
```

Then look at `F:\PantheraIDS\Check\` and confirm the structure is what PantheraIDS
expects before committing to the full run.

### 4. Full run

```powershell
pids run --source "E:\Raw\Site1" --dest "F:\PantheraIDS" `
         --state C:\pids\site1.sqlite `
         --resume `
         --verify-sample 0.01 `
         --out C:\pids\site1-reports
```

`--resume` makes this safe to re-issue: anything already copied is skipped by a database
lookup, so re-running after an interruption costs nothing.

### 5. If it was interrupted

Exactly the same command again. It picks up where it stopped. To re-try only the files
that errored (e.g. after a USB disconnect):

```powershell
pids run --source "E:\Raw\Site1" --dest "F:\PantheraIDS" `
         --state C:\pids\site1.sqlite --retry-failed
```

### 6. Reports

Written automatically with `--out`, or regenerated any time from the state DB:

```powershell
pids report --state C:\pids\site1.sqlite --out C:\pids\site1-reports
```

| File | Contents |
|---|---|
| `restructuring_log.csv` | Every source → destination mapping |
| `qc_summary.csv` | Camera × date × image count |
| `qc_checks.csv` | Pass/fail checks with explanations |
| `unsorted.csv` | Quarantined images and why |
| `conflicts.csv` | Filename collisions and how each was resolved |
| `failures.csv` | Errors, for targeted re-runs |

---

## Things worth knowing before a big run

### Put source and destination on different physical drives

This matters more than every other setting combined. Reading and writing 3 TB through
one USB HDD makes the head contend with itself: roughly half the throughput and a lot of
seeking. Different drives ≈ 7–9 h; same drive ≈ 15–20 h.

### Keep `--state` on the local disk

The default is `state\pids.sqlite` relative to where you run the command. Point it at
`C:\pids\` as shown above so the manifest survives a data drive being unplugged.

**The tool refuses to put the state DB on a network share.** SQLite locking over SMB/NFS
is unreliable and can corrupt the database. If the state file genuinely has to live on a
share, `--journal jsonl` switches to an append-only log that is safe there.

### Workers

`--workers auto` probes the destination device and picks a count. More is not better: on
a spinning USB HDD the best value is usually **2**, and raising it makes the job slower
through seek thrash. To measure rather than guess on your actual hardware:

```powershell
pids calibrate --source "E:\Raw\Site1" --dest "F:\PantheraIDS"
```

### Collisions

Camera-trap firmware restarts filenames at `IMG_0001.JPG` inside every `100EK113`-style
subfolder, so two *different* images can want the same destination name.

The default is `--on-collision keep-first`: the first wins, the rest are recorded in
`conflicts.csv` **and not copied**. Nothing is lost — the source is untouched — but the
output tree is incomplete, and `scan` will show a large conflict count. The alternatives:

| Policy | Behaviour |
|---|---|
| `keep-first` *(default)* | First wins; rest logged, not copied |
| `hash-dedupe` | Identical bytes → one copy; genuinely different → suffixed `_001` |
| `suffix` | Always suffix; lossless, but true duplicates are copied twice |

If `scan` reports a lot of conflicts, `hash-dedupe` is usually what you actually want.

### One site per run

Each run handles a single site, so `Check/CAxxx/` is unambiguous. If more than one site
is found under the source root the run stops and lists them — that usually means the
`--source` path is one level too high. `--allow-cross-site-merge` overrides it when the
extra folder level is not really a site.

---

## Development

```bash
git clone https://github.com/pantheracorp/ImageHelpers
cd ImageHelpers
uv sync --extra dev     # creates .venv, fetches Python 3.12
uv run pytest           # unit tests
uv run pytest -m slow   # plus scale and crash-injection tests
uv run pids --help      # run against the working copy
```

Generate a test tree without needing real images:

```bash
uv run python tools/make_tree.py --out /tmp/fake --cameras 8 --per-camera 500
```

### Building the Windows `.exe`

PyInstaller cannot cross-compile — a Windows binary must be built on Windows. Pushing a
`v*` tag runs `.github/workflows/release.yml` on a `windows-latest` runner and attaches
`pids.exe` to the release. To build locally on a Windows machine:

```powershell
uv sync --extra freeze
uv run pyinstaller packaging/pids.spec
```

The result is unsigned, so SmartScreen and corporate AV will often flag it. `uv tool
install` is the recommended path wherever you can install software; the `.exe` is for
locked-down machines only.

## License

MIT — see [LICENSE](LICENSE).
