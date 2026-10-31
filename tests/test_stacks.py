import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from phototag.stacks import (
    Shot,
    load_manifest,
    match_sequence,
    order_members,
    parse_exif_time,
    parse_manifest,
    pick_representative,
)

START = datetime(2026, 10, 31, 17, 3, 22, tzinfo=timezone.utc)  # 14:03:22 in Sao Paulo
LOCAL_START = datetime(2026, 10, 31, 14, 3, 22)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def manifest_dict(
    frames: int = 5,
    spacing_s: float = 3.0,
    press_s: float = 0.3,
    model: Optional[str] = "ILCE-7M3",
    kind: str = "focus_stack",
) -> Dict[str, Any]:
    frame_list = []
    for i in range(frames):
        pressed = START + timedelta(seconds=i * spacing_s)
        frame_list.append(
            {
                "index": i + 1,
                "pressed_at": iso(pressed),
                "released_at": iso(pressed + timedelta(seconds=press_s)),
                "shutter_confirmed": True,
                "ready_confirmed": True,
            }
        )
    end = START + timedelta(seconds=frames * spacing_s)
    return {
        "format": "open-alpha-link.sequence",
        "version": 1,
        "id": "3F9A1C2E-8B1D-4C6A-9E0F-2A7B5C4D3E21",
        "kind": kind,
        "outcome": "completed",
        "time_zone": "America/Sao_Paulo",
        "started_at": iso(START),
        "ended_at": iso(end),
        "camera": {"name": "Travel A7 III", "model": model},
        "plan": {"frames": frames},
        "expected_images": frames,
        "frames": frame_list,
    }


def shots_for(
    frames: int,
    camera_behind_s: float,
    spacing_s: float = 3.0,
    model: str = "ILCE-7M3",
    raw_and_jpeg: bool = False,
    skip: tuple = (),
) -> List[Shot]:
    shots = []
    for i in range(frames):
        if i in skip:
            continue
        taken = LOCAL_START + timedelta(seconds=i * spacing_s + 0.1 - camera_behind_s)
        taken = taken.replace(microsecond=0)  # EXIF has whole seconds
        suffixes = [".ARW", ".JPG"] if raw_and_jpeg else [".JPG"]
        for suffix in suffixes:
            shots.append(Shot(f"inbox/card/DSC{100 + i:05d}{suffix}", taken, model))
    return shots


def parse(data: Dict[str, Any]):
    manifest = parse_manifest(data)
    assert manifest is not None
    return manifest


def test_parse_manifest_converts_to_local_wall_clock():
    manifest = parse(manifest_dict())

    assert manifest.started_at == LOCAL_START
    assert manifest.frames[0].pressed_at == LOCAL_START
    assert manifest.short_id == "3f9a1c2e"
    assert manifest.tags == ["focus_stack", "stack_3f9a1c2e"]


def test_intervalometer_tag_is_timelapse():
    assert parse(manifest_dict(kind="intervalometer")).tags[0] == "timelapse"


def test_parse_manifest_rejects_foreign_json():
    assert parse_manifest({"format": "something-else"}) is None
    assert parse_manifest({**manifest_dict(), "version": 2}) is None
    assert parse_manifest([1, 2]) is None
    assert parse_manifest({**manifest_dict(), "time_zone": "Mars/Base"}) is None


def test_load_manifest_never_raises(tmp_path):
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "other.json").write_text(json.dumps({"hello": 1}))
    (tmp_path / "seq.json").write_text(json.dumps(manifest_dict()))

    assert load_manifest(tmp_path / "broken.json") is None
    assert load_manifest(tmp_path / "other.json") is None
    assert load_manifest(tmp_path / "missing.json") is None
    assert load_manifest(tmp_path / "seq.json") is not None


@pytest.mark.parametrize("camera_behind", [37.0, -37.0, 0.0, 119.0])
def test_match_finds_the_camera_clock_offset(camera_behind):
    manifest = parse(manifest_dict())

    result = match_sequence(manifest, shots_for(5, camera_behind), max_skew=120)

    assert result.accepted
    assert result.matched_frames == 5
    assert abs(result.offset_seconds - camera_behind) <= 1.5
    assert sorted(result.assignments.values()) == [1, 2, 3, 4, 5]


