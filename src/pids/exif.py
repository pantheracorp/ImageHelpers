"""Header-only JPEG EXIF date extraction -- no exiftool, no Pillow.

``DateTimeOriginal`` lives in the JPEG APP1 segment's TIFF IFD structure, within the
first few KB of the file (DESIGN.md sec 3.3, sec 5).  Parsing it directly removes ~6,000
exiftool process spawns from the 600k-file run and removes the exiftool install from
field laptops.

The parser is deliberately total: it never raises on malformed input.  Every failure
returns a reason code that the pipeline turns into a quarantine record (sec 7.2), so a
corrupt file can never be filed under a guessed date and can never abort a run.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator

#: First read from each file.  Big enough for essentially every camera-trap JPEG's
#: EXIF block, small enough that it is one sequential read the kernel would have done
#: anyway -- the bytes stay in the page cache for the copy that follows (sec 3.2).
HEADER_BYTES = 64 * 1024

#: Hard ceiling on how far we will grow the window chasing IFD offsets.  Some cameras
#: put a large thumbnail before the Exif SubIFD; beyond this we call it truncated
#: rather than reading the whole file into RAM.
MAX_HEADER_BYTES = 1024 * 1024

#: Junk protection: a real IFD has tens of entries, not thousands.
MAX_IFD_ENTRIES = 512

TAG_DATETIME = 0x0132  # ModifyDate (IFD0)
TAG_EXIF_IFD = 0x8769  # pointer to the Exif SubIFD
TAG_DATETIME_ORIGINAL = 0x9003  # DateTimeOriginal
TAG_DATETIME_DIGITIZED = 0x9004  # CreateDate / DateTimeDigitized

TYPE_ASCII = 2
TYPE_LONG = 4

# Reason codes recorded in the state DB and unsorted.csv (sec 7.2).
NOT_JPEG = "not_jpeg"
NO_EXIF = "no_exif"
ZERO_DATE = "zero_date"
UNPARSEABLE = "unparseable"
TRUNCATED_HEADER = "truncated_header"
READ_ERROR = "read_error"


@dataclass(frozen=True)
class ExifDate:
    """Outcome of a header parse.  Exactly one of ``raw``/``reason`` is set."""

    raw: str | None = None
    reason: str | None = None
    tag: int | None = None  # which tag supplied the value, for diagnostics

    @property
    def ok(self) -> bool:
        return self.raw is not None


class _Window:
    """A growable view of the start of a file.

    Starts at ``HEADER_BYTES`` and extends only if an IFD offset points past the end,
    which keeps the common case to a single read per file.
    """

    __slots__ = ("fh", "buf", "limit", "truncated")

    def __init__(self, fh: BinaryIO, initial: int = HEADER_BYTES, limit: int = MAX_HEADER_BYTES):
        self.fh = fh
        self.limit = limit
        self.truncated = False
        self.buf = fh.read(initial)

    def ensure(self, need: int) -> bool:
        """Make the buffer at least ``need`` bytes long if the file allows it."""
        if need <= len(self.buf):
            return True
        if need > self.limit:
            self.truncated = True
            return False
        chunk = self.fh.read(need - len(self.buf))
        if chunk:
            self.buf += chunk
        if need > len(self.buf):
            self.truncated = True
            return False
        return True


def _iter_ifd_entries(
    win: _Window, tiff: int, ifd_off: int, bo: str
) -> Iterator[tuple[int, int, int, int]]:
    """Yield ``(tag, type, count, entry_pos)`` for one IFD.

    ``tiff`` is the absolute offset of the TIFF header; IFD offsets are relative to it.
    """
    base = tiff + ifd_off
    if ifd_off <= 0 or not win.ensure(base + 2):
        return
    (n_entries,) = struct.unpack_from(bo + "H", win.buf, base)
    if n_entries == 0 or n_entries > MAX_IFD_ENTRIES:
        return
    if not win.ensure(base + 2 + n_entries * 12):
        # Parse whatever entries are fully inside the window; the missing tail is
        # recorded as truncation by ensure().
        n_entries = max(0, (len(win.buf) - (base + 2)) // 12)
    for i in range(n_entries):
        pos = base + 2 + i * 12
        tag, typ, count = struct.unpack_from(bo + "HHI", win.buf, pos)
        yield tag, typ, count, pos


def _ascii_value(win: _Window, tiff: int, bo: str, typ: int, count: int, pos: int) -> str | None:
    """Read an ASCII IFD value, inline (<=4 bytes) or via its offset."""
    if typ != TYPE_ASCII or not 0 < count <= 64:
        return None
    if count <= 4:
        raw = win.buf[pos + 8 : pos + 8 + count]
    else:
        (off,) = struct.unpack_from(bo + "I", win.buf, pos + 8)
        start = tiff + off
        if off <= 0 or not win.ensure(start + count):
            return None
        raw = win.buf[start : start + count]
    text = raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    return text or None


def _long_value(win: _Window, bo: str, typ: int, pos: int) -> int | None:
    if typ != TYPE_LONG:
        return None
    (value,) = struct.unpack_from(bo + "I", win.buf, pos + 8)
    return value


def _parse_tiff(win: _Window, tiff: int) -> ExifDate:
    """Walk IFD0 -> Exif SubIFD looking for a capture timestamp.

    Preference order is ``DateTimeOriginal`` (0x9003), then ``DateTimeDigitized``
    (0x9004), then IFD0's ``DateTime`` (0x0132).  DESIGN.md sec 5.2 also names
    ``SubSecDateTimeOriginal``; that is an exiftool composite of 0x9003 plus the
    sub-second tag 0x9291, so it carries no date information 0x9003 lacks and is not
    a separate fallback here.
    """
    if not win.ensure(tiff + 8):
        return ExifDate(reason=TRUNCATED_HEADER)
    order = win.buf[tiff : tiff + 2]
    if order == b"II":
        bo = "<"
    elif order == b"MM":
        bo = ">"
    else:
        return ExifDate(reason=UNPARSEABLE)
    (magic,) = struct.unpack_from(bo + "H", win.buf, tiff + 2)
    if magic != 42:
        return ExifDate(reason=UNPARSEABLE)
    (ifd0,) = struct.unpack_from(bo + "I", win.buf, tiff + 4)

    sub_ifd: int | None = None
    ifd0_datetime: str | None = None
    for tag, typ, count, pos in _iter_ifd_entries(win, tiff, ifd0, bo):
        if tag == TAG_EXIF_IFD:
            sub_ifd = _long_value(win, bo, typ, pos)
        elif tag == TAG_DATETIME:
            ifd0_datetime = _ascii_value(win, tiff, bo, typ, count, pos)

    if sub_ifd:
        found: dict[int, str] = {}
        for tag, typ, count, pos in _iter_ifd_entries(win, tiff, sub_ifd, bo):
            if tag in (TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED):
                value = _ascii_value(win, tiff, bo, typ, count, pos)
                if value:
                    found[tag] = value
        for tag in (TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED):
            if tag in found:
                return ExifDate(raw=found[tag], tag=tag)

    if ifd0_datetime:
        return ExifDate(raw=ifd0_datetime, tag=TAG_DATETIME)
    return ExifDate(reason=TRUNCATED_HEADER if win.truncated else NO_EXIF)


def read_datetime(fh: BinaryIO) -> ExifDate:
    """Extract the EXIF capture timestamp from an open JPEG handle.

    The handle is left at an arbitrary position; callers seek back to 0 before copying.
    """
    try:
        win = _Window(fh)
    except OSError:
        return ExifDate(reason=READ_ERROR)

    buf = win.buf
    if len(buf) < 4 or buf[0] != 0xFF or buf[1] != 0xD8:
        return ExifDate(reason=NOT_JPEG)

    i = 2
    while True:
        if not win.ensure(i + 4):
            return ExifDate(reason=TRUNCATED_HEADER)
        buf = win.buf
        if buf[i] != 0xFF:
            # Not at a marker boundary: the header is damaged.
            return ExifDate(reason=UNPARSEABLE)
        marker = buf[i + 1]
        while marker == 0xFF:  # fill bytes
            i += 1
            if not win.ensure(i + 4):
                return ExifDate(reason=TRUNCATED_HEADER)
            buf = win.buf
            marker = buf[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2  # standalone markers carry no payload
            continue
        if marker in (0xDA, 0xD9):
            return ExifDate(reason=NO_EXIF)  # reached image data; no APP1 present
        (seg_len,) = struct.unpack_from(">H", buf, i + 2)
        if seg_len < 2:
            return ExifDate(reason=UNPARSEABLE)
        payload = i + 4
        if marker == 0xE1:
            if not win.ensure(payload + 6):
                return ExifDate(reason=TRUNCATED_HEADER)
            # APP1 is shared with XMP (`http://ns.adobe.com/xap/1.0/\0`); only the
            # `Exif\0\0` flavour carries the TIFF structure we want.
            if win.buf[payload : payload + 6] == b"Exif\x00\x00":
                return _parse_tiff(win, payload + 6)
        i = i + 2 + seg_len


def read_datetime_from_path(path: str) -> ExifDate:
    """Convenience wrapper for tests and one-off inspection."""
    try:
        with open(path, "rb") as fh:
            return read_datetime(fh)
    except OSError:
        return ExifDate(reason=READ_ERROR)


def parse_exif_datetime(raw: str | None) -> tuple[str | None, str | None]:
    """``'2024:07:14 06:31:02'`` -> ``('071424', None)``.

    A direct string slice, not 600k ``as.POSIXct``-equivalent parses.  No timezone
    conversion is applied: EXIF ``DateTimeOriginal`` is already local camera time, and
    converting it would shift images across date boundaries (sec 6).
    """
    if not raw:
        return None, NO_EXIF
    text = raw.strip()
    if len(text) < 10:
        return None, UNPARSEABLE
    year, month, day = text[0:4], text[5:7], text[8:10]
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return None, UNPARSEABLE
    if text[4] not in ":-/" or text[7] not in ":-/":
        return None, UNPARSEABLE
    y, m, d = int(year), int(month), int(day)
    if y == 0 or m == 0 or d == 0:
        return None, ZERO_DATE
    if not (1900 <= y <= 2999 and 1 <= m <= 12 and 1 <= d <= 31):
        return None, UNPARSEABLE
    return f"{m:02d}{d:02d}{y % 100:02d}", None
