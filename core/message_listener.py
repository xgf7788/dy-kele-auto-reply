"""Message listener for monitoring IM messages from multiple stores."""
import asyncio
import hashlib
import json
import re
import time
from typing import Dict, List, Optional, Callable, Set, Tuple
from datetime import datetime
from core.listener import PageUtilsMixin, ConversationMixin, ExtractionMixin, DedupMixin

from playwright.async_api import Page

from config import settings
from core.store_manager import StoreManager, Store, StoreStatus
from models.message import Message, User
from storage import get_db, MessageDatabase
from utils.logger import get_logger
from utils.constants import (
    EMOJI_PATTERN,
    EMOJI_PATTERN_JS,
    SYSTEM_STATUS_TEXTS,
    BOT_MESSAGE_PATTERNS,
    STATS_MESSAGE_PATTERNS,
    EVALUATION_PATTERNS,
    SELECTORS_CONVERSATION,
    SELECTORS_MESSAGE,
    SELECTORS_HEADER,
    SELECTORS_INPUT,
)

logger = get_logger(__name__)


class MessageListener(PageUtilsMixin, ConversationMixin, ExtractionMixin, DedupMixin):
    """Listener for IM messages from Douyin Kele."""

    def __init__(self, store_manager: StoreManager, message_callback: Callable[[Message], None]):
        self.store_manager = store_manager
        self.message_callback = message_callback
        self._running = False
        self._tasks: List[asyncio.Task] = []
        # Use Dict instead of Set to track message timestamps for LRU cleanup
        # Key: message_id, Value: (timestamp when added, store_id)
        self._processed_messages: Dict[str, Tuple[float, str]] = {}
        self._last_message_times: Dict[str, float] = {}  # Last message time per store
        self._sent_messages: Dict[str, Set[str]] = {}  # Track messages sent by Bot per store
        self.db: MessageDatabase = get_db()  # Database for persistent dedup

        # Track current conversation and message counts for real-time new message detection
        self._current_conversation: Dict[str, Optional[str]] = {}  # store_id -> user_name
        self._conversation_message_counts: Dict[str, int] = {}  # store_id -> last known message count
        
        # Track pending messages per user (messages that need replies)
        # Key: store_id + user_name, Value: list of pending message dicts
        self._pending_messages: Dict[str, List[Dict]] = {}
        
        # Track last message content and time per user for deduplication
        # Key: store_id + user_name, Value: {content: str, time: str}
        self._last_message_info: Dict[str, Dict] = {}
        
        # Track messages that have been sent to API but waiting for reply to be sent
        # Key: message_id, Value: {message, reply_text, timestamp}
        self._awaiting_reply: Dict[str, Dict] = {}
        
        # ===== 方案1: 跟踪正在处理的用户 =====
        # Key: store_id, Value: user_name (正在处理的用户，None表示空闲)
        self._processing_users: Dict[str, Optional[str]] = {}
        self._processing_lock = asyncio.Lock()  # 保护 processing_users 的锁
        
        # ===== 保险解锁任务跟踪 =====
        # Key: store_id, Value: asyncio.Task 当前店铺的保险解锁任务
        # 用于避免多个保险任务叠加，导致在处理中误释放锁
        self._unlock_tasks: Dict[str, asyncio.Task] = {}
        
        # ===== 页面自动刷新跟踪 =====
        # Key: store_id, Value: timestamp of last page refresh
        self._last_page_refresh: Dict[str, float] = {}

    async def start(self) -> None:
        """Start listening for messages from all online stores."""
        self._running = True

        # Start listener for each online store
        for store in self.store_manager.get_all_stores():
            if store.config.enabled:
                task = asyncio.create_task(
                    self._listen_store(store),
                    name=f"listener_{store.store_id}"
                )
                self._tasks.append(task)

        # Start dedup cleanup task
        cleanup_task = asyncio.create_task(
            self._cleanup_dedup_cache(),
            name="dedup_cleanup"
        )
        self._tasks.append(cleanup_task)

        logger.info(f"Message listener started for {len(self._tasks)} stores")

    async def stop(self) -> None:
        """Stop all listeners."""
        self._running = False

        for task in self._tasks:
            task.cancel()

        # Wait for all tasks to complete
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        self._tasks.clear()
        logger.info("Message listener stopped")

    @staticmethod
    def _normalize_user_name(user_name: str) -> str:
        """Remove trailing punctuation and emojis from user name for better matching.
        
        IM sidebar may include trailing punctuation or emojis in extracted text,
        while the chat header shows the clean name. Normalizing prevents
        verification and click failures.
        
        NOTE: If the user name consists entirely of emojis, the original is preserved
        to support users whose nickname is only emoji(s).
        """
        if not user_name:
            return user_name
        original = user_name.strip()
        # Remove trailing punctuation
        user_name = user_name.rstrip('，。！？、；：""''（）《》…—~.,!?;:\'"()[]{}')
        # Remove trailing emojis (uses shared EMOJI_PATTERN from utils.constants)
        emoji_pattern = re.compile(EMOJI_PATTERN, flags=re.UNICODE)
        user_name = emoji_pattern.sub('', user_name)
        # If stripping leaves nothing, the name was likely pure emoji - keep original
        if not user_name:
            return original
        return user_name

    # ===== 方案1: 设置/获取正在处理的用户 =====
    def set_processing_user(self, store_id: str, user_name: Optional[str]) -> None:
        """设置/清除正在处理的用户。
        
        当 MessageHandler 开始处理某用户时调用此方法通知监听器，
        监听器在处理期间不会切换到其他用户的会话。
        
        Args:
            store_id: 店铺ID
            user_name: 正在处理的用户名，None表示处理完成/空闲
        """
        if user_name:
            self._processing_users[store_id] = user_name
            logger.info(f"[处理状态] 店铺 {store_id} 开始处理用户: {user_name}")
        else:
            if store_id in self._processing_users:
                old_user = self._processing_users.pop(store_id)
                logger.info(f"[处理状态] 店铺 {store_id} 完成处理用户: {old_user}")
            # 处理完成/空闲时，取消对应的保险解锁任务，避免误释放后续用户的锁
            self._cancel_unlock_task(store_id)
    
    def get_processing_user(self, store_id: str) -> Optional[str]:
        """获取当前正在处理的用户。
        
        Args:
            store_id: 店铺ID
            
        Returns:
            正在处理的用户名，如果没有则返回None
        """
        return self._processing_users.get(store_id)
    
    def is_processing_user(self, store_id: str, user_name: str) -> bool:
        """检查指定用户是否正在处理中。
        
        Args:
            store_id: 店铺ID
            user_name: 用户名
            
        Returns:
            True 如果该用户正在处理中
        """
        return self._processing_users.get(store_id) == user_name
    
    def _cancel_unlock_task(self, store_id: str) -> None:
        """取消指定店铺的保险解锁任务。"""
        task = self._unlock_tasks.pop(store_id, None)
        if task and not task.done():
            task.cancel()
            logger.debug(f"[保险任务] 店铺 {store_id} 取消旧的保险解锁任务")
    
    def _renew_unlock_task(self, store_id: str, user_name: str, timeout: int = 300) -> None:
        """续租保险解锁任务：取消旧任务并启动新任务。
        
        避免多个保险任务叠加导致在处理中误释放锁。
        """
        self._cancel_unlock_task(store_id)
        task = asyncio.create_task(
            self._auto_unlock_after_timeout(store_id, user_name, timeout=timeout),
            name=f"auto_unlock_{store_id}_{user_name}"
        )
        self._unlock_tasks[store_id] = task
        logger.debug(f"[保险任务] 店铺 {store_id} 用户 {user_name} 续租保险解锁任务 {timeout}s")
    
    def is_any_user_processing(self, store_id: str) -> bool:
        """检查指定店铺是否有用户正在处理中。
        
        Args:
            store_id: 店铺ID
            
        Returns:
            True 如果有用户正在处理中
        """
        return store_id in self._processing_users and self._processing_users[store_id] is not None
    
    async def _auto_unlock_after_timeout(self, store_id: str, user_name: str, timeout: int = 300) -> None:
        """保险解锁：在指定时间后，如果还在处理同一用户，则自动释放锁。
        
        防止因为异常或回调失败导致 processing_user 永远被占用。
        """
        try:
            await asyncio.sleep(timeout)
            current = self._processing_users.get(store_id)
            if current == user_name:
                logger.warning(f"[保险解锁] 店铺 {store_id} 处理用户 {user_name} 超过 {timeout} 秒未释放，强制解锁")
                self.set_processing_user(store_id, None)
        except asyncio.CancelledError:
            # 任务被取消是正常行为（续租或正常完成），无需记录错误
            logger.debug(f"[保险任务] 店铺 {store_id} 用户 {user_name} 的保险解锁任务被取消")
            raise
    
    async def _listen_store(self, store: Store) -> None:
        """Listen for messages from a specific store."""
        retry_count = 0
        # No max_retries limit for 24/7 operation

        offline_start_time = None
        
        while self._running and store.config.enabled:
            try:
                # Wait for store to be online
                if not store.is_online:
                    if offline_start_time is None:
                        offline_start_time = time.time()
                        logger.warning(f"[监听暂停] 店铺: {store.store_id} 离线，等待恢复...")
                    await asyncio.sleep(5)
                    continue
                else:
                    if offline_start_time is not None:
                        offline_duration = time.time() - offline_start_time
                        logger.info(f"[监听恢复] 店铺: {store.store_id} 已恢复，暂停了 {offline_duration:.0f} 秒")
                        offline_start_time = None

                retry_count = 0
                logger.info(f"[监听启动] 店铺: {store.store_id}")

                # Navigate to IM page
                await self._navigate_to_im(store)

                # Start message polling
                await self._poll_messages(store)

            except asyncio.CancelledError:
                logger.info(f"Listener for store {store.store_id} cancelled")
                break
            except Exception as e:
                retry_count += 1
                logger.error(f"Store {store.store_id} listener error: {e}")
                
                # Log retry but never stop for 24/7 operation
                logger.warning(f"Store {store.store_id} will retry (attempt {retry_count})")
                
                # Fast retry for quick recovery
                await asyncio.sleep(2)

    async def _poll_messages(self, store: Store) -> None:
        """Poll for new messages from the IM interface.
        
        Strategy:
        1. Check sidebar for conversations with unread badges
        2. Only switch to a conversation if it has unread badge
        3. Process messages in that conversation
        4. Go back to checking sidebar
        
        This avoids unnecessary switching - we only switch when sidebar shows unread.
        """
        page = store.page
        if not page:
            return

        # Initialize tracking for this store
        self._current_conversation[store.store_id] = None
        self._conversation_message_counts[store.store_id] = 0
        self._last_page_refresh[store.store_id] = time.time()  # 初始化页面刷新时间

        poll_loop_count = 0
        first_run = True
        
        while self._running and store.is_online:
            try:
                poll_loop_count += 1
                
                # Every 600 iterations (about 5 minute), log status
                if poll_loop_count % 600 == 0:
                    logger.debug(f"[监听心跳] 店铺: {store.store_id}, 轮询次数: {poll_loop_count}, 当前用户: {self._current_conversation.get(store.store_id)}")
                
                # STEP 0: First run - just detect current conversation, don't process messages
                # 首次进入IM界面，只记录当前会话，不处理消息
                # 优先检查左侧边栏的未读消息（STEP 1）
                if first_run:
                    first_run = False
                    
                    # 检查是否有正在处理的用户（从上次会话恢复）
                    processing_user = self.get_processing_user(store.store_id)
                    if processing_user:
                        logger.info(f"[首次运行] 店铺: {store.store_id} 检测到正在处理用户 {processing_user}，继续处理")
                        self._current_conversation[store.store_id] = processing_user
                    else:
                        # 首次进入IM，不检测默认会话，避免把店铺名称/系统标题误认为用户名
                        # 消息处理完全交给 STEP 1（检查左侧边栏未读），有新消息再开始处理
                        logger.debug(f"[首次运行] 店铺: {store.store_id}, 跳过默认会话检测，等待侧边栏未读消息")
                    
                    # 首次运行后直接进入 STEP 1 检查侧边栏未读消息
                    logger.debug(f"[首次运行] 店铺: {store.store_id}, 进入 STEP 1")
                
                # ===== 关键: 获取当前处理状态 =====
                processing_user = self.get_processing_user(store.store_id)

                # STEP 1: Check sidebar for unread conversations
                # _find_first_unread_conversation 已经内置了 processing_user 保护：
                # 如果正在处理用户A，它只会返回用户A的未读（或None），绝不会返回用户B
                unread_conv = await self._find_first_unread_conversation(page, store)

                if unread_conv:
                    user_name = unread_conv.get('user_name')
                    current_user = self._current_conversation.get(store.store_id)

                    # ===== 防御性检查: 不应该出现 user_name != processing_user =====
                    # 因为 _find_first_unread_conversation 已经过滤了，但保留此检查作为安全网
                    if processing_user and user_name != processing_user:
                        logger.error(
                            f"[严重错误] _find_first_unread_conversation 返回了非处理中用户! "
                            f"处理中: {processing_user}, 返回: {user_name}。"
                            f"拒绝切换，继续等待。"
                        )
                        await asyncio.sleep(0.5)
                        continue

                    # ===== 处理新会话或继续处理同一会话 =====
                    if user_name != current_user:
                        logger.info(f"[新会话] 店铺: {store.store_id}, 用户: {user_name}")

                        # 切换前立即上锁
                        if not processing_user:
                            self.set_processing_user(store.store_id, user_name)
                            processing_user = user_name
                            logger.info(f"[预上锁] 店铺 {store.store_id} 用户 {user_name} 切换前上锁")

                        # 带重试的会话切换（最多3次）
                        switch_success = await self._switch_to_conversation(
                            page, store, user_name, unread_conv.get('element')
                        )

                        retry_attempts = 0
                        max_switch_retries = 2
                        while not switch_success and retry_attempts < max_switch_retries:
                            retry_attempts += 1
                            still_in_sidebar = await self._is_user_in_sidebar(page, user_name)
                            if not still_in_sidebar:
                                logger.warning(
                                    f"[切换放弃] 店铺 {store.store_id} 用户 {user_name} "
                                    f"已不在侧边栏中，放弃切换"
                                )
                                break

                            logger.warning(
                                f"[切换重试] 店铺 {store.store_id} 用户 {user_name} "
                                f"第 {retry_attempts}/{max_switch_retries} 次重试"
                            )
                            await asyncio.sleep(1.5)  # 等待页面稳定

                            retry_clicked = await self._click_conversation_by_name(
                                page, store.store_id, store, user_name
                            )
                            if retry_clicked:
                                # 动态验证等待
                                max_wait = 5.0
                                check_interval = 0.3
                                elapsed = 0.0
                                while elapsed <= max_wait:
                                    try:
                                        switch_success = await asyncio.wait_for(
                                            self._verify_current_conversation_user(
                                                page, user_name, store.store_id
                                            ),
                                            timeout=10.0
                                        )
                                    except asyncio.TimeoutError:
                                        switch_success = False
                                    if switch_success:
                                        self._current_conversation[store.store_id] = user_name
                                        self._conversation_message_counts[store.store_id] = 0
                                        break
                                    await asyncio.sleep(check_interval)
                                    elapsed += check_interval

                        if not switch_success:
                            # 所有重试都失败了 — 不释放锁，等待下次轮询重试
                            # 释放锁会导致其他用户抢占，当前用户的消息可能遗漏
                            logger.error(
                                f"[切换失败] 店铺 {store.store_id} 用户 {user_name} "
                                f"{max_switch_retries + 1} 次尝试均失败，保持锁定等待下次轮询"
                            )
                            # NOTE: 不释放 processing_user 锁！
                            # 如果释放了锁，其他用户可能抢占，导致当前用户消息遗漏
                            await asyncio.sleep(2.0)
                            continue

                        logger.info(f"[切换成功] 店铺 {store.store_id} 用户 {user_name}")

                        # 切换后多次尝试提取消息
                        has_messages = False
                        max_extract_attempts = 3
                        for extract_attempt in range(max_extract_attempts):
                            # 使用 asyncio.wait 而不是 wait_for！
                            # wait_for 超时后会尝试 cancel 任务，但如果协程卡在
                            # Playwright CDP 操作中（page.evaluate），cancel 本身也会卡死。
                            # asyncio.wait 超时后直接返回，不尝试取消任务。
                            task = asyncio.ensure_future(
                                self._process_conversation_messages(page, store, user_name)
                            )
                            done, pending = await asyncio.wait([task], timeout=10.0)
                            if pending:
                                # 超时！任务已泄露（卡在浏览器中），但我们必须继续
                                logger.warning(
                                    f"[提取超时] 店铺 {store.store_id} 用户 {user_name} "
                                    f"_process_conversation_messages 超时 (10s)，第 {extract_attempt+1} 次"
                                )
                                has_messages = False
                                break  # 超时不重试！浏览器可能卡死
                            else:
                                has_messages = task.result()
                            if has_messages:
                                break
                            if extract_attempt < max_extract_attempts - 1:
                                logger.warning(
                                    f"[无消息重试] 店铺 {store.store_id} 用户 {user_name} "
                                    f"第 {extract_attempt+1} 次未提取到消息，等待后重试..."
                                )
                                await asyncio.sleep(2.0)

                        if has_messages:
                            self._last_message_times[store.store_id] = time.time()
                            self._renew_unlock_task(store.store_id, user_name, timeout=300)
                        else:
                            logger.info(
                                f"[无新消息] 店铺 {store.store_id} 用户 {user_name} "
                                f"未提取到消息"
                            )
                            if not self.is_any_user_processing(store.store_id):
                                self.set_processing_user(store.store_id, None)
                                processing_user = None
                            else:
                                logger.info(
                                    f"[保持锁定] 店铺 {store.store_id} 用户 {user_name} "
                                    f"Handler 仍在处理中，保持锁定"
                                )
                        continue
                    else:
                        # Same user but still has unread - process without switching
                        logger.debug(f"[继续处理] 店铺: {store.store_id}, 用户: {user_name}")

                        if not processing_user:
                            self.set_processing_user(store.store_id, user_name)
                            processing_user = user_name
                            logger.info(f"[继续处理-上锁] 店铺 {store.store_id} 用户 {user_name}")

                        # 使用 asyncio.wait 而不是 wait_for，避免 cancel 卡死
                        task = asyncio.ensure_future(
                            self._process_conversation_messages(page, store, user_name)
                        )
                        done, pending = await asyncio.wait([task], timeout=10.0)
                        if pending:
                            logger.warning(
                                f"[提取超时] 店铺 {store.store_id} 用户 {user_name} "
                                f"_process_conversation_messages 超时 (10s)，继续轮询"
                            )
                            has_messages = False
                        else:
                            has_messages = task.result()
                        if has_messages:
                            self._renew_unlock_task(store.store_id, user_name, timeout=300)
                        else:
                            # 同会话无新消息
                            if processing_user == user_name:
                                logger.debug(
                                    f"[无新消息但处理中] 店铺 {store.store_id} "
                                    f"用户 {user_name} 仍在处理中，保持锁定"
                                )
                            else:
                                logger.info(f"[无新消息] 店铺 {store.store_id} 用户 {user_name} 释放锁")
                                self.set_processing_user(store.store_id, None)
                                processing_user = None
                        continue
                
                # STEP 2: No unread in sidebar, check if current user has new messages
                # This handles case where we're already in conversation and new message arrives
                current_user = self._current_conversation.get(store.store_id)
                if current_user:
                    # ===== 防御修复: 如果 current_user 是时间格式，重置并跳过 =====
                    if self._is_time_format(current_user):
                        logger.warning(f"[时间格式用户] 店铺 {store.store_id} current_user 是时间格式 '{current_user}'，重置会话跟踪")
                        self._current_conversation[store.store_id] = None
                        self._conversation_message_counts[store.store_id] = 0
                        await asyncio.sleep(0.5)
                        continue
                    
                    # ===== 方案1: 检查当前用户是否是正在处理的用户 =====
                    if processing_user and current_user != processing_user:
                        # 当前会话不是正在处理的用户，跳过检查新消息
                        logger.debug(f"[处理中跳过检查] 店铺 {store.store_id} 正在处理 {processing_user}")
                        await asyncio.sleep(1)
                        continue
                    
                    # Check for NEW messages (not all messages)
                    new_messages = await self._check_current_conversation_for_new_messages(page, store, current_user)
                    if new_messages:
                        logger.info(f"[新消息] 店铺: {store.store_id}, 用户: {current_user}, 数量: {len(new_messages)}")
                        
                        # ===== 关键修复: 续租锁 =====
                        # 如果当前有锁，续租它（重置保险解锁计时器）
                        if processing_user == current_user:
                            self._renew_unlock_task(store.store_id, current_user, timeout=300)
                        elif not processing_user:
                            # 没有锁，设置锁并启动保险解锁
                            self.set_processing_user(store.store_id, current_user)
                            processing_user = current_user
                            self._renew_unlock_task(store.store_id, current_user, timeout=300)
                        
                        # Process immediately
                        for msg in new_messages:
                            await self._process_single_message(msg, store, expected_user=current_user)
                        continue
                
                # STEP 3: Check if page needs refresh
                await self._check_and_refresh_page(store)
                
                # STEP 4: No new messages anywhere, just wait
                # No unread messages in sidebar
                await asyncio.sleep(settings.message_poll_interval)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Store {store.store_id}: Message polling error: {e}")
                await asyncio.sleep(1)  # 错误后快速重试
        
        # Polling loop ended - log why
        if not self._running:
            logger.info(f"[监听结束] 店铺: {store.store_id}, 原因: 监听器停止")
        elif not store.is_online:
            logger.warning(f"[监听结束] 店铺: {store.store_id}, 原因: 店铺离线")
        else:
            logger.warning(f"[监听结束] 店铺: {store.store_id}, 原因: 未知")
    
    def _is_time_format(self, text: str) -> bool:
        """Check if text is a time format like '12小时前', '2分钟前', '刚刚', etc.

        NOTE: HH:MM like '10:20' is NOT filtered as it could be a username.
        NOTE: Single digits like '7' are NOT filtered as they could be usernames.
        """
        import re
        time_patterns = [
            # Standard relative time formats
            r'^\d+分钟前?$',     # 5分钟前, 1分钟
            r'^\d+小时前?$',     # 12小时前, 1小时
            r'^\d+天前?$',       # 3天前, 1天
            r'^\d+周前?$',       # 2周前, 1周
            r'^\d+月前?$',       # 1月前, 1月
            r'^\d+年前?$',       # 1年前, 1年
            r'^刚刚$',           # 刚刚
            r'^-?\d+秒$',       # -5秒, 5秒 (relative seconds without '前')
            r'^-?\d+秒前$',     # -5秒前, 5秒前 (relative seconds with '前')
            # "Less than N" time formats (<30, <30分钟, <1小时, etc.)
            r'^<\d+分钟?$',      # <30分钟, <1分钟
            r'^<\d+小时?$',      # <1小时
            r'^<\d+天?$',        # <1天
            r'^<\d+$',           # <30 (bare number after <)
            # "Within N" time formats
            r'^\d+分钟内?$',      # 30分钟内
            r'^\d+小时内?$',      # 1小时内
        ]
        for pattern in time_patterns:
            if re.match(pattern, text):
                return True
        return False

    def _parse_message_time(self, time_str: str) -> datetime:
        """Parse message timestamp from string to datetime object.
        
        Handles formats like:
        - '10:44' (HH:MM) - assumes today
        - '2024-03-05' (date only)
        - '刚刚' (just now)
        - '' (empty) - returns current time
        
        Args:
            time_str: Time string from message element
            
        Returns:
            datetime object (naive, local time)
        """
        from datetime import datetime, timedelta
        import re
        
        if not time_str:
            return datetime.now()
        
        time_str = time_str.strip()
        
        # Handle '刚刚' (just now)
        if time_str == '刚刚':
            return datetime.now()
        
        # Handle HH:MM format (e.g., '10:44')
        if re.match(r'^\d{1,2}:\d{2}$', time_str):
            try:
                # Parse time
                hour, minute = map(int, time_str.split(':'))
                now = datetime.now()
                msg_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                
                # If the time is in the future (more than 1 hour ahead), assume it's from yesterday
                if msg_time > now + timedelta(hours=1):
                    msg_time = msg_time - timedelta(days=1)
                
                return msg_time
            except:
                return datetime.now()
        
        # Handle 'X分钟前' (X minutes ago)
        if re.match(r'^\d+分钟前$', time_str):
            try:
                minutes = int(time_str.replace('分钟前', ''))
                return datetime.now() - timedelta(minutes=minutes)
            except:
                return datetime.now()
        
        # Handle 'X小时前' (X hours ago)
        if re.match(r'^\d+小时前$', time_str):
            try:
                hours = int(time_str.replace('小时前', ''))
                return datetime.now() - timedelta(hours=hours)
            except:
                return datetime.now()
        
        # Handle date format '2024-03-05'
        if re.match(r'^\d{4}-\d{2}-\d{2}$', time_str):
            try:
                return datetime.strptime(time_str, '%Y-%m-%d')
            except:
                return datetime.now()
        
        # Default: return current time
        return datetime.now()

    async def add_store_listener(self, store_id: str) -> None:
        """Add a listener for a new store."""
        store = self.store_manager.get_store(store_id)
        if not store:
            logger.error(f"Store {store_id} not found")
            return

        # Check if already listening
        for task in self._tasks:
            if task.get_name() == f"listener_{store_id}":
                logger.warning(f"Listener for store {store_id} already exists")
                return

        task = asyncio.create_task(
            self._listen_store(store),
            name=f"listener_{store_id}"
        )
        self._tasks.append(task)

    def get_stats(self) -> Dict:
        """Get listener statistics."""
        return {
            "running": self._running,
            "active_listeners": len([t for t in self._tasks if not t.done()]),
            "processed_messages": len(self._processed_messages),
            "stores": {
                store_id: {
                    "last_message_time": self._last_message_times.get(store_id)
                }
                for store_id in self._last_message_times
            }
        }
