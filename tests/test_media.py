import hashlib
import os
import time
from pathlib import Path

from PIL import Image

from phototag.media import (
    file_hashes,
    find_media_files,
    is_stable,
    is_video,
    pixel_hash,
    unique_destination,
)


def make_jpeg(path: Path, color: str = "red") -> Path:
    Image.new("RGB", (16, 16), color).save(path, format="JPEG")
    return path


def test_find_media_files_ignores_json_and_other_files(tmp_path):
    (tmp_path / "card").mkdir()
    make_jpeg(tmp_path / "card" / "DSC0001.JPG")
    (tmp_path / "card" / "DSC0001.ARW").write_bytes(b"raw")
    (tmp_path / "clip.MP4").write_bytes(b"video")
    (tmp_path / "20261031-140322-focus_stack-3f9a1c2e.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("hi")

    found = [p.relative_to(tmp_path).as_posix() for p in find_media_files(tmp_path)]
    images = find_media_files(tmp_path, include_videos=False)

    assert found == ["card/DSC0001.ARW", "card/DSC0001.JPG", "clip.MP4"]
    assert all(not is_video(p) for p in images) and len(images) == 2


def test_file_hashes_match_hashlib(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello")

    hashes = file_hashes(path)

    assert hashes is not None
    assert hashes.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert hashes.sha1 == hashlib.sha1(b"hello").hexdigest()
    assert file_hashes(tmp_path / "missing") is None


def test_pixel_hash_ignores_metadata_but_not_pixels(tmp_path):
    plain = make_jpeg(tmp_path / "plain.jpg")
    exif = Image.Exif()
    exif[0x010E] = "a description"  # ImageDescription
    with Image.open(plain) as img:
        img.save(tmp_path / "tagged.jpg", format="JPEG", exif=exif, quality="keep")
    other = make_jpeg(tmp_path / "blue.jpg", "blue")

    assert file_hashes(plain) != file_hashes(tmp_path / "tagged.jpg")
    assert pixel_hash(plain) == pixel_hash(tmp_path / "tagged.jpg")
    assert pixel_hash(plain) != pixel_hash(other)


def test_pixel_hash_covers_png_and_skips_raw(tmp_path):
    Image.new("RGB", (4, 4), "green").save(tmp_path / "a.png")
    (tmp_path / "a.arw").write_bytes(b"raw")

    assert pixel_hash(tmp_path / "a.png")
    assert pixel_hash(tmp_path / "a.arw") is None


def test_is_stable_waits_for_recently_modified_files(tmp_path):
    path = tmp_path / "syncing.jpg"
    path.write_bytes(b"x")
    assert not is_stable(path)

    old = time.time() - 60
    os.utime(path, (old, old))
    assert is_stable(path)
    assert not is_stable(tmp_path / "missing.jpg")


def test_unique_destination_avoids_overwrites(tmp_path):
    source = Path("/somewhere/DSC0001.JPG")
    assert unique_destination(tmp_path, source) == tmp_path / "DSC0001.JPG"

    (tmp_path / "DSC0001.JPG").write_bytes(b"x")
    dest = unique_destination(tmp_path, source)
    assert dest != tmp_path / "DSC0001.JPG"
    assert dest.name.startswith("DSC0001_") and dest.suffix == ".JPG"
