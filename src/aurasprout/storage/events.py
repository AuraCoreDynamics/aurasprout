"""SMB-safe SQLite WAL-free Event & Consumer Marker Store (`PRAGMA journal_mode = TRUNCATE`)."""

from __future__ import annotations

import base64
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aurasprout.contracts.paths import SproutPaths, validate_identifier
from aurasprout.storage.mutex import SproutMutex


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteEventStore:
    """Thread-, process-, and SMB-safe event & marker store over `.sprout/events/events.sqlite`.

    Every operation acquires `SproutMutex`, opens a short-lived SQLite connection
    configured with `PRAGMA journal_mode = TRUNCATE` and `PRAGMA synchronous = FULL`
    (never WAL shared memory), executes atomically, and closes the connection cleanly
    in `try/finally`.
    """

    def __init__(
        self,
        paths: Optional[SproutPaths] = None,
        db_path: Optional[Path] = None,
        default_retention: int = 1000,
    ) -> None:
        self.paths = paths or SproutPaths()
        self.db_path = Path(db_path).resolve() if db_path else self.paths.events_db
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        lock_name = f"events-{self.db_path.stem}"
        self.mutex = SproutMutex(self.paths.lock_dir(lock_name))
        self.default_retention = int(default_retention)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = TRUNCATE;")
        conn.execute("PRAGMA synchronous = FULL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_schema(self) -> None:
        with self.mutex:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL,
                        topic_id TEXT NOT NULL,
                        sequence_number INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        source_node_id TEXT NOT NULL,
                        event_type TEXT,
                        payload_b64 TEXT NOT NULL,
                        UNIQUE(topic_id, sequence_number)
                    );
                    CREATE INDEX IF NOT EXISTS idx_events_topic_seq
                        ON events(topic_id, sequence_number);
                    CREATE TABLE IF NOT EXISTS markers (
                        consumer_id TEXT NOT NULL,
                        topic_id TEXT NOT NULL,
                        sequence_number INTEGER NOT NULL,
                        updated_at_utc TEXT NOT NULL,
                        custom_state TEXT,
                        PRIMARY KEY (consumer_id, topic_id)
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def get_pragmas(self) -> Dict[str, Any]:
        with self.mutex:
            conn = self._connect()
            try:
                jm = conn.execute("PRAGMA journal_mode;").fetchone()[0]
                sync = conn.execute("PRAGMA synchronous;").fetchone()[0]
                return {"journal_mode": str(jm).upper(), "synchronous": int(sync)}
            finally:
                conn.close()

    def publish(
        self,
        topic_id: str,
        payload_b64: str,
        source_node_id: str,
        event_type: Optional[str] = None,
        retention_max: Optional[int] = None,
    ) -> Dict[str, Any]:
        safe_topic = validate_identifier(topic_id, "topicId")
        # Validate base64 encoding cleanly before persistence
        base64.b64decode(payload_b64.encode("ascii"), validate=True)
        event_id = uuid.uuid4().hex
        ts = _utc_now()
        limit = self.default_retention if retention_max is None else int(retention_max)

        with self.mutex:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) AS max_seq FROM events WHERE topic_id = ?",
                    (safe_topic,),
                ).fetchone()
                next_seq = int(row["max_seq"]) + 1
                conn.execute(
                    """
                    INSERT INTO events (
                        event_id, topic_id, sequence_number, timestamp, source_node_id, event_type, payload_b64
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, safe_topic, next_seq, ts, source_node_id, event_type, payload_b64),
                )
                if limit > 0:
                    conn.execute(
                        """
                        DELETE FROM events
                        WHERE topic_id = ?
                          AND sequence_number <= (
                              SELECT COALESCE(MAX(sequence_number), 0) - ? FROM events WHERE topic_id = ?
                          )
                        """,
                        (safe_topic, limit, safe_topic),
                    )
                conn.commit()
                return {
                    "eventId": event_id,
                    "topicId": safe_topic,
                    "sequenceNumber": next_seq,
                    "timestamp": ts,
                    "sourceNodeId": source_node_id,
                    "eventType": event_type,
                    "payload": payload_b64,
                }
            finally:
                conn.close()

    def consume(
        self,
        topic_id: str,
        after_sequence: int = 0,
        count: int = 100,
    ) -> List[Dict[str, Any]]:
        safe_topic = validate_identifier(topic_id, "topicId")
        after_val = max(0, int(after_sequence))
        limit = max(1, min(int(count), 10000))

        with self.mutex:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT event_id, topic_id, sequence_number, timestamp, source_node_id, event_type, payload_b64
                    FROM events
                    WHERE topic_id = ? AND sequence_number > ?
                    ORDER BY sequence_number ASC
                    LIMIT ?
                    """,
                    (safe_topic, after_val, limit),
                ).fetchall()
                return [
                    {
                        "eventId": r["event_id"],
                        "topicId": r["topic_id"],
                        "sequenceNumber": int(r["sequence_number"]),
                        "timestamp": r["timestamp"],
                        "sourceNodeId": r["source_node_id"],
                        "eventType": r["event_type"],
                        "payload": r["payload_b64"],
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    def list_topics(self) -> List[str]:
        with self.mutex:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT topic_id FROM events ORDER BY topic_id ASC"
                ).fetchall()
                return [str(r["topic_id"]) for r in rows]
            finally:
                conn.close()

    def topic_exists(self, topic_id: str) -> bool:
        safe_topic = validate_identifier(topic_id, "topicId")
        with self.mutex:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM events WHERE topic_id = ? LIMIT 1",
                    (safe_topic,),
                ).fetchone()
                return row is not None
            finally:
                conn.close()

    def get_marker(self, consumer_id: str, topic_id: str) -> Dict[str, Any]:
        safe_consumer = validate_identifier(consumer_id, "consumerId")
        safe_topic = validate_identifier(topic_id, "topicId")
        with self.mutex:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT sequence_number, updated_at_utc, custom_state
                    FROM markers
                    WHERE consumer_id = ? AND topic_id = ?
                    """,
                    (safe_consumer, safe_topic),
                ).fetchone()
                if row is None:
                    return {
                        "topicId": safe_topic,
                        "sequenceNumber": None,
                        "consumerId": safe_consumer,
                        "updatedAtUtc": None,
                        "customState": None,
                    }
                return {
                    "topicId": safe_topic,
                    "sequenceNumber": int(row["sequence_number"]),
                    "consumerId": safe_consumer,
                    "updatedAtUtc": row["updated_at_utc"],
                    "customState": row["custom_state"],
                }
            finally:
                conn.close()

    def set_marker(
        self,
        consumer_id: str,
        topic_id: str,
        sequence_number: int,
        custom_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        safe_consumer = validate_identifier(consumer_id, "consumerId")
        safe_topic = validate_identifier(topic_id, "topicId")
        seq = max(0, int(sequence_number))
        ts = _utc_now()
        with self.mutex:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO markers (consumer_id, topic_id, sequence_number, updated_at_utc, custom_state)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(consumer_id, topic_id) DO UPDATE SET
                        sequence_number = excluded.sequence_number,
                        updated_at_utc = excluded.updated_at_utc,
                        custom_state = excluded.custom_state
                    """,
                    (safe_consumer, safe_topic, seq, ts, custom_state),
                )
                conn.commit()
                return {
                    "topicId": safe_topic,
                    "sequenceNumber": seq,
                    "consumerId": safe_consumer,
                    "updatedAtUtc": ts,
                    "customState": custom_state,
                }
            finally:
                conn.close()

