"""CLI interface for phototag."""

import asyncio
import os
import logging
import shutil
from pathlib import Path
from typing import Optional, List
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table
from rich.prompt import Confirm, Prompt
from dotenv import load_dotenv

from phototag.ai.openai_service import OpenAIService
from phototag.storage.tag_review import TagReviewStorage
from phototag.storage.exif import EXIFHandler
from phototag.storage.immich import ImmichUploader
from phototag.models.ai import ProcessedPhoto

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


def get_photo_files(directory: Path) -> List[Path]:
    """Get list of supported photo files."""
    extensions = {
        ".jpg",
        ".jpeg",
        ".png",
        ".tiff",
        ".tif",
        ".raw",
        ".dng",
        ".cr2",
        ".nef",
        ".arw",
    }
    return [f for f in directory.rglob("*") if f.suffix.lower() in extensions]


def move_photos(photo_files: List[Path], destination_dir: Path) -> int:
    """Move photos to destination directory (EXIF metadata is embedded)."""
    moved_count = 0

    for photo_path in photo_files:
        try:
            # Move the photo file
            dest_photo = destination_dir / photo_path.name
            if dest_photo.exists():
                # Handle naming conflict by adding timestamp
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                stem = photo_path.stem
                suffix = photo_path.suffix
                dest_photo = destination_dir / f"{stem}_{timestamp}{suffix}"

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
    parallel: int = typer.Option(
        1, "--parallel", "-p", help="Number of parallel AI analyses"
    ),
    skip_existing: bool = typer.Option(
        True, "--skip-existing", help="Skip photos with existing EXIF metadata"
    ),
):
    """Process photos with AI analysis and embed EXIF metadata."""

    # Get directory environment variable
    default_inbox = os.getenv("INBOX_DIR", "./inbox")

    # Use provided directory or default
    if inbox_dir is None:
        inbox_dir = Path(default_inbox)

    # Log directory usage
    console.print(f"📁 Processing photos in: {inbox_dir.absolute()}")

    # Validate environment
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        console.print("❌ OPENAI_API_KEY not found in environment", style="red")
        raise typer.Exit(1)

    if not inbox_dir.exists():
        console.print(f"❌ Directory not found: {inbox_dir}", style="red")
        raise typer.Exit(1)

    # Initialize services
    ai_service = OpenAIService(api_key)
    tag_storage = TagReviewStorage()
    exif_handler = EXIFHandler()

    # Get existing tags from approved and pending lists for AI context
    existing_tags = tag_storage.get_all_available_tags()
    approved_count = len(tag_storage.get_approved_tag_names())
    pending_count = len(tag_storage.get_pending_tag_names())
    console.print(
        f"📋 Found {approved_count} approved + {pending_count} pending tags for AI context"
    )

    # Find photos to process
    photo_files = get_photo_files(inbox_dir)
    # Skip files that already have EXIF metadata with description (simple check)
    if skip_existing:
        filtered_files = []
        for f in photo_files:
            metadata = exif_handler.read_exif_metadata(f)
            if not metadata or not metadata.get("description", "").strip():
                filtered_files.append(f)
        photo_files = filtered_files

    console.print(f"📸 Found {len(photo_files)} photos to process")

    if not photo_files:
        console.print("✅ No photos to process")
        return

    # Process photos
    async def process_photos():
        for photo_path in photo_files:
            try:
                console.print(f"🤖 Analyzing {photo_path.name}...")

                # AI analysis with both approved and pending tags for context
                analysis = await ai_service.analyze_photo(photo_path, existing_tags)

                # Only use approved tags for EXIF creation (pending tags need review first)
                approved_tag_names = tag_storage.get_approved_tag_names()
                approved_tags_used = [
                    tag
                    for tag in analysis.existing_tags_used
                    if tag in approved_tag_names
                ]

                exif_handler.add_exif_metadata(
                    photo_path,
                    analysis.rating,
                    approved_tags_used,
                    analysis.description,
                    analysis.notes,
                )

                # Store new pending tags for review (avoid duplicates with existing pending/approved)
                current_pending_tags = tag_storage.get_pending_tag_names()
                for new_tag in analysis.new_tags_needed:
                    if (
                        new_tag not in approved_tag_names
                        and new_tag not in current_pending_tags
                    ):
                        tag_storage.add_pending_tag(
                            new_tag, str(photo_path), analysis.confidence
                        )

                console.print(
                    f"✅ Processed {photo_path.name} (Rating: {analysis.rating}/5)"
                )

            except Exception as e:
                console.print(
                    f"❌ Failed to process {photo_path.name}: {e}", style="red"
                )

    # Run processing
    asyncio.run(process_photos())

    # Move processed photos to intermediate folder
    if photo_files:
        processed_dir = Path(os.getenv("PROCESSED_DIR", "./processed"))
        processed_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"📦 Moving processed photos to: {processed_dir}")
        moved_count = move_photos(photo_files, processed_dir)
        console.print(f"✅ Moved {moved_count} processed photos")

    # Check for pending tags
    if tag_storage.has_pending_tags():
        pending_count = len(tag_storage.get_pending_tags())
        console.print(
            f"⚠️  {pending_count} new tags need review before upload", style="yellow"
        )
        console.print("Run 'phototag review-tags' to approve/reject new tags")


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
                    f"✅ Upload completed. {len(get_photo_files(source_dir))} photos uploaded successfully."
                )

                # Move photos to destination directory
                console.print(f"📦 Moving photos to destination directory...")
                photo_files = get_photo_files(source_dir)
                moved_count = move_photos(photo_files, destination_dir)
                console.print(f"✅ Moved {moved_count} photos to {destination_dir}")

            else:
                console.print("❌ Upload failed", style="red")
                raise typer.Exit(1)

    except Exception as e:
        console.print(f"❌ Upload failed: {e}", style="red")
        raise typer.Exit(1)


@app.command()
def review_tags():
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

    # Interactive approval
    approved_tags = []
    rejected_tags = []

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
        photos_to_update = tag_storage.approve_tags(approved_tags)

        # Update EXIF data with approved tags
        for photo_path in photos_to_update:
            photo_file = Path(photo_path)
            if photo_file.exists():
                # Get which approved tags apply to this photo
                relevant_tags = [tag for tag in approved_tags]  # Simplified for now
                exif_handler.update_exif_tags(photo_file, relevant_tags)

        console.print(f"📝 Updated {len(photos_to_update)} photos with EXIF metadata")

    if rejected_tags:
        console.print(f"❌ Rejecting {len(rejected_tags)} tags...")
        tag_storage.reject_tags(rejected_tags)

    remaining = len(tag_storage.get_pending_tags())
    if remaining == 0:
        console.print("🎉 All tags reviewed! Ready for upload.")
    else:
        console.print(f"📋 {remaining} tags still pending")


@app.command()
def config():
    """Show configuration and setup instructions."""
    console.print("📋 Configuration Requirements:")
    console.print()

    required_vars = [
        ("OPENAI_API_KEY", "OpenAI API key for photo analysis"),
    ]

    directory_vars = [
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
    app()
