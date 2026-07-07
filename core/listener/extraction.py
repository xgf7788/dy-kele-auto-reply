"""Message extraction from chat history — parsing, processing, dispatching."""

import asyncio
import hashlib
import json
import re
import time
from typing import Dict, List, Optional

from playwright.async_api import Page

from config import settings
from core.store_manager import Store, StoreStatus
from models.message import Message, User, MessageStatus
from storage import get_db, MessageDatabase
from utils.logger import get_logger
from utils.constants import (
    EMOJI_PATTERN,
    EMOJI_PATTERN_JS,
    SYSTEM_STATUS_TEXTS,
    BOT_MESSAGE_PATTERNS,
    STATS_MESSAGE_PATTERNS,
    EVALUATION_PATTERNS,
    SELECTORS_MESSAGE,
    SELECTORS_HEADER,
)


logger = get_logger(__name__)


class ExtractionMixin:
    """Mixin providing extraction methods for MessageListener."""

    async def _extract_chat_history(self, page: Page, store_id: str, user_name: str) -> List[Dict]:
        """Extract all user messages from the chat history."""
        messages = []
        user_id = f"user_{user_name}"  # Generate a user_id from user_name

        try:
            # Extracting chat history
            
            # CRITICAL: Verify we're in the correct conversation before extracting
            try:
                is_correct_user = await asyncio.wait_for(
                    self._verify_current_conversation_user(page, user_name, store_id),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.warning(f"Store {store_id}: _verify_current_conversation_user 超时 (10s)，跳过提取")
                return messages
            if not is_correct_user:
                # 验证失败直接返回空，不再浪费时间尝试提取
                logger.warning(f"Store {store_id}: 不在 {user_name} 的会话中，跳过提取")
                return messages

            # Wait for chat messages to load
            # First, let's check the page structure by getting some HTML
            try:
                page_html = await page.evaluate("() => document.body.innerHTML.substring(0, 2000)")
                # Page HTML preview
            except Exception as e:
                pass  # Auto-added to fix empty block

            message_selectors = SELECTORS_MESSAGE

            # Retry logic: page may still be loading messages after switch
            message_elements = []
            for attempt in range(3):
                for selector in message_selectors:
                    try:
                        elements = await page.query_selector_all(selector)
                        if elements and len(elements) > 0:
                            message_elements = elements
                            break
                    except Exception:
                        continue
                if message_elements:
                    break
                if attempt < 2:
                    logger.debug(f"Store {store_id}: No messages found on attempt {attempt + 1}, waiting 1s...")
                    await asyncio.sleep(1.0)

            if not message_elements:
                logger.warning(f"Store {store_id}: No message elements found in chat for {user_name} after 3 attempts")
                # Try to find any text elements in the chat area
                try:
                    all_text = await page.evaluate("""
                        () => {
                            const chatArea = document.querySelector('[class*="chat"], [class*="conversation"], [class*="im"]');
                            if (chatArea) {
                                return chatArea.innerText.substring(0, 500);
                            }
                            return document.body.innerText.substring(0, 500);
                        }
                    """)
                    # Chat area text preview
                except Exception:
                    pass

                # Take screenshot for debugging
                try:
                    await page.screenshot(path=f"storage/debug_no_messages_{store_id}_{user_name[:20]}.png")
                    # Screenshot saved
                except:
                    pass
                return []

            # Found message elements in chat

            # NEW: Use JavaScript to extract messages with updated selectors
            # Based on actual page structure: messages in .my-4 containers
            chat_data = await asyncio.wait_for(page.evaluate("""
                () => {
                    const messages = [];
                    
                    // Find message containers by class structure
                    // Based on actual page: messages are in .my-4 containers (style1) or .csUI-NormalMessage (style2)
                    const msgContainers = document.querySelectorAll('.my-4, .csUI-NormalMessage');
                    
                    // Collect debug info
                    const debugInfo = {
                        containerCount: msgContainers.length,
                        windowWidth: window.innerWidth,
                        windowHeight: window.innerHeight,
                        skipped: [],
                        potential: [],
                        found: []
                    };
                    
                    msgContainers.forEach((container, idx) => {
                        const html = container.outerHTML || '';
                        const rect = container.getBoundingClientRect();
                        
                        // Skip if not visible
                        if (rect.width === 0 || rect.height === 0) return;
                        
                        // Detect which style is being used
                        const isStyle1 = html.includes('leftMsg-ewM7qC') || html.includes('rightMsg') || 
                                         container.classList.contains('my-4');
                        const isStyle2 = html.includes('csUI-NormalMessage') || html.includes('csUI-Text');
                        
                        // Get text content - look for the actual message text
                        // Try style2 first, then style1
                        let text = '';
                        let textEl = null;
                        
                        if (isStyle2) {
                            textEl = container.querySelector('.csUI-Text');
                            if (textEl) {
                                text = textEl.innerText.trim();
                            }
                        }
                        
                        // If style2 didn't extract text, try style1
                        if (!text) {
                            textEl = container.querySelector('.whitespace-pre-wrap, .leftMsg-ewM7qC, [class*="msg"]:not([class*="csContext"])');
                            if (textEl) {
                                text = textEl.innerText.trim();
                            }
                        }
                        
                        // Fallback: get text from container itself if no text element found
                        if (!text) {
                            const allText = container.querySelectorAll('div, span');
                            for (const el of allText) {
                                const t = el.innerText.trim();
                                if (t && t.length > 1 && t.length < 500 && 
                                    !t.includes('发送') && !t.includes('Enter')) {
                                    text = t;
                                    break;
                                }
                            }
                        }
                        
                        // Check for image messages (no text, just <img> tag)
                        let hasImage = false;
                        let imageSrc = '';
                        if (!text || text.length < 1) {
                            const imgEl = container.querySelector('img');
                            if (imgEl) {
                                imageSrc = imgEl.getAttribute('src') || '';
                                text = '【图片】';
                                hasImage = true;
                            }
                        }

                        // Skip if no text or too short/long (image messages are kept)
                        if (!text || text.length < 1 || text.length > 500) return;
                        
                        // Skip UI elements
                        const normalizedText = text.replace(/\\s+/g, ' ').trim();
                        if (normalizedText.includes('发送') || normalizedText.includes('Enter') ||
                            normalizedText.includes('抖音私信') || normalizedText.includes('当前咨询') ||
                            normalizedText.includes('历史咨询') || normalizedText.includes('系统') ||
                            // NOTE: Removed pure number filter to allow phone numbers
                            // Previous: /^\\d+$/.test(normalizedText) || 
                            /^\\d+:\\d+$/.test(normalizedText)) {
                            return;
                        }
                        
                        // Skip shop/business info messages
                        if (/^.+?\\s+经营源$/.test(normalizedText) || normalizedText === '经营源') {
                            return;
                        }
                        
                        // Determine if user message based on layout classes
                        // User messages: flex-row (left side) - style1
                        // Bot messages: flex-row-reverse (right side) - style1
                        // Style2: csUI-NormalMessage_left for user, csUI-NormalMessage_right for bot
                        
                        // Get className for container class checks
                        const className = container.className || '';
                        
                        // Style1 checks - check both container class and child elements (html)
                        // For style1: .my-4 container has children with .leftMsg-ewM7qC or .rightMsg classes
                        const hasLeftMsgClass = html.includes('leftMsg-ewM7qC') || html.includes('leftMsg');
                        const hasRightMsgClass = html.includes('rightMsg');
                        const hasFlexRow = className.includes('flex-row') && !className.includes('flex-row-reverse');
                        const hasFlexRowReverse = className.includes('flex-row-reverse');
                        const hasSelfEnd = className.includes('self-end');
                        
                        // Style2 checks - using className for container and html for nested card classes
                        const hasCsUILeft = className.includes('csUI-NormalMessage_left') || 
                                           html.includes('csUI-NormalMessage_wrapper_content_card_normal');
                        const hasCsUIRight = className.includes('csUI-NormalMessage_right') || 
                                            html.includes('csUI-NormalMessage_wrapper_content_card_right');
                        
                        // Determine message type based on classes
                        // Priority: explicit left/right classes > flex classes
                        let isUser = hasLeftMsgClass || hasCsUILeft;
                        let isBot = hasRightMsgClass || hasCsUIRight;
                        
                        // Fallback to flex classes only if no explicit class found
                        if (!isUser && !isBot) {
                            isUser = hasFlexRow && !hasFlexRowReverse;
                            isBot = hasFlexRowReverse || hasSelfEnd;
                        }
                        
                        // Correct logic for style2
                        if (hasCsUILeft && !hasCsUIRight) {
                            isUser = true;
                            isBot = false;
                        }
                        if (hasCsUIRight && !hasCsUILeft) {
                            isUser = false;
                            isBot = true;
                        }
                        
                        // Get avatar info
                        const avatarEl = container.querySelector('img');
                        const hasAvatar = !!avatarEl;
                        
                        // Get timestamp
                        const timeEl = container.querySelector('.text-gray-3, [class*="time"]');
                        const timestamp = timeEl ? timeEl.innerText : '';
                        
                        // Bot pattern check
                        const botPatterns = ['您好亲', '小主，', '温馨提示', '服务后可以抵扣', '团购券不是全部', '即修到家', '该消息由智能回复'];
                        const isBotPattern = botPatterns.some(p => text.includes(p));
                        
                        // Stats/system patterns - skip
                        const statsPatterns = ['今日接待数', '留资数', '首响率', '未达标', '用户评分', '商家还在等待你的回复'];
                        if (statsPatterns.some(p => text.includes(p))) return;
                        
                        // Skip evaluation cards
                        const evalPatterns = ['评价卡片', '服务评价', '满意度评价', '请对本次服务进行评价'];
                        if (evalPatterns.some(p => text.includes(p))) return;
                        
                        // NOTE: Phone number filtering removed - we need to capture phone numbers from user messages
                        // Previous: if (/^1\\d{2}[\\s\\-]?\\d{4}[\\s\\-]?\\d{4}$/.test(text.trim())) return;
                        
                        // Determine final isUser flag
                        const finalIsUser = (isUser || hasLeftMsgClass || hasCsUILeft) && !isBot && !isBotPattern;
                        
                        // Extract message timestamp from the time element above the message
                        // HTML format: <p class="text-xs text-gray-3 absolute whitespace-nowrap invisible">2026-04-07 13:28:32</p>
                        let msgTime = '';
                        try {
                            // Look for time element with specific class pattern
                            const timeEl = container.querySelector('p.text-xs.text-gray-3, [class*="text-gray-3"][class*="text-xs"], .message-time, [class*="time"]');
                            if (timeEl) {
                                msgTime = timeEl.innerText.trim();
                            }
                            // Alternative: look for any element with date pattern in previous siblings
                            if (!msgTime || !msgTime.match(/\\d{4}-\\d{2}-\\d{2}/)) {
                                // Check previous siblings for time
                                let prev = container.previousElementSibling;
                                for (let k = 0; k < 3 && prev; k++) {
                                    const prevText = prev.innerText || '';
                                    if (prevText.match(/\\d{4}-\\d{2}-\\d{2}\\s+\\d{2}:\\d{2}:\\d{2}/)) {
                                        msgTime = prevText.trim();
                                        break;
                                    }
                                    prev = prev.previousElementSibling;
                                }
                            }
                        } catch (e) {
                            msgTime = '';
                        }
                        
                        messages.push({
                            content: text,
                            isUser: finalIsUser,
                            isBot: isBot || isBotPattern,
                            hasAvatar: hasAvatar,
                            hasImage: hasImage,
                            imageSrc: imageSrc,
                            timestamp: timestamp,
                            msgTime: msgTime,  // Message send time from the page
                            left: rect.left,
                            top: rect.top,
                            classHint: hasLeftMsgClass ? 'leftMsg' : (isBot ? 'right' : 'unknown')
                        });
                        
                        debugInfo.found.push({
                            idx: idx,
                            text: text.substring(0, 40),
                            isUser: finalIsUser,
                            isBot: isBot || isBotPattern,
                            left: rect.left,
                            hasLeftMsg: hasLeftMsgClass,
                            hasRightMsg: hasRightMsgClass,
                            hasFlexRow: hasFlexRow,
                            hasFlexRowReverse: hasFlexRowReverse
                        });
                    });
                    
                    console.log('Found', messages.length, 'messages from', msgContainers.length, 'containers');
                    
                    return {messages: messages, debug: debugInfo};
                }
            """), timeout=15.0)

            # Extract messages and debug info
            messages = chat_data.get('messages', [])
            chat_data = messages  # Use messages for backward compatibility

            # CRITICAL: Filter and process user messages
            # Only return messages that haven't been replied to yet
            bot_patterns = [
                '您好！欢迎咨询',
                '您好亲，请问',
                '您好 亲，请问',
                '您好，请问需要什么服务',
                '小主，请问您',
                '小主，收到您的联系方式',
                '小主，空调维修清洗我们可以处理',
                '小主，您说的服务我们暂时无法提供',
                '该消息由智能回复',
                '商家正在快马加鞭',
                '商家还在等待你的回复',
                '温馨提示：',
                '团购券不是全部维修费用',
                '具体费用由师傅根据情况',
                '服务后可以抵扣',
                '预约金可抵扣',
                '尾款请在平台支付',
                # Shop/store info messages
                '经营源',  # Shop business source info
                '即修到家',  # Shop name/brand
            ]

            skipped_count = {'non_user': 0, 'ui': 0, 'stats': 0, 'eval': 0, 'bot': 0, 'shop': 0, 'length': 0}
            
            for idx, msg_data in enumerate(chat_data):
                content = msg_data.get('content', '')
                is_user = msg_data.get('isUser', False)

                # Clean up content - remove newlines and extra spaces
                content = ' '.join(content.split())

                # Processing message

                # Skip non-user messages
                if not is_user:
                    skipped_count['non_user'] += 1
                    continue

                # Skip UI elements and buttons
                ui_patterns = ['发送', 'Enter', '按Enter键发送', '发送消息']
                if any(pattern in content for pattern in ui_patterns):
                    skipped_count['ui'] += 1
                    continue

                # Skip system/stats messages (stats cards)
                stats_patterns = ['今日接待数', '留资数', '首响率', '未达标', '用户评分', '商家还在等待你的回复']
                if any(pattern in content for pattern in stats_patterns):
                    skipped_count['stats'] += 1
                    continue

                # Skip evaluation/rating cards
                eval_patterns = ['评价卡片', '服务评价', '满意度评价', '请对本次服务进行评价']
                if any(pattern in content for pattern in eval_patterns):
                    skipped_count['eval'] += 1
                    continue

                # Skip bot messages from cache
                if self.is_bot_message(store_id, content):
                    skipped_count['bot'] += 1
                    continue

                # Skip bot pattern messages
                if any(pattern in content for pattern in bot_patterns):
                    skipped_count['bot'] += 1
                    continue

                # Skip shop/business info messages (format: "username 经营源" or just "经营源")
                if '经营源' in content:
                    skipped_count['shop'] += 1
                    continue

                # Skip empty or too short/long messages
                if len(content) < 2 or len(content) > 500:
                    skipped_count['length'] += 1
                    continue

                # Get timestamp from message (user-sent time)
                msg_time_str = msg_data.get('timestamp', '')
                msg_timestamp = self._parse_message_time(msg_time_str)

                # 图片消息：content 保持 [图片]，用 imageSrc 保证 message_id 唯一
                _dedup_suffix = ''
                if msg_data.get('hasImage') and msg_data.get('imageSrc'):
                    _dedup_suffix = ':' + hashlib.md5(msg_data['imageSrc'].encode()).hexdigest()[:8]

                # Generate message ID based on content hash (STABLE - no timestamp)
                content_hash = hashlib.md5(f"{user_name}:{content}{_dedup_suffix}".encode()).hexdigest()[:16]
                message_id = f"{store_id}_{user_name}_{content_hash}"

                # CRITICAL: Check if message already exists by content + timestamp (more accurate)
                try:
                    exists = await self.db.message_exists_by_content_and_time(
                        store_id=store_id,
                        user_id=user_id,
                        content=content,
                        msg_timestamp=msg_timestamp
                    )
                    if exists:
                        # Message already exists
                        continue
                except Exception as e:
                    logger.warning(f"Store {store_id}: Could not check message by content+time: {e}")
                    # Fallback to old method
                    try:
                        is_replied = await self.db.is_message_replied(message_id)
                        if is_replied:
                            # Message already replied
                            continue
                    except Exception as e2:
                        logger.warning(f"Store {store_id}: Could not check message status: {e2}")

                # Also check if we've seen this content recently (regardless of ID)
                # IMPORTANT: Use user_name in signature to prevent cross-user confusion
                content_signature = f"{user_name}:{content[:50]}"
                if content_signature in self._processed_messages:
                    # Message content already processed
                    self._processed_messages[content_signature] = (time.time(), store_id)
                    continue
                self._processed_messages[content_signature] = (time.time(), store_id)

                # CRITICAL: Include conversation_id to ensure each user has separate conversation history
                conversation_id = f"{store_id}_{user_name}"
                messages.append({
                    'message_id': message_id,
                    'conversation_id': conversation_id,
                    'content': content,
                    'isUser': True,  # This is a user message
                    'user': {
                        'user_id': user_id,
                        'nickname': user_name,
                        'user_unique_id': user_id
                    },
                    'timestamp': int(msg_timestamp.timestamp() * 1000),
                    'type': 'text',
                    'source': 'chat_history',
                    'is_customer': True
                })

            # Returning user messages
            return messages

        except asyncio.TimeoutError:
            logger.warning(f"Store {store_id}: _extract_chat_history 操作超时，返回空列表")
            return []
        except Exception as e:
            # Error extracting chat history
            return []


    async def _check_current_conversation_for_new_messages(self, page: Page, store: Store, user_name: str) -> List[Dict]:
        """Check the current conversation for new messages.

        This handles the case where user is already in the conversation
        and new messages arrive without showing unread badge in sidebar.

        Returns list of new messages that haven't been processed yet.
        
        注意：如果正在处理其他用户，此方法会返回空列表，避免干扰处理器。
        """
        # ===== 关键修复: 检查 processing_user =====
        processing_user = self.get_processing_user(store.store_id)
        if processing_user and user_name != processing_user:
            logger.warning(f"[关键错误] _check_current_conversation_for_new_messages 被错误调用! "
                          f"尝试检查 {user_name}，但正在处理 {processing_user}。返回空列表！")
            return []
        
        # ===== 防御修复: 如果 user_name 是时间格式，重置并返回空列表 =====
        if self._is_time_format(user_name):
            logger.warning(f"[时间格式用户] 店铺 {store.store_id} 检测到时间格式用户名 '{user_name}'，重置会话跟踪")
            self._current_conversation[store.store_id] = None
            self._conversation_message_counts[store.store_id] = 0
            return []
        
        new_messages = []

        try:
            # Checking for new messages in conversation

            # Force scroll to bottom to trigger any lazy-loaded messages
            try:
                await page.evaluate("""
                    () => {
                        // Scroll chat container
                        const containers = document.querySelectorAll('.chatRoom-SbANJ5, [class*="chatRoom"], [class*="chat-room"], [class*="message-list"]');
                        for (const container of containers) {
                            container.scrollTop = container.scrollHeight;
                        }
                        // Scroll window
                        window.scrollTo(0, document.body.scrollHeight);
                    }
                """)
                await asyncio.sleep(0.5)  # Wait for messages to render
            except Exception as e:
                # Scroll failed
                pass

            # CRITICAL: Verify we're in the correct conversation by checking the page
            try:
                is_correct_user = await asyncio.wait_for(
                    self._verify_current_conversation_user(page, user_name, store.store_id),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.warning(f"Store {store.store_id}: _verify_current_conversation_user 超时 (10s)，假设不在正确会话")
                is_correct_user = False
            if not is_correct_user:
                logger.warning(f"Store {store.store_id}: Page verification failed! Not in conversation with '{user_name}'.")
                # ===== 关键修复: 验证失败直接清除会话跟踪并释放锁 =====
                # 不再调用 _click_conversation_by_name 尝试切换，因为：
                # 1. page.evaluate 遍历全页面 DOM 可能在浏览器端卡死
                # 2. asyncio.wait_for 取消不了已发送到浏览器的 CDP 命令
                # 3. 主轮询循环会在下一轮通过 STEP 1（侧边栏未读）自然恢复
                logger.warning(f"Store {store.store_id}: 验证会话 {user_name} 失败，清除会话跟踪并跳过（下轮自动恢复）")
                self._current_conversation[store.store_id] = None
                self._conversation_message_counts[store.store_id] = 0
                # 如果 processing_user 是当前用户（Listener 自己上的锁），释放它
                current_processing = self.get_processing_user(store.store_id)
                if current_processing == user_name:
                    self.set_processing_user(store.store_id, None)
                return new_messages
            
            # Also check tracking state
            current_tracked_user = self._current_conversation.get(store.store_id)
            if current_tracked_user != user_name:
                logger.warning(f"Store {store.store_id}: 状态切换 从 '{current_tracked_user}' 到 '{user_name}'")
                self._current_conversation[store.store_id] = user_name
                self._conversation_message_counts[store.store_id] = 0
                # Clear last message info for new user
                pending_key = f"{store.store_id}_{user_name}"
                if pending_key in self._last_message_info:
                    del self._last_message_info[pending_key]
                # NOTE: We NO LONGER clear _processed_messages when switching users
                # This prevents re-processing already replied messages when switching back

            # Extract current chat history
            chat_messages = await self._extract_chat_history(page, store.store_id, user_name)

            if not chat_messages:
                # No messages found in conversation
                return new_messages

            current_count = len(chat_messages)
            last_count = self._conversation_message_counts.get(store.store_id, 0)
            
            logger.debug(f"[会话检查] 店铺: {store.store_id}, 用户: {user_name}, 计数: {current_count}")

            # STRATEGY: Check ALL messages for new content
            # Don't rely on count - check each message's ID against processed set
            pending_key = f"{store.store_id}_{user_name}"
            last_info = self._last_message_info.get(pending_key, {})
            last_content = last_info.get('content', '')
            last_time = last_info.get('time', '')
            
            # Check ALL messages, not just new ones (count-based detection is unreliable)
            # Message ID based deduplication will handle duplicates
            logger.debug(f"[消息检查] 店铺: {store.store_id}, 检查 {current_count} 条")
            
            # Check messages
            for i in range(current_count):
                raw_msg = chat_messages[i]

                # CRITICAL: Map JavaScript fields to Python expected fields
                content = raw_msg.get('content', '')
                is_user = raw_msg.get('isUser', False)
                msg_time = raw_msg.get('msgTime', '')  # Extract message time

                # Skip shop/business info messages (e.g., "经营源")
                if '经营源' in content:
                    continue

                # 图片消息：content 保持 [图片]，用 imageSrc 保证 message_id 唯一
                import hashlib
                _dedup_suffix = ''
                if raw_msg.get('hasImage') and raw_msg.get('imageSrc'):
                    _dedup_suffix = ':' + hashlib.md5(str(raw_msg['imageSrc']).encode()).hexdigest()[:8]

                # Build message ID for deduplication check
                content_hash = hashlib.md5(f"{user_name}:{content}{_dedup_suffix}".encode()).hexdigest()[:16]
                message_id = f"{store.store_id}_{user_name}_{content_hash}"
                
                # Check long-term memory
                if message_id in self._processed_messages:
                    # 跳过已处理
                    continue
                
                # 永久去重检查：如果消息已在数据库中（无论多久前），都跳过
                try:
                    exists_in_db = await self.db.message_exists(message_id)
                    if exists_in_db:
                        is_replied = await self.db.is_message_replied(message_id)
                        if is_replied:
                            # 跳过已回复
                            continue
                        else:
                            # 消息存在但未回复（之前处理失败了），允许重新处理
                            # 继续处理未回复
                            pass
                except Exception as e:
                    # DB检查失败
                    pass
                
                # NOTE: Removed content+time deduplication as it can incorrectly skip valid messages
                # Message ID based on content+timestamp is sufficient
                
                # Update last message info
                last_content = content
                last_time = msg_time
                
                conversation_id = f"{store.store_id}_{user_name}"
                msg = {
                    'message_id': message_id,
                    'conversation_id': conversation_id,
                    'content': content,
                    'msg_time': msg_time,
                    'is_user': is_user,
                    'user': {
                        'nickname': user_name,
                        'user_id': user_name
                    },
                    'timestamp': int(time.time() * 1000),  # 使用当前时间戳
                    'type': 'text',
                    'is_customer': True
                }

                # Only include user messages
                if is_user:
                    new_messages.append(msg)
                    logger.info(f"[新消息] 店铺: {store.store_id}, 内容: {content[:50]}...")
            
            # Save last message info for next check
            self._last_message_info[pending_key] = {'content': last_content, 'time': last_time}

            if new_messages:
                logger.info(f"[新消息] 店铺: {store.store_id}, 用户: {user_name}, {len(new_messages)} 条")

            # Update the message count
            self._conversation_message_counts[store.store_id] = current_count
            
            if not new_messages:
                # 无新消息
                pass

        except Exception as e:
            logger.error(f"Store {store.store_id}: Error checking current conversation: {e}", exc_info=True)

        return new_messages


    async def _process_conversation_messages(self, page: Page, store: Store, user_name: str) -> bool:
        """Extract messages from a conversation and add to pending queue.
        
        Returns True if the conversation has chat content (new or existing messages).
        
        注意：此方法只能在确保 processing_user 是当前用户时才调用，
        否则会切换页面导致正在进行的回复操作失败。
        """
        processed = False

        try:
            # ===== 防御修复: 如果 user_name 是时间格式，拒绝处理 =====
            if self._is_time_format(user_name):
                logger.warning(f"[时间格式用户] 店铺 {store.store_id} 拒绝处理时间格式用户名 '{user_name}'")
                return False
            
            # ===== 关键修复: 检查 processing_user =====
            # 如果正在处理其他用户，不应该切换会话
            processing_user = self.get_processing_user(store.store_id)
            if processing_user and user_name != processing_user:
                logger.warning(f"[关键错误] _process_conversation_messages 被错误调用! "
                              f"尝试处理 {user_name}，但正在处理 {processing_user}。跳过！")
                return False
            
            # CRITICAL: Verify we're in the correct conversation before extracting messages
            try:
                is_correct = await asyncio.wait_for(
                    self._verify_current_conversation_user(page, user_name, store.store_id),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.warning(f"Store {store.store_id}: _verify_current_conversation_user 超时 (10s)，假设不在正确会话")
                is_correct = False
            if not is_correct:
                logger.warning(f"Store {store.store_id}: Not in correct conversation for {user_name}. Attempting to switch...")
                
                # 再次检查 processing_user，因为上面的检查可能已经过时
                processing_user = self.get_processing_user(store.store_id)
                if processing_user and user_name != processing_user:
                    logger.warning(f"[关键错误] 会话切换前检查发现正在处理其他用户! "
                                  f"目标: {user_name}, 正在处理: {processing_user}。中止切换！")
                    return False
                
                # Try to find and click the conversation again (with timeout to prevent permanent hang)
                try:
                    conv_clicked = await asyncio.wait_for(
                        self._click_conversation_by_name(page, store.store_id, store, user_name),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Store {store.store_id}: _click_conversation_by_name 总超时 (30s)，放弃切换 {user_name}")
                    conv_clicked = False
                if conv_clicked:
                    # 优化：动态等待验证，最多3秒
                    max_wait = 3.0
                    check_interval = 0.3
                    elapsed = 0.0
                    while elapsed <= max_wait:
                        is_correct = await self._verify_current_conversation_user(page, user_name, store.store_id)
                        if is_correct:
                            break
                        await asyncio.sleep(check_interval)
                        elapsed += check_interval
                
                if not is_correct:
                    logger.warning(f"Store {store.store_id}: Cannot verify conversation with {user_name}, but will try to extract messages anyway")
                    # Don't return False - try to extract messages anyway
                    # Sometimes verification fails but we're actually in the right conversation
            
            # Extract chat history
            chat_messages = await self._extract_chat_history(page, store.store_id, user_name)

            if not chat_messages:
                logger.warning(f"[警告] 店铺: {store.store_id}, 未获取到任何消息")
                return False

            # Found messages from user

            # CRITICAL: Update current conversation tracking BEFORE processing messages
            previous_user = self._current_conversation.get(store.store_id)
            if previous_user != user_name:
                # Switching conversation
                # Clear message count when switching users
                self._conversation_message_counts[store.store_id] = 0
                # NOTE: We NO LONGER clear _processed_messages here
                # Database + memory check in _process_single_message is sufficient for deduplication
                # Clearing processed messages caused re-processing of already replied messages
                # when switching back to the same conversation
            self._current_conversation[store.store_id] = user_name
            logger.debug(f"[监控会话] 店铺: {store.store_id}, 用户: {user_name}")

            # Add messages to pending queue instead of processing immediately
            pending_key = f"{store.store_id}_{user_name}"
            if pending_key not in self._pending_messages:
                self._pending_messages[pending_key] = []
            
            # CRITICAL: For new user (no previous info), find the last AI reply and only process messages after it
            # This avoids re-processing messages that AI has already replied to
            last_info = self._last_message_info.get(pending_key, {})
            is_new_user = not last_info
            
            if is_new_user and len(chat_messages) > 0:
                # Find the last AI (bot) message
                last_bot_msg_idx = -1
                for idx in range(len(chat_messages) - 1, -1, -1):
                    msg = chat_messages[idx]
                    # isBot=True means it's an AI/bot message (right side)
                    if msg.get('isBot', False) or not msg.get('isUser', True):
                        last_bot_msg_idx = idx
                        break
                
                if last_bot_msg_idx >= 0:
                    # Only keep messages AFTER the last bot message
                    chat_messages = chat_messages[last_bot_msg_idx + 1:]
                    logger.debug(f"[新用户] 店铺: {store.store_id}, 用户: {user_name}, 处理 {len(chat_messages)} 条消息")
                else:
                    # No bot message found, process ALL user messages
                    # No AI reply yet, all messages need to be processed
                    user_msgs = [m for m in chat_messages if m.get('isUser', False)]
                    chat_messages = user_msgs
                    logger.debug(f"[新用户] 店铺: {store.store_id}, 用户: {user_name}, 处理所有 {len(chat_messages)} 条消息")
            
            last_content = last_info.get('content', '')
            last_time = last_info.get('time', '')
            
            new_messages_added = 0
            for msg_idx, raw_msg in enumerate(chat_messages):
                # CRITICAL: Map JavaScript fields to Python expected fields
                content = raw_msg.get('content', '')
                is_user_msg = raw_msg.get('isUser', False)
                msg_time = raw_msg.get('msgTime', '')  # Extract message time

                # Only process user messages
                if not is_user_msg:
                    continue

                # Skip shop/business info messages (e.g., "经营源")
                if '经营源' in content:
                    continue

                # 图片消息：content 保持 [图片]，用 imageSrc 保证 message_id 唯一
                _dedup_suffix = ''
                if raw_msg.get('hasImage') and raw_msg.get('imageSrc'):
                    _dedup_suffix = ':' + hashlib.md5(str(raw_msg['imageSrc']).encode()).hexdigest()[:8]

                # Generate message_id for deduplication check
                content_hash = hashlib.md5(f"{user_name}:{content}{_dedup_suffix}".encode()).hexdigest()[:16]
                message_id = f"{store.store_id}_{user_name}_{content_hash}"

                # Check long-term memory (processed set)
                if message_id in self._processed_messages:
                    self._processed_messages[message_id] = (time.time(), store.store_id)
                    logger.debug(f"[跳过已处理] 店铺: {store.store_id}, 内容: {content[:30]}...")
                    continue
                
                # 永久去重检查：如果消息已在数据库中（无论多久前），都跳过
                try:
                    exists_in_db = await self.db.message_exists(message_id)
                    if exists_in_db:
                        is_replied = await self.db.is_message_replied(message_id)
                        if is_replied:
                            logger.debug(f"[跳过已回复] 店铺: {store.store_id}, 内容: {content[:30]}...")
                            continue
                        else:
                            # 消息存在但未回复（之前处理失败了），允许重新处理
                            logger.debug(f"[继续处理] 店铺: {store.store_id}, 消息未回复")
                except Exception as e:
                    # DB检查失败，继续处理
                    pass
                
                # NOTE: We only use message_id for deduplication now
                # The content+time check is removed because it can incorrectly skip valid new messages
                # when user sends multiple messages with same content
                
                # Update last message info
                last_content = content
                last_time = msg_time

                # Build properly structured message
                conversation_id = f"{store.store_id}_{user_name}"
                msg = {
                    'message_id': message_id,
                    'conversation_id': conversation_id,
                    'content': content,
                    'msg_time': msg_time,  # Include message time
                    'is_user': is_user_msg,
                    'user': {
                        'nickname': user_name,
                        'user_id': user_name
                    },
                    'timestamp': int(time.time() * 1000),
                    'type': 'text',
                    'is_customer': True
                }
                
                # Add to pending queue
                self._pending_messages[pending_key].append(msg)
                new_messages_added += 1
                processed = True
            
            # Save last message info for next check
            self._last_message_info[pending_key] = {'content': last_content, 'time': last_time}

            if new_messages_added > 0:
                logger.info(f"[提取消息] 店铺: {store.store_id}, 用户: {user_name}, 新增 {new_messages_added} 条")
                # Process pending messages immediately
                await self._process_pending_messages(store, user_name)
            
            # CRITICAL: Update message count for this conversation
            # This ensures we can detect truly new messages in next check
            user_msg_count = len(chat_messages)  # chat_messages already filtered to user messages only
            old_count = self._conversation_message_counts.get(store.store_id, 0)
            self._conversation_message_counts[store.store_id] = user_msg_count
            logger.debug(f"[消息计数] 店铺: {store.store_id}, 用户: {user_name}, 计数: {user_msg_count}")

            # 关键修复：
            # - 同用户且 Handler 正在处理时：只要有聊天内容就返回 True，避免 Listener 在回复发送期间切换页面。
            # - 新用户切换场景：只有真正提取到需要处理的新消息时才返回 True。
            #   如果都去重/已回复，返回 False 释放锁，避免其他用户消息被阻塞 300 秒。
            processing_user = self.get_processing_user(store.store_id)
            if processing_user == user_name:
                # 同用户处理中：保持锁，防止切换
                if chat_messages:
                    return True
            elif new_messages_added > 0:
                # 新用户切换：只有确实需要处理的消息才保持锁
                return True
            return processed

        except Exception as e:
            logger.error(f"Store {store.store_id}: Error processing conversation messages: {e}")
            return False


    async def _process_single_message(self, msg: Dict, store: Store, expected_user: str = None) -> bool:
        """Process a single message.

        Args:
            msg: The message dictionary
            store: The store instance
            expected_user: The expected username (for validation)

        Returns True if message was processed.
        """
        message_id = None
        try:
            message_id = msg.get('message_id') if msg else None
            msg_user = msg.get('user', {}) if msg else {}
            msg_user_name = msg_user.get('nickname', 'unknown') if msg_user else 'unknown'
            msg_content = msg.get('content', '') if msg else ''

            # Processing single message

            # Validate inputs
            if not msg:
                logger.error(f"Store {store.store_id}: msg is None or empty")
                return False
            if not message_id:
                logger.error(f"Store {store.store_id}: message_id is None or empty")
                return False

            # Validate user matches expected user
            if expected_user and msg_user_name != expected_user:
                logger.warning(f"Store {store.store_id}: User mismatch! Message from '{msg_user_name}' but expected '{expected_user}'. Content: {msg_content[:50]}...")
                # Still process but log the mismatch

            # Check if already processed
            # Strategy: 
            # - If message has time (from page): use both memory and DB for dedup
            # - If no time: check if already replied in DB (CRITICAL: prevent duplicate replies)
            has_time = bool(msg.get('msg_time'))
            
            if has_time:
                # Has timestamp: check memory first
                if message_id in self._processed_messages:
                    self._processed_messages[message_id] = (time.time(), store.store_id)
                    return True
                # Check database — 必须检查是否已回复，不能只看是否存在
                try:
                    exists_in_db = await self.db.message_exists(message_id)
                    if exists_in_db:
                        is_replied = await self.db.is_message_replied(message_id)
                        if is_replied:
                            self._processed_messages[message_id] = (time.time(), store.store_id)
                            return True
                        # 消息存在但未回复 → 检查是否正在处理中（5分钟内的消息视为处理中）
                        is_recent = await self.db.message_exists_within_minutes(message_id, minutes=5)
                        if is_recent:
                            logger.debug(f"Store {store.store_id}: 消息 {message_id[:20]}... 正在处理中，跳过")
                            self._processed_messages[message_id] = (time.time(), store.store_id)
                            return True
                        # 消息存在超过5分钟且未回复 → 允许重新处理（之前可能失败了）
                        logger.debug(f"Store {store.store_id}: 消息 {message_id[:20]}... 存在但未回复，重新处理")
                except Exception as e:
                    logger.warning(f"Store {store.store_id}: DB check failed: {e}")
            else:
                # No timestamp: CRITICAL - Check if already replied (not just exists!)
                try:
                    exists_in_db = await self.db.message_exists(message_id)
                    if exists_in_db:
                        is_replied = await self.db.is_message_replied(message_id)
                        if is_replied:
                            return True
                        # 消息存在但未回复 → 检查是否正在处理中
                        is_recent = await self.db.message_exists_within_minutes(message_id, minutes=5)
                        if is_recent:
                            logger.debug(f"Store {store.store_id}: 消息 {message_id[:20]}... 正在处理中，跳过")
                            return True
                        logger.debug(f"Store {store.store_id}: 消息 {message_id[:20]}... 存在但未回复，重新处理")
                except Exception as e:
                    logger.warning(f"Store {store.store_id}: DB check failed: {e}")

            # Mark as processed in memory (ALWAYS, not just if has_time)
            # 必须始终加入内存去重，否则下一轮轮询会重复处理正在回复中的消息
            self._processed_messages[message_id] = (time.time(), store.store_id)
            logger.info(f"[收到消息] 店铺: {store.store_id}, 用户: {msg_user_name}, 内容: {msg_content[:50]}...")

            # Create Message object
            # Creating Message object
            message = self._parse_message(msg, store.store_id)
            
            if message is None:
                logger.error(f"Store {store.store_id}: _parse_message returned None for {message_id}")
                logger.error(f"Store {store.store_id}: Raw message data: {msg}")
                return False
            
            if message:
                store.total_messages += 1
                # Created Message object

                # CRITICAL: Ensure message has correct user info before callback
                # If message.user.nickname is empty, use the expected_user from context
                if not message.user.nickname and expected_user:
                    logger.warning(f"Store {store.store_id}: Message user nickname is empty, using expected_user '{expected_user}'")
                    message.user.nickname = expected_user
                    message.user.user_id = expected_user

                # Pass expected_user for validation in callback
                try:
                    await self._safe_callback(message, expected_user=expected_user)
                    # Message processed successfully
                    return True
                except Exception as callback_error:
                    logger.error(f"Store {store.store_id}: Callback failed for message {message_id}: {callback_error}")
                    # Remove from processed set so it can be retried
                    self._processed_messages.pop(message_id, None)
                    return False
            else:
                logger.warning(f"Store {store.store_id}: Failed to parse message {message_id} - _parse_message returned None")
                # Log the raw message for debugging
                # Raw message data

            return False

        except Exception as e:
            logger.error(f"Store {store.store_id}: Error processing single message: {e}")
            return False


    async def _process_pending_messages(self, store: Store, user_name: str) -> None:
        """Process all pending messages for a specific user.

        This ensures all messages for the current user are processed before switching.
        多条消息会在入队前合并为一条，避免 Handler 分开处理。
        """
        pending_key = f"{store.store_id}_{user_name}"
        pending_list = self._pending_messages.get(pending_key, [])

        if not pending_list:
            return

        # ===== 消息合并：多条消息合并为一条，用 # 连接 =====
        # 必须在入队前合并，否则 Worker 可能在消息全部入队前就开始处理
        if len(pending_list) > 1:
            combined_content = '#'.join(m['content'] for m in pending_list)
            logger.info(f"[消息合并] 店铺 {store.store_id} 用户 {user_name}: {len(pending_list)} 条 -> {combined_content[:80]}...")

            # 用第一条消息作为载体，替换内容为合并后的
            merged_msg = dict(pending_list[0])
            merged_msg['content'] = combined_content
            # 重新生成 message_id（基于合并后内容）
            import hashlib
            content_hash = hashlib.md5(f"{user_name}:{combined_content}".encode()).hexdigest()[:16]
            merged_msg['message_id'] = f"{store.store_id}_{user_name}_{content_hash}"

            # 标记原始消息为已处理，防止后续重复提取
            for msg in pending_list:
                msg_id = msg.get('message_id', '')
                if msg_id:
                    self._processed_messages[msg_id] = (time.time(), store.store_id)

            # 处理合并后的消息
            try:
                success = await self._process_single_message(merged_msg, store, expected_user=user_name)
                if success:
                    logger.info(f"[合并处理] 店铺 {store.store_id} 用户 {user_name}: 成功")
            except Exception as e:
                logger.error(f"Store {store.store_id}: Error processing merged message: {e}")

            # 清空 pending 列表
            self._pending_messages.pop(pending_key, None)
            return

        # 单条消息：直接处理
        processed_count = 0
        for msg in pending_list[:]:
            try:
                success = await self._process_single_message(msg, store, expected_user=user_name)
                if success:
                    processed_count += 1
                    if msg in self._pending_messages.get(pending_key, []):
                        self._pending_messages[pending_key].remove(msg)
                else:
                    logger.warning(f"Store {store.store_id}: Failed to process message for {user_name}, will retry")
            except Exception as e:
                logger.error(f"Store {store.store_id}: Error processing pending message: {e}")

        # Clean up empty pending lists
        if not self._pending_messages.get(pending_key):
            self._pending_messages.pop(pending_key, None)


    def _parse_message(self, data: Dict, store_id: str) -> Optional[Message]:
        """Parse raw message data into Message object."""
        try:
            # Only process messages from customers (users), not from the bot itself
            is_from_customer = data.get("is_customer", True)
            if not is_from_customer:
                # Skipping bot message
                return None

            message = Message.from_kele_data(data, store_id)
            # Parsed message
            return message
        except Exception as e:
            logger.error(f"Failed to parse message: {e}")
            # Raw data that failed to parse
            return None


    async def _extract_via_websocket_intercept(self, page: Page, store_id: str) -> None:
        """Alternative: Intercept WebSocket messages.

        This method uses Playwright's route API to intercept WebSocket traffic.
        """
        # Note: WebSocket interception in Playwright is limited
        # This would require more advanced techniques
        pass

