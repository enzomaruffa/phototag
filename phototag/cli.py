"""CLI interface for phototag."""

import os
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Optional, List, Dict
import multiprocessing

import typer
from rich.console import Console
from rich.table import Table
from rich.prompt import Confirm, Prompt
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
)
from dotenv import load_dotenv

from phototag.media import (
    file_hashes,
    find_media_files,
    is_stable,
    is_video,
    unique_destination,
)
from phototag.storage.tag_review import TagReviewStorage
from phototag.storage.exif import EXIFHandler
from phototag.storage.immich import ImmichUploader, read_cli_api_key
from phototag.storage.state_db import ProcessingStateDB, PhotoStatus
from phototag.processing.photo_processor import PhotoProcessor

# Load environment variables
load_dotenv()

# Configure logging to show messages on console
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)

app = typer.Typer(help="AI-powered photo tagging and upload system for Immich")
console = Console()


def move_photos(photo_files: List[Path], destination_dir: Path) -> int:
    """Move media files to destination directory (EXIF metadata is embedded)."""
    moved_count = 0

    for photo_path in photo_files:
        try:
            dest_photo = unique_destination(destination_dir, photo_path)
            shutil.move(str(photo_path), str(dest_photo))
            console.print(f"📦 Moved: {photo_path.name} → {dest_photo.name}")
            moved_count += 1

        except Exception as e:
            console.print(f"⚠️  Failed to move {photo_path.name}: {e}", style="yellow")

    return moved_count


