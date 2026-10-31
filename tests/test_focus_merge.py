import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import pytest

from phototag.focus_merge import decode, merge
from phototag.stack_ops import match_inbox, merge_stacks
from phototag.storage.exif import EXIFHandler
from phototag.storage.state_db import ProcessingStateDB

from .test_stacks import manifest_dict

SEQUENCE_ID = "3F9A1C2E-8B1D-4C6A-9E0F-2A7B5C4D3E21"
WIDTH, HEIGHT = 480, 360


def scene() -> np.ndarray:
    """Fine random texture at several scales, so every region has detail."""
    rng = np.random.default_rng(7)
    texture = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
    for sigma in (0.8, 2.0, 5.0):
        noise = rng.random((HEIGHT, WIDTH)).astype(np.float32)
        texture += cv2.GaussianBlur(noise, (0, 0), sigma)
    texture = (texture - texture.min()) / (texture.max() - texture.min())
    rgb = np.dstack([texture, texture * 0.8 + 0.1, 1 - texture * 0.5])
    return (rgb * 255).astype(np.uint8)


def shift(image: np.ndarray, dx: float, dy: float, zoom: float = 1.0) -> np.ndarray:
    """Content moved by (dx, dy) and magnified around the centre."""
    centre = (WIDTH / 2, HEIGHT / 2)
    matrix = np.asarray(cv2.getRotationMatrix2D(centre, 0, zoom), dtype=np.float64)
    matrix[0, 2] += dx
    matrix[1, 2] += dy
    return cv2.warpAffine(image, matrix, (WIDTH, HEIGHT), borderMode=cv2.BORDER_REFLECT)


def frames_with_one_sharp_band(
    directory: Path,
    moves: List[Tuple[float, float, float]],
    focus_on: Optional[List[int]] = None,
) -> List[Path]:
    """Frame i is focused on horizontal band i (or band focus_on[i]); blur
    grows with the distance from it."""
    base = scene().astype(np.float32)
    levels = [base] + [cv2.GaussianBlur(base, (0, 0), s) for s in (1, 2, 3, 4)]
    depth = (np.arange(HEIGHT, dtype=np.float32) + 0.5) / HEIGHT
    paths = []
    for i, (dx, dy, zoom) in enumerate(moves):
        band = focus_on[i] if focus_on else i
        target = (band + 0.5) / len(moves)
        sigma = np.clip(12 * (np.abs(depth - target) - 0.5 / len(moves)), 0, 4)
        low = np.floor(sigma).astype(int)
        high = np.minimum(low + 1, 4)
        mix = (sigma - low)[:, None, None]
        rows = np.arange(HEIGHT)
        stacked = np.stack(levels)
        frame = stacked[low, rows] * (1 - mix) + stacked[high, rows] * mix
        path = directory / f"DSC{i:05d}.png"
        cv2.imwrite(str(path), shift(frame.astype(np.uint8), dx, dy, zoom))
        paths.append(path)
    return paths


def focus(image: np.ndarray) -> float:
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY).astype(np.float32)
    return float(cv2.Laplacian(gray, cv2.CV_32F).var())


