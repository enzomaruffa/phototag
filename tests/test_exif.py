import json
import subprocess

from PIL import Image

from phototag.storage.exif import EXIFHandler


def test_a_single_new_keyword_is_written(tmp_path):
    photo = tmp_path / "a.jpg"
    Image.new("RGB", (16, 16), "green").save(photo)
    subprocess.run(
        [
            "exiftool",
            "-q",
            "-overwrite_original",
            "-Rating=3",
            "-ImageDescription=Leaves",
            "-Keywords=plant",
            str(photo),
        ],
        check=True,
    )

    assert EXIFHandler().add_exif_metadata(photo, 3, ["plant", "macro"], "Leaves")

    out = subprocess.run(
        ["exiftool", "-j", "-Keywords", str(photo)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out)[0]["Keywords"] == ["plant", "macro"]