@app.command()
def process(
    inbox_dir: Optional[Path] = typer.Argument(
        None, help="Directory containing photos to process (defaults to INBOX_DIR)"
    ),
    workers: int = typer.Option(
        2,
        "--workers",
        "-w",
        help="Number of parallel workers (default: 2, max: CPU count)",
    ),
    skip_existing: bool = typer.Option(
        True, "--skip-existing", help="Skip photos already processed"
    ),
    continue_session: bool = typer.Option(
        False, "--continue", "-c", help="Continue from previous interrupted session"
    ),
    retry_failed: bool = typer.Option(
        False, "--retry-failed", help="Retry previously failed photos"
    ),
):
    """Process photos with AI analysis and embed EXIF metadata with resumption support."""

    # Get directory environment variables
    default_inbox = os.getenv("INBOX_DIR", "./inbox")
    default_processed = os.getenv("PROCESSED_DIR", "./processed")
    outbox_dir = Path(os.getenv("OUTBOX_DIR", "./outbox"))

    # Use provided directory or default
    if inbox_dir is None:
        inbox_dir = Path(default_inbox)

    processed_dir = Path(default_processed)
    processed_dir.mkdir(parents=True, exist_ok=True)

    # Log directory usage
    console.print(f"📁 Processing photos in: {inbox_dir.absolute()}")
    console.print(f"📦 Will move processed to: {processed_dir.absolute()}")

    # Validate environment
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        console.print("❌ OPENAI_API_KEY not found in environment", style="red")
        raise typer.Exit(1)

    if not inbox_dir.exists():
        console.print(f"❌ Directory not found: {inbox_dir}", style="red")
        raise typer.Exit(1)

    # Validate worker count
    max_workers = multiprocessing.cpu_count()
    if workers > max_workers:
        console.print(
            f"⚠️  Limiting workers to CPU count: {max_workers}", style="yellow"
        )
        workers = max_workers

    # Check for resumable session
    if continue_session:
        state_db = ProcessingStateDB()
        resumable = state_db.get_resumable_photos()
        total_resumable = sum(len(photos) for photos in resumable.values())

        if total_resumable > 0:
            console.print(
                f"🔄 Found {total_resumable} photos to resume from previous session:"
            )
            for status, photos in resumable.items():
                if photos:
                    console.print(f"  • {status}: {len(photos)} photos")
        else:
            console.print("ℹ️  No incomplete photos found from previous sessions")
            continue_session = False

    # Find photos to process, skipping files that may still be syncing
    all_files = find_media_files(inbox_dir)
    photo_files = [f for f in all_files if is_stable(f)]
    syncing = len(all_files) - len(photo_files)
    if syncing:
        console.print(
            f"⏳ Skipping {syncing} files modified <30s ago (probably still syncing) - rerun to pick them up",
            style="yellow",
        )
    video_count = sum(1 for f in photo_files if is_video(f))
    console.print(
        f"📸 Found {len(photo_files)} media files in inbox ({video_count} videos will pass through without AI analysis)"
    )

    if not photo_files and not continue_session:
        console.print("✅ No photos to process")
        return

    # Initialize processor
    processor = PhotoProcessor(
        api_key=api_key,
        inbox_dir=inbox_dir,
        processed_dir=processed_dir,
        worker_count=workers,
        outbox_dir=outbox_dir,
    )

    # Setup progress display
    def create_progress_display(stats: Dict[str, int]):
        """Create a rich progress display for processing."""
        table = Table(title="Processing Status", show_header=True)
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right")

        status_emojis = {
            PhotoStatus.PENDING.value: "🕰️",
            PhotoStatus.AI_ANALYZING.value: "🤖",
            PhotoStatus.AWAITING_TAG_REVIEW.value: "🏷️",
            PhotoStatus.PROCESSED.value: "✅",
            PhotoStatus.FAILED.value: "❌",
            PhotoStatus.DUPLICATE.value: "👯",
        }

        for status, count in stats.items():
            if count > 0:
                emoji = status_emojis.get(status, "📎")
                table.add_row(f"{emoji} {status}", str(count))

        return table

    # Process with progress tracking
    console.print(f"\n🚀 Starting processing with {workers} workers...")
    console.print("Press Ctrl+C to safely interrupt\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"Processing photos with {workers} workers...",
            total=None,  # Indeterminate progress
        )

        def update_progress(stats):
            processed = stats.get(PhotoStatus.PROCESSED.value, 0)
            failed = stats.get(PhotoStatus.FAILED.value, 0)
            awaiting = stats.get(PhotoStatus.AWAITING_TAG_REVIEW.value, 0)

            progress.update(
                task,
                description=f"Processed: {processed} | Awaiting tags: {awaiting} | Failed: {failed}",
            )

        # Run processing
        results = processor.process_batch(
            photo_files,
            skip_existing=skip_existing,
            continue_session=continue_session,
            retry_failed=retry_failed,
            progress_callback=update_progress,
        )

    # Show final results (scoped to this run - not all-time database totals)
    console.print("\n🎯 Processing Complete!")

    if results.get("duplicates"):
        console.print(
            f"👯 {results['duplicates']} duplicates diverted straight to the outbox (already uploaded)"
        )

    final_state_db = ProcessingStateDB()
    final_stats = final_state_db.get_statistics(since=results.get("started_at"))
    console.print(create_progress_display(final_stats))

    if results["interrupted"] > 0:
        console.print(
            "\n⚠️  Processing was interrupted. Run with --continue to resume.",
            style="yellow",
        )

    # Check for pending tags
    tag_storage = TagReviewStorage()
    if tag_storage.has_pending_tags():
        pending_count = len(tag_storage.get_pending_tags())
        console.print(f"\n🏷️  {pending_count} new tags need review", style="yellow")
        console.print("Run 'phototag review-tags' to approve/reject new tags")

    # Show failed photos if any
    if final_stats.get(PhotoStatus.FAILED.value, 0) > 0:
        console.print(
            f"\n❌ {final_stats[PhotoStatus.FAILED.value]} photos failed. Run 'phototag status --failed' for details, then 'phototag retry' to re-queue them.",
            style="red",
        )


