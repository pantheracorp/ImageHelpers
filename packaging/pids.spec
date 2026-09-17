# PyInstaller spec for the frozen Windows build (DESIGN.md sec 10.1).
#
#   uv run pyinstaller --clean --noconfirm packaging/pids.spec
#
# Must be run ON Windows -- PyInstaller bundles the host interpreter and cannot
# cross-compile.  Building this on macOS produces a Mach-O binary, not an .exe.
#
# One-file mode: the operator gets a single pids.exe with no Python install. It
# unpacks to a temp directory on each launch (~1-2 s), which is irrelevant against a
# multi-hour copy job.

from PyInstaller.utils.hooks import collect_submodules

# Click discovers subcommands through decorators at import time rather than through
# static imports, so nothing here is guaranteed to be picked up by the dependency
# analysis.  Collect the package explicitly.
hidden = collect_submodules("pids")

analysis = Analysis(
    ["entry.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # Only things that are certainly unused. Deliberately conservative: `email` and
    # `xml` look excludable but importlib.metadata parses metadata via email.message,
    # and over-trimming buys a few hundred KB in exchange for an ImportError that only
    # appears in the frozen build on an operator's machine.
    excludes=[
        "tkinter",
        "unittest",
        "pytest",
        "pydoc_data",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name="pids",
    console=True,
    onefile=True,
    upx=False,          # UPX-packed binaries trip AV heuristics far more often.
    strip=False,
    debug=False,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
)
