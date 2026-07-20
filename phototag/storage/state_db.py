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
    DUPLICATE = "duplicate"


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
        if not hasattr(self._local, "connection"):
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

            # SHA-1 checksums of assets already on the Immich server, pulled
            # by 'phototag immich-sync' so intake can catch duplicates of
            # photos uploaded by OTHER clients (phone app, web)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS immich_checksums (
                    checksum TEXT PRIMARY KEY,
                    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

            # Content-hash columns (added after v1 schemas shipped, so migrate
            # in place). content_hash = original inbox bytes; processed_hash /
            # processed_sha1 = bytes after the EXIF write (what actually gets
            # uploaded, so processed_sha1 equals Immich's checksum for it).
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(photos)")}
            if "content_hash" not in columns:
                conn.execute("ALTER TABLE photos ADD COLUMN content_hash TEXT")
            if "duplicate_of" not in columns:
                conn.execute("ALTER TABLE photos ADD COLUMN duplicate_of TEXT")
            if "processed_hash" not in columns:
                conn.execute("ALTER TABLE photos ADD COLUMN processed_hash TEXT")
            if "processed_sha1" not in columns:
                conn.execute("ALTER TABLE photos ADD COLUMN processed_sha1 TEXT")

            # Indexes for performance
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_worker ON photos(worker_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_session ON photos(session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_hash ON photos(content_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_processed_hash ON photos(processed_hash)"
            )

    def add_photo(
        self,
        filepath: str,
        session_id: Optional[str],
        content_hash: Optional[str] = None,
    ) -> bool:
        """Add a new photo to processing queue."""
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO photos (filepath, status, session_id, content_hash)
                    VALUES (?, ?, ?, ?)
                """,
                    (filepath, PhotoStatus.PENDING.value, session_id, content_hash),
                )
                return conn.total_changes > 0
        except sqlite3.Error as e:
            logger.error(f"Failed to add photo {filepath}: {e}")
            return False

    def find_duplicate(
        self, content_hash: str, exclude_filepath: Optional[str] = None
    ) -> Optional[str]:
        """Filepath of a live photo with the same content, if any.

        Matches both the original bytes (content_hash) and the post-EXIF bytes
        (processed_hash), so a copy of an already-processed file wandering
        back into the inbox is caught too. Failed photos don't count (they
        should be retried, not deduped against) and neither do other
        duplicates (point at the canonical copy instead).
        """
        conn = self._get_connection()
        row = conn.execute(
            """
            SELECT filepath FROM photos
            WHERE (content_hash = ? OR processed_hash = ?) AND filepath != ?
              AND status NOT IN (?, ?)
            LIMIT 1
        """,
            (
                content_hash,
                content_hash,
                exclude_filepath or "",
                PhotoStatus.FAILED.value,
                PhotoStatus.DUPLICATE.value,
            ),
        ).fetchone()
        return row["filepath"] if row else None

    def record_duplicate(
        self,
        filepath: str,
        content_hash: Optional[str],
        duplicate_of: str,
        moved_to: str,
        session_id: Optional[str],
    ) -> bool:
        """Record an inbox file diverted to the outbox as a duplicate."""
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO photos
                    (filepath, status, content_hash, duplicate_of,
                     moved_to_path, moved_at, session_id)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                """,
                    (
                        filepath,
                        PhotoStatus.DUPLICATE.value,
                        content_hash,
                        duplicate_of,
                        moved_to,
                        session_id,
                    ),
                )
                return True
        except sqlite3.Error as e:
            logger.error(f"Failed to record duplicate {filepath}: {e}")
            return False

    def claim_next_photo(
        self, worker_id: str, include_awaiting_tags: bool = True
    ) -> Optional[str]:
        """Atomically claim the next pending photo for processing."""
        with self.transaction() as conn:
            # Build list of statuses to claim from
            statuses = [PhotoStatus.PENDING.value]
            if include_awaiting_tags:
                statuses.append(PhotoStatus.AWAITING_TAG_REVIEW.value)

            # Find and lock next available photo
            placeholders = ",".join("?" * len(statuses))
            result = conn.execute(
                f"""
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
            """,
                [PhotoStatus.LOCKED.value, worker_id] + statuses,
            )

            row = result.fetchone()
            return row["filepath"] if row else None

    def update_photo_status(
        self, filepath: str, status: PhotoStatus, data: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Update photo processing status with optional data."""
        try:
            with self.transaction() as conn:
                updates = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
                values = [status.value]

                if data:
                    if "ai_response" in data:
                        updates.append("ai_response_json = ?")
                        updates.append("ai_analyzed_at = CURRENT_TIMESTAMP")
                        values.append(json.dumps(data["ai_response"]))

                    if "pending_tags" in data:
                        updates.append("pending_tags_json = ?")
                        values.append(json.dumps(data["pending_tags"]))

                    if "approved_tags" in data:
                        updates.append("approved_tags_json = ?")
                        values.append(json.dumps(data["approved_tags"]))

                    if "error" in data:
                        updates.append("error_message = ?")
                        updates.append("error_at = CURRENT_TIMESTAMP")
                        updates.append("retry_count = retry_count + 1")
                        values.append(data["error"])

                    if "moved_to" in data:
                        updates.append("moved_to_path = ?")
                        updates.append("moved_at = CURRENT_TIMESTAMP")
                        values.append(data["moved_to"])

                    if "exif_written" in data and data["exif_written"]:
                        updates.append("exif_written_at = CURRENT_TIMESTAMP")

                    # (sha256, sha1) of the file AFTER the EXIF write - the
                    # bytes that actually get uploaded to Immich
                    if "processed_hashes" in data and data["processed_hashes"]:
                        updates.append("processed_hash = ?")
                        values.append(data["processed_hashes"][0])
                        updates.append("processed_sha1 = ?")
                        values.append(data["processed_hashes"][1])

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
        result = conn.execute(
            """
            SELECT * FROM photos WHERE filepath = ?
        """,
            (filepath,),
        )
        row = result.fetchone()
        return dict(row) if row else None

    def get_stuck_photos(self, timeout_minutes: int = 5) -> List[str]:
        """Find photos that have been locked for too long."""
        conn = self._get_connection()
        cutoff = datetime.now() - timedelta(minutes=timeout_minutes)
        result = conn.execute(
            """
            SELECT filepath FROM photos 
            WHERE status = ? AND locked_at < ?
        """,
            (PhotoStatus.LOCKED.value, cutoff),
        )
        return [row["filepath"] for row in result]

    # Mid-pipeline states a crashed/killed worker can leave behind. Resetting them
    # to pending is safe: saved AI analysis is reused and EXIF writes are idempotent.
    STUCK_STATUSES = (
        PhotoStatus.LOCKED.value,
        PhotoStatus.AI_ANALYZING.value,
        PhotoStatus.AI_ANALYZED.value,
        PhotoStatus.EXIF_WRITING.value,
        PhotoStatus.EXIF_WRITTEN.value,
        PhotoStatus.MOVING.value,
    )

    def unlock_stuck_photos(self, timeout_minutes: int = 5) -> int:
        """Reset photos that have been stuck mid-pipeline for too long."""
        with self.transaction() as conn:
            cutoff = datetime.now() - timedelta(minutes=timeout_minutes)
            placeholders = ",".join("?" * len(self.STUCK_STATUSES))
            result = conn.execute(
                f"""
                UPDATE photos
                SET status = ?, worker_id = NULL, locked_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status IN ({placeholders})
                  AND (locked_at < ? OR locked_at IS NULL)
            """,
                (PhotoStatus.PENDING.value, *self.STUCK_STATUSES, cutoff),
            )
            return result.rowcount

    def get_photos_awaiting_tags(self, tags: List[str]) -> List[str]:
        """Get photos waiting for specific tags to be approved."""
        conn = self._get_connection()
        photos = []
        result = conn.execute(
            """
            SELECT filepath, pending_tags_json 
            FROM photos 
            WHERE status = ?
        """,
            (PhotoStatus.AWAITING_TAG_REVIEW.value,),
        )

        for row in result:
            if row["pending_tags_json"]:
                pending = json.loads(row["pending_tags_json"])
                if any(tag in pending for tag in tags):
                    photos.append(row["filepath"])

        return photos

    def get_resumable_photos(self) -> Dict[str, List[str]]:
        """Get photos grouped by their resumable state."""
        conn = self._get_connection()
        resumable = {
            "ai_analyzing": [],
            "ai_analyzed": [],
            "exif_writing": [],
            "exif_written": [],
            "moving": [],
        }

        # Photos that need AI analysis retry
        result = conn.execute(
            """
            SELECT filepath FROM photos 
            WHERE status IN (?, ?)
        """,
            (PhotoStatus.AI_ANALYZING.value, PhotoStatus.LOCKED.value),
        )
        resumable["ai_analyzing"] = [row["filepath"] for row in result]

        # Photos that completed AI but need EXIF
        result = conn.execute(
            """
            SELECT filepath FROM photos 
            WHERE status = ? AND pending_tags_json IS NULL
        """,
            (PhotoStatus.AI_ANALYZED.value,),
        )
        resumable["ai_analyzed"] = [row["filepath"] for row in result]

        # Photos stuck in EXIF writing
        result = conn.execute(
            """
            SELECT filepath FROM photos WHERE status = ?
        """,
            (PhotoStatus.EXIF_WRITING.value,),
        )
        resumable["exif_writing"] = [row["filepath"] for row in result]

        # Photos with EXIF written but not moved
        result = conn.execute(
            """
            SELECT filepath FROM photos WHERE status = ?
        """,
            (PhotoStatus.EXIF_WRITTEN.value,),
        )
        resumable["exif_written"] = [row["filepath"] for row in result]

        # Photos stuck during move
        result = conn.execute(
            """
            SELECT filepath FROM photos WHERE status = ?
        """,
            (PhotoStatus.MOVING.value,),
        )
        resumable["moving"] = [row["filepath"] for row in result]

        return resumable

    def get_statistics(self, since: Optional[str] = None) -> Dict[str, int]:
        """Get processing statistics, optionally scoped to rows touched after a UTC timestamp."""
        conn = self._get_connection()
        stats = {}

        if since:
            result = conn.execute(
                """
                SELECT status, COUNT(*) as count
                FROM photos
                WHERE updated_at >= ?
                GROUP BY status
            """,
                (since,),
            )
        else:
            result = conn.execute("""
                SELECT status, COUNT(*) as count
                FROM photos
                GROUP BY status
            """)

        for row in result:
            stats[row["status"]] = row["count"]

        return stats

    def get_failed_photos(self) -> List[Dict]:
        """Get details of failed photos."""
        conn = self._get_connection()
        result = conn.execute(
            """
            SELECT filepath, error_message, error_at, retry_count 
            FROM photos 
            WHERE status = ?
            ORDER BY error_at DESC
        """,
            (PhotoStatus.FAILED.value,),
        )
        return [dict(row) for row in result]

    def create_session(
        self, resumed_from: Optional[str] = None, worker_count: int = 1
    ) -> str:
        """Create a new processing session."""
        base_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self.transaction() as conn:
            # Timestamp has second resolution - suffix on collision so two
            # runs started within the same second both get a session
            session_id = base_id
            for attempt in range(1, 100):
                exists = conn.execute(
                    "SELECT 1 FROM processing_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not exists:
                    break
                session_id = f"{base_id}_{attempt}"
            conn.execute(
                """
                INSERT INTO processing_sessions
                (session_id, resumed_from_session, worker_count)
                VALUES (?, ?, ?)
            """,
                (session_id, resumed_from, worker_count),
            )
        return session_id

    def update_session_stats(self, session_id: Optional[str]):
        """Update session statistics."""
        if not session_id:
            return
        with self.transaction() as conn:
            # Count processed and failed
            result = conn.execute(
                """
                SELECT 
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as processed,
                    SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as failed
                FROM photos 
                WHERE session_id = ?
            """,
                (PhotoStatus.PROCESSED.value, PhotoStatus.FAILED.value, session_id),
            )

            row = result.fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE processing_sessions 
                    SET photos_processed = ?, photos_failed = ?
                    WHERE session_id = ?
                """,
                    (row["processed"] or 0, row["failed"] or 0, session_id),
                )

    def cleanup_old_records(self, days: int = 30) -> int:
        """Remove old completed records.

        Note: this also forgets the content hashes of those photos, so a copy
        re-synced after cleanup will be processed (and uploaded) again.
        """
        with self.transaction() as conn:
            cutoff = datetime.now() - timedelta(days=days)
            result = conn.execute(
                """
                DELETE FROM photos
                WHERE status IN (?, ?) AND moved_at < ?
            """,
                (PhotoStatus.PROCESSED.value, PhotoStatus.DUPLICATE.value, cutoff),
            )
            return result.rowcount

    def reset_photo(self, filepath: str) -> bool:
        """Reset a photo to pending, clearing errors but keeping any saved AI analysis."""
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    UPDATE photos
                    SET status = ?, worker_id = NULL, locked_at = NULL,
                        error_message = NULL, error_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE filepath = ?
                """,
                    (PhotoStatus.PENDING.value, filepath),
                )
                return conn.total_changes > 0
        except sqlite3.Error as e:
            logger.error(f"Failed to reset photo {filepath}: {e}")
            return False

    def get_photos_needing_tags(self, tags: List[str]) -> Dict[str, List[str]]:
        """Map each photo's CURRENT file location to the subset of `tags` its AI analysis wanted.

        Uses moved_to_path when the photo has already been moved to processed/,
        so tag backfill works after files leave the inbox.
        """
        conn = self._get_connection()
        result = conn.execute("""
            SELECT filepath, moved_to_path, ai_response_json
            FROM photos
            WHERE ai_response_json IS NOT NULL
        """)

        photos = {}
        for row in result:
            try:
                needed = json.loads(row["ai_response_json"]).get("new_tags_needed", [])
            except (json.JSONDecodeError, AttributeError):
                continue
            matching = [t for t in tags if t in needed]
            if matching:
                photos[row["moved_to_path"] or row["filepath"]] = matching

        return photos

    def update_processed_hashes(
        self, current_path: str, sha256: str, sha1: str
    ) -> bool:
        """Refresh the post-EXIF hashes of a photo located by its CURRENT path.

        Tag backfill (review-tags) rewrites EXIF on files that already moved
        to processed/, changing their bytes - the stored post-EXIF hashes must
        follow or re-entry detection silently degrades.
        """
        try:
            with self.transaction() as conn:
                conn.execute(
                    """
                    UPDATE photos
                    SET processed_hash = ?, processed_sha1 = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE moved_to_path = ? OR (moved_to_path IS NULL AND filepath = ?)
                """,
                    (sha256, sha1, current_path, current_path),
                )
                return conn.total_changes > 0
        except sqlite3.Error as e:
            logger.error(f"Failed to update processed hashes for {current_path}: {e}")
            return False

    def replace_immich_checksums(self, checksums: List[str]) -> int:
        """Replace the known-on-Immich checksum set with a fresh server pull."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM immich_checksums")
            conn.executemany(
                "INSERT OR IGNORE INTO immich_checksums (checksum) VALUES (?)",
                [(c,) for c in checksums],
            )
            row = conn.execute(
                "SELECT COUNT(*) as count FROM immich_checksums"
            ).fetchone()
            return row["count"]

    def has_immich_checksum(self, checksum: str) -> bool:
        """True if an asset with this SHA-1 already exists on the Immich server."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT 1 FROM immich_checksums WHERE checksum = ?", (checksum,)
        ).fetchone()
        return row is not None

    def immich_checksum_info(self) -> Dict[str, Any]:
        """Count and last sync time of the Immich checksum mirror."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT COUNT(*) as count, MAX(synced_at) as synced_at FROM immich_checksums"
        ).fetchone()
        return {"count": row["count"], "synced_at": row["synced_at"]}

    def get_all_photos(self) -> List[Dict]:
        """Get filepath and status for every tracked photo."""
        conn = self._get_connection()
        result = conn.execute("SELECT filepath, status FROM photos")
        return [dict(row) for row in result]

    def delete_photo(self, filepath: str) -> bool:
        """Remove a photo record entirely."""
        try:
            with self.transaction() as conn:
                conn.execute("DELETE FROM photos WHERE filepath = ?", (filepath,))
                return conn.total_changes > 0
        except sqlite3.Error as e:
            logger.error(f"Failed to delete photo record {filepath}: {e}")
            return False

    def close(self):
        """Close database connection."""
        if hasattr(self._local, "connection"):
            self._local.connection.close()
            del self._local.connection