@app.command()
def watch(
    interval: int = typer.Option(
        30, "--interval", "-i", help="Seconds between inbox scans"
    ),
    workers: int = typer.Option(
        2, "--workers", "-w", help="Number of parallel workers"
    ),
):
    """Watch the inbox and automatically process new files as they arrive (Ctrl+C to stop)."""

    inbox_dir = Path(os.getenv("INBOX_DIR", "./inbox"))
    processed_dir = Path(os.getenv("PROCESSED_DIR", "./processed"))
    outbox_dir = Path(os.getenv("OUTBOX_DIR", "./outbox"))
    processed_dir.mkdir(parents=True, exist_ok=True)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        console.print("❌ OPENAI_API_KEY not found in environment", style="red")
        raise typer.Exit(1)

    if not inbox_dir.exists():
        console.print(f"❌ Directory not found: {inbox_dir}", style="red")
        raise typer.Exit(1)

    console.print(
        f"👀 Watching {inbox_dir.absolute()} (every {interval}s, Ctrl+C to stop)"
    )

    try:
        while True:
            all_files = find_media_files(inbox_dir)
            stable = [f for f in all_files if is_stable(f)]
            syncing = len(all_files) - len(stable)

            if stable:
                video_count = sum(1 for f in stable if is_video(f))
                console.print(
                    f"\n📸 Found {len(stable)} new files ({video_count} videos) - processing..."
                )
                processor = PhotoProcessor(
                    api_key=api_key,
                    inbox_dir=inbox_dir,
                    processed_dir=processed_dir,
                    worker_count=workers,
                    outbox_dir=outbox_dir,
                )
                results = processor.process_batch(stable)
                console.print(
                    f"✅ Done: {results['processed']} processed, "
                    f"{results['awaiting_tags']} awaiting tags, "
                    f"{results['duplicates']} duplicates diverted, {results['failed']} failed"
                )
                if results["failed"]:
                    console.print(
                        "Run 'phototag retry' to re-queue failures", style="yellow"
                    )
            elif syncing:
                console.print(f"⏳ {syncing} files still syncing, waiting...")

            time.sleep(interval)

    except KeyboardInterrupt:
        console.print("\n👋 Stopped watching")


@app.command()
def upload(
    source_dir: Optional[Path] = typer.Argument(
        None, help="Directory containing photos to upload (defaults to PROCESSED_DIR)"
    ),
    destination_dir: Optional[Path] = typer.Argument(
        None, help="Directory to move photos after upload (defaults to OUTBOX_DIR)"
    ),
    album: Optional[str] = typer.Option(
        None, "--album", "-a", help="Album name for upload"
    ),
    skip_ai: bool = typer.Option(
        False, "--skip-ai", help="Skip AI analysis and just upload"
    ),
):
    """Upload photos to Immich (with tag review check)."""

    # Get directory environment variables
    default_processed = os.getenv("PROCESSED_DIR", "./processed")
    default_outbox = os.getenv("OUTBOX_DIR", "./outbox")

    # Use provided directories or defaults
    if source_dir is None:
        source_dir = Path(default_processed)
    if destination_dir is None:
        destination_dir = Path(default_outbox)

    # Log directory usage
    console.print(f"📁 Using source directory: {source_dir.absolute()}")
    console.print(f"📁 Will move photos to: {destination_dir.absolute()}")

    # Validate directories
    if not source_dir.exists():
        console.print(f"❌ Source directory not found: {source_dir}", style="red")
        raise typer.Exit(1)

    # Create destination directory if it doesn't exist
    destination_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"✅ Destination directory ready: {destination_dir}")

    # Get required environment variables
    server_host = os.getenv("IMMICH_SERVER_HOST")
    server_user = os.getenv("IMMICH_SERVER_USER")
    ssh_config_name = os.getenv("IMMICH_SSH_CONFIG_NAME")

    if not ssh_config_name and not all([server_host, server_user]):
        console.print(
            "❌ Either IMMICH_SSH_CONFIG_NAME or both IMMICH_SERVER_HOST and IMMICH_SERVER_USER must be set",
            style="red",
        )
        raise typer.Exit(1)

    if not skip_ai:
        # Check for pending tags
        tag_storage = TagReviewStorage()
        if tag_storage.has_pending_tags():
            pending_count = len(tag_storage.get_pending_tags())
            console.print(f"⚠️  {pending_count} tags pending review", style="yellow")

            should_continue = Confirm.ask("Upload anyway without reviewing tags?")
            if not should_continue:
                console.print("Upload cancelled. Run 'phototag review-tags' first.")
                raise typer.Exit(0)

    # Upload to Immich
    try:
        if ssh_config_name:
            uploader_context = ImmichUploader(ssh_config_name=ssh_config_name)
        else:
            uploader_context = ImmichUploader(server_host, server_user)

        with uploader_context as uploader:
            console.print("🔗 SSH tunnel established")

            def retry_callback() -> bool:
                """Ask user if they want to retry after connection failure."""
                return Confirm.ask(
                    "⚠️  Connection lost during upload. Retry?", default=True
                )

            success = uploader.upload_photos(
                photo_dir=source_dir, album_name=album, retry_callback=retry_callback
            )

            if success:
                console.print(
                    f"✅ Upload completed. {len(find_media_files(source_dir))} files uploaded successfully."
                )

                # Move photos to destination directory
                console.print("📦 Moving photos to destination directory...")
                photo_files = find_media_files(source_dir)
                moved_count = move_photos(photo_files, destination_dir)
                console.print(f"✅ Moved {moved_count} photos to {destination_dir}")

            else:
                console.print("❌ Upload failed", style="red")
                raise typer.Exit(1)

    except Exception as e:
        console.print(f"❌ Upload failed: {e}", style="red")
        raise typer.Exit(1)


