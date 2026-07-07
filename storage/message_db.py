"""Message storage using SQLite for persistence and deduplication."""
import json
import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from models.message import Message, MessageStatus, MessageType, Conversation, User
from utils.logger import get_logger

logger = get_logger(__name__)


class MessageDatabase:
    """SQLite database for message storage."""

    def __init__(self, db_path: str = "storage/messages.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Initialize database connection and create tables."""
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._create_tables()
        logger.info(f"Connected to database: {self.db_path}")

    async def close(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("Database connection closed")

    async def _create_tables(self) -> None:
        """Create database tables if they don't exist."""
        await self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_nickname TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_auto_reply_enabled INTEGER DEFAULT 1,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_nickname TEXT,
                content TEXT NOT NULL,
                message_type TEXT DEFAULT 'text',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending',
                reply_content TEXT,
                reply_timestamp TIMESTAMP,
                api_response_time_ms INTEGER,
                error_message TEXT,
                raw_data TEXT,
                FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_messages_store
                ON messages(store_id);
            CREATE INDEX IF NOT EXISTS idx_messages_status
                ON messages(status);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                ON messages(timestamp);
        """)
        await self._connection.commit()

    async def message_exists(self, message_id: str) -> bool:
        """Check if a message already exists by ID."""
        if self._connection is None:
            logger.warning("Database not connected, cannot check message exists")
            return False
        
        async with self._connection.execute(
            "SELECT 1 FROM messages WHERE message_id = ?",
            (message_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def message_exists_by_content_and_time(
        self, 
        store_id: str, 
        user_id: str, 
        content: str, 
        msg_timestamp: datetime
    ) -> bool:
        """Check if a message with same content and timestamp already exists.
        
        This is more accurate than just checking message_id because it considers
        the actual message content and user-sent timestamp.
        """
        if self._connection is None:
            logger.warning("Database not connected, cannot check message by content+time")
            return False
        
        # Normalize content for comparison (remove extra whitespace)
        normalized_content = ' '.join(content.split()) if content else ''
        
        # Check for messages with same store_id, user_id, content and timestamp
        # Allow 1-minute tolerance for timestamp comparison
        async with self._connection.execute(
            """
            SELECT 1 FROM messages 
            WHERE store_id = ? 
              AND user_id = ? 
              AND content = ? 
              AND ABS(strftime('%s', timestamp) - strftime('%s', ?)) < 60
            LIMIT 1
            """,
            (store_id, user_id, normalized_content, msg_timestamp.isoformat())
        ) as cursor:
            result = await cursor.fetchone()
            if result:
                logger.debug(f"Message exists (content+time match): store={store_id}, user={user_id}")
            return result is not None

    async def is_message_replied(self, message_id: str) -> bool:
        """Check if a message has already been replied to."""
        async with self._connection.execute(
            "SELECT status FROM messages WHERE message_id = ?",
            (message_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0] == 'replied'
            return False

    async def message_exists_within_minutes(self, message_id: str, minutes: int = 10) -> bool:
        """Check if a message exists and was created within the last N minutes.
        
        This is useful for short-term deduplication of messages without timestamps.
        Messages older than the window are considered "stale" and can be re-processed.
        
        Args:
            message_id: The message ID to check
            minutes: Time window in minutes (default 10)
            
        Returns:
            True if message exists AND was created within the time window
        """
        if self._connection is None:
            logger.warning("Database not connected, cannot check message exists")
            return False
        
        try:
            async with self._connection.execute(
                """
                SELECT 1 FROM messages 
                WHERE message_id = ? 
                  AND timestamp >= datetime('now', '-{} minutes')
                LIMIT 1
                """.format(minutes),
                (message_id,)
            ) as cursor:
                return await cursor.fetchone() is not None
        except Exception as e:
            logger.warning(f"Error checking message exists within {minutes} minutes: {e}")
            return False

    async def save_message(self, message: Message) -> None:
        """Save a message to the database."""
        await self._connection.execute(
            """
            INSERT OR REPLACE INTO messages (
                message_id, conversation_id, store_id, user_id, user_nickname,
                content, message_type, timestamp, status, reply_content,
                reply_timestamp, api_response_time_ms, error_message, raw_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.message_id,
                message.conversation_id,
                message.store_id,
                message.user.user_id,
                message.user.nickname,
                message.content,
                message.message_type.value,
                message.timestamp.isoformat(),
                message.status.value,
                message.reply_content,
                message.reply_timestamp.isoformat() if message.reply_timestamp else None,
                message.api_response_time_ms,
                message.error_message,
                json.dumps(message.raw_data) if message.raw_data else None,
            )
        )
        await self._connection.commit()

    async def update_message_status(
        self,
        message_id: str,
        status: MessageStatus,
        reply_content: Optional[str] = None,
        error_message: Optional[str] = None,
        api_response_time_ms: Optional[int] = None
    ) -> None:
        """Update message status."""
        await self._connection.execute(
            """
            UPDATE messages SET
                status = ?,
                reply_content = ?,
                reply_timestamp = ?,
                error_message = ?,
                api_response_time_ms = ?
            WHERE message_id = ?
            """,
            (
                status.value,
                reply_content,
                datetime.now().isoformat() if reply_content else None,
                error_message,
                api_response_time_ms,
                message_id,
            )
        )
        await self._connection.commit()

    async def get_conversation_history(
        self,
        conversation_id: str,
        limit: int = 20
    ) -> List[Message]:
        """Get conversation history."""
        async with self._connection.execute(
            """
            SELECT * FROM messages
            WHERE conversation_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (conversation_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row) for row in reversed(rows)]

    async def get_pending_messages(self, limit: int = 100) -> List[Message]:
        """Get pending messages for processing."""
        async with self._connection.execute(
            """
            SELECT * FROM messages
            WHERE status = 'pending'
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row) for row in rows]

    async def save_conversation(self, conversation: Conversation) -> None:
        """Save or update conversation."""
        await self._connection.execute(
            """
            INSERT OR REPLACE INTO conversations (
                conversation_id, store_id, user_id, user_nickname,
                updated_at, is_auto_reply_enabled, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation.conversation_id,
                conversation.store_id,
                conversation.user.user_id,
                conversation.user.nickname,
                datetime.now().isoformat(),
                int(conversation.is_auto_reply_enabled),
                json.dumps(conversation.metadata),
            )
        )
        await self._connection.commit()

    async def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        """Get conversation by ID."""
        async with self._connection.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (conversation_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return self._row_to_conversation(row)
            return None

    async def get_stats(self, store_id: Optional[str] = None) -> Dict[str, Any]:
        """Get message statistics."""
        where_clause = "WHERE store_id = ?" if store_id else ""
        params = (store_id,) if store_id else ()

        async with self._connection.execute(
            f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'replied' THEN 1 ELSE 0 END) as replied,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending
            FROM messages
            {where_clause}
            """,
            params
        ) as cursor:
            row = await cursor.fetchone()
            return {
                "total": row[0],
                "replied": row[1],
                "failed": row[2],
                "pending": row[3],
            }

    def _row_to_message(self, row: aiosqlite.Row) -> Message:
        """Convert database row to Message object."""
        user = User(
            user_id=row["user_id"],
            nickname=row["user_nickname"],
        )
        return Message(
            message_id=row["message_id"],
            conversation_id=row["conversation_id"],
            store_id=row["store_id"],
            user=user,
            content=row["content"],
            message_type=MessageType(row["message_type"]),
            timestamp=datetime.fromisoformat(row["timestamp"]),
            status=MessageStatus(row["status"]),
            reply_content=row["reply_content"],
            reply_timestamp=datetime.fromisoformat(row["reply_timestamp"]) if row["reply_timestamp"] else None,
            api_response_time_ms=row["api_response_time_ms"],
            error_message=row["error_message"],
            raw_data=json.loads(row["raw_data"]) if row["raw_data"] else {},
        )

    def _row_to_conversation(self, row: aiosqlite.Row) -> Conversation:
        """Convert database row to Conversation object."""
        user = User(
            user_id=row["user_id"],
            nickname=row["user_nickname"],
        )
        return Conversation(
            conversation_id=row["conversation_id"],
            store_id=row["store_id"],
            user=user,
            is_auto_reply_enabled=bool(row["is_auto_reply_enabled"]),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )


# Global database instance
_db_instance: Optional[MessageDatabase] = None


def get_db() -> MessageDatabase:
    """Get global database instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = MessageDatabase()
    return _db_instance