def centre(image: np.ndarray, size: int = 200) -> np.ndarray:
    h, w = image.shape[:2]
    return image[(h - size) // 2 : (h + size) // 2, (w - size) // 2 : (w + size) // 2]


def test_merge_is_sharper_than_every_frame(tmp_path):
    paths = frames_with_one_sharp_band(
        tmp_path, [(0, 0, 1.0), (2, -1, 1.0), (-3, 2, 1.0)]
    )

    result = merge(paths)

    merged = focus(result.image)
    singles = [focus(decode(p)) for p in paths]
    assert merged > 1.5 * max(singles)
    # Each band's pixels came from the frame that was sharp there
    band = result.depth.shape[0] // 3
    for i in range(3):
        rows = result.depth[i * band + 15 : (i + 1) * band - 15]
        assert np.bincount(rows.ravel(), minlength=3).argmax() == i


@pytest.mark.parametrize("move", [(3.0, -2.0, 1.0), (-1.5, 4.0, 1.0), (0.0, 0.0, 1.04)])
def test_alignment_recovers_shift_and_magnification(tmp_path, move):
    # Focused like the middle frame, so only the geometry differs
    paths = frames_with_one_sharp_band(
        tmp_path, [(0, 0, 1.0), (0, 0, 1.0), move], focus_on=[0, 1, 1]
    )

    result = merge(paths)

    moved = result.alignments[2]
    dx, dy, zoom = move
    assert moved.scale == pytest.approx(zoom, abs=0.003)
    # The warp maps middle-frame coords to the moved frame's coords
    expected = np.array([dx, dy]) + (1 - zoom) * np.array([WIDTH / 2, HEIGHT / 2])
    assert np.allclose(moved.shift, expected, atol=0.1)
    assert result.alignments[1].method == "reference"
    # Alignment borders are cropped away
    assert result.image.shape[0] < HEIGHT or result.image.shape[1] < WIDTH


def test_merged_centre_matches_the_sharp_scene(tmp_path):
    paths = frames_with_one_sharp_band(tmp_path, [(0, 0, 1.0)] * 3)

    result = merge(paths)

    x, y, w, h = result.crop
    truth = cv2.cvtColor(scene(), cv2.COLOR_BGR2RGB)[y : y + h, x : x + w]
    error = np.abs(centre(result.image).astype(float) / 257 - centre(truth))
    assert float(error.mean()) < 6


def setup_stack(tmp_path: Path) -> Tuple[ProcessingStateDB, List[Path]]:
    db = ProcessingStateDB(storage_dir=tmp_path / ".phototag")
    manifest = {
        **manifest_dict(frames=3),
        "plan": {"frames": 3, "direction": "in", "step": 16},
        "annotations": {"title": "Moss", "tags": ["macro"]},
    }
    db.import_sequence(
        SEQUENCE_ID, "focus_stack", json.dumps(manifest), "s.json", "", 3
    )
    card = tmp_path / "inbox" / "card"
    card.mkdir(parents=True)
    paths = frames_with_one_sharp_band(card, [(0, 0, 1.0), (1, 1, 1.0), (-1, 0, 1.0)])
    subprocess.run(
        [
            "exiftool",
            "-q",
            "-overwrite_original",
            "-EXIF:Model=ILCE-7M3",
            str(paths[1]),
        ],
        check=True,
    )
    members = []
    for i, path in enumerate(paths):
        db.add_photo(str(path), None)
        members.append((str(path), i + 1, i + 1, int(i == 1)))
    db.record_sequence_match(SEQUENCE_ID, 0.0, 3, members)
    return db, paths


def read_tags(path: Path) -> dict:
    out = subprocess.run(
        [
            "exiftool",
            "-j",
            "-n",
            "-Keywords",
            "-UserComment",
            "-Orientation",
            "-Model",
            "-Title",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)[0]


def test_merge_stacks_writes_tagged_outputs_and_feeds_the_inbox(tmp_path):
    db, frames = setup_stack(tmp_path)
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in frames]
    inbox = tmp_path / "inbox"
    out = tmp_path / "merged"

    summary = merge_stacks(db, out, [], inbox_dir=inbox)

    assert summary.failed == [] and len(summary.merged) == 1
    stack = summary.merged[0]
    base = "2026-10-31_140322_focus_stack_3f9a1c2e_merged"
    assert stack.jpeg == out / f"{base}.jpg"
    assert stack.tiff == out / f"{base}.tif"
    assert stack.depth == out / f"{base}_depth.png"
    tiff = cv2.imread(str(stack.tiff), cv2.IMREAD_UNCHANGED)
    assert tiff is not None and tiff.dtype == np.uint16
    for target in (stack.jpeg, stack.tiff):
        tags = read_tags(target)
        assert tags["Keywords"] == ["focus_stack_merged", "stack_3f9a1c2e", "macro"]
        assert tags["UserComment"] == "focus_stack merged from 3 frames, in, step 16"
        assert tags["Orientation"] == 1
        assert tags["Model"] == "ILCE-7M3"  # copied from the middle frame
        assert tags["Title"] == "Moss"
    assert stack.inbox_copy == inbox / f"{base}.jpg"
    assert stack.inbox_copy is not None
    assert stack.inbox_copy.read_bytes() == stack.jpeg.read_bytes()
    sequence = db.get_sequence(SEQUENCE_ID)
    assert sequence is not None
    assert sequence["merged_path"] == str(stack.jpeg)
    assert sequence["merged_inbox_path"] == str(stack.inbox_copy)
    # Source frames are only read
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in frames] == before

    again = merge_stacks(db, out, [], inbox_dir=inbox)
    assert again.skipped == 1 and again.merged == []


