import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from phototag.dating import (
    exiftool_to_iso,
    filename_sequence,
    parse_visible_date,
    resolve_capture_date,
    source_context,
)


def test_filename_sequence_takes_the_trailing_number():
    assert filename_sequence(Path("PICT0043.JPG")) == 43
    assert filename_sequence(Path("roll 2 - 12.jpg")) == 12
    assert filename_sequence(Path("cover.jpg")) is None


def test_parse_visible_date_returns_noon():
    assert parse_visible_date("2004-07-09") == datetime(2004, 7, 9, 12)
    assert parse_visible_date("2004/7/9") == datetime(2004, 7, 9, 12)


def test_parse_visible_date_rejects_garbage_and_implausible_years():
    assert parse_visible_date(None) is None
    assert parse_visible_date("July 9th") is None
    assert parse_visible_date("2004-02-30") is None
    assert parse_visible_date("1901-01-01") is None


def test_exiftool_to_iso():
    assert exiftool_to_iso("2026:07:19 22:14:46") == "2026-07-19T22:14:46"
    assert exiftool_to_iso("2026:07:19 22:14:46+02:00") == "2026-07-19T22:14:46"
    assert exiftool_to_iso("not a date") is None


def test_source_context_flags_toy_cameras():
    toy = source_context(1280, 720, 200_000)
    modern = source_context(6000, 4000, 12_000_000)

    assert "toy/keychain camera" in toy
    assert "toy/keychain camera" not in modern
    assert "6000x4000 (24.0 MP" in modern


def test_existing_exif_date_is_never_overwritten(tmp_path):
    assert (
        resolve_capture_date(tmp_path / "a.jpg", True, "2020-01-01", neighbours=[])
        is None
    )


def test_printed_stamp_wins_and_sequence_orders_photos():
    resolved = resolve_capture_date(Path("PICT0005.JPG"), False, "2004-07-09", [])

    assert resolved == (datetime(2004, 7, 9, 12, 0, 5), "stamp")


def test_trusted_neighbour_anchors_a_dateless_photo():
    neighbours: List[Tuple[str, Optional[str], str]] = [
        ("in/PICT0010.JPG", "2004-07-09T15:00:00", "exif"),
        ("in/PICT0003.JPG", "2001-01-01T00:00:00", "mtime"),  # guesses never anchor
    ]

    resolved = resolve_capture_date(Path("in/PICT0012.JPG"), False, None, neighbours)

    assert resolved == (datetime(2004, 7, 9, 15, 0, 2), "neighbour")


def test_mtime_is_the_last_resort(tmp_path):
    photo = tmp_path / "scan.jpg"
    photo.write_bytes(b"x")
    stamp = datetime(2020, 5, 6, 7, 8, 9).timestamp()
    os.utime(photo, (stamp, stamp))

    assert resolve_capture_date(photo, False, None, []) == (
        datetime(2020, 5, 6, 7, 8, 9),
        "mtime",
    )
