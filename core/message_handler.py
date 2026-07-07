"""Message handler for processing and replying to messages."""
import asyncio
import time
from datetime import datetime
from typing import Optional, Dict
from dataclasses import dataclass

from config import settings, StoreConfig
from core.api_client import ApiClient
from core.store_manager import StoreManager, Store
from models.message import Message, MessageStatus, Conversation, ReplyResponse, User
from storage import get_db, MessageDatabase
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class HandlerStats:
    """Statistics for message handling."""
    total_received: int = 0
    total_processed: int = 0
    total_replied: int = 0
    total_failed: int = 0
    total_skipped: int = 0
    avg_response_time_ms: float = 0.0


class MessageHandler:
    """Handler for processing incoming messages and sending replies."""

    def __init__(
        self,
        store_manager: StoreManager,
        num_workers: int = None,
        on_reply_sent: Optional[callable] = None,
        message_listener: Optional[object] = None
    ):
        self.store_manager = store_manager
        self.num_workers = num_workers or settings.handler_workers
        self.db: MessageDatabase = get_db()
        self.stats = HandlerStats()
        self._running = False
        self._conversations: Dict[str, Conversation] = {}
        self._on_reply_sent = on_reply_sent  # Callback when reply is sent
        self.message_listener = message_listener  # Reference to message listener
        
        # ===== 方案4: 用户级消息队列 =====
        # 按店铺+用户分组的消息队列，Key: "{store_id}_{user_id}"
        self._user_queues: Dict[str, asyncio.Queue] = {}
        # 用户级工作线程，Key: "{store_id}_{user_id}"
        self._user_workers: Dict[str, asyncio.Task] = {}
        # 保护用户队列和工作者字典的锁
        self._users_dict_lock = asyncio.Lock()
        
        # 按用户分锁，粒度更细：同店铺不同用户可以并发处理
        # Key: "{store_id}_{user_id}", Value: asyncio.Lock
        self._user_locks: Dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()  # 保护锁字典的锁
        
        # 记录每个用户当前所在的会话（避免不必要的切换）
        self._user_current_conversation: Dict[str, str] = {}
        
        # 店铺级别的导航锁（只锁住页面导航，不锁住消息发送）
        self._nav_locks: Dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        """Start the message handler."""
        self._running = True
        logger.info(f"[消息处理器] 启动，使用用户级队列模式")

    async def stop(self) -> None:
        """Stop the message handler."""
        self._running = False
        
        # 等待所有用户队列为空
        for user_key, queue in self._user_queues.items():
            await queue.join()
        
        # 取消所有用户工作者
        for task in self._user_workers.values():
            task.cancel()
        
        if self._user_workers:
            await asyncio.gather(*self._user_workers.values(), return_exceptions=True)
        
        self._user_workers.clear()
        logger.info("[消息处理器] 已停止")

    def _get_user_key(self, store_id: str, user_id: str) -> str:
        """生成用户唯一标识."""
        return f"{store_id}_{user_id}"
    
    def _get_user_lock_key(self, store_id: str, user_id: str) -> str:
        """生成用户锁的键名。"""
        return f"{store_id}_{user_id}"
    
    async def _get_user_lock(self, store_id: str, user_id: str) -> asyncio.Lock:
        """获取指定用户的操作锁。
        
        每个用户有独立的锁，同店铺的不同用户可以并发处理消息，
        只有同一个用户的多条消息才会串行处理。
        
        Args:
            store_id: 店铺ID
            user_id: 用户ID
            
        Returns:
            该用户的异步锁
        """
        lock_key = self._get_user_lock_key(store_id, user_id)
        
        # 先检查锁是否已存在（快速路径，无需加锁）
        if lock_key in self._user_locks:
            return self._user_locks[lock_key]
        
        # 使用锁保护字典操作（慢速路径）
        async with self._locks_lock:
            # 双重检查，防止其他协程已创建
            if lock_key not in self._user_locks:
                self._user_locks[lock_key] = asyncio.Lock()
                logger.debug(f"[锁管理] 为用户 {lock_key} 创建新的锁")
            return self._user_locks[lock_key]
    
    async def _get_nav_lock(self, store_id: str) -> asyncio.Lock:
        """获取指定店铺的导航锁（用于页面导航操作）。
        
        导航锁只用于需要切换页面的操作，粒度比用户锁更粗，
        确保页面导航的串行执行。
        
        Args:
            store_id: 店铺ID
            
        Returns:
            该店铺的导航锁
        """
        # 先检查锁是否已存在
        if store_id in self._nav_locks:
            return self._nav_locks[store_id]
        
        # 使用锁保护字典操作
        async with self._locks_lock:
            if store_id not in self._nav_locks:
                self._nav_locks[store_id] = asyncio.Lock()
            return self._nav_locks[store_id]

    async def enqueue(self, message: Message) -> bool:
        """Add a message to the processing queue.
        
        使用用户级队列，确保同一用户的消息由同一个工作线程串行处理。

        Args:
            message: The message to process

        Returns:
            True if successfully queued
        """
        try:
            # Check for duplicate by message_id first (fast check)
            if await self.db.message_exists(message.message_id):
                # Message exists - check if already replied to prevent duplicate replies
                try:
                    is_replied = await self.db.is_message_replied(message.message_id)
                    if is_replied:
                        logger.debug(f"[入队跳过] 消息已回复: {message.message_id}")
                        return False
                    else:
                        # Message exists but not replied (pending/failed), allow reprocessing
                        logger.debug(f"[入队允许] 消息未回复，重新处理: {message.message_id}")
                        # Continue to save and queue
                except Exception as e:
                    # If check fails, assume already processed to be safe
                    logger.warning(f"[入队检查失败] 检查消息回复状态失败: {e}")
                    return False

            # Save to database
            await self.db.save_message(message)
            
            # ===== 方案4: 将消息放入对应用户的队列 =====
            user_key = self._get_user_key(message.store_id, message.user.user_id)
            
            async with self._users_dict_lock:
                # 如果该用户队列不存在，创建队列并启动工作者
                if user_key not in self._user_queues:
                    self._user_queues[user_key] = asyncio.Queue()
                    # 启动该用户的专用工作线程
                    task = asyncio.create_task(
                        self._user_worker_loop(user_key, message.store_id, message.user),
                        name=f"user_worker_{user_key}"
                    )
                    self._user_workers[user_key] = task
                    logger.debug(f"[用户队列] 创建: {user_key}")
                
                # ===== 关键修复: 检查是否已经在处理中（由 Listener 设置锁）=====
                # 如果 Listener 已经设置了 processing_user，Handler 不需要重复设置
                # 这避免了 Listener 和 Handler 之间的竞争条件
                if self.message_listener:
                    current_processing = self.message_listener.get_processing_user(message.store_id)
                    if not current_processing:
                        # Listener 没有设置锁，Handler 需要设置
                        queue_size = self._user_queues[user_key].qsize()
                        if queue_size == 0:
                            self.message_listener.set_processing_user(
                                message.store_id, 
                                message.user.nickname
                            )
                            logger.info(f"[入队即锁定] 店铺 {message.store_id} 用户 {message.user.nickname} "
                                       f"(队列大小: {queue_size})")
                    else:
                        logger.debug(f"[入队] 店铺 {message.store_id} 用户 {message.user.nickname}，"
                                    f"锁已由 Listener 设置 ({current_processing})")
                
                # 将消息放入用户队列
                await self._user_queues[user_key].put(message)
            
            self.stats.total_received += 1
            logger.debug(f"[入队成功] {user_key}, {message.message_id}")
            return True

        except Exception as e:
            logger.error(f"[入队失败] 消息 {message.message_id}: {e}")
            return False

    async def _user_worker_loop(self, user_key: str, store_id: str, user: User) -> None:
        """用户级工作循环 - 每个用户有独立的工作线程.
        
        确保同一用户的消息按顺序处理，不同用户之间并行处理。
        处理期间通知监听器，防止页面被切换。
        """
        logger.debug(f"[工作者启动] {user_key}")
        
        queue = self._user_queues.get(user_key)
        if not queue:
            logger.error(f"[用户工作者] 队列不存在: {user_key}")
            return
        
        while self._running:
            try:
                # 等待消息，带超时检查
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # ===== 收集同批消息：如果队列中还有更多消息，一次性取出合并 =====
                batch = [message]
                while not queue.empty():
                    try:
                        msg = queue.get_nowait()
                        batch.append(msg)
                    except asyncio.QueueEmpty:
                        break
                
                # 如果有多条消息，合并内容（用 # 连接）
                if len(batch) > 1:
                    combined_content = "#".join(m.content for m in batch)
                    logger.info(f"[消息合并] {user_key}: 合并 {len(batch)} 条消息 -> {combined_content[:100]}...")
                    batch[0].content = combined_content
                
                # ===== 方案1: 通知监听器正在处理该用户（续租锁）=====
                # 注意：锁可能已经在 Listener 中设置（发现新会话时），这里只是确认/续租
                if self.message_listener:
                    current_lock = self.message_listener.get_processing_user(store_id)
                    if current_lock != user.nickname:
                        logger.warning(f"[锁不一致] 店铺 {store_id} 当前锁是 {current_lock}，Handler 开始处理 {user.nickname}")
                    self.message_listener.set_processing_user(store_id, user.nickname)
                    logger.info(f"[开始处理] 店铺 {store_id}, 用户: {user.nickname}")
                
                try:
                    # 完整处理消息（API调用 + 发送回复）
                    await self._process_message_complete(batch[0])
                except Exception as e:
                    logger.exception(f"[处理错误] 用户 {user_key}: {e}")
                    await self._mark_failed(batch[0], str(e))
                    # 将其余消息也标记为失败
                    for msg in batch[1:]:
                        msg.status = MessageStatus.FAILED
                        msg.error_message = str(e)
                        await self.db.update_message_status(msg.message_id, MessageStatus.FAILED, error_message=str(e))
                else:
                    # 将其余消息同步为与第一条相同的状态（只更新数据库，不重复统计）
                    for msg in batch[1:]:
                        msg.status = batch[0].status
                        if batch[0].status == MessageStatus.REPLIED:
                            msg.reply_content = batch[0].reply_content
                            await self.db.update_message_status(
                                msg.message_id,
                                MessageStatus.REPLIED,
                                reply_content=batch[0].reply_content
                            )
                        elif batch[0].status == MessageStatus.SKIPPED:
                            msg.error_message = "Merged and skipped"
                            await self.db.update_message_status(msg.message_id, MessageStatus.SKIPPED, error_message="Merged and skipped")
                        elif batch[0].status == MessageStatus.FAILED:
                            msg.error_message = batch[0].error_message or "Merged processing failed"
                            await self.db.update_message_status(msg.message_id, MessageStatus.FAILED, error_message=msg.error_message)
                finally:
                    for _ in batch:
                        queue.task_done()
                    
                    # ===== 方案1: 通知监听器处理完成，清除锁 =====
                    # 关键: Handler 完成时（无论成功或失败）都要清除锁
                    # 这样才能让 Listener 处理下一个用户的新消息
                    # 
                    # 关键修复：完成一批后不要立即释放锁。Listener 轮询周期为 0.5s，
                    # 如果用户紧接着发送新消息，Listener 可能还没把新消息入队。
                    # 立即释放锁会导致 Listener 切换到其他用户，新回复被发送到错误会话。
                    # 因此先短暂等待，给 Listener 机会把新消息入队。
                    if self.message_listener and queue.empty():
                        # 关键修复：完成一批后不要立即释放锁。Listener 轮询周期为 0.5s，
                        # 如果用户紧接着发送新消息，Listener 可能还没把新消息入队。
                        # 立即释放锁会导致 Listener 切换到其他用户，新回复被发送到错误会话。
                        # 因此先短暂等待，给 Listener 机会把新消息入队。
                        grace_period = 0.6
                        check_interval = 0.1
                        elapsed = 0.0
                        has_new = False
                        while elapsed < grace_period:
                            if not queue.empty():
                                has_new = True
                                break
                            await asyncio.sleep(check_interval)
                            elapsed += check_interval
                        
                        if has_new:
                            logger.info(f"[继续处理] 店铺 {store_id}, 用户: {user.nickname} 检测到新消息，保持锁定")
                            continue
                        
                        self.message_listener.set_processing_user(store_id, None)
                        logger.info(f"[完成处理] 店铺 {store_id}, 用户: {user.nickname}")
                    elif self.message_listener:
                        # 队列还有消息，继续处理，保持锁
                        pass
                    
            except asyncio.CancelledError:
                logger.debug(f"[工作者取消] {user_key}")
                break
            except Exception as e:
                logger.exception(f"[用户工作者] 异常: {user_key}: {e}")
                await asyncio.sleep(1)
        
        # ===== 关键修复: 工作线程退出时清理 processing_user =====
        # 确保即使工作线程异常退出，也不会一直锁定
        if self.message_listener:
            current_processing = self.message_listener.get_processing_user(store_id)
            if current_processing == user.nickname:
                self.message_listener.set_processing_user(store_id, None)
                logger.debug(f"[工作者退出] 店铺 {store_id}, 用户: {user.nickname}")
        
        logger.debug(f"[工作者停止] {user_key}")

    async def _process_message_complete(self, message: Message) -> None:
        """完整处理单条消息：API调用 + 发送回复.
        
        这是原子操作，要么全部完成，要么标记为失败。
        """
        start_time = time.time()
        
        # Update status
        message.status = MessageStatus.PROCESSING
        await self.db.save_message(message)
        
        # Get or create conversation
        conversation = await self._get_conversation(message)
        conversation.add_message(message)
        
        # Get store configuration
        store = self.store_manager.get_store(message.store_id)
        if not store:
            logger.error(f"[处理错误] 店铺 {message.store_id} 不存在")
            await self._mark_failed(message, "Store not found")
            return
        
        if not store.is_online:
            logger.warning(f"[处理警告] 店铺 {message.store_id} 页面不可用")
            await self._mark_failed(message, "Store offline")
            return
        
        try:
            # Call API for reply
            async with ApiClient(store.config) as api_client:
                reply_response = await api_client.get_reply(message, conversation)
            
            api_time_ms = int((time.time() - start_time) * 1000)
            
            reply_text = reply_response.reply[:30] if reply_response.reply else 'None'
            logger.info(f"[API响应] {message.message_id[:20]}... 状态: {reply_response.code}, 回复: {reply_text}...")
            
            if not reply_response.should_reply():
                logger.debug(f"[无需回复] {message.message_id[:20]}...")
                await self._mark_skipped(message, reply_response.message)
                return
            
            # Apply delay if specified
            if reply_response.delay > 0:
                await asyncio.sleep(reply_response.delay)
            
            # Send reply - 使用用户锁确保串行
            success = await self._send_reply(store, message, reply_response.reply)
            
            if success:
                message.reply_content = reply_response.reply
                message.reply_timestamp = datetime.now()
                message.api_response_time_ms = api_time_ms
                await self._mark_replied(message)
                
                # Update conversation
                conversation.add_message(message)
                
                # Update store stats
                store.total_replies += 1
            else:
                await self._mark_failed(message, "Failed to send reply")
            
            # Update stats
            self._update_avg_response_time(api_time_ms)
            
        except Exception as e:
            logger.exception(f"[处理异常] 消息 {message.message_id}: {e}")
            await self._mark_failed(message, str(e))

    async def _get_conversation(self, message: Message) -> Conversation:
        """Get or create conversation for message."""
        conv_id = message.conversation_id
        
        if conv_id not in self._conversations:
            # Try to load from database
            db_conv = await self.db.get_conversation(conv_id)
            if db_conv:
                self._conversations[conv_id] = db_conv
            else:
                # Create new conversation
                self._conversations[conv_id] = Conversation(
                    conversation_id=conv_id,
                    store_id=message.store_id,
                    user=message.user,
                )
                await self.db.save_conversation(self._conversations[conv_id])
        
        return self._conversations[conv_id]

    async def _send_reply(self, store: Store, message: Message, reply_text: str) -> bool:
        """Send a reply through the browser.
        
        使用用户级别的细粒度锁：
        1. 不同店铺的用户：完全并行
        2. 同店铺的不同用户：并行处理（各自独立锁）
        3. 同店铺的同一用户：串行处理（避免消息顺序混乱）
        
        注意：调用此方法前，监听器已经知道正在处理该用户，不会切换页面。

        Args:
            store: Store instance
            message: Original message
            reply_text: Text to reply with

        Returns:
            True if sent successfully
        """
        user_id = message.user.user_id
        user_name = message.user.nickname
        
        # 获取该用户的独立锁
        user_lock = await self._get_user_lock(store.store_id, user_id)
        
        logger.debug(f"[发送准备] 店铺: {store.store_id}, 用户: {user_name}")
        
        async with user_lock:
            # ===== 关键修复: 获取页面级锁 =====
            # 确保同一时刻只有一个协程可以操作页面
            async with store._page_lock:
                logger.debug(f"[发送开始] 店铺: {store.store_id}, 用户: {user_name}")
                try:
                    page = store.page
                    if not page:
                        logger.error(f"[发送失败] 店铺 {store.store_id}: 页面不可用")
                        return False
                    
                    # Check if we need to navigate to IM page
                    current_url = page.url
                    if "cs/web" not in current_url and "/im" not in current_url.lower():
                        logger.debug(f"[页面导航] 店铺 {store.store_id}")
                        
                        # Try to find and click 在线咨询 button
                        consultation_selectors = [
                            'text=在线咨询',
                            'button:has-text("咨询")',
                            'a:has-text("咨询")',
                            '[class*="consult"]',
                            '[class*="im-entry"]',
                            '[class*="online"]',
                            'div[role="button"]:has-text("咨询")',
                        ]
                        
                        button_clicked = False
                        for selector in consultation_selectors:
                            try:
                                elements = await page.query_selector_all(selector)
                                for elem in elements:
                                    is_visible = await elem.is_visible()
                                    if is_visible:
                                        await elem.click()
                                        button_clicked = True
                                        await asyncio.sleep(2)
                                        break
                                if button_clicked:
                                    break
                            except:
                                continue
                        
                        if not button_clicked:
                            # Fallback: direct navigation
                            await page.goto("https://life.douyin.com/pc/im", wait_until="load")
                        
                        # Wait for IM page to load
                        await asyncio.sleep(5)
                        
                        # Check if a new page/tab opened
                        context = page.context
                        pages = context.pages
                        if len(pages) > 1:
                            for p in pages:
                                url = p.url
                                if "cs/web" in url or "/im" in url.lower():
                                    store.page = p
                                    page = p
                                    break
                    
                    # Navigate to conversation by clicking on user name in left sidebar
                    logger.debug(f"[会话切换] 店铺: {store.store_id}, 用户: {user_name}")
                    
                    # First check if already in the correct conversation
                    already_in_conversation = False
                    try:
                        header_selectors = [
                            '[class*="chat-header"]',
                            '[class*="conversation-header"]',
                            '[class*="title"]',
                        ]
                        for header_sel in header_selectors:
                            try:
                                header_elem = await page.query_selector(header_sel)
                                if header_elem:
                                    header_text = await header_elem.inner_text()
                                    if user_name in header_text:
                                        already_in_conversation = True
                                        # 已在会话中
                                        break
                            except:
                                continue
                    except Exception as e:
                        pass
                    
                    conversation_clicked = already_in_conversation
                    
                    if not conversation_clicked:
                        # Strategy 1: Use JavaScript to precisely find and click conversation
                        try:
                            click_result = await page.evaluate(f"""
                                (userName) => {{
                                    const sidebar = document.querySelector('.sidebar, [class*="sidebar"], [class*="conversation-list"], [class*="session-list"]');
                                    if (!sidebar) return 'sidebar not found';
                                    
                                    const items = sidebar.querySelectorAll('div[role="button"], div[class*="item"], div[class*="session"], div[class*="conversation"]');
                                    
                                    for (const item of items) {{
                                        const text = item.innerText || item.textContent || '';
                                        const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
                                        if (lines.length === 0) continue;

                                        // 遍历所有行，不只看第一行（用户名和时间可能在同行或不同行）
                                        let matched = false;
                                        for (const line of lines) {{
                                            if (line === userName || line.startsWith(userName + ' ') || line.startsWith(userName + '\\n')) {{
                                                matched = true;
                                                break;
                                            }}
                                        }}
                                        // 宽松匹配：任意行以用户名开头
                                        if (!matched) {{
                                            for (const line of lines) {{
                                                if (line.startsWith(userName)) {{
                                                    matched = true;
                                                    break;
                                                }}
                                            }}
                                        }}
                                        if (matched) {{
                                            const rect = item.getBoundingClientRect();
                                            if (rect.left < 300 && rect.width > 50) {{
                                                item.click();
                                                return 'clicked: ' + lines[0];
                                            }}
                                        }}
                                    }}
                                    
                                    const allElements = sidebar.querySelectorAll('*');
                                    for (const el of allElements) {{
                                        if (el.children.length === 0) {{
                                            const text = el.textContent || '';
                                            const trimmed = text.trim();
                                            if (trimmed === userName) {{
                                                let clickable = el.parentElement;
                                                while (clickable && clickable !== sidebar) {{
                                                    const rect = clickable.getBoundingClientRect();
                                                    if (rect.height > 40 && rect.width > 200) {{
                                                        clickable.click();
                                                        return 'clicked parent of: ' + trimmed;
                                                    }}
                                                    clickable = clickable.parentElement;
                                                }}
                                            }}
                                        }}
                                    }}
                                    
                                    return 'not found in sidebar';
                                }}
                            """, user_name)
                            
                            if 'clicked' in str(click_result):
                                conversation_clicked = True
                                logger.debug(f"[会话切换] JS点击成功")
                        except Exception as e:
                            # JS策略失败
                            pass
                    
                    # Strategy 2: Find conversation by querying elements
                    if not conversation_clicked:
                        try:
                            conv_elements = await page.query_selector_all('[class*="conversation"], [class*="session"], [class*="chat-item"]')
                            for conv in conv_elements:
                                try:
                                    box = await conv.bounding_box()
                                    if box and box['x'] < 300:
                                        conv_text = await conv.inner_text()
                                        lines = [l.strip() for l in conv_text.split('\\n') if l.strip()]
                                        for line in lines:
                                            # 不跳过短数字（<=3位）— 单/双/三位数字也是合法用户名
                                            if len(line) > 3 and line.isdigit():
                                                continue
                                            if line == user_name or line.startswith(user_name):
                                                await conv.click()
                                                conversation_clicked = True
                                                logger.debug(f"[会话切换] 元素点击成功")
                                                break
                                        if conversation_clicked:
                                            break
                                except Exception as e:
                                    continue
                        except Exception as e:
                            # 元素查询失败
                            pass
                    
                    if not conversation_clicked:
                        logger.error(f"[发送失败] 店铺 {store.store_id}: 无法找到并点击 {user_name} 的会话")
                        return False
                    
                    # Wait for conversation to load
                    await asyncio.sleep(2.0)
                    
                    # Verify we are in the correct conversation
                    in_correct_conversation = False
                    verify_attempts = 0
                    max_attempts = 3
                    
                    while verify_attempts < max_attempts and not in_correct_conversation:
                        verify_attempts += 1
                        try:
                            header_selectors = [
                                '[class*="user-info"] [class*="name"]',
                                '[class*="user-info"] h1',
                                '[class*="user-info"]',
                                '[class*="chat-header"]',
                                '[class*="conversation-header"]',
                                '[class*="header"] [class*="title"]',
                                '[class*="title"]',
                                '[class*="header-title"]',
                                '[class*="chat-title"]',
                                '[class*="right"] [class*="name"]',
                                '[class*="right-panel"] [class*="name"]',
                                '[class*="user-detail"] [class*="name"]',
                            ]
                            for header_sel in header_selectors:
                                try:
                                    header_elems = await page.query_selector_all(header_sel)
                                    for header_elem in header_elems:
                                        if header_elem:
                                            header_text = await header_elem.inner_text()
                                            if user_name in header_text:
                                                in_correct_conversation = True
                                                logger.debug(f"[会话验证] 成功")
                                                break
                                    if in_correct_conversation:
                                        break
                                except Exception as e:
                                    continue
                        except Exception as e:
                            pass
                        
                        # Fallback: JavaScript verification
                        if not in_correct_conversation:
                            try:
                                js_result = await page.evaluate(f"""
                                    (userName) => {{
                                        const selectors = [
                                            '[class*="user-info"] [class*="name"]',
                                            '[class*="user-info"] h1',
                                            '[class*="user-detail"]',
                                            '[class*="chat-header"]',
                                            '[class*="conversation-header"]',
                                            '[class*="header"] [class*="title"]',
                                            '[class*="title"]',
                                            '[class*="nickname"]',
                                            '[class*="user-name"]',
                                            '[class*="username"]',
                                        ];
                                        
                                        for (const sel of selectors) {{
                                            const elems = document.querySelectorAll(sel);
                                            for (const elem of elems) {{
                                                const text = elem.innerText || elem.textContent || '';
                                                if (text.trim().includes(userName)) {{
                                                    return {{ found: true }};
                                                }}
                                            }}
                                        }}
                                        
                                        const rightPanel = document.querySelector('[class*="right"], [class*="right-panel"], [class*="user-panel"]');
                                        if (rightPanel) {{
                                            const text = rightPanel.innerText || '';
                                            if (text.includes(userName)) {{
                                                return {{ found: true }};
                                            }}
                                        }}
                                        
                                        return {{ found: false }};
                                    }}
                                """, user_name)
                                
                                if js_result and js_result.get('found'):
                                    in_correct_conversation = True
                                    logger.debug(f"[会话验证] JS成功")
                            except Exception as e:
                                logger.warning(f"[会话验证] JS验证失败: {e}")
                        
                        if not in_correct_conversation and verify_attempts < max_attempts:
                            await asyncio.sleep(2.0)
                    
                    if not in_correct_conversation:
                        # Final fallback: check if user name appears anywhere on page
                        try:
                            has_user_messages = await page.evaluate(f"""
                                (userName) => {{
                                    const allText = document.body.innerText || '';
                                    return allText.includes(userName);
                                }}
                            """, user_name)
                            if has_user_messages:
                                in_correct_conversation = True
                                logger.debug(f"[会话验证] 页面验证成功")
                        except Exception as e:
                            logger.warning(f"[会话验证] 页面内容验证失败: {e}")
                    
                    if not in_correct_conversation:
                        logger.error(f"[发送失败] 店铺 {store.store_id}: 不在正确的会话中 ({user_name})")
                        try:
                            screenshot_path = f"storage/debug_not_in_conv_{store.store_id}_{int(time.time())}.png"
                            await page.screenshot(path=screenshot_path)
                        except:
                            pass
                        return False
                    
                    # Find input box
                    input_selectors = [
                        '[class*="input"] textarea',
                        '[class*="message-input"]',
                        '[data-e2e="message-input"]',
                        'textarea[placeholder*="回复"]',
                        'textarea[placeholder*="消息"]',
                    ]
                    
                    input_box = None
                    for selector in input_selectors:
                        try:
                            input_box = await page.wait_for_selector(selector, timeout=2000)
                            if input_box:
                                break
                        except:
                            continue
                    
                    if not input_box:
                        logger.error(f"[发送失败] 店铺 {store.store_id}: 找不到输入框")
                        return False
                    
                    # Type and send message
                    await input_box.fill("")
                    await input_box.type(reply_text, delay=10)
                    await input_box.press("Enter")
                    
                    # Record the sent message
                    if self._on_reply_sent:
                        self._on_reply_sent(store.store_id, reply_text)
                    
                    logger.info(f"[回复成功] 店铺: {store.store_id}, 用户: {user_name}")  # 保留：重要事件
                    return True
                    
                except Exception as e:
                    logger.exception(f"[发送异常] 店铺 {store.store_id}: {e}")
                    return False
                finally:
                    logger.debug(f"[发送结束] 店铺: {store.store_id}, 用户: {user_name}")

    async def _mark_replied(self, message: Message) -> None:
        """Mark message as replied."""
        message.status = MessageStatus.REPLIED
        await self.db.update_message_status(
            message.message_id,
            MessageStatus.REPLIED,
            reply_content=message.reply_content,
            api_response_time_ms=message.api_response_time_ms
        )
        self.stats.total_replied += 1
        self.stats.total_processed += 1

    async def _mark_failed(self, message: Message, error: str) -> None:
        """Mark message as failed."""
        message.status = MessageStatus.FAILED
        message.error_message = error
        await self.db.update_message_status(
            message.message_id,
            MessageStatus.FAILED,
            error_message=error
        )
        self.stats.total_failed += 1
        self.stats.total_processed += 1

    async def _mark_skipped(self, message: Message, reason: str) -> None:
        """Mark message as skipped."""
        message.status = MessageStatus.SKIPPED
        await self.db.update_message_status(
            message.message_id,
            MessageStatus.SKIPPED,
            error_message=reason
        )
        self.stats.total_skipped += 1
        self.stats.total_processed += 1

    def _update_avg_response_time(self, response_time_ms: int) -> None:
        """Update average response time."""
        n = self.stats.total_processed
        self.stats.avg_response_time_ms = (
            (self.stats.avg_response_time_ms * (n - 1) + response_time_ms) / n
            if n > 0 else response_time_ms
        )

    def get_stats(self) -> Dict:
        """Get handler statistics."""
        active_workers = len([w for w in self._user_workers.values() if not w.done()])
        return {
            "total_received": self.stats.total_received,
            "total_processed": self.stats.total_processed,
            "total_replied": self.stats.total_replied,
            "total_failed": self.stats.total_failed,
            "total_skipped": self.stats.total_skipped,
            "avg_response_time_ms": round(self.stats.avg_response_time_ms, 2),
            "active_user_workers": active_workers,
            "active_user_queues": len(self._user_queues),
        }
