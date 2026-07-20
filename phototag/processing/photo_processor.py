"""Photo processor with multiprocessing and resumption support."""

import os
import signal
import logging
import shutil
import json
from pathlib import Path
from typing import List, Optional, Dict, Any, Callable
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, wait
import multiprocessing
import asyncio

from ..ai.openai_service import OpenAIService
from ..media import is_video, unique_destination
from ..storage.tag_review import TagReviewStorage
from ..storage.exif import EXIFHandler
from ..storage.state_db import ProcessingStateDB, PhotoStatus
from ..models.ai import AIAnalysisResponse

logger = logging.getLogger(__name__)


# Module-level worker function to avoid pickling issues
def process_worker_function(worker_id: str, api_key: str, inbox_dir: str, processed_dir: str, force_process: bool = True) -> Dict[str, int]:
    """Worker process for photo processing."""
    # Each worker creates its own service instances
    ai_service = OpenAIService(api_key)
    exif_handler = EXIFHandler()
    tag_storage = TagReviewStorage()
    state_db = ProcessingStateDB()
    
    results = {
        'processed': 0,
        'awaiting_tags': 0,
        'failed': 0
    }
    
    # Process photos until no more available
    max_iterations = 1000  # Safety limit
    for _ in range(max_iterations):
        # Claim next photo (including those awaiting tag review if force_process=True)
        photo_path = state_db.claim_next_photo(worker_id, include_awaiting_tags=force_process)
        if not photo_path:
            break  # No more photos to process
        
        try:
            # Process the photo with state tracking
            status = process_single_photo(
                Path(photo_path),
                ai_service,
                exif_handler,
                tag_storage,
                state_db,
                Path(processed_dir),
                worker_id,
                force_process
            )
            
            if status == PhotoStatus.PROCESSED:
                results['processed'] += 1
            elif status == PhotoStatus.AWAITING_TAG_REVIEW:
                results['awaiting_tags'] += 1
            elif status == PhotoStatus.FAILED:
                results['failed'] += 1
                
        except Exception as e:
            logger.error(f"Worker {worker_id} error processing {photo_path}: {e}")
            state_db.update_photo_status(
                photo_path, 
                PhotoStatus.FAILED,
                {'error': str(e)}
            )
            results['failed'] += 1
    
    # Close database connection for this worker
    state_db.close()
    
    return results