@app.command()
def immich_sync():
    """Pull asset checksums from Immich so duplicates of photos uploaded by ANY client are caught.

    Run this from time to time (especially if other devices upload to Immich):
    'phototag process' checks incoming files against these checksums and
    diverts matches straight to the outbox instead of re-uploading them.
    """
    # Reuse the key 'immich login' already stored; IMMICH_API_KEY only overrides
    api_key = os.getenv("IMMICH_API_KEY") or read_cli_api_key()
    if not api_key:
        console.print(
            "❌ No Immich API key found - run 'immich login' (which the upload flow needs anyway) or set IMMICH_API_KEY",
            style="red",
        )
        raise typer.Exit(1)

    server_host = os.getenv("IMMICH_SERVER_HOST")
    server_user = os.getenv("IMMICH_SERVER_USER")
    ssh_config_name = os.getenv("IMMICH_SSH_CONFIG_NAME")

    if not ssh_config_name and not all([server_host, server_user]):
        console.print(
            "❌ Either IMMICH_SSH_CONFIG_NAME or both IMMICH_SERVER_HOST and IMMICH_SERVER_USER must be set",
            style="red",
        )
        raise typer.Exit(1)

    try:
        if ssh_config_name:
            uploader_context = ImmichUploader(ssh_config_name=ssh_config_name)
        else:
            uploader_context = ImmichUploader(server_host, server_user)

        with uploader_context as uploader:
            console.print("🔗 SSH tunnel established")
            console.print("🔍 Fetching asset checksums from Immich...")
            checksums = uploader.get_asset_checksums(api_key)

        if checksums is None:
            console.print("❌ Could not fetch checksums from Immich", style="red")
            raise typer.Exit(1)

        state_db = ProcessingStateDB()
        count = state_db.replace_immich_checksums(checksums)
        console.print(f"✅ Synced {count} asset checksums from Immich")
        console.print(
            "New inbox files matching any of them will be diverted straight to the outbox"
        )

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ Sync failed: {e}", style="red")
        raise typer.Exit(1)


