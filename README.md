# PhotoTag

AI-powered photo tagging and upload pipeline for Immich. Analyzes photos with OpenAI vision models, embeds ratings/tags/descriptions into EXIF, and uploads everything to your Immich server. Videos flow through the same pipeline without AI analysis.

```
inbox/ ──▶ [AI analysis + EXIF] ──▶ processed/ ──▶ [Immich upload] ──▶ outbox/
              (videos skip AI          │
               and move straight       ▼
               through)          [state database]
                                 (resume anywhere)
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
| `make retry` | Re-queue failed photos and process them again |
| `make review` | Review pending AI-suggested tags |
| `make upload` | Upload to Immich (`ALBUM="Trip 2026"` optional) |
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
| `INBOX_DIR` / `PROCESSED_DIR` / `OUTBOX_DIR` | Pipeline directories (default `./inbox`, `./processed`, `./outbox`) |
| `IMMICH_SSH_CONFIG_NAME` | SSH config entry for the Immich server tunnel |
| `IMMICH_SERVER_HOST` + `IMMICH_SERVER_USER` | Alternative to the SSH config entry |

`phototag config` shows what's set and what's missing.

## How processing works

Each photo moves through tracked states in a SQLite database (`.phototag/processing_state.db`), so you can interrupt at any point (Ctrl+C) and resume with `phototag process`:

1. **AI analysis** — GPT-4o rates the photo 1–5 stars, writes a description, and picks tags (strongly preferring your existing tag vocabulary). The response is saved immediately, so a photo is never analyzed (or billed) twice.
2. **Tag review** — brand-new tags are queued for your approval; by default photos continue processing with only approved tags, and `phototag review-tags` backfills approved tags into EXIF afterwards.
3. **EXIF write** — rating, description, keywords, and notes are embedded with exiftool (existing metadata is preserved, keywords are merged).
4. **Move** — the file lands in `processed/`, ready for upload.

**Videos** are detected by extension and go straight from step 0 to step 4 — no AI, no EXIF, just tracked and moved so they ride along to Immich with everything else.

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
make fmt    # black
make lint   # ruff
```
