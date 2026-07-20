"""Photo processor with multiprocessing and resumption support."""

import os
import signal
import logging
import shutil
import json
from pathlib import Path
from typing import List, Optional, Dict, Any, Callable
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

from ..ai.openai_service import OpenAIService
from ..storage.tag_review import TagReviewStorage
from ..storage.exif import EXIFHandler
from ..storage.state_db import ProcessingStateDB, PhotoStatus
from ..models.ai import AIAnalysisResponse

logger = logging.getLogger(__name__)


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
        
        # Initialize services
        self.state_db = ProcessingStateDB()
        self.tag_storage = TagReviewStorage()
        self.exif_handler = EXIFHandler()
        
        # Processing control
        self._shutdown_requested = False
        self._original_sigint = None
        
    def process_batch(self, 
                     photo_files: List[Path],
                     skip_existing: bool = True,
                     continue_session: bool = False,
                     retry_failed: bool = False,
                     progress_callback: Optional[Callable] = None) -> Dict[str, int]:
        """Process a batch of photos with multiprocessing."""
        
        # Create or resume session
        if continue_session:
            # Find last incomplete session
            resumable = self.state_db.get_resumable_photos()
            total_resumable = sum(len(photos) for photos in resumable.values())
            if total_resumable > 0:
                logger.info(f"Resuming {total_resumable} photos from previous session")
                self.session_id = self.state_db.create_session(
                    resumed_from="auto", worker_count=self.worker_count
                )
        else:
            self.session_id = self.state_db.create_session(
                worker_count=self.worker_count
            )
        
        # Unlock any stuck photos
        unlocked = self.state_db.unlock_stuck_photos()
        if unlocked:
            logger.info(f"Unlocked {unlocked} stuck photos")
        
        # Add new photos to database
        added_count = 0
        for photo_path in photo_files:
            # Check if already in database
            existing = self.state_db.get_photo_status(str(photo_path))
            
            if existing:
                if skip_existing and existing['status'] == PhotoStatus.PROCESSED.value:
                    continue
                if not retry_failed and existing['status'] == PhotoStatus.FAILED.value:
                    continue
                if existing['status'] == PhotoStatus.FAILED.value:
                    # Reset failed photo for retry
                    self.state_db.reset_photo(str(photo_path))
            else:
                # Add new photo
                if self.state_db.add_photo(str(photo_path), self.session_id):
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
        
        with ProcessPoolExecutor(max_workers=self.worker_count) as executor:
            # Submit initial workers
            futures = []
            for i in range(self.worker_count):
                worker_id = f"worker_{i}"
                future = executor.submit(self._process_worker, worker_id)
                futures.append(future)
            
            # Monitor progress
            try:
                for future in as_completed(futures):
                    if self._shutdown_requested:
                        break
                    
                    try:
                        worker_results = future.result()
                        for key in results:
                            if key in worker_results:
                                results[key] += worker_results[key]
                        
                        if progress_callback:
                            stats = self.state_db.get_statistics()
                            progress_callback(stats)
                            
                    except Exception as e:
                        logger.error(f"Worker failed: {e}")
                
            except KeyboardInterrupt:
                logger.info("Graceful shutdown requested...")
                self._shutdown_requested = True
                executor.shutdown(wait=True)
                results['interrupted'] = len([f for f in futures if not f.done()])
        
        # Update session statistics
        self.state_db.update_session_stats(self.session_id)
        
        # Restore signal handlers
        self._restore_signal_handlers()
        
        return results
    
    def _process_worker(self, worker_id: str) -> Dict[str, int]:
        """Worker process for photo processing."""
        # Each worker needs its own service instances (not thread-safe across processes)
        ai_service = OpenAIService(self.api_key)
        exif_handler = EXIFHandler()
        tag_storage = TagReviewStorage()
        state_db = ProcessingStateDB()
        
        results = {
            'processed': 0,
            'awaiting_tags': 0,
            'failed': 0
        }
        
        while not self._shutdown_requested:
            # Claim next photo
            photo_path = state_db.claim_next_photo(worker_id)
            if not photo_path:
                break  # No more photos to process
            
            try:
                # Process the photo with state tracking
                status = self._process_single_photo(
                    Path(photo_path),
                    ai_service,
                    exif_handler,
                    tag_storage,
                    state_db,
                    worker_id
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
    
    def _process_single_photo(self,
                            photo_path: Path,
                            ai_service: OpenAIService,
                            exif_handler: EXIFHandler,
                            tag_storage: TagReviewStorage,
                            state_db: ProcessingStateDB,
                            worker_id: str) -> PhotoStatus:
        """Process a single photo with full state tracking."""
        
        filepath = str(photo_path)
        
        # Check current state
        current = state_db.get_photo_status(filepath)
        if not current:
            return PhotoStatus.FAILED
        
        try:
            # Determine where to resume from
            if current['status'] in [PhotoStatus.PENDING.value, 
                                    PhotoStatus.LOCKED.value,
                                    PhotoStatus.AI_ANALYZING.value]:
                # Need to do AI analysis
                state_db.update_photo_status(filepath, PhotoStatus.AI_ANALYZING)
                
                existing_tags = tag_storage.get_all_available_tags()
                
                # Run async function in sync context
                import asyncio
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
                
                if pending_tags:
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
                
                # All tags approved, continue to EXIF
                current['ai_response_json'] = json.dumps(analysis.model_dump())
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
                
                dest_path = self.processed_dir / photo_path.name
                if dest_path.exists():
                    # Handle naming conflict
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    stem = photo_path.stem
                    suffix = photo_path.suffix
                    dest_path = self.processed_dir / f"{stem}_{timestamp}{suffix}"
                
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
        photos = self.state_db.get_photos_awaiting_tags(approved_tags)
        completed = 0
        
        for filepath in photos:
            photo_path = Path(filepath)
            if not photo_path.exists():
                continue
            
            # Get stored AI response
            photo_data = self.state_db.get_photo_status(filepath)
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
                self.state_db.update_photo_status(filepath, PhotoStatus.EXIF_WRITING)
                
                success = self.exif_handler.add_exif_metadata(
                    photo_path,
                    analysis.rating,
                    approved_tags,
                    analysis.description,
                    analysis.notes
                )
                
                if success:
                    # Move to processed
                    dest_path = self.processed_dir / photo_path.name
                    shutil.move(str(photo_path), str(dest_path))
                    
                    self.state_db.update_photo_status(
                        filepath,
                        PhotoStatus.PROCESSED,
                        {'moved_to': str(dest_path)}
                    )
                    completed += 1
                    
            except Exception as e:
                logger.error(f"Failed to complete {filepath}: {e}")
        
        return completed