def process_single_photo(photo_path: Path,
                        ai_service: OpenAIService,
                        exif_handler: EXIFHandler,
                        tag_storage: TagReviewStorage,
                        state_db: ProcessingStateDB,
                        processed_dir: Path,
                        worker_id: str,
                        force_process: bool = True) -> PhotoStatus:
    """Process a single photo with full state tracking."""
    
    filepath = str(photo_path)
    
    # Check current state
    current = state_db.get_photo_status(filepath)
    if not current:
        return PhotoStatus.FAILED
    
    try:
        if is_video(photo_path):
            # Videos can't be AI-tagged - move them straight to processed
            state_db.update_photo_status(filepath, PhotoStatus.MOVING)
            dest_path = unique_destination(processed_dir, photo_path)
            shutil.move(str(photo_path), str(dest_path))
            state_db.update_photo_status(
                filepath, PhotoStatus.PROCESSED, {'moved_to': str(dest_path)}
            )
            logger.info(f"Moved video {photo_path.name} through pipeline (no AI analysis)")
            return PhotoStatus.PROCESSED

        # Determine where to resume from. Branch on whether an analysis is already
        # saved (not on status): claimed photos are always in 'locked', so a photo
        # claimed from awaiting_tag_review would otherwise be re-analyzed and
        # re-billed for no reason.
        if not current['ai_response_json']:
            # Need to do AI analysis
            state_db.update_photo_status(filepath, PhotoStatus.AI_ANALYZING)
            
            existing_tags = tag_storage.get_all_available_tags()
            
            # Run async function in sync context
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                analysis = loop.run_until_complete(
                    ai_service.analyze_photo(photo_path, existing_tags)
                )
            finally:
                loop.close()
            
            # Save AI response
            state_db.update_photo_status(
                filepath, 
                PhotoStatus.AI_ANALYZED,
                {'ai_response': analysis.model_dump()}
            )
            
            # Check for pending tags
            approved_tags = tag_storage.get_approved_tag_names()
            pending_tags = [
                tag for tag in analysis.new_tags_needed 
                if tag not in approved_tags
            ]
            
            if pending_tags and not force_process:
                # Store pending tags for review
                for tag in pending_tags:
                    tag_storage.add_pending_tag(
                        tag, filepath, analysis.confidence
                    )
                
                state_db.update_photo_status(
                    filepath,
                    PhotoStatus.AWAITING_TAG_REVIEW,
                    {'pending_tags': pending_tags}
                )
                
                logger.info(f"Photo {photo_path.name} awaiting {len(pending_tags)} tag approvals")
                return PhotoStatus.AWAITING_TAG_REVIEW
            elif pending_tags and force_process:
                # Store pending tags for review but continue processing
                for tag in pending_tags:
                    tag_storage.add_pending_tag(
                        tag, filepath, analysis.confidence
                    )
                logger.info(f"Processing {photo_path.name} without {len(pending_tags)} pending tags")
            
            # All tags approved, continue to EXIF
            current['ai_response_json'] = json.dumps(analysis.model_dump())
            current['status'] = PhotoStatus.AI_ANALYZED.value
        else:
            # Analysis already saved from a previous run - resume at the EXIF step
            current['status'] = PhotoStatus.AI_ANALYZED.value

        if current['status'] == PhotoStatus.AI_ANALYZED.value:
            # Write EXIF metadata
            state_db.update_photo_status(filepath, PhotoStatus.EXIF_WRITING)
            
            ai_data = json.loads(current['ai_response_json'])
            analysis = AIAnalysisResponse(**ai_data)
            
            # Only use approved tags
            approved_tags = tag_storage.get_approved_tag_names()
            tags_to_write = [
                tag for tag in analysis.existing_tags_used 
                if tag in approved_tags
            ]
            
            success = exif_handler.add_exif_metadata(
                photo_path,
                analysis.rating,
                tags_to_write,
                analysis.description,
                analysis.notes
            )
            
            if not success:
                raise Exception("Failed to write EXIF metadata")
            
            state_db.update_photo_status(
                filepath,
                PhotoStatus.EXIF_WRITTEN,
                {'exif_written': True}
            )
            current['status'] = PhotoStatus.EXIF_WRITTEN.value
        
        if current['status'] == PhotoStatus.EXIF_WRITTEN.value:
            # Move file to processed directory
            state_db.update_photo_status(filepath, PhotoStatus.MOVING)

            dest_path = unique_destination(processed_dir, photo_path)
            shutil.move(str(photo_path), str(dest_path))
            
            state_db.update_photo_status(
                filepath,
                PhotoStatus.PROCESSED,
                {'moved_to': str(dest_path)}
            )
            
            logger.info(f"Successfully processed {photo_path.name}")
            return PhotoStatus.PROCESSED
        
        # Shouldn't reach here
        return PhotoStatus.FAILED
        
    except Exception as e:
        logger.error(f"Error processing {photo_path}: {e}")
        state_db.update_photo_status(
            filepath,
            PhotoStatus.FAILED,
            {'error': str(e)}
        )
        return PhotoStatus.FAILED