@app.command()
def review_tags(
    approve_all: bool = typer.Option(
        False, "--approve-all", "-a", help="Approve every pending tag without prompting"
    ),
):
    """Review and approve/reject pending tags."""
    tag_storage = TagReviewStorage()
    exif_handler = EXIFHandler()

    pending_tags = tag_storage.get_pending_tags()

    if not pending_tags:
        console.print("✅ No tags pending review")
        return

    console.print(f"📋 {len(pending_tags)} tags pending review:")

    # Show pending tags table
    table = Table()
    table.add_column("Tag Name")
    table.add_column("Suggested By Photo")
    table.add_column("Confidence")

    for tag in pending_tags:
        confidence = f"{tag.confidence:.2f}" if tag.confidence else "N/A"
        table.add_row(tag.name, Path(tag.suggested_by_photo).name, confidence)

    console.print(table)

    # Approval: bulk or one-by-one
    approved_tags = []
    rejected_tags = []

    if approve_all:
        approved_tags = [tag.name for tag in pending_tags]
    else:
        mode = Prompt.ask(
            f"Review {len(pending_tags)} tags",
            choices=["all", "none", "each", "quit"],
            default="each",
        )
        if mode == "quit":
            return
        elif mode == "all":
            approved_tags = [tag.name for tag in pending_tags]
        elif mode == "none":
            rejected_tags = [tag.name for tag in pending_tags]
        else:
            for tag in pending_tags:
                choice = Prompt.ask(
                    f"Approve tag '{tag.name}'?", choices=["y", "n", "q"], default="y"
                )

                if choice == "q":
                    break
                elif choice == "y":
                    approved_tags.append(tag.name)
                else:
                    rejected_tags.append(tag.name)

    # Apply decisions
    if approved_tags:
        console.print(f"✅ Approving {len(approved_tags)} tags...")
        tag_storage.approve_tags(approved_tags)

        # Backfill EXIF for every photo whose AI analysis wanted these tags,
        # at the photo's CURRENT location (they've usually moved to processed/)
        state_db = ProcessingStateDB()
        photos_needing = state_db.get_photos_needing_tags(approved_tags)

        updated = 0
        gone = 0
        for photo_path, tags_for_photo in photos_needing.items():
            photo_file = Path(photo_path)
            if photo_file.exists():
                if exif_handler.update_exif_tags(photo_file, tags_for_photo):
                    updated += 1
                    # The EXIF rewrite changed the bytes - keep the stored
                    # post-EXIF hashes in sync for re-entry dedup
                    hashes = file_hashes(photo_file)
                    if hashes:
                        state_db.update_processed_hashes(
                            photo_path, hashes.sha256, hashes.sha1
                        )
            else:
                gone += 1

        console.print(f"📝 Backfilled tags into {updated} photos")
        if gone:
            console.print(
                f"⚠️  {gone} photos no longer on disk (already uploaded?) - skipped",
                style="yellow",
            )

        # Check for photos that were waiting for these tags to be approved
        console.print("\n🔍 Checking for photos awaiting these tags...")

        # Get processed directory
        processed_dir = Path(os.getenv("PROCESSED_DIR", "./processed"))
        processed_dir.mkdir(parents=True, exist_ok=True)

        # Use processor to complete pending photos
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            processor = PhotoProcessor(
                api_key=api_key,
                inbox_dir=Path(os.getenv("INBOX_DIR", "./inbox")),
                processed_dir=processed_dir,
                worker_count=1,
            )

            completed = processor.complete_pending_photos(approved_tags)
            if completed > 0:
                console.print(
                    f"🎉 Automatically completed {completed} photos that were waiting for these tags!"
                )
            else:
                console.print("ℹ️  No photos were waiting for these specific tags")
        else:
            console.print(
                "⚠️  Could not check for waiting photos (API key not configured)",
                style="yellow",
            )

    if rejected_tags:
        console.print(f"❌ Rejecting {len(rejected_tags)} tags...")
        tag_storage.reject_tags(rejected_tags)

    remaining = len(tag_storage.get_pending_tags())
    if remaining == 0:
        console.print("🎉 All tags reviewed! Ready for upload.")
    else:
        console.print(f"📋 {remaining} tags still pending")


