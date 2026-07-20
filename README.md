# PhotoTag

AI-powered photo tagging and upload pipeline for Immich. Analyzes photos with OpenAI vision models, embeds ratings/tags/descriptions into EXIF, and uploads everything to your Immich server. Videos flow through the same pipeline without AI analysis. Duplicates never enter the pipeline at all — they're detected by content hash at intake and diverted straight to the outbox.

```
inbox/ ──▶ [dedup check] ──▶ [AI analysis + EXIF] ──▶ processed/ ──▶ [Immich upload] ──▶ outbox/
                │               (videos skip AI          │
                │ duplicate      and move straight       ▼
                │ content        through)          [state database]
                ▼                                  (resume anywhere)
             outbox/  (already uploaded — skip AI cost and re-upload)
```

## Quick start

```bash
make setup            # install dependencies (uv)
cp .env.example .env  # then fill in OPENAI_API_KEY + Immich SSH settings

# Drop photos/videos into inbox/, then:
make process          # AI-analyze photos, pass videos through
make review           # approve/reject new AI-suggested tags
make upload           # upload processed/ to Immich, move to outbox/
```

`make help` lists everything:

| Target | What it does |
|---|---|
| `make process` | Analyze inbox photos with AI (`WORKERS=4` to override) |
| `make watch` | Watch the inbox and auto-process new files as they arrive |
| `make retry` | Re-queue failed photos and process them again |
| `make review` | Review pending AI-suggested tags (bulk approve/reject or one-by-one) |
| `make upload` | Upload to Immich (`ALBUM="Trip 2026"` optional) |
| `make sync-hashes` | Pull Immich asset checksums so duplicates from any client are caught |
| `make status` | Show processing status |
| `make failed` | Show failed photo details |
| `make doctor` | Fix database/disk drift (stuck or orphaned records) |

All targets wrap the `phototag` CLI (`uv run phototag --help` for the full option set).

## Requirements

- [uv](https://docs.astral.sh/uv/)
- `exiftool` (`brew install exiftool` / `apt install exiftool`)
- `immich` CLI for uploads
- An OpenAI API key

## Configuration

Set in `.env` (see `.env.example`):

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Required for photo analysis |
| `OPENAI_MODEL` | Vision model (default `gpt-4o-mini`; set `gpt-4o` for max quality at ~15× the cost) |
| `INBOX_DIR` / `PROCESSED_DIR` / `OUTBOX_DIR` | Pipeline directories (default `./inbox`, `./processed`, `./outbox`) |
| `IMMICH_SSH_CONFIG_NAME` | SSH config entry for the Immich server tunnel |
| `IMMICH_SERVER_HOST` + `IMMICH_SERVER_USER` | Alternative to the SSH config entry |
| `IMMICH_API_KEY` | Optional — `immich-sync` reuses the key stored by `immich login`; set this only to override it |

`phototag config` shows what's set and what's missing.

## How processing works

Each photo moves through tracked states in a SQLite database (`.phototag/processing_state.db`), so you can interrupt at any point (Ctrl+C) and resume with `phototag process`:

1. **AI analysis** — the model rates the photo 1–5 stars, writes a description, and picks tags (strongly preferring your existing tag vocabulary). Responses use OpenAI's JSON mode, so they can't fail parsing, and are saved immediately — a photo is never analyzed (or billed) twice.
2. **Tag review** — brand-new tags are queued for your approval; by default photos continue processing with only approved tags, and `phototag review-tags` backfills approved tags into EXIF afterwards.
3. **EXIF write** — rating, description, keywords, and notes are embedded with exiftool (existing metadata is preserved, keywords are merged).
4. **Move** — the file lands in `processed/`, ready for upload.

**Videos** are detected by extension and go straight from step 0 to step 4 — no AI, no EXIF, just tracked and moved so they ride along to Immich with everything else.

### Duplicate detection

Every file is hashed **twice** along the pipeline, and incoming inbox files are checked against both:

- **At intake** (SHA-256 of the original bytes, before the EXIF write mutates them) — catches the same photo re-delivered by a sync tool under its old name or a new one, and identical copies within a batch.
- **At the move to `processed/`** (the post-EXIF bytes — exactly what gets uploaded) — catches copies of already-processed files re-entering the inbox: dragged back from the outbox, downloaded from Immich, or bounced through a sync loop. Tag backfill (`review-tags`) rewrites EXIF on processed files, so it refreshes these hashes as it goes.

A match means the file skips the whole pipeline and moves straight to `outbox/`: no AI cost, no re-upload, no duplicate in Immich. The opposite case — a *different* photo re-using an already-processed name — is detected by hash mismatch and processed as a new photo instead of being silently skipped.

Immich's own server-side checksum dedup can't replace the intake check: the EXIF write changes the uploaded bytes, so a re-synced original never matches what the server has. The local hash memory is what makes this work.

**`make sync-hashes`** (`phototag immich-sync`) extends dedup to photos that reached Immich through *other* clients (phone app, web upload). It mirrors the server's asset checksums (SHA-1) into the local database; intake then checks incoming files against that set too. Run it from time to time. No extra setup: it reuses the API key `immich login` already stored (the upload flow needs that login anyway); `IMMICH_API_KEY` overrides it if ever needed. `phototag status` shows how many checksums are mirrored and when they were last synced.

Caveats: detection is exact-content only (a re-encoded or resized copy won't match), photos processed before this feature have no stored hashes, and `phototag db-clean` forgets the hashes of the records it deletes.

### Sync-tool safety

Files modified less than 30 seconds ago are skipped (both in `process` and `watch`) — a file that a sync tool (Syncthing, Dropbox, ...) is still delivering looks like a corrupt image, and waiting one cycle avoids failing on it. `make watch` picks them up automatically on the next scan.

### Failure handling

- AI analysis retries 3× with backoff (2s, 8s) — enough to survive files still being delivered by a sync tool (Syncthing, Dropbox, etc.).
- Photos that still fail are marked in the database with the error. `make failed` shows them, `make retry` re-queues and reprocesses them.
- `make doctor` reconciles the database with the filesystem: unsticks records left behind by a killed worker, adopts files that made it to `processed/` without the database hearing about it, and deletes records whose files are gone.

## Supported formats

- **Photos**: JPG, JPEG, PNG, TIFF
- **RAW**: ARW, CR2, NEF, DNG, RAW (decoded via rawpy for analysis; originals untouched)
- **Videos** (passthrough): MP4, MOV, AVI, M4V, MPG, MPEG, MTS, M2TS, 3GP, WMV, WEBM, MKV

## SSH setup for Immich

Recommended — an entry in `~/.ssh/config`:

```
Host my-immich-server
    HostName your-server.com
    User your-username
    IdentityFile ~/.ssh/id_rsa
```

with `IMMICH_SSH_CONFIG_NAME=my-immich-server` in `.env`. The uploader opens a tunnel to the server's Immich port (2283), reuses an existing tunnel when one is up, and retries on connection loss.

## Development

```bash
uv sync --group dev
make fmt        # ruff format
make lint       # ruff check
make typecheck  # ty
make check      # lint + typecheck + format check
```
