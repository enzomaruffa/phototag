# PhotoTag

AI photo tagging + Immich upload pipeline: `inbox/ → (dedup check) → AI analysis → EXIF write → processed/ → Immich upload → outbox/`. Videos ride the pipeline without AI or EXIF. Duplicates are diverted from inbox straight to outbox at intake.

## Commands

Everything runs through `uv` and the Makefile — never bare `python`/`pip`.

- `make process` / `make watch` / `make retry` — analyze inbox (watch = polling loop)
- `make review` — approve/reject AI-suggested tags, backfills EXIF afterwards
- `make upload` — Immich upload over an SSH tunnel, then move to outbox
- `make sync-hashes` — mirror Immich asset checksums for cross-client dedup
- `make status` / `make failed` / `make doctor` — inspect and reconcile state
- `make check` — ruff check + ty + ruff format --check (run before committing)
- `make fmt` — ruff format (black was removed; do not reintroduce it)

## Architecture

- `phototag/cli.py` — all Typer commands; thin orchestration, rich output
- `phototag/processing/photo_processor.py` — batch enqueue (dedup happens here), multiprocessing workers, per-photo state machine
- `phototag/storage/state_db.py` — SQLite state DB (`.phototag/processing_state.db`), WAL mode, thread-local connections
- `phototag/storage/exif.py` — exiftool wrapper; `phototag/storage/immich.py` — SSH tunnel + Immich API/CLI
- `phototag/ai/openai_service.py` — vision analysis, JSON-mode responses, retry with backoff
- `phototag/media.py` — extension sets, `file_hashes()`, `is_stable()`, `unique_destination()`
- `phototag/dating.py` — capture-date inference for dateless sources (toy cams, film scans): printed-stamp parsing, neighbour anchors, filename-sequence offsets, and the AI source-class prompt hint

## Invariants — read before touching the pipeline

- **`photos.filepath` (the ORIGINAL inbox path) is the primary key.** Records keep that key forever; `moved_to_path` tracks where the file actually lives now.
- **Files are hashed twice.** At intake (original bytes → `content_hash`) and at the move to processed/ (post-EXIF bytes → `processed_hash`/`processed_sha1`); `find_duplicate()` matches both. The EXIF write mutates the file, which is why the intake hash must happen first and why Immich's server-side dedup can't catch re-synced originals. Anything that rewrites EXIF on a processed file (tag backfill in `review_tags`) must refresh the processed hashes via `update_processed_hashes()`. `file_hashes()` computes SHA-256 (local identity) and SHA-1 (Immich's checksum algorithm) in one pass; `processed_sha1` equals Immich's checksum for the uploaded asset.
- **Photo status flow**: pending → locked → ai_analyzing → ai_analyzed → (awaiting_tag_review) → exif_writing → exif_written → moving → processed; terminal side-states: failed, duplicate. Duplicate records live in the outbox by design — `doctor` must not garbage-collect them, and `find_duplicate()` must keep excluding failed + duplicate rows.
- **AI responses are saved immediately** after analysis; any resume path must check `ai_response_json` before re-analyzing (re-analysis = re-billing).
- **Workers are separate processes** (ProcessPoolExecutor); anything passed to them must be picklable, and each creates its own service/DB instances. Dedup runs serially in the parent during enqueue precisely to avoid worker races.
- **Sync-tool safety**: files with mtime < 30s are skipped (`is_stable`) because Syncthing delivers partial files that look corrupt.
- **Capture dates are write-once and inferred conservatively.** `resolve_and_write_capture_date` never overwrites an existing EXIF date; inferred dates chain exif → printed stamp → neighbour anchor → mtime, and only `exif`/`stamp` sources may anchor neighbours (guesses don't compound). It runs at the EXIF step, BEFORE the post-EXIF hash, so `processed_hash` stays correct.
- Schema changes go in `_init_database` as idempotent `PRAGMA table_info` + `ALTER TABLE` migrations — the DB at `.phototag/` is live and never recreated.

## Conventions

- Formatting `ruff format`, linting `ruff check`, types `ty` — all three must pass (`make check`).
- Type hints everywhere; `Optional[X]` explicitly, never a bare `= None` default on a typed param.
- No test suite yet. Verify pipeline changes end-to-end with small `.mp4` files (videos skip AI, so the full intake → dedup → move flow runs without an OpenAI key) in a scratch directory — `ProcessingStateDB` keys off `Path.cwd()`.
- `.env` holds real credentials; `.env.example` is the template. New config vars must appear in `.env.example`, the README config table, and `phototag config`.
- Prefer reusing existing credentials over new config: `immich-sync` reads the API key `immich login` stored (`read_cli_api_key()` in `storage/immich.py`) rather than requiring `IMMICH_API_KEY`.
