"""Header EXIF parser: correct dates, including corrupt/truncated/0000:00:00 files.

Build-plan deliverable 1 (DESIGN.md sec 12).
"""

from __future__ import annotations

import io

import pytest

from pids import exif, synth


def parse(data: bytes) -> exif.ExifDate:
    return exif.read_datetime(io.BytesIO(data))


def test_little_endian_datetime_original():
    result = parse(synth.jpeg("2024:07:14 06:31:02"))
    assert result.raw == "2024:07:14 06:31:02"
    assert result.tag == exif.TAG_DATETIME_ORIGINAL


def test_big_endian_byte_order():
    assert parse(synth.jpeg("2024:07:14 06:31:02", order="MM")).raw == "2024:07:14 06:31:02"


def test_xmp_app1_before_exif_app1_is_skipped():
    """APP1 is shared with XMP; only the Exif flavour carries the TIFF structure."""
    assert parse(synth.jpeg("2024:01:02 03:04:05", with_xmp=True)).raw == "2024:01:02 03:04:05"


def test_falls_back_to_datetime_digitized():
    result = parse(synth.jpeg(None, dt_digitized="2023:01:02 03:04:05"))
    assert result.raw == "2023:01:02 03:04:05"
    assert result.tag == exif.TAG_DATETIME_DIGITIZED


def test_falls_back_to_ifd0_datetime():
    result = parse(synth.jpeg(None, dt_modify="2022:05:06 07:08:09"))
    assert result.raw == "2022:05:06 07:08:09"
    assert result.tag == exif.TAG_DATETIME


def test_datetime_original_wins_over_the_others():
    result = parse(
        synth.jpeg("2024:07:14 06:31:02", dt_digitized="2000:01:01 00:00:00", dt_modify="1999:01:01 00:00:00")
    )
    assert result.raw == "2024:07:14 06:31:02"


def test_exif_beyond_the_initial_window_is_still_found():
    """A large ICC profile pushes APP1 past 64 KB; the window grows instead of failing."""
    data = synth.jpeg("2024:07:14 06:31:02", filler_segments=2)
    assert len(data) > exif.HEADER_BYTES
    assert parse(data).raw == "2024:07:14 06:31:02"


def test_exif_beyond_the_hard_limit_is_reported_truncated():
    data = synth.jpeg("2024:07:14 06:31:02", filler_segments=20)
    assert len(data) > exif.MAX_HEADER_BYTES
    assert parse(data).reason == exif.TRUNCATED_HEADER


def test_no_exif_segment():
    assert parse(synth.jpeg(None, include_exif=False)).reason == exif.NO_EXIF


def test_corrupt_byte_order_mark():
    assert parse(synth.jpeg("2024:07:14 06:31:02", corrupt_tiff=True)).reason == exif.UNPARSEABLE


def test_truncated_mid_ifd():
    assert parse(synth.jpeg("2024:07:14 06:31:02", truncate_to=40)).reason in (
        exif.TRUNCATED_HEADER,
        exif.NO_EXIF,
    )


def test_not_a_jpeg():
    assert parse(b"PK\x03\x04not a jpeg at all").reason == exif.NOT_JPEG


def test_empty_file():
    assert parse(b"").reason == exif.NOT_JPEG


def test_garbage_after_soi_does_not_raise():
    assert parse(b"\xff\xd8" + b"\x00" * 500).reason == exif.UNPARSEABLE


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2024:07:14 06:31:02", "071424"),
        ("2024:01:01 00:00:00", "010124"),
        ("1999:12:31 23:59:59", "123199"),
        ("2024-07-14 06:31:02", "071424"),
        ("2024:07:14", "071424"),
    ],
)
def test_mmddyy_formatting(raw, expected):
    assert exif.parse_exif_datetime(raw) == (expected, None)


@pytest.mark.parametrize(
    "raw,reason",
    [
        (None, exif.NO_EXIF),
        ("", exif.NO_EXIF),
        ("0000:00:00 00:00:00", exif.ZERO_DATE),
        ("2024:00:14 06:31:02", exif.ZERO_DATE),
        ("2024:07:00 06:31:02", exif.ZERO_DATE),
        ("2024:13:14 06:31:02", exif.UNPARSEABLE),
        ("2024:07:32 06:31:02", exif.UNPARSEABLE),
        ("not a date at all", exif.UNPARSEABLE),
        ("1800:07:14 06:31:02", exif.UNPARSEABLE),
        ("2024", exif.UNPARSEABLE),
    ],
)
def test_bad_dates_get_a_reason(raw, reason):
    date, got = exif.parse_exif_datetime(raw)
    assert date is None and got == reason


def test_parser_never_raises_on_random_prefixes():
    """Fuzz the structure: a corrupt card must never abort a 600k-file run."""
    base = synth.jpeg("2024:07:14 06:31:02")
    for cut in range(0, len(base), 7):
        for mutate in (base[:cut], base[cut:], base[:cut] + b"\xff" * 5 + base[cut:]):
            exif.read_datetime(io.BytesIO(mutate))  # must not raise
