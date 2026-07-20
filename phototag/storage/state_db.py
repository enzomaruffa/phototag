"""SQLite database for tracking photo processing state with resumption support."""

import sqlite3
import json
import threading
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from contextlib import contextmanager
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class PhotoStatus(Enum):
    """Photo processing status states."""
    PENDING = "pending"
    LOCKED = "locked"
    AI_ANALYZING = "ai_analyzing"
    AI_ANALYZED = "ai_analyzed"
    AWAITING_TAG_REVIEW = "awaiting_tag_review"
    EXIF_WRITING = "exif_writing"
    EXIF_WRITTEN = "exif_written"
    MOVING = "moving"
    PROCESSED = "processed"
    FAILED = "failed"


class ProcessingStateDB:
    """Thread-safe SQLite database for processing state management."""
    
    def __init__(self, storage_dir: Path = Path.cwd() / ".phototag"):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(exist_ok=True)
        self.db_path = self.storage_dir / "processing_state.db"
        self._local = threading.local()
        self._lock = threading.RLock()
        self._init_database()
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'connection'):
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrent access
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")  # 10 second timeout
            self._local.connection = conn
        return self._local.connection
    
    @contextmanager
    def transaction(self):
        """Context manager for database transactions."""
        conn = self._get_connection()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    
    def _init_database(self):
        """Initialize database schema."""
        with self.transaction() as conn:
            # Photos table - main state tracking
            conn.execute("""
                CREATE TABLE IF NOT EXISTS photos (
                    filepath TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending',
                    worker_id TEXT,
                    locked_at TIMESTAMP,
                    
                    -- AI Analysis
                    ai_response_json TEXT,
                    ai_analyzed_at TIMESTAMP,
                    
                    -- Tag tracking
                    pending_tags_json TEXT,
                    approved_tags_json TEXT,
                    
                    -- EXIF and file operations
                    exif_written_at TIMESTAMP,
                    moved_at TIMESTAMP,
                    moved_to_path TEXT,
                    
                    -- Error handling
                    error_message TEXT,
                    error_at TIMESTAMP,
                    retry_count INTEGER DEFAULT 0,
                    
                    -- Metadata
                    session_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Processing sessions table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processing_sessions (
                    session_id TEXT PRIMARY KEY,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resumed_from_session TEXT,
                    worker_count INTEGER,
                    completed_at TIMESTAMP,
                    photos_processed INTEGER DEFAULT 0,
                    photos_failed INTEGER DEFAULT 0
                )
            """)
            
            # Indexes for performance
            conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_worker ON photos(worker_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_session ON photos(session_id)")
    
    def add_photo(self, filepath: str, session_id: str) -> bool:
        """Add a new photo to processing queue."""
        try:
            with self.transaction() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO photos (filepath, status, session_id)
                    VALUES (?, ?, ?)
                """, (filepath, PhotoStatus.PENDING.value, session_id))
                return conn.total_changes > 0
        except sqlite3.Error as e:
            logger.error(f"Failed to add photo {filepath}: {e}")
            return False
    
    def claim_next_photo(self, worker_id: str, include_awaiting_tags: bool = True) -> Optional[str]:
        """Atomically claim the next pending photo for processing."""
        with self.transaction() as conn:
            # Build list of statuses to claim from
            statuses = [PhotoStatus.PENDING.value]
            if include_awaiting_tags:
                statuses.append(PhotoStatus.AWAITING_TAG_REVIEW.value)
            
            # Find and lock next available photo
            placeholders = ','.join('?' * len(statuses))
            result = conn.execute(f"""
                UPDATE photos 
                SET status = ?, worker_id = ?, locked_at = CURRENT_TIMESTAMP, 
                    updated_at = CURRENT_TIMESTAMP
                WHERE filepath IN (
                    SELECT filepath FROM photos 
                    WHERE status IN ({placeholders})
                    ORDER BY created_at
                    LIMIT 1
                )
                RETURNING filepath
            """, [PhotoStatus.LOCKED.value, worker_id] + statuses)
            
            row = result.fetchone()
            return row['filepath'] if row else None
    
    def update_photo_status(self, filepath: str, status: PhotoStatus, 
                           data: Dict[str, Any] = None) -> bool:
        """Update photo processing status with optional data."""
        try:
            with self.transaction() as conn:
                updates = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
                values = [status.value]
                
                if data:
                    if 'ai_response' in data:
                        updates.append("ai_response_json = ?")
                        updates.append("ai_analyzed_at = CURRENT_TIMESTAMP")
                        values.append(json.dumps(data['ai_response']))
                    
                    if 'pending_tags' in data:
                        updates.append("pending_tags_json = ?")
                        values.append(json.dumps(data['pending_tags']))
                    
                    if 'approved_tags' in data:
                        updates.append("approved_tags_json = ?")
                        values.append(json.dumps(data['approved_tags']))
                    
                    if 'error' in data:
                        updates.append("error_message = ?")
                        updates.append("error_at = CURRENT_TIMESTAMP")
                        updates.append("retry_count = retry_count + 1")
                        values.append(data['error'])
                    
                    if 'moved_to' in data:
                        updates.append("moved_to_path = ?")
                        updates.append("moved_at = CURRENT_TIMESTAMP")
                        values.append(data['moved_to'])
                    
                    if 'exif_written' in data and data['exif_written']:
                        updates.append("exif_written_at = CURRENT_TIMESTAMP")
                
                values.append(filepath)
                query = f"UPDATE photos SET {', '.join(updates)} WHERE filepath = ?"
                conn.execute(query, values)
                return conn.total_changes > 0
                
        except sqlite3.Error as e:
            logger.error(f"Failed to update photo {filepath}: {e}")
            return False
    
    def get_photo_status(self, filepath: str) -> Optional[Dict]:
        """Get current status and data for a photo."""
        conn = self._get_connection()
        result = conn.execute("""
            SELECT * FROM photos WHERE filepath = ?
        """, (filepath,))
        row = result.fetchone()
        return dict(row) if row else None
    
    def get_stuck_photos(self, timeout_minutes: int = 5) -> List[str]:
        """Find photos that have been locked for too long."""
        conn = self._get_connection()
        cutoff = datetime.now() - timedelta(minutes=timeout_minutes)
        result = conn.execute("""
            SELECT filepath FROM photos 
            WHERE status = ? AND locked_at < ?
        """, (PhotoStatus.LOCKED.value, cutoff))
        return [row['filepath'] for row in result]
    
    def unlock_stuck_photos(self, timeout_minutes: int = 5) -> int:
        """Reset photos that have been locked too long."""
        with self.transaction() as conn:
            cutoff = datetime.now() - timedelta(minutes=timeout_minutes)
            result = conn.execute("""
                UPDATE photos 
                SET status = ?, worker_id = NULL, locked_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = ? AND locked_at < ?
            """, (PhotoStatus.PENDING.value, PhotoStatus.LOCKED.value, cutoff))
            return result.rowcount
    
    def get_photos_awaiting_tags(self, tags: List[str]) -> List[str]:
        """Get photos waiting for specific tags to be approved."""
        conn = self._get_connection()
        photos = []
        result = conn.execute("""
            SELECT filepath, pending_tags_json 
            FROM photos 
            WHERE status = ?
        """, (PhotoStatus.AWAITING_TAG_REVIEW.value,))
        
        for row in result:
            if row['pending_tags_json']:
                pending = json.loads(row['pending_tags_json'])
                if any(tag in pending for tag in tags):
                    photos.append(row['filepath'])
        
        return photos
    
    def get_resumable_photos(self) -> Dict[str, List[str]]:
        """Get photos grouped by their resumable state."""
        conn = self._get_connection()
        resumable = {
            'ai_analyzing': [],
            'ai_analyzed': [],
            'exif_writing': [],
            'exif_written': [],
            'moving': []
        }
        
        # Photos that need AI analysis retry
        result = conn.execute("""
            SELECT filepath FROM photos 
            WHERE status IN (?, ?)
        """, (PhotoStatus.AI_ANALYZING.value, PhotoStatus.LOCKED.value))
        resumable['ai_analyzing'] = [row['filepath'] for row in result]
        
        # Photos that completed AI but need EXIF
        result = conn.execute("""
            SELECT filepath FROM photos 
            WHERE status = ? AND pending_tags_json IS NULL
        """, (PhotoStatus.AI_ANALYZED.value,))
        resumable['ai_analyzed'] = [row['filepath'] for row in result]
        
        # Photos stuck in EXIF writing
        result = conn.execute("""
            SELECT filepath FROM photos WHERE status = ?
        """, (PhotoStatus.EXIF_WRITING.value,))
        resumable['exif_writing'] = [row['filepath'] for row in result]
        
        # Photos with EXIF written but not moved
        result = conn.execute("""
            SELECT filepath FROM photos WHERE status = ?
        """, (PhotoStatus.EXIF_WRITTEN.value,))
        resumable['exif_written'] = [row['filepath'] for row in result]
        
        # Photos stuck during move
        result = conn.execute("""
            SELECT filepath FROM photos WHERE status = ?
        """, (PhotoStatus.MOVING.value,))
        resumable['moving'] = [row['filepath'] for row in result]
        
        return resumable
    
    def get_statistics(self) -> Dict[str, int]:
        """Get processing statistics."""
        conn = self._get_connection()
        stats = {}
        
        result = conn.execute("""
            SELECT status, COUNT(*) as count 
            FROM photos 
            GROUP BY status
        """)
        
        for row in result:
            stats[row['status']] = row['count']
        
        # Add failed with retry exhausted
        result = conn.execute("""
            SELECT COUNT(*) as count 
            FROM photos 
            WHERE status = ? AND retry_count >= 3
        """, (PhotoStatus.FAILED.value,))
        stats['permanently_failed'] = result.fetchone()['count']
        
        return stats
    
    def get_failed_photos(self) -> List[Dict]:
        """Get details of failed photos."""
        conn = self._get_connection()
        result = conn.execute("""
            SELECT filepath, error_message, error_at, retry_count 
            FROM photos 
            WHERE status = ?
            ORDER BY error_at DESC
        """, (PhotoStatus.FAILED.value,))
        return [dict(row) for row in result]
    
    def create_session(self, resumed_from: Optional[str] = None, 
                      worker_count: int = 1) -> str:
        """Create a new processing session."""
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO processing_sessions 
                (session_id, resumed_from_session, worker_count)
                VALUES (?, ?, ?)
            """, (session_id, resumed_from, worker_count))
        return session_id
    
    def update_session_stats(self, session_id: str):
        """Update session statistics."""
        with self.transaction() as conn:
            # Count processed and failed
            result = conn.execute("""
                SELECT 
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as processed,
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as failed
                FROM photos 
                WHERE session_id = ?
            """, (PhotoStatus.PROCESSED.value, PhotoStatus.FAILED.value, session_id))
            
            row = result.fetchone()
            if row:
                conn.execute("""
                    UPDATE processing_sessions 
                    SET photos_processed = ?, photos_failed = ?
                    WHERE session_id = ?
                """, (row['processed'] or 0, row['failed'] or 0, session_id))
    
    def cleanup_old_records(self, days: int = 30) -> int:
        """Remove old completed records."""
        with self.transaction() as conn:
            cutoff = datetime.now() - timedelta(days=days)
            result = conn.execute("""
                DELETE FROM photos 
                WHERE status = ? AND moved_at < ?
            """, (PhotoStatus.PROCESSED.value, cutoff))
            return result.rowcount
    
    def reset_photo(self, filepath: str) -> bool:
        """Reset a photo to pending state."""
        return self.update_photo_status(filepath, PhotoStatus.PENDING, 
                                       {'error': None})
    
    def close(self):
        """Close database connection."""
        if hasattr(self._local, 'connection'):
            self._local.connection.close()
            del self._local.connection