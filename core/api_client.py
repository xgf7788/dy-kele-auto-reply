"""API client for custom reply service with connection pooling."""
import asyncio
import time
from typing import Optional, Dict, Any
import aiohttp

from config import StoreConfig, settings
from models.message import Message, ReplyResponse, Conversation
from utils.logger import get_logger
from utils.helpers import retry_with_backoff

logger = get_logger(__name__)

# Module-level session singleton for connection reuse across requests
# Using a TCPConnector with keepalive to avoid repeated TCP handshakes
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    """Get or create a shared aiohttp session with connection pooling.

    Uses a connector that:
    - Limits concurrent connections to 20 (configurable via API_SESSION_POOL_SIZE)
    - Keeps connections alive for 30s for reuse
    - Enables TCP keepalive probes
    """
    global _session

    if _session is not None and not _session.closed:
        return _session

    async with _session_lock:
        # Double-check after acquiring lock
        if _session is not None and not _session.closed:
            return _session

        connector = aiohttp.TCPConnector(
            limit=getattr(settings, 'api_session_pool_size', 20),
            keepalive_timeout=getattr(settings, 'api_keepalive_timeout', 30),
            enable_cleanup_closed=True,
            force_close=False,
        )
        timeout = aiohttp.ClientTimeout(
            total=getattr(settings, 'default_api_timeout', 60),
            connect=10,
        )
        _session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        logger.info(f"Created shared API session (pool={connector._limit})")
        return _session


async def close_session() -> None:
    """Close the shared session gracefully."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None
        logger.info("API session closed")


class ApiClient:
    """Client for communicating with the custom reply API.

    Uses a module-level shared session for connection reuse.
    Each store can still have its own timeout/auth config.
    """

    def __init__(self, config: StoreConfig):
        self.config = config
        self._own_timeout = aiohttp.ClientTimeout(total=config.api_timeout)

    async def __aenter__(self):
        """Async context manager entry — shared session, per-request timeout."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit — no-op (session is shared)."""
        pass

    @retry_with_backoff(max_retries=3, initial_delay=1.0)
    async def get_reply(
        self,
        message: Message,
        conversation: Optional[Conversation] = None
    ) -> ReplyResponse:
        """Get reply from API for a message.

        Args:
            message: The message to reply to
            conversation: Optional conversation context

        Returns:
            ReplyResponse object
        """
        session = await _get_session()
        user_name = message.user.nickname

        payload = {
            "msg_id": message.message_id,
            "acc_id": self.config.name,
            "goods_order": "",
            "msg": message.content,
            "from_id": user_name,
            "from_name": user_name,
            "goods_id": "",
            "cy_id": user_name,
            "acc_type": "抖音来客私信",
            "msg_type": 0,
            "goods_name": "",
            "cy_name": user_name,
            "browser_id": 0
        }

        start_time = time.time()
        max_retries = 3
        retry_count = 0

        while retry_count <= max_retries:
            try:
                headers = {"Content-Type": "application/json"}
                if self.config.api_key:
                    headers["Authorization"] = f"Bearer {self.config.api_key}"

                logger.info(f"[请求API] 消息ID: {message.message_id}, 用户: {user_name}, 内容: {message.content[:100]}...")

                async with session.post(
                    self.config.api_endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self._own_timeout,
                ) as response:
                    api_response_time_ms = int((time.time() - start_time) * 1000)

                    raw_text = await response.text()

                    if response.status != 200:
                        logger.error(f"API HTTP error: {response.status} - {raw_text[:200]}")
                        retry_count += 1
                        if retry_count > max_retries:
                            raise Exception(
                                f"API HTTP error after {max_retries} retries: "
                                f"{response.status} - {raw_text[:200]}"
                            )
                        logger.warning(
                            f"API retry {retry_count}/{max_retries} for message "
                            f"{message.message_id} (HTTP {response.status})"
                        )
                        await asyncio.sleep(5)
                        continue

                    try:
                        response_data = await response.json()
                    except Exception as e:
                        logger.error(f"API JSON parse error: {e}. Raw: {raw_text[:200]}")
                        retry_count += 1
                        if retry_count > max_retries:
                            raise Exception(f"API JSON parse error after {max_retries} retries: {e}")
                        logger.warning(
                            f"API retry {retry_count}/{max_retries} for message "
                            f"{message.message_id} (JSON error)"
                        )
                        await asyncio.sleep(5)
                        continue

                    return ReplyResponse.from_api_response(response_data)

            except asyncio.TimeoutError:
                retry_count += 1
                if retry_count > max_retries:
                    raise Exception(f"API timeout after {max_retries} retries")
                logger.warning(
                    f"API timeout for message {message.message_id} "
                    f"(attempt {retry_count}/{max_retries}), retrying..."
                )
                await asyncio.sleep(5)
            except Exception as e:
                retry_count += 1
                if retry_count > max_retries:
                    raise
                logger.error(
                    f"API error for message {message.message_id}: {e}, "
                    f"retry {retry_count}/{max_retries}"
                )
                await asyncio.sleep(5)

        raise Exception(f"API failed after {max_retries} retries")

    async def health_check(self) -> bool:
        """Check if the API is responsive.

        Returns:
            True if API is responsive
        """
        try:
            return True
        except Exception as e:
            logger.error(f"API health check failed: {e}")
            return False
