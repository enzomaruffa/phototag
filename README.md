# PhotoTag

AI-powered photo tagging and upload system for Immich. Automatically analyzes photos using OpenAI's vision models, embeds metadata into EXIF data, and uploads to your Immich server.

## Features

- **AI-Powered Analysis**: Uses OpenAI GPT-4 Vision to analyze photo content, subjects, and quality
- **EXIF Metadata Embedding**: Adds descriptions, tags, and ratings directly to photo files
- **Smart Tag Management**: Learns from your existing tags and suggests new ones for review
- **Workflow Management**: Organized inbox → processed → outbox workflow
- **Immich Integration**: Direct upload to Immich via SSH tunnel
- **RAW Support**: Handles RAW files (ARW, CR2, NEF, DNG) and standard formats
- **Resumable Processing**: Full state tracking - interrupt anytime and resume exactly where you left off
- **Parallel Processing**: Process multiple photos simultaneously with configurable worker count
- **Automatic Recovery**: Detects and recovers from stuck or failed photos

## Installation

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd phototag
   ```

2. **Install with uv** (recommended):
   ```bash
   uv sync
   ```

   Or with pip:
   ```bash
   pip install -e .
   ```

3. **Set up environment variables** in `.env`:
   ```env
   OPENAI_API_KEY=your_openai_api_key_here
   
   # Directory configuration (optional - will use defaults)
   INBOX_DIR=./inbox
   PROCESSED_DIR=./processed
   OUTBOX_DIR=./outbox
   
   # Immich SSH configuration (choose one option)
   # Option 1: Use SSH config entry
   IMMICH_SSH_CONFIG_NAME=my-immich-server
   
   # Option 2: Direct host/user specification
   IMMICH_SERVER_HOST=your-immich-server.com
   IMMICH_SERVER_USER=your-username
   ```

4. Install exiftool for metadata handling:
   ```bash
   sudo apt install exiftool
   ```

   or on macOS with Homebrew:

   ```bash
   brew install exiftool
   ```

## Quick Start

1. **Check configuration**:
   ```bash
   phototag config
   ```

2. **Add photos to inbox**:
   ```bash
   mkdir inbox
   cp /path/to/your/photos/* inbox/
   ```

3. **Process photos with AI**:
   ```bash
   phototag process
   ```

4. **Review suggested tags**:
   ```bash
   phototag review-tags
   ```

5. **Upload to Immich**:
   ```bash
   phototag upload --album "My Photos"
   ```

## Resumption & Recovery Features

### Interrupt-Safe Processing

PhotoTag saves its state after every processing step:
1. **AI Analysis** - Saved immediately after completion
2. **Tag Check** - Tracks which tags need approval
3. **EXIF Writing** - Confirms metadata was written
4. **File Move** - Records final location

You can safely interrupt processing at any time with Ctrl+C and resume later:

```bash
# Start processing
phototag process --workers 4
# Press Ctrl+C to interrupt safely

# Resume where you left off
phototag process --continue
```

### Processing Status

Check the current state of all photos:

```bash
# Show overall statistics
phototag status

# Show details of failed photos
phototag status --failed

# Show photos waiting for tag approval
phototag status --awaiting-tags
```

### Recovery Commands

Handle stuck or failed photos:

```bash
# Reset photos stuck in processing
phototag reset-stuck

# Retry failed photos
phototag process --retry-failed

# Show database statistics
phototag db-stats

# Clean old processed records (>30 days)
phototag db-clean --days 30
```

## Commands

### `phototag process`

Analyzes photos with AI and embeds metadata into EXIF data.

```bash
phototag process [OPTIONS] [INBOX_DIR]

Options:
  -w, --workers INTEGER    Number of parallel workers (default: 2, max: CPU count)
  --skip-existing         Skip photos already processed (default: True)
  -c, --continue          Continue from previous interrupted session
  --retry-failed          Retry previously failed photos
```

**What it does:**
- Analyzes each photo for content, subjects, and quality
- Generates descriptions and suggests tags
- Embeds approved tags and metadata into EXIF data
- Saves state after every processing step for perfect resumption
- Moves processed photos to `processed/` directory atomically
- Saves new tag suggestions for review
- Supports graceful interruption with Ctrl+C

### `phototag review-tags`

Interactive review of AI-suggested tags.

```bash
phototag review-tags
```

**Features:**
- Shows table of pending tags with confidence scores
- Interactive approval/rejection
- Updates EXIF data for approved tags
- **Auto-completes** photos that were waiting for approved tags
- Learns from your decisions for future suggestions

### `phototag upload`

Uploads photos to Immich server via SSH tunnel.

```bash
phototag upload [OPTIONS] [SOURCE_DIR] [DESTINATION_DIR]

Options:
  -a, --album TEXT    Album name for upload
  --skip-ai BOOL     Skip AI analysis check (default: False)
```

**What it does:**
- Establishes SSH tunnel to Immich server
- Uploads all photos with embedded metadata
- Moves uploaded photos to `outbox/` directory
- Handles connection retries and failures gracefully

### `phototag config`

Shows current configuration and setup status.

```bash
phototag config
```

## Workflow

The typical workflow involves three directories:

1. **Inbox** (`./inbox/`): Drop photos here for processing
2. **Processed** (`./processed/`): AI-analyzed photos ready for upload
3. **Outbox** (`./outbox/`): Successfully uploaded photos

```
Photos -> Inbox -> [AI Analysis] -> Processed -> [Upload] -> Outbox
                         ↓
                  [State Database]
                   (Resume from any point)
```

### Parallel Processing

Process multiple photos simultaneously for faster throughput:

```bash
# Use 4 workers for faster processing
phototag process --workers 4

# Monitor progress with status in another terminal
watch phototag status
```

## AI Analysis

The AI service analyzes each photo for:

- **Content Description**: Natural language description of the photo
- **Subject Tags**: What's in the photo (people, objects, scenes)
- **Quality Rating**: 1-5 star rating based on technical and artistic quality
- **Technical Notes**: Camera settings, lighting conditions, etc.

### Tag Learning System

PhotoTag builds a knowledge base of your tagging preferences:

- **Approved Tags**: Tags you've approved for automatic use
- **Pending Tags**: New AI suggestions awaiting your review
- **Context-Aware**: AI considers your existing tags when making suggestions

## EXIF Metadata

The following metadata is embedded into your photos:

- `Description`: AI-generated photo description
- `Keywords`: Approved tags as comma-separated keywords
- `Rating`: 1-5 star quality rating
- `UserComment`: Technical notes and analysis details

## SSH Configuration

### Option 1: SSH Config Entry (Recommended)

Add to `~/.ssh/config`:
```
Host my-immich-server
    HostName your-server.com
    User your-username
    Port 22
    IdentityFile ~/.ssh/id_rsa
```

Then set:
```env
IMMICH_SSH_CONFIG_NAME=my-immich-server
```

### Option 2: Direct Configuration

```env
IMMICH_SERVER_HOST=your-server.com
IMMICH_SERVER_USER=your-username
```

## Supported Formats

- **Standard**: JPG, JPEG, PNG, TIFF, TIF
- **RAW**: RAW, DNG, CR2, NEF, ARW

RAW files are automatically processed for AI analysis while preserving the original file.

## Development

### Setup Development Environment

```bash
uv sync --group dev
```

### Code Quality

```bash
# Format code
uv run black .

# Lint code
uv run ruff check .

# Run tests
uv run pytest
```