@app.command()
def status(
    failed: bool = typer.Option(
        False, "--failed", "-f", help="Show details of failed photos"
    ),
    awaiting_tags: bool = typer.Option(
        False, "--awaiting-tags", "-t", help="Show photos awaiting tag approval"
    ),
):
    """Show current processing status and statistics."""
    state_db = ProcessingStateDB()

    # Get statistics
    stats = state_db.get_statistics()

    # Create main status table
    table = Table(title="📊 Processing Status", show_header=True)
    table.add_column("Status", style="cyan", width=25)
    table.add_column("Count", justify="right", style="bold")
    table.add_column("Description", style="dim")

    status_info = {
        PhotoStatus.PENDING.value: ("🕰️ Pending", "Waiting to be processed"),
        PhotoStatus.LOCKED.value: ("🔒 Locked", "Currently being processed"),
        PhotoStatus.AI_ANALYZING.value: ("🤖 AI Analyzing", "AI analysis in progress"),
        PhotoStatus.AI_ANALYZED.value: (
            "📤 AI Analyzed",
            "Analysis complete, pending next step",
        ),
        PhotoStatus.AWAITING_TAG_REVIEW.value: (
            "🏷️ Awaiting Tags",
            "Waiting for tag approval",
        ),
        PhotoStatus.EXIF_WRITING.value: ("✏️ Writing EXIF", "Writing metadata"),
        PhotoStatus.EXIF_WRITTEN.value: (
            "📝 EXIF Written",
            "Metadata written, pending move",
        ),
        PhotoStatus.MOVING.value: ("📦 Moving", "Moving to processed folder"),
        PhotoStatus.PROCESSED.value: ("✅ Processed", "Successfully completed"),
        PhotoStatus.FAILED.value: ("❌ Failed", "Processing failed"),
        PhotoStatus.DUPLICATE.value: (
            "👯 Duplicate",
            "Same content as an earlier photo; diverted to outbox",
        ),
    }

    total = 0
    for status_value, (display_name, description) in status_info.items():
        count = stats.get(status_value, 0)
        if count > 0:
            table.add_row(display_name, str(count), description)
            total += count

    if total == 0:
        console.print("💭 No photos in processing database")
        return

    console.print(table)
    console.print(f"\n📁 Total photos tracked: {total}")

    # Show Immich checksum mirror freshness
    immich_info = state_db.immich_checksum_info()
    if immich_info["count"]:
        console.print(
            f"🌐 Immich checksums mirrored: {immich_info['count']} "
            f"(last synced {immich_info['synced_at']} UTC - run 'phototag immich-sync' to refresh)"
        )
    else:
        console.print(
            "🌐 No Immich checksums mirrored - run 'phototag immich-sync' to catch duplicates of photos uploaded by other clients"
        )

    # Show stuck photos
    stuck = state_db.get_stuck_photos()
    if stuck:
        console.print(f"\n⚠️  {len(stuck)} photos appear to be stuck (locked > 5 min)")
        console.print("Run 'phototag reset-stuck' to unlock them")

    # Show failed photos details
    if failed:
        failed_photos = state_db.get_failed_photos()
        if failed_photos:
            console.print(f"\n❌ Failed Photos ({len(failed_photos)} total):")

            failed_table = Table(show_header=True)
            failed_table.add_column("Photo", style="red")
            failed_table.add_column("Error", style="yellow")
            failed_table.add_column("Failed At")
            failed_table.add_column("Retries")

            for photo in failed_photos[:10]:  # Show max 10
                failed_table.add_row(
                    Path(photo["filepath"]).name,
                    photo["error_message"][:50] + "..."
                    if len(photo["error_message"]) > 50
                    else photo["error_message"],
                    photo["error_at"] if photo["error_at"] else "Unknown",
                    str(photo["retry_count"]),
                )

            console.print(failed_table)
            if len(failed_photos) > 10:
                console.print(f"... and {len(failed_photos) - 10} more")
            console.print(
                "\nRun 'phototag process --retry-failed' to retry failed photos"
            )

    # Show photos awaiting tags
    if awaiting_tags:
        awaiting = []
        result = state_db._get_connection().execute(
            "SELECT filepath, pending_tags_json FROM photos WHERE status = ?",
            (PhotoStatus.AWAITING_TAG_REVIEW.value,),
        )

        for row in result:
            if row["pending_tags_json"]:
                import json

                tags = json.loads(row["pending_tags_json"])
                awaiting.append((Path(row["filepath"]).name, tags))

        if awaiting:
            console.print(f"\n🏷️ Photos Awaiting Tag Approval ({len(awaiting)} total):")

            tag_table = Table(show_header=True)
            tag_table.add_column("Photo", style="cyan")
            tag_table.add_column("Pending Tags", style="yellow")

            for photo_name, tags in awaiting[:10]:
                tag_table.add_row(photo_name, ", ".join(tags))

            console.print(tag_table)
            if len(awaiting) > 10:
                console.print(f"... and {len(awaiting) - 10} more")
            console.print("\nRun 'phototag review-tags' to approve/reject pending tags")


@app.command()
def reset_stuck():
    """Reset photos that have been stuck in processing."""
    state_db = ProcessingStateDB()

    stuck = state_db.get_stuck_photos()
    if not stuck:
        console.print("✅ No stuck photos found")
        return

    console.print(f"🔄 Found {len(stuck)} stuck photos:")
    for photo_path in stuck[:5]:
        console.print(f"  • {Path(photo_path).name}")
    if len(stuck) > 5:
        console.print(f"  ... and {len(stuck) - 5} more")

    if Confirm.ask("Reset these photos to pending?"):
        count = state_db.unlock_stuck_photos()
        console.print(f"✅ Reset {count} photos to pending status")
        console.print("Run 'phototag process --continue' to resume processing")