def test_offset_beyond_the_skew_window_does_not_match():
    manifest = parse(manifest_dict())

    result = match_sequence(manifest, shots_for(5, 300.0), max_skew=120)

    assert not result.accepted
    assert result.assignments == {}


def test_raw_and_jpeg_map_to_the_same_frame():
    manifest = parse(manifest_dict())

    result = match_sequence(manifest, shots_for(5, 10, raw_and_jpeg=True), 120)

    assert result.matched_frames == 5
    assert len(result.assignments) == 10
    assert result.assignments["inbox/card/DSC00100.ARW"] == 1
    assert result.assignments["inbox/card/DSC00100.JPG"] == 1


def test_eighty_percent_of_frames_is_enough():
    manifest = parse(manifest_dict(frames=5))

    four = match_sequence(manifest, shots_for(5, 5, skip=(2,)), 120)
    three = match_sequence(manifest, shots_for(5, 5, skip=(1, 2)), 120)

    assert four.accepted and four.matched_frames == 4
    assert not three.accepted


def test_other_camera_models_are_ignored_unless_model_is_unknown():
    shots = shots_for(5, 5, model="ILCE-7RM5")

    assert not match_sequence(parse(manifest_dict()), shots, 120).accepted
    assert match_sequence(parse(manifest_dict(model=None)), shots, 120).accepted


def test_held_press_collects_every_image_inside_it():
    # One 4 s press (a held burst) and the camera writes 5 images during it
    manifest = parse(manifest_dict(frames=1, press_s=4.0))
    shots = [
        Shot(f"inbox/DSC{i}.JPG", LOCAL_START + timedelta(seconds=i - 20), "ILCE-7M3")
        for i in range(5)
    ]

    result = match_sequence(manifest, shots, 120)

    assert result.accepted
    assert set(result.assignments.values()) == {1}
    assert len(result.assignments) == 5


def test_ties_prefer_the_smallest_offset():
    # A single short frame and one photo: a range of offsets matches; the
    # one closest to zero wins.
    manifest = parse(manifest_dict(frames=1, press_s=0.0))
    shots = [Shot("inbox/a.JPG", LOCAL_START - timedelta(seconds=10), "ILCE-7M3")]

    result = match_sequence(manifest, shots, 120)

    assert result.offset_seconds == pytest.approx(8.5)


def test_unrelated_photo_outside_the_window_is_not_assigned():
    manifest = parse(manifest_dict())
    shots = shots_for(5, 37) + [
        Shot("inbox/later.JPG", LOCAL_START + timedelta(hours=2), "ILCE-7M3")
    ]

    result = match_sequence(manifest, shots, 120)

    assert "inbox/later.JPG" not in result.assignments


def test_representative_is_the_middle_exposure_as_jpeg():
    manifest = parse(manifest_dict(frames=5))
    shots = shots_for(5, 0, raw_and_jpeg=True)
    result = match_sequence(manifest, shots, 120)

    ordered = order_members(result.assignments, shots)

    assert ordered[:2] == ["inbox/card/DSC00100.ARW", "inbox/card/DSC00100.JPG"]
    assert pick_representative(ordered) == "inbox/card/DSC00102.JPG"


def test_representative_of_raw_only_and_even_counts():
    assert (
        pick_representative(["a/1.ARW", "a/2.ARW", "a/3.ARW", "a/4.ARW"]) == "a/2.ARW"
    )
    assert pick_representative([]) is None


def test_parse_exif_time_uses_subseconds():
    assert parse_exif_time("2026:10:31 14:03:22", "045") == datetime(
        2026, 10, 31, 14, 3, 22, 45000
    )
    assert parse_exif_time("2026:10:31 14:03:22+02:00") == datetime(
        2026, 10, 31, 14, 3, 22
    )
    assert parse_exif_time("0000:00:00 00:00:00") is None
    assert parse_exif_time(None) is None


def test_long_intervalometer_matches_quickly():
    manifest = parse(manifest_dict(frames=600, spacing_s=5.0))

    result = match_sequence(manifest, shots_for(600, 42, spacing_s=5.0), 120)

    assert result.accepted
    assert abs(result.offset_seconds - 42) <= 1.5


def test_paths_survive_as_given():
    manifest = parse(manifest_dict(frames=1))
    shot = Shot(str(Path("inbox") / "x.JPG"), LOCAL_START, "ILCE-7M3")

    assert list(match_sequence(manifest, [shot], 120).assignments) == ["inbox/x.JPG"]
