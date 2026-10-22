"""Export finished photos as JPEGs sorted into one folder per star rating.

Reads processed/ and outbox/, keeps photos whose capture date falls in
[--since, --until), and writes <out>/<rating>/<date>_<name>.jpg. RAW files are
converted with sips; JPEGs are copied as-is. Existing exports are skipped, so
re-running only adds new photos.
"""

import argparse
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple

from phototag.media import IMAGE_EXTENSIONS

SOURCE_DIRS = [Path("processed"), Path("outbox")]


def read_metadata(files: List[Path]) -> List[dict]:
    out = subprocess.run(
        ["exiftool", "-json", "-fast2", "-DateTimeOriginal", "-Rating", "-@", "-"],
        input="\n".join(str(f) for f in files),
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out) if out.strip() else []


def export_one(src: Path, dest: Path, quality: int) -> Optional[str]:
    tmp = dest.with_name(dest.name + ".partial")
    if src.suffix.lower() in {".jpg", ".jpeg"}:
        shutil.copy2(src, tmp)
    else:
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
            + [str(src), "--out", str(tmp)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            return f"{src.name}: {result.stderr.strip() or 'sips failed'}"
    tmp.rename(dest)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("outbox/drive-export"))
    parser.add_argument("--since", help="YYYY-MM-DD[ HH:MM], camera time")
    parser.add_argument("--until", help="YYYY-MM-DD[ HH:MM], camera time, exclusive")
    parser.add_argument("--quality", type=int, default=92)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    def exif_stamp(value: Optional[str]) -> Optional[str]:
        return value.replace("-", ":") if value else None

    since, until = exif_stamp(args.since), exif_stamp(args.until)
    out_root = args.out.resolve()

    files = [
        f
        for d in SOURCE_DIRS
        if d.exists()
        for f in d.rglob("*")
        if f.suffix.lower() in IMAGE_EXTENSIONS and out_root not in f.resolve().parents
    ]
    jobs: List[Tuple[Path, Path]] = []
    no_date = 0
    for meta in read_metadata(files):
        taken = str(meta.get("DateTimeOriginal") or "")
        if not taken:
            no_date += 1
            continue
        if (since and taken < since) or (until and taken >= until):
            continue
        src = Path(meta["SourceFile"])
        rating = str(meta.get("Rating") or "unrated")
        stamp = taken[:19].replace(":", "").replace(" ", "_")
        dest = out_root / rating / f"{stamp}_{src.stem}.jpg"
        if not dest.exists():
            jobs.append((src, dest))

    for _, dest in jobs:
        dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} photos to export to {out_root} ({no_date} without a date)")

    done = 0
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for error in pool.map(
            lambda job: export_one(job[0], job[1], args.quality), jobs
        ):
            done += 1
            if error:
                errors.append(error)
            if done % 50 == 0 or done == len(jobs):
                print(f"{done}/{len(jobs)} exported", flush=True)
    for error in errors:
        print(f"FAILED {error}")


if __name__ == "__main__":
    main()