@app.command()
def retry():
    """Re-queue failed photos so the next 'phototag process' run picks them up."""
    state_db = ProcessingStateDB()

    failed_photos = state_db.get_failed_photos()
    if not failed_photos:
        console.print("✅ No failed photos to retry")
        return

    reset_count = 0
    missing_count = 0
    for photo in failed_photos:
        if Path(photo["filepath"]).exists():
            state_db.reset_photo(photo["filepath"])
            reset_count += 1
        else:
            missing_count += 1

    console.print(f"🔄 Re-queued {reset_count} failed photos")
    if missing_count:
        console.print(
            f"⚠️  Skipped {missing_count} whose files no longer exist (run 'phototag doctor' to clean them up)",
            style="yellow",
        )
    console.print("Run 'phototag process' to process them")


@app.command()
def doctor():
    """Reconcile the state database with what's actually on disk."""
    state_db = ProcessingStateDB()
    processed_dir = Path(os.getenv("PROCESSED_DIR", "./processed"))

    stuck_statuses = set(ProcessingStateDB.STUCK_STATUSES)
    reset_count = 0
    marked_processed = 0
    deleted = 0

    for photo in state_db.get_all_photos():
        filepath = photo["filepath"]
        status = photo["status"]

        # Duplicates live in the outbox by design - their records are history,
        # not drift, so don't garbage-collect them
        if status in (PhotoStatus.PROCESSED.value, PhotoStatus.DUPLICATE.value):
            continue

        if Path(filepath).exists():
            # File is still in the inbox; unstick it if a worker died mid-pipeline
            if status in stuck_statuses:
                state_db.reset_photo(filepath)
                reset_count += 1
        else:
            # File is gone from the inbox
            if (processed_dir / Path(filepath).name).exists():
                # It made it to processed but the database never heard about it
                state_db.update_photo_status(
                    filepath,
                    PhotoStatus.PROCESSED,
                    {"moved_to": str(processed_dir / Path(filepath).name)},
                )
                marked_processed += 1
            else:
                # Orphaned record - file was moved or deleted outside phototag
                state_db.delete_photo(filepath)
                deleted += 1

    if not (reset_count or marked_processed or deleted):
        console.print("✅ Database and disk are in sync - nothing to fix")
        return

    if reset_count:
        console.print(f"🔄 Reset {reset_count} stuck photos to pending")
    if marked_processed:
        console.print(
            f"✅ Marked {marked_processed} photos as processed (found in processed dir)"
        )
    if deleted:
        console.print(f"🗑️  Removed {deleted} orphaned records (files no longer exist)")


@app.command()
def db_clean(
    days: int = typer.Option(
        30, "--days", "-d", help="Remove records older than this many days"
    ),
):
    """Clean up old processing records from database."""
    state_db = ProcessingStateDB()

    # Get current statistics before cleanup
    stats = state_db.get_statistics()
    processed_count = stats.get(PhotoStatus.PROCESSED.value, 0)

    if processed_count == 0:
        console.print("ℹ️  No processed records to clean")
        return

    console.print(f"🗑️  Will remove processed records older than {days} days")
    console.print(f"📁 Currently tracking {processed_count} processed photos")

    if Confirm.ask("Proceed with cleanup?"):
        removed = state_db.cleanup_old_records(days)
        console.print(f"✅ Removed {removed} old records")

        # Show new statistics
        new_stats = state_db.get_statistics()
        new_processed = new_stats.get(PhotoStatus.PROCESSED.value, 0)
        console.print(f"📁 Now tracking {new_processed} processed photos")