class PhotoProcessor:
    """Handles photo processing with state tracking and multiprocessing."""
    
    def __init__(self, 
                 api_key: str,
                 inbox_dir: Path,
                 processed_dir: Path,
                 worker_count: int = 2,
                 session_id: Optional[str] = None):
        self.api_key = api_key
        self.inbox_dir = inbox_dir
        self.processed_dir = processed_dir
        self.worker_count = min(worker_count, multiprocessing.cpu_count())
        self.session_id = session_id
        
        # Don't initialize services here - they'll be created per process
        # Processing control
        self._shutdown_requested = False
        self._original_sigint = None
    
    def process_batch(self, 
                     photo_files: List[Path],
                     skip_existing: bool = True,
                     continue_session: bool = False,
                     retry_failed: bool = False,
                     force_process: bool = True,
                     progress_callback: Optional[Callable] = None) -> Dict[str, int]:
        """Process a batch of photos with multiprocessing."""
        
        # Initialize services for main process
        state_db = ProcessingStateDB()
        
        # Create or resume session
        if continue_session:
            # Find last incomplete session
            resumable = state_db.get_resumable_photos()
            total_resumable = sum(len(photos) for photos in resumable.values())
            if total_resumable > 0:
                logger.info(f"Resuming {total_resumable} photos from previous session")
                self.session_id = state_db.create_session(
                    resumed_from="auto", worker_count=self.worker_count
                )
        else:
            self.session_id = state_db.create_session(
                worker_count=self.worker_count
            )
        
        # Unlock any stuck photos
        unlocked = state_db.unlock_stuck_photos()
        if unlocked:
            logger.info(f"Unlocked {unlocked} stuck photos")
        
        # Add new photos to database
        added_count = 0
        for photo_path in photo_files:
            # Check if already in database
            existing = state_db.get_photo_status(str(photo_path))
            
            if existing:
                if skip_existing and existing['status'] == PhotoStatus.PROCESSED.value:
                    continue
                if not retry_failed and existing['status'] == PhotoStatus.FAILED.value:
                    continue
                if existing['status'] == PhotoStatus.FAILED.value:
                    # Reset failed photo for retry
                    state_db.reset_photo(str(photo_path))
            else:
                # Add new photo
                if state_db.add_photo(str(photo_path), self.session_id):
                    added_count += 1
        
        logger.info(f"Added {added_count} new photos to processing queue")
        
        # Setup signal handling for graceful shutdown
        self._setup_signal_handlers()
        
        # Process photos with workers
        results = {
            'processed': 0,
            'awaiting_tags': 0,
            'failed': 0,
            'interrupted': 0
        }
        # UTC to match sqlite's CURRENT_TIMESTAMP; used to scope stats to this run
        run_started_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        results['started_at'] = run_started_utc

        with ProcessPoolExecutor(max_workers=self.worker_count) as executor:
            # Submit initial workers - pass only serializable data
            futures = []
            for i in range(self.worker_count):
                worker_id = f"worker_{i}"
                future = executor.submit(
                    process_worker_function,
                    worker_id,
                    self.api_key,
                    str(self.inbox_dir),
                    str(self.processed_dir),
                    force_process
                )
                futures.append(future)

            # Monitor progress: poll every couple of seconds so the progress
            # callback updates live instead of only when a worker exits
            try:
                pending = set(futures)
                while pending and not self._shutdown_requested:
                    done, pending = wait(pending, timeout=2)

                    for future in done:
                        try:
                            worker_results = future.result()
                            for key in results:
                                if key in worker_results:
                                    results[key] += worker_results[key]
                        except Exception as e:
                            logger.error(f"Worker failed: {e}")

                    if progress_callback:
                        progress_callback(state_db.get_statistics(since=run_started_utc))

            except KeyboardInterrupt:
                logger.info("Graceful shutdown requested...")
                self._shutdown_requested = True
                executor.shutdown(wait=True)
                results['interrupted'] = len([f for f in futures if not f.done()])
        
        # Update session statistics
        state_db.update_session_stats(self.session_id)
        
        # Restore signal handlers
        self._restore_signal_handlers()
        
        return results
    
    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            logger.info("Received shutdown signal, finishing current photos...")
            self._shutdown_requested = True
        
        self._original_sigint = signal.signal(signal.SIGINT, signal_handler)
    
    def _restore_signal_handlers(self):
        """Restore original signal handlers."""
        if self._original_sigint:
            signal.signal(signal.SIGINT, self._original_sigint)
    
    def complete_pending_photos(self, approved_tags: List[str]) -> int:
        """Complete processing for photos awaiting approved tags."""
        state_db = ProcessingStateDB()
        
        photos = state_db.get_photos_awaiting_tags(approved_tags)
        completed = 0
        
        for filepath in photos:
            photo_path = Path(filepath)
            if not photo_path.exists():
                continue
            
            # Get stored AI response
            photo_data = state_db.get_photo_status(filepath)
            if not photo_data or not photo_data['ai_response_json']:
                continue
            
            try:
                # Check if all pending tags are now approved
                pending_tags = json.loads(photo_data['pending_tags_json'] or '[]')
                if not all(tag in approved_tags for tag in pending_tags):
                    continue  # Still waiting for some tags
                
                # Complete EXIF and move
                ai_data = json.loads(photo_data['ai_response_json'])
                analysis = AIAnalysisResponse(**ai_data)
                
                # Write EXIF
                state_db.update_photo_status(filepath, PhotoStatus.EXIF_WRITING)
                
                exif_handler = EXIFHandler()
                success = exif_handler.add_exif_metadata(
                    photo_path,
                    analysis.rating,
                    approved_tags,
                    analysis.description,
                    analysis.notes
                )
                
                if success:
                    # Move to processed
                    dest_path = unique_destination(self.processed_dir, photo_path)
                    shutil.move(str(photo_path), str(dest_path))
                    
                    state_db.update_photo_status(
                        filepath,
                        PhotoStatus.PROCESSED,
                        {'moved_to': str(dest_path)}
                    )
                    completed += 1
                    
            except Exception as e:
                logger.error(f"Failed to complete {filepath}: {e}")
        
        return completed