def test_forced_remerge_picks_a_new_inbox_key(tmp_path):
    db, _ = setup_stack(tmp_path)
    inbox = tmp_path / "inbox"
    first = merge_stacks(db, tmp_path / "merged", [], inbox_dir=inbox).merged[0]
    assert first.inbox_copy is not None
    # The first copy was picked up and moved on by the pipeline
    db.add_photo(str(first.inbox_copy), None)
    first.inbox_copy.unlink()

    second = merge_stacks(db, tmp_path / "merged", [], inbox_dir=inbox, force=True)

    copy = second.merged[0].inbox_copy
    assert copy is not None and copy != first.inbox_copy and copy.exists()
    sequence = db.get_sequence(SEQUENCE_ID)
    assert sequence is not None and sequence["merged_inbox_path"] == str(copy)


def test_forced_remerge_replaces_a_copy_still_waiting_in_the_inbox(tmp_path):
    db, _ = setup_stack(tmp_path)
    inbox = tmp_path / "inbox"
    first = merge_stacks(db, tmp_path / "merged", [], inbox_dir=inbox).merged[0]

    second = merge_stacks(db, tmp_path / "merged", [], inbox_dir=inbox, force=True)

    assert second.merged[0].inbox_copy == first.inbox_copy
    assert sorted(p.name for p in inbox.glob("*.jpg")) == [
        "2026-10-31_140322_focus_stack_3f9a1c2e_merged.jpg"
    ]


def test_only_focus_stacks_merge_and_id_filters(tmp_path):
    db, _ = setup_stack(tmp_path)

    with pytest.raises(ValueError):
        merge_stacks(db, tmp_path / "m", [], kind="exposure_bracket")
    assert merge_stacks(db, tmp_path / "m", [], sequence_id="deadbeef").merged == []
    assert len(merge_stacks(db, tmp_path / "m", [], sequence_id="3F9A1C2E").merged) == 1


def test_merged_image_is_never_matched_as_a_frame(tmp_path):
    db, _ = setup_stack(tmp_path)
    inbox = tmp_path / "inbox"
    merged = merge_stacks(db, tmp_path / "merged", [], inbox_dir=inbox).merged[0]
    assert merged.inbox_copy is not None and merged.inbox_copy.exists()
    loose = inbox / "IMG_0001.png"
    cv2.imwrite(str(loose), scene())
    db.import_sequence(
        "AAAAAAAA-0000-0000-0000-000000000000",
        "focus_stack",
        json.dumps({**manifest_dict(frames=1), "id": "AAAAAAAA-0000"}),
        "t.json",
        "",
        1,
    )

    class Exif(EXIFHandler):
        seen: List[Path] = []

        def read_shot_metadata(self, paths: List[Path]) -> List[Dict[str, Any]]:
            self.seen.extend(paths)
            return []

    exif = Exif()
    match_inbox(db, inbox, 120.0, exif=exif)

    assert exif.seen == [loose]
