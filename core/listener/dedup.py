"""Deduplication and bot-message tracking for the IM listener."""

import asyncio
import hashlib
import json
import time
from typing import Dict, List, Optional, Set, Tuple

from models.message import Message, MessageStatus
from storage import get_db, MessageDatabase
from utils.logger import get_logger


logger = get_logger(__name__)


class DedupMixin:
    """Mixin providing dedup methods for MessageListener."""

    def record_sent_message(self, store_id: str, message_content: str) -> None:
        """Record a message sent by the Bot to help distinguish from user messages.

        Args:
            store_id: The store ID
            message_content: The content of the message sent by Bot
        """
        if store_id not in self._sent_messages:
            self._sent_messages[store_id] = set()

        # Store normalized version of the message (first 100 chars for matching)
        normalized = message_content.strip()[:100]
        self._sent_messages[store_id].add(normalized)

        # Keep only recent messages (limit to 50 per store)
        if len(self._sent_messages[store_id]) > 50:
            # Convert to list, keep last 50, convert back to set
            self._sent_messages[store_id] = set(list(self._sent_messages[store_id])[-50:])

        # Recorded sent message


    def is_bot_message(self, store_id: str, message_content: str) -> bool:
        """Check if a message was sent by the Bot (by comparing with sent message cache).

        Args:
            store_id: The store ID
            message_content: The message content to check

        Returns:
            True if the message matches one sent by Bot
        """
        if store_id not in self._sent_messages:
            return False

        normalized = message_content.strip()[:100]

        # Check exact match
        if normalized in self._sent_messages[store_id]:
            return True

        # Check if message starts with any sent message (handles truncated content)
        for sent_msg in self._sent_messages[store_id]:
            if normalized.startswith(sent_msg[:50]) or sent_msg.startswith(normalized[:50]):
                return True

        return False


    async def _cleanup_dedup_cache(self) -> None:
        """Periodically clean up old message IDs from dedup cache using LRU strategy.
        
        Cleanup strategy:
        1. Remove entries older than 72 hours
        2. If still too many (>100k), remove oldest 50%
        3. Run every 6 hours
        """
        MAX_AGE_HOURS = 72
        MAX_ENTRIES = 100000
        CLEANUP_INTERVAL = 21600  # 6 hours in seconds
        
        while self._running:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL)
                
                current_time = time.time()
                max_age_seconds = MAX_AGE_HOURS * 3600
                
                # Phase 1: Remove entries older than MAX_AGE_HOURS
                expired_keys = [
                    key for key, (timestamp, _) in self._processed_messages.items()
                    if current_time - timestamp > max_age_seconds
                ]
                
                for key in expired_keys:
                    del self._processed_messages[key]
                
                if expired_keys:
                    logger.info(f"[内存清理] 清理了 {len(expired_keys)} 条超过 {MAX_AGE_HOURS} 小时的消息记录")
                
                # Phase 2: If still too many entries, remove oldest 50%
                current_count = len(self._processed_messages)
                if current_count > MAX_ENTRIES:
                    # Sort by timestamp and keep only newest 50%
                    sorted_items = sorted(
                        self._processed_messages.items(),
                        key=lambda x: x[1][0],  # Sort by timestamp
                        reverse=True  # Newest first
                    )
                    keep_count = MAX_ENTRIES // 2
                    self._processed_messages = dict(sorted_items[:keep_count])
                    removed_count = current_count - len(self._processed_messages)
                    logger.info(f"[内存清理] 条目数超过 {MAX_ENTRIES}，清理了最老的 {removed_count} 条，保留 {len(self._processed_messages)} 条")
                
                # Log current status
                if self._processed_messages:
                    logger.info(f"[内存清理] 当前内存中共有 {len(self._processed_messages)} 条消息记录")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[内存清理] 错误: {e}")


    async def _safe_callback(self, message: Message, expected_user: str = None) -> None:
        """Safely call the message callback."""
        try:
            # Validate user if expected_user is provided
            if expected_user and message.user.nickname != expected_user:
                logger.warning(f"CRITICAL: User mismatch in callback! Message from '{message.user.nickname}' but expected '{expected_user}'")
                logger.warning(f"Message content: {message.content[:50]}...")
                # FIX: Update message user to match expected user instead of skipping
                # Fixing message user
                message.user.nickname = expected_user
                message.user.user_id = expected_user

            logger.info(f"[提交处理] 店铺: {message.store_id}, 用户: {message.user.nickname}, 内容: {message.content[:50]}...")
            # Message details
            result = self.message_callback(message)
            if asyncio.iscoroutine(result):
                await result
            # Message callback completed
        except Exception as e:
            logger.error(f"Message callback error for user {message.user.nickname}: {e}")

