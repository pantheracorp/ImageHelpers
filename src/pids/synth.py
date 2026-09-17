"""Synthetic JPEGs with crafted EXIF, for tests and the scale-test tree generator.

DESIGN.md sec 12 deliverable 2: scale testing without 3 TB.  Everything here builds
real JPEG byte structure -- SOI, APP1/``Exif\\0\\0``, a TIFF header, IFD0, an Exif
SubIFD, SOS, EOI -- so the header parser is exercised against the same layout a camera
produces, including the awkward cases: big-endian byte order, an XMP APP1 before the
Exif one, a SubIFD past the 64 KB read window, and truncated files.
"""

from __future__ import annotations

import struct

TAG_DATETIME = 0x0132
TAG_MAKE = 0x010F
TAG_EXIF_IFD = 0x8769
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME_DIGITIZED = 0x9004

TYPE_ASCII = 2
TYPE_LONG = 4

DEFAULT_DT = "2024:07:14 06:31:02"

_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"


def _ascii(value: str) -> bytes:
    return value.encode("ascii", "replace") + b"\x00"


def _pack_ifd(bo: str, entries: list[tuple], data_offset: int, next_ifd: int = 0):
    """Pack one IFD plus the external data its long values point at."""
    out = struct.pack(bo + "H", len(entries))
    data = b""
    for tag, typ, count, payload in entries:
        if isinstance(payload, int):
            value = struct.pack(bo + "I", payload)
        elif len(payload) <= 4:
            value = payload.ljust(4, b"\x00")
        else:
            value = struct.pack(bo + "I", data_offset + len(data))
            data += payload
        out += struct.pack(bo + "HHI", tag, typ, count) + value
    out += struct.pack(bo + "I", next_ifd)
    return out, data


def build_tiff(
    dt_original: str | None = DEFAULT_DT,
    dt_digitized: str | None = None,
    dt_modify: str | None = None,
    order: str = "II",
    pad_before_data: int = 0,
) -> bytes:
    """A TIFF/EXIF block with the requested timestamps present.

    ``pad_before_data`` inserts filler between the IFDs and their values, which is how
    a camera with a large embedded thumbnail pushes the timestamp past a small read
    window.
    """
    bo = "<" if order == "II" else ">"
    sub_entries: list[tuple] = []
    if dt_original is not None:
        sub_entries.append(
            (TAG_DATETIME_ORIGINAL, TYPE_ASCII, len(_ascii(dt_original)), _ascii(dt_original))
        )
    if dt_digitized is not None:
        sub_entries.append(
            (TAG_DATETIME_DIGITIZED, TYPE_ASCII, len(_ascii(dt_digitized)), _ascii(dt_digitized))
        )

    ifd0_entries: list[tuple] = [(TAG_MAKE, TYPE_ASCII, 8, _ascii("RECONYX"))]
    if dt_modify is not None:
        ifd0_entries.append(
            (TAG_DATETIME, TYPE_ASCII, len(_ascii(dt_modify)), _ascii(dt_modify))
        )

    ifd0_off = 8
    ifd0_size = 2 + 12 * (len(ifd0_entries) + (1 if sub_entries else 0)) + 4
    sub_off = ifd0_off + ifd0_size
    sub_size = (2 + 12 * len(sub_entries) + 4) if sub_entries else 0
    data_off = sub_off + sub_size + pad_before_data

    if sub_entries:
        ifd0_entries.append((TAG_EXIF_IFD, TYPE_LONG, 1, sub_off))

    ifd0_bytes, ifd0_data = _pack_ifd(bo, ifd0_entries, data_off)
    sub_bytes, sub_data = (
        _pack_ifd(bo, sub_entries, data_off + len(ifd0_data)) if sub_entries else (b"", b"")
    )

    header = order.encode("ascii") + struct.pack(bo + "H", 42) + struct.pack(bo + "I", ifd0_off)
    return header + ifd0_bytes + sub_bytes + b"\x00" * pad_before_data + ifd0_data + sub_data


#: A JPEG segment length field is 16 bits, so no single segment can exceed this.
MAX_SEGMENT_PAYLOAD = 65533


def _segment(marker: int, payload: bytes) -> bytes:
    if len(payload) > MAX_SEGMENT_PAYLOAD:
        raise ValueError(
            f"segment payload {len(payload)} exceeds the JPEG limit of {MAX_SEGMENT_PAYLOAD}"
        )
    return struct.pack(">BBH", 0xFF, marker, len(payload) + 2) + payload


def jpeg(
    dt: str | None = DEFAULT_DT,
    size: int = 1024,
    order: str = "II",
    include_exif: bool = True,
    dt_digitized: str | None = None,
    dt_modify: str | None = None,
    with_xmp: bool = False,
    pad_before_data: int = 0,
    filler_segments: int = 0,
    truncate_to: int | None = None,
    corrupt_tiff: bool = False,
) -> bytes:
    """Build a JPEG whose EXIF says ``dt``.

    ``size`` is padded with filler after the scan marker, so a tree of 600k files can
    be made in minutes at a few KB each.  ``filler_segments`` inserts APP2-sized
    segments *before* the Exif APP1, which is how a real file (a large ICC profile, a
    JFIF thumbnail) pushes the timestamp past a 64 KB read window -- the case that
    makes the parser grow its window rather than quarantine the file.
    """
    out = b"\xff\xd8"
    if with_xmp:
        out += _segment(0xE1, _XMP_HEADER + b"<x:xmpmeta/>")
    for _ in range(filler_segments):
        out += _segment(0xE2, b"ICC_PROFILE\x00" + b"\x00" * (MAX_SEGMENT_PAYLOAD - 12))
    if include_exif:
        tiff = build_tiff(dt, dt_digitized, dt_modify, order, pad_before_data)
        if corrupt_tiff:
            tiff = b"XX" + tiff[2:]  # byte-order mark neither II nor MM
        out += _segment(0xE1, b"Exif\x00\x00" + tiff)
    out += _segment(0xDB, b"\x00" + b"\x10" * 64)  # a plausible quantisation table
    out += _segment(0xDA, b"\x01\x01\x00")  # start of scan
    filler = max(0, size - len(out) - 2)
    out += b"\x5a" * filler + b"\xff\xd9"
    if truncate_to is not None:
        out = out[:truncate_to]
    return out


def zero_date_jpeg(size: int = 1024) -> bytes:
    """The classic dead-clock camera output."""
    return jpeg("0000:00:00 00:00:00", size=size)


def no_exif_jpeg(size: int = 1024) -> bytes:
    return jpeg(None, size=size, include_exif=False)
