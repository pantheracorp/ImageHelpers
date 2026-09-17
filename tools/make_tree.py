#!/usr/bin/env python3
"""Generate a synthetic camera-trap tree for scale and correctness testing.

DESIGN.md sec 12 deliverable 2: 600k tiny JPEGs with crafted EXIF, both confirmed
layouts, and seeded collisions -- so the pipeline can be tested at scale without 3 TB
of real data.

    # a quick correctness tree
    python tools/make_tree.py --out /tmp/tree --cameras 6 --per-camera 40

    # the scale test (600k files at 4 KB each is ~2.4 GB on disk)
    python tools/make_tree.py --out /tmp/big --cameras 120 --per-camera 5000 --size 4096

Layouts produced:
    flat    ROOT/CAM101/IMG_0001.JPG
    nested  ROOT/Site/CAM101/100EK113/IMG_0001.JPG
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pids import synth  # noqa: E402

SITES = ("NorthRange", "SouthRange", "RiverBlock", "Escarpment")


def build(
    out: str,
    cameras: int,
    per_camera: int,
    size: int,
    bad_exif: float,
    collisions: float,
    layout: str,
    seed: int,
    days: int,
    subfolders: int,
    quiet: bool = False,
) -> dict:
    rng = random.Random(seed)
    stats = {"files": 0, "bytes": 0, "bad_exif": 0, "collisions": 0, "cameras": cameras}
    started = time.monotonic()

    for index in range(cameras):
        camera_number = 101 + index
        prefix = rng.choice(("CAM", "CA", "CT")) if layout == "mixed" else "CAM"
        camera_folder = f"{prefix}{camera_number}"
        if index % 17 == 16:
            camera_folder += " (Stolen)"  # trailing field notes must survive

        nested = layout == "nested" or (layout == "mixed" and index % 2 == 0)
        if nested:
            site = SITES[index % len(SITES)]
            base = os.path.join(out, site, camera_folder)
        else:
            base = os.path.join(out, camera_folder)

        for file_index in range(per_camera):
            day = 14 + (file_index * days) // max(1, per_camera)
            dt = f"2024:07:{day:02d} 0{file_index % 9}:31:02"
            # Collisions come from DCIM subfolder numbering restarting at IMG_0001.
            subfolder_index = file_index % subfolders if nested else 0
            colliding = rng.random() < collisions and nested
            name_index = (file_index % 50) + 1 if colliding else file_index + 1
            directory = (
                os.path.join(base, f"{100 + subfolder_index}EK113") if nested else base
            )
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"IMG_{name_index:04d}.JPG")
            if colliding and os.path.exists(path):
                stats["collisions"] += 1
                path = os.path.join(directory, f"IMG_{name_index:04d}_dup{file_index}.JPG")

            roll = rng.random()
            if roll < bad_exif / 3:
                payload = synth.no_exif_jpeg(size=size)
                stats["bad_exif"] += 1
            elif roll < 2 * bad_exif / 3:
                payload = synth.zero_date_jpeg(size=size)
                stats["bad_exif"] += 1
            elif roll < bad_exif:
                payload = synth.jpeg(dt, size=size, truncate_to=200)
                stats["bad_exif"] += 1
            else:
                payload = synth.jpeg(
                    dt,
                    size=size,
                    order="MM" if file_index % 13 == 0 else "II",
                    with_xmp=file_index % 7 == 0,
                )
            with open(path, "wb") as fh:
                fh.write(payload)
            stats["files"] += 1
            stats["bytes"] += len(payload)
            if not quiet and stats["files"] % 20000 == 0:
                rate = stats["files"] / (time.monotonic() - started)
                print(f"  {stats['files']:,} files ({rate:,.0f}/s)", flush=True)

    # A file with no resolvable camera folder, so the quarantine path is exercised.
    stray = os.path.join(out, "Loose Images")
    os.makedirs(stray, exist_ok=True)
    with open(os.path.join(stray, "IMG_9999.JPG"), "wb") as fh:
        fh.write(synth.jpeg(size=size))
    stats["files"] += 1

    stats["seconds"] = round(time.monotonic() - started, 2)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cameras", type=int, default=8)
    parser.add_argument("--per-camera", type=int, default=50)
    parser.add_argument("--size", type=int, default=2048, help="Bytes per JPEG.")
    parser.add_argument("--bad-exif", type=float, default=0.02, help="Fraction with bad EXIF.")
    parser.add_argument("--collisions", type=float, default=0.15, help="Fraction reusing names.")
    parser.add_argument(
        "--layout", choices=("flat", "nested", "mixed"), default="mixed"
    )
    parser.add_argument("--days", type=int, default=5, help="Distinct capture days per camera.")
    parser.add_argument("--subfolders", type=int, default=3, help="DCIM subfolders per camera.")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    stats = build(
        out=args.out,
        cameras=args.cameras,
        per_camera=args.per_camera,
        size=args.size,
        bad_exif=args.bad_exif,
        collisions=args.collisions,
        layout=args.layout,
        seed=args.seed,
        days=args.days,
        subfolders=args.subfolders,
        quiet=args.quiet,
    )
    print(
        f"{stats['files']:,} files, {stats['bytes'] / 1e6:,.1f} MB, "
        f"{stats['bad_exif']:,} bad EXIF, {stats['collisions']:,} seeded collisions "
        f"in {stats['seconds']}s -> {args.out}"
    )


if __name__ == "__main__":
    main()
