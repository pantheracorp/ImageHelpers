"""Entry point for the frozen build.

PyInstaller needs a real script to analyse; ``[project.scripts]`` console entry points
are a packaging-metadata concept and mean nothing to it. This module exists only so
``packaging/pids.spec`` has something to point at.

``freeze_support`` is called because the copy pipeline may spawn helper processes, and
without it a frozen Windows binary re-executes its own argv in each child instead of
starting a worker -- which shows up as the CLI mysteriously running itself several times.
"""

from __future__ import annotations

import multiprocessing
import sys


def _main() -> int:
    multiprocessing.freeze_support()
    from pids.cli import main

    main()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