@app.command()
def db_stats():
    """Show detailed database statistics."""
    state_db = ProcessingStateDB()

    # Get statistics
    stats = state_db.get_statistics()

    # Get database file size
    db_path = state_db.db_path
    db_size = db_path.stat().st_size if db_path.exists() else 0

    # Format size
    if db_size < 1024:
        size_str = f"{db_size} bytes"
    elif db_size < 1024 * 1024:
        size_str = f"{db_size / 1024:.1f} KB"
    else:
        size_str = f"{db_size / (1024 * 1024):.1f} MB"

    console.print("\n🗜️ Database Statistics")
    console.print(f"Location: {db_path}")
    console.print(f"Size: {size_str}")

    # Get session information
    conn = state_db._get_connection()
    result = conn.execute("SELECT COUNT(*) as count FROM processing_sessions")
    session_count = result.fetchone()["count"]

    result = conn.execute(
        """SELECT 
            COUNT(*) as total,
            SUM(photos_processed) as processed,
            SUM(photos_failed) as failed
        FROM processing_sessions"""
    )
    session_stats = result.fetchone()

    console.print(f"\n📦 Processing Sessions: {session_count}")
    if session_stats["processed"]:
        console.print(f"  • Total processed: {session_stats['processed']}")
    if session_stats["failed"]:
        console.print(f"  • Total failed: {session_stats['failed']}")

    # Show photo distribution
    total = sum(stats.values())
    if total > 0:
        console.print(f"\n📸 Photos in Database: {total}")

        # Create a simple bar chart
        max_count = max(stats.values()) if stats else 1
        for status in [
            PhotoStatus.PROCESSED,
            PhotoStatus.FAILED,
            PhotoStatus.AWAITING_TAG_REVIEW,
            PhotoStatus.PENDING,
        ]:
            count = stats.get(status.value, 0)
            if count > 0:
                bar_length = int((count / max_count) * 30)
                bar = "█" * bar_length
                percentage = (count / total) * 100
                console.print(
                    f"  {status.value:20} {bar} {count:5} ({percentage:.1f}%)"
                )


@app.command()
def config():
    """Show configuration and setup instructions."""
    console.print("📋 Configuration Requirements:")
    console.print()

    required_vars = [
        ("OPENAI_API_KEY", "OpenAI API key for photo analysis"),
    ]

    directory_vars = [
        ("OPENAI_MODEL", "Vision model for analysis (default: gpt-4o-mini)"),
        ("INBOX_DIR", "Default directory for photos to process (default: ./inbox)"),
        (
            "PROCESSED_DIR",
            "Intermediate directory for processed photos (default: ./processed)",
        ),
        ("OUTBOX_DIR", "Final directory for uploaded photos (default: ./outbox)"),
    ]

    optional_vars = [
        ("IMMICH_SERVER_HOST", "Immich server hostname (or use SSH config)"),
        ("IMMICH_SERVER_USER", "SSH username for server (or use SSH config)"),
        ("IMMICH_SSH_CONFIG_NAME", "SSH config entry name (alternative to host/user)"),
        (
            "IMMICH_API_KEY",
            "Optional override; 'immich-sync' reuses the key stored by 'immich login'",
        ),
    ]

    # Required variables table
    table = Table(title="Required Variables")
    table.add_column("Environment Variable")
    table.add_column("Description")
    table.add_column("Status")

    for var, desc in required_vars:
        value = os.getenv(var)
        status = "✅ Set" if value else "❌ Missing"
        table.add_row(var, desc, status)

    console.print(table)
    console.print()

    # Directory configuration
    dir_table = Table(title="Directory Configuration (optional)")
    dir_table.add_column("Environment Variable")
    dir_table.add_column("Description")
    dir_table.add_column("Status")

    for var, desc in directory_vars:
        value = os.getenv(var)
        status = f"✅ Set: {value}" if value else "📁 Using default"
        dir_table.add_row(var, desc, status)

    console.print(dir_table)
    console.print()

    # SSH configuration options
    ssh_table = Table(title="SSH Configuration (choose one option)")
    ssh_table.add_column("Environment Variable")
    ssh_table.add_column("Description")
    ssh_table.add_column("Status")

    for var, desc in optional_vars:
        value = os.getenv(var)
        status = "✅ Set" if value else "❌ Missing"
        ssh_table.add_row(var, desc, status)

    console.print(ssh_table)
    console.print()
    console.print("Create a .env file in your project directory with these variables.")
    console.print(
        "For SSH: either set IMMICH_SSH_CONFIG_NAME (to use ~/.ssh/config entry)"
    )
    console.print("or set both IMMICH_SERVER_HOST and IMMICH_SERVER_USER.")


if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n⚠️  Interrupted by user", style="yellow")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n❌ Error: {e}", style="red")
        sys.exit(1)
