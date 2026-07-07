"""Conversation management — sidebar scanning, switching, user verification."""

import asyncio
import re
import time
from typing import Dict, List, Optional

from playwright.async_api import Page

from config import settings
from core.store_manager import Store, StoreStatus
from utils.logger import get_logger
from utils.constants import (
    EMOJI_PATTERN_JS,
    SYSTEM_STATUS_TEXTS,
    SELECTORS_CONVERSATION,
)


logger = get_logger(__name__)


class ConversationMixin:
    """Mixin providing conversation methods for MessageListener."""

    async def _find_first_unread_conversation(self, page: Page, store: Store) -> Optional[Dict]:
        """Find the first unread conversation in the sidebar.

        Returns a dict with conversation info and element, or None if no unread found.
        Prioritizes unread conversations that are NOT the current conversation.
        
        注意：如果某个用户正在处理中（有回复发送流程在进行），会跳过该用户。
        """
        # ===== 方案1: 获取正在处理的用户 =====
        processing_user = self.get_processing_user(store.store_id)
        
        try:
            # Find conversation containers
            conversation_selectors = SELECTORS_CONVERSATION

            conversations = []
            for selector in conversation_selectors:
                try:
                    items = await page.query_selector_all(selector)
                    if items and len(items) > 0:
                        conversations = items
                        break
                except Exception:
                    continue

            if not conversations:
                # FALLBACK: Use JavaScript to find conversations
                try:
                    conversations_js = await asyncio.wait_for(page.evaluate("""
                        () => {
                            const results = [];
                            const windowWidth = window.innerWidth;
                            const windowHeight = window.innerHeight;
                            const allElements = document.querySelectorAll('*');
                            const startTime = Date.now();
                            const TIME_LIMIT = 3000;  // 3秒浏览器端超时

                            for (const el of allElements) {
                                // 浏览器端时间守卫
                                if (Date.now() - startTime > TIME_LIMIT) break;
                                const rect = el.getBoundingClientRect();
                                const text = (el.innerText || '').trim();
                                
                                if (rect.left > windowWidth * 0.25) continue;
                                if (rect.width < 150 || rect.width > 400) continue;
                                if (rect.height < 50 || rect.height > 150) continue;
                                if (rect.top < 100 || rect.bottom > windowHeight - 150) continue;
                                
                                const lines = text.split('\n').filter(l => l.trim());
                                if (lines.length < 1) continue;

                                const systemTexts = ['今日接待', '留资数', '首响率', '用户评分',
                                                     '抖音私信', '数据看板', '客服分流'];

                                // Skip relative time formats only
                                const timePatterns = [
                                    /^\\d+分钟前?$/, /^\\d+小时前?$/, /^\\d+天前?$/,
                                    /^\\d+周前?$/, /^\\d+月前?$/, /^\\d+年前?$/,
                                    /^刚刚$/,
                                    /^-?\\d+秒$/, /^-?\\d+秒前$/,
                                    /^<\\d+分钟?$/, /^<\\d+小时?$/, /^<\\d+天?$/,
                                    /^<\\d+$/, /^\\d+分钟内?$/, /^\\d+小时内?$/,
                                ];

                                const firstLine = lines[0].trim();
                                const systemTexts = ['今日接待', '留资数', '首响率', '用户评分',
                                                     '抖音私信', '数据看板', '客服分流'];
                                if (systemTexts.some(st => firstLine.includes(st))) continue;
                                if (firstLine.length < 2 || firstLine.length > 20) continue;

                                // Skip relative time formats only
                                const timePatterns = [
                                    /^\\d+分钟前?$/, /^\\d+小时前?$/, /^\\d+天前?$/,
                                    /^\\d+周前?$/, /^\\d+月前?$/, /^\\d+年前?$/,
                                    /^刚刚$/,
                                    /^-?\\d+秒$/, /^-?\\d+秒前$/,
                                    /^<\\d+分钟?$/, /^<\\d+小时?$/, /^<\\d+天?$/,
                                    /^<\\d+$/, /^\\d+分钟内?$/, /^\\d+小时内?$/,
                                ];
                                if (timePatterns.some(p => p.test(firstLine))) continue;

                                let hasUnread = el.outerHTML.toLowerCase().includes('unread');
                                const indicators = el.querySelectorAll('*');
                                for (const ind of indicators) {
                                    const style = window.getComputedStyle(ind);
                                    const bg = style.backgroundColor || '';
                                    if ((bg.includes('255') || bg.includes('red')) &&
                                        ind.getBoundingClientRect().width < 25) {
                                        hasUnread = true; break;
                                    }
                                }

                                results.push({
                                    userName: firstLine,
                                    hasUnread: hasUnread,
                                    preview: lines.slice(0, 3)
                                });

                                let hasUnread = el.outerHTML.toLowerCase().includes('unread');
                                const indicators = el.querySelectorAll('*');
                                for (const ind of indicators) {
                                    const style = window.getComputedStyle(ind);
                                    const bg = style.backgroundColor || '';
                                    if ((bg.includes('255') || bg.includes('red')) &&
                                        ind.getBoundingClientRect().width < 25) {
                                        hasUnread = true; break;
                                    }
                                }

                                results.push({
                                    userName: bestName,
                                    hasUnread: hasUnread,
                                    preview: lines.slice(0, 3)
                                });
                            }
                            
                            const seen = new Set();
                            return results.filter(r => {
                                if (seen.has(r.userName)) return false;
                                seen.add(r.userName); return true;
                            });
                        }
                    """), timeout=10.0)

                    if conversations_js:
                        # JS fallback found conversations
                        # IMPORTANT: Don't use hasUnread from JS fallback - it may be inaccurate
                        # Only use the user names, check unread status properly later
                        conversations = [{'js_fallback': True, 'user_name': conv.get('userName'), 'hasUnread': False} for conv in conversations_js if conv.get('userName')]
                except asyncio.TimeoutError:
                    logger.warning(f"Store {store.store_id}: _find_first_unread_conversation JS fallback 超时 (10s)")
                    conversations_js = None
                except Exception as js_err:
                    # JS fallback error
                    pass
                
                if not conversations:
                    return None

            current_user = self._current_conversation.get(store.store_id)
            unread_conversations = []
            current_user_unread = None

            # Check each conversation for unread messages
            for idx, conv in enumerate(conversations):
                try:
                    # Skip JS fallback entries - they don't have element handles
                    # Our CSS selectors should find most real conversations
                    if isinstance(conv, dict) and conv.get('js_fallback'):
                        continue
                    
                    # Get user name and check if has unread
                    conv_data = await self._get_conversation_info(conv, store.store_id)

                    if not conv_data:
                        continue

                    user_name = conv_data.get('user_name')
                    has_unread = conv_data.get('has_unread', False)

                    if has_unread:
                        # ===== 关键修复: 返回所有有未读消息的用户 =====
                        # 不跳过任何用户，让调用方决定如何处理
                        conv_info = {
                            'user_name': user_name,
                            'has_unread': True,
                            'element': conv,
                            'lines': conv_data.get('lines', [])
                        }
                        
                        # If this is the current user's unread, save it for later
                        if user_name == current_user:
                            current_user_unread = conv_info
                        else:
                            # This is a different user's unread - add to list
                            unread_conversations.append(conv_info)

                except Exception as e:
                    # Error checking conversation
                    continue

            # ===== 关键修复: 如果正在处理某用户，只返回该用户的未读 =====
            # 绝对不允许在处理用户A时切换到用户B，这会导致用户A的消息丢失
            if processing_user:
                # 检查 processing_user 是否在 unread_conversations 中
                for conv in unread_conversations:
                    if conv.get('user_name') == processing_user:
                        logger.debug(f"[发现未读-处理中用户] 店铺: {store.store_id}, 用户: {processing_user}")
                        return conv
                # 检查当前用户是否是 processing_user
                if current_user_unread and current_user_unread.get('user_name') == processing_user:
                    logger.debug(f"[发现未读-当前用户] 店铺: {store.store_id}, 用户: {processing_user}")
                    return current_user_unread
                # processing_user 没有未读消息 — 返回 None，保持页面不动
                # 这比返回其他用户的未读要好：宁可不切换，也不能切错
                logger.debug(f"[无未读-处理中] 店铺: {store.store_id}, 处理用户: {processing_user}, 无新未读")
                return None

            # ===== 没有正在处理的用户 — 正常的优先级逻辑 =====
            # Priority 1: Return the first unread conversation that is NOT the current one
            if unread_conversations:
                selected = unread_conversations[0]
                logger.debug(f"[发现其他未读] 店铺: {store.store_id}, 用户: {selected['user_name']}")
                return selected

            # Priority 2: If no other unread conversations, return current user's unread (if any)
            if current_user_unread:
                return current_user_unread

            return None

        except Exception as e:
            logger.error(f"Store {store.store_id}: Error in _find_first_unread_conversation: {e}")
            return None


    async def _get_conversation_info(self, conv, store_id: str) -> Optional[Dict]:
        """Get conversation info including user name and unread status."""
        try:
            conv_text = await conv.inner_text()
            if not conv_text:
                return None

            lines = [l.strip() for l in conv_text.split('\n') if l.strip()]
            if len(lines) < 1:
                return None

            # Extract user name using same logic as before
            user_name = await self._extract_user_name(conv, lines)
            if not user_name:
                return None

            # Check if has avatar (real conversation)
            has_avatar = await conv.evaluate("""
                (el) => {
                    const html = el.outerHTML || '';
                    return html.includes('<img') || html.includes('avatar');
                }
            """)
            if not has_avatar:
                return None

            # Check for unread indicator
            has_unread = await self._has_unread_indicator(conv)

            return {
                'user_name': user_name,
                'has_unread': has_unread,
                'lines': lines
            }

        except Exception as e:
            # Error getting conversation info
            return None

    # SYSTEM_STATUS_TEXTS and other constants imported from utils.constants


    async def _extract_user_name(self, conv, lines) -> Optional[str]:
        """Extract user name from conversation element."""
        # Strategy 1: Try specific selectors (优先匹配抖音来客会话用户名 class)
        try:
            name_selectors = [
                '[class*="conversationName"]',  # 抖音来客会话用户名
                '[class*="name"]:not([class*="nickname"]):not([class*="message"]):not([class*="message-content"]):not([class*="conversation"])',
                '[class*="nickname"]',
                '[class*="title"]:not([class*="header"])',
            ]
            for name_sel in name_selectors:
                name_elem = await conv.query_selector(name_sel)
                if name_elem:
                    name_text = await name_elem.inner_text()
                    name_text = name_text.strip()
                    if (name_text and len(name_text) > 0 and len(name_text) < 20
                        and not self._is_time_format(name_text)
                        and name_text not in SYSTEM_STATUS_TEXTS):
                        return self._normalize_user_name(name_text[:30])
        except Exception:
            pass

        # Strategy 2: Use JavaScript - 优先查找 conversationName 类，避免取到消息预览
        try:
            user_name = await conv.evaluate("""
                (el) => {
                    const statusTexts = ['已留资', '未留资', '已回复', '未回复', '广告源', '经营源', '系统消息'];
                    const timePatterns = [
                        /^\\d+分钟前?$/ , /^\\d+小时前?$/ , /^\\d+天前?$/ ,
                        /^\\d+周前?$/ , /^\\d+月前?$/ , /^\\d+年前?$/ ,
                        /^刚刚$/ ,
                        /^-?\\d+秒$/ , /^-?\\d+秒前$/ ,
                        /^<\\d+分钟?$/ ,  // <30分钟
                        /^<\\d+小时?$/ ,  // <1小时
                        /^<\\d+天?$/ ,    // <1天
                        /^<\\d+$/ ,        // <30
                        /^\\d+分钟内?$/ ,   // 30分钟内
                        /^\\d+小时内?$/ ,   // 1小时内
                    ];
                    const isValidName = (text) => {
                        if (!text || text.length < 1 || text.length > 20) return false;
                        // NOTE: removed /^\\d+$/.test(text) — single-digit usernames like "7" are valid
                        if (timePatterns.some(p => p.test(text))) return false;
                        if (statusTexts.some(st => text === st || text.includes(st))) return false;
                        return true;
                    };
                    
                    // 2.1 优先查找 conversationName 类
                    const nameEl = el.querySelector('[class*="conversationName"]');
                    if (nameEl) {
                        const text = nameEl.innerText.trim();
                        if (isValidName(text)) return text;
                    }
                    
                    // 2.2 查找所有 class 包含 name/nickname/title 的元素，优先使用最短的（通常是用户名）
                    const candidates = el.querySelectorAll('[class*="name"], [class*="nickname"], [class*="title"]');
                    let best = null;
                    for (const cand of candidates) {
                        const text = cand.innerText.trim();
                        if (isValidName(text)) {
                            if (!best || text.length < best.length) {
                                best = text;
                            }
                        }
                    }
                    if (best) return best;
                    
                    // 2.3 Fallback: 按视觉顺序取第一行有效文本（innerText 第一行通常是用户名）
                    const firstLine = (el.innerText || '').split('\\n').map(l => l.trim()).filter(l => l)[0];
                    if (isValidName(firstLine)) return firstLine;
                    
                    // 2.4 最后：TreeWalker 遍历所有文本节点
                    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null, false);
                    let node;
                    while (node = walker.nextNode()) {
                        const text = node.textContent.trim();
                        if (isValidName(text)) return text;
                    }
                    
                    return null;
                }
            """)
            if user_name:
                return self._normalize_user_name(user_name.strip()[:30])
        except Exception:
            pass

        # Strategy 3: Fallback to lines — 取第一个有效行
        # 用户名在 DOM 中通常位于消息预览上方
        for line in lines:
            candidate = line[:30].strip()
            if (candidate and len(candidate) >= 1 and len(candidate) < 20
                and not self._is_time_format(candidate)
                and candidate not in SYSTEM_STATUS_TEXTS):
                return self._normalize_user_name(candidate)

        return None


    async def _switch_to_conversation(self, page: Page, store: Store, user_name: str, conv_element) -> bool:
        """Switch to a specific conversation.

        Returns True if successfully switched and verified.

        注意：此方法只能在确保 processing_user 是当前用户或 None 时才调用。
        会获取 store._page_lock 以防止与 Handler 的 _send_reply 发生页面操作冲突。
        """
        # ===== 防御修复: 如果 user_name 是时间格式，拒绝切换 =====
        if self._is_time_format(user_name):
            logger.warning(f"[时间格式用户] 店铺 {store.store_id} 拒绝切换到时间格式用户名 '{user_name}'")
            return False

        # ===== 关键修复: 检查 processing_user =====
        processing_user = self.get_processing_user(store.store_id)
        if processing_user and user_name != processing_user:
            logger.error(f"[关键错误] _switch_to_conversation 被错误调用! "
                        f"尝试切换到 {user_name}，但正在处理 {processing_user}。拒绝切换！")
            return False

        # ===== 获取页面锁，避免与 Handler 的 _send_reply 并发操作页面 =====
        # 使用超时机制防止死锁：如果 Handler 长时间持锁，Listener 不应无限等待
        LOCK_TIMEOUT = 12.0
        try:
            await asyncio.wait_for(store._page_lock.acquire(), timeout=LOCK_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                f"[锁超时] Store {store.store_id}: _switch_to_conversation 等待页面锁 "
                f"{LOCK_TIMEOUT}s 超时，放弃切换"
            )
            return False

        try:
            # 整个切换体加上 25s 超时保护（锁内操作总时限）
            async def _do_switch():
                # Click to open this conversation
                clicked = await self._click_conversation(conv_element, page, store.store_id, user_name)
                if not clicked:
                    logger.error(f"Store {store.store_id}: Failed to click conversation for {user_name}")
                    return False

                # 优化：动态等待聊天区域加载完成，而不是固定等待3秒
                # 每 0.3s 验证一次，最多 5s；页面加载快时立即返回，提升切换速度
                is_correct = False
                max_wait = 5.0
                check_interval = 0.3
                elapsed = 0.0
                while elapsed <= max_wait:
                    is_correct = await self._verify_current_conversation_user(page, user_name, store.store_id)
                    if is_correct:
                        break
                    await asyncio.sleep(check_interval)
                    elapsed += check_interval

                if not is_correct:
                    logger.error(f"Store {store.store_id}: 切换后无法验证会话 {user_name}，可能页面未正确加载")
                    return False

                # Reset state for new user
                previous_user = self._current_conversation.get(store.store_id)
                if previous_user != user_name:
                    # Switching conversation
                    self._conversation_message_counts[store.store_id] = 0
                    # Clear last message info for previous user
                    previous_pending_key = f"{store.store_id}_{previous_user}"
                    if previous_pending_key in self._last_message_info:
                        del self._last_message_info[previous_pending_key]

                # Update current conversation
                self._current_conversation[store.store_id] = user_name
                logger.info(f"[切换会话] 店铺: {store.store_id}, 用户: {user_name}")
                return True

            try:
                return await asyncio.wait_for(_do_switch(), timeout=25.0)
            except asyncio.TimeoutError:
                logger.error(f"Store {store.store_id}: _switch_to_conversation 执行超时 (25s)")
                return False

        except Exception as e:
            logger.error(f"Store {store.store_id}: Error switching to conversation: {e}")
            return False
        finally:
            store._page_lock.release()
    

    async def _click_conversation(self, conv, page, store_id: str, user_name: str) -> bool:
        """Click on a conversation to open the chat."""
        try:
            # Attempting to click conversation

            # Try to click using JavaScript first (most reliable)
            click_result = await page.evaluate("""
                (element) => {
                    // Log element info for debugging
                    console.log('Clicking conversation element:', element.tagName, element.className);

                    // Find clickable parent
                    let clickable = element;
                    while (clickable && clickable !== document.body) {
                        if (clickable.tagName === 'DIV' || clickable.tagName === 'BUTTON' || clickable.tagName === 'A') {
                            clickable.click();
                            console.log('Clicked parent element:', clickable.tagName);
                            return 'clicked_parent';
                        }
                        clickable = clickable.parentElement;
                    }
                    // Try clicking the element itself
                    element.click();
                    return 'clicked_element';
                }
            """, conv)

            # JavaScript click result

            if click_result:
                # Successfully clicked conversation
                return True

            # Fallback: try Playwright click
            # Trying Playwright click
            await conv.click()
            # Playwright click succeeded
            return True

        except Exception as e:
            logger.error(f"Store {store_id}: Error clicking conversation for {user_name}: {e}")
            return False


    async def _click_conversation_by_name(self, page: Page, store_id: str, store: Store, user_name: str) -> bool:
        """Click on a conversation by user name.

        Returns True if successfully clicked.

        Acquires store._page_lock to prevent concurrent page operations with Handler._send_reply.

        警告：此方法会操作页面（点击会话），调用前必须确保 processing_user
        是当前用户或 None，否则会干扰正在进行的回复操作。
        """
        # ===== 关键修复: 检查 processing_user =====
        processing_user = self.get_processing_user(store_id)
        if processing_user and user_name != processing_user:
            logger.error(f"[关键错误] _click_conversation_by_name 被错误调用! "
                        f"尝试点击 {user_name}，但正在处理 {processing_user}。拒绝点击！")
            return False

        # ===== 获取页面锁，避免与 Handler 的 _send_reply 并发操作页面 =====
        # 使用超时机制防止死锁：如果 Handler 长时间持锁，Listener 不应无限等待
        LOCK_TIMEOUT = 12.0  # 最多等待 12 秒获取页面锁
        try:
            lock_acquired = await asyncio.wait_for(store._page_lock.acquire(), timeout=LOCK_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                f"[锁超时] Store {store_id}: 等待页面锁 {LOCK_TIMEOUT}s 超时，"
                f"可能被 Handler 或其他操作长时间持锁，放弃本次点击"
            )
            return False

        try:
            return await asyncio.wait_for(
                self.__click_conversation_by_name_impl(page, store_id, user_name),
                timeout=15.0  # impl 内部的 JS evaluate 也可能超时
            )
        except asyncio.TimeoutError:
            logger.warning(f"Store {store_id}: __click_conversation_by_name_impl 执行超时 (15s)")
            return False
        finally:
            store._page_lock.release()

    async def __click_conversation_by_name_impl(self, page: Page, store_id: str, user_name: str) -> bool:
        """Internal implementation of _click_conversation_by_name (page_lock already held)."""
        try:
            # Looking for conversation to click
            
            # FALLBACK: Try JavaScript click first
            try:
                normalized = self._normalize_user_name(user_name)
                _emoji_re = EMOJI_PATTERN_JS
                js_result = await page.evaluate(f"""
                    () => {{
                        const targetName = '{user_name}';
                        const normalizedTarget = targetName.replace(/[，。！？、；：""''（）《》…—~.,!?;:\\'\"()[]{{}}]+$/g, '').replace(/[{_emoji_re}]+$/gu, '');
                        const allElements = document.querySelectorAll('*');
                        const startTime = Date.now();
                        const TIME_LIMIT = 3000;  // 3秒浏览器端超时，防止 DOM 遍历卡死

                        for (const el of allElements) {{
                            // 浏览器端时间守卫：超过 3 秒立即退出
                            if (Date.now() - startTime > TIME_LIMIT) {{
                                return {{success: false, reason: 'timeout'}};
                            }}
                            const text = (el.innerText || '').trim();
                            const rect = el.getBoundingClientRect();
                            const windowWidth = window.innerWidth;
                            
                            if (rect.left > windowWidth * 0.25) continue;
                            if (rect.width < 150 || rect.width > 400) continue;
                            if (rect.height < 40 || rect.height > 150) continue;
                            
                            const lines = text.split('\n').filter(l => l.trim());
                            if (lines.length < 1) continue;
                            
                            const firstLine = lines[0].trim();
                            if (firstLine === targetName || firstLine.includes(targetName) ||
                                (normalizedTarget && (firstLine === normalizedTarget || firstLine.includes(normalizedTarget) || normalizedTarget.includes(firstLine)))) {{
                                el.click();
                                let parent = el.parentElement;
                                for (let i = 0; i < 5 && parent; i++) {{
                                    if (parent.onclick || parent.tagName === 'BUTTON') {{
                                        parent.click(); break;
                                    }}
                                    parent = parent.parentElement;
                                }}
                                return {{success: true}};
                            }}
                        }}
                        return {{success: false}};
                    }}
                """)
                
                if js_result.get('success'):
                    # JS click successful
                    await asyncio.sleep(2.0)
                    return True
            except Exception as js_err:
                # JS click failed
                pass
            
            # Get all conversation items from sidebar
            conv_selectors = [
                '.conversationItem-RaXg9G',  # Primary selector
                '[class*="conversationItem"]',
                '[class*="session-list"] > div',
                '[class*="conversation-list"] > div',
                '[class*="im-session"]',
                '[class*="conversation"]',
                '[class*="session"]',
            ]
            
            for selector in conv_selectors:
                try:
                    conversations = await page.query_selector_all(selector)
                    for conv in conversations:
                        try:
                            # Check if this is the right conversation
                            conv_text = await conv.inner_text()
                            normalized = self._normalize_user_name(user_name)
                            if user_name in conv_text or (normalized and normalized in conv_text):
                                # Check if it has avatar (real conversation)
                                has_avatar = await conv.evaluate("""
                                    (el) => {
                                        const html = el.outerHTML || '';
                                        return html.includes('<img') || html.includes('avatar');
                                    }
                                """)
                                if has_avatar:
                                    await conv.click()
                                    # Clicked conversation
                                    return True
                        except:
                            continue
                except:
                    continue
            
            # Fallback: Use JavaScript to find and click
            _emoji_re = EMOJI_PATTERN_JS
            result = await page.evaluate(f"""
                (userName) => {{
                    const normalizedName = userName.replace(/[，。！？、；：""''（）《》…—~.,!?;:\\'\"()[]{{}}]+$/g, '').replace(/[{_emoji_re}]+$/gu, '');
                    // Find sidebar
                    const sidebar = document.querySelector('[class*="sidebar"], [class*="session-list"], [class*="conversation-list"]');
                    if (!sidebar) return false;
                    
                    // Find all conversation items
                    const items = sidebar.querySelectorAll('div');
                    for (const item of items) {{
                        const text = item.innerText || item.textContent || '';
                        if (text.includes(userName) || (normalizedName && text.includes(normalizedName))) {{
                            // Check if it has avatar
                            const hasAvatar = item.querySelector('img') !== null || 
                                              item.outerHTML.includes('avatar');
                            if (hasAvatar) {{
                                item.click();
                                return true;
                            }}
                        }}
                    }}
                    return false;
                }}
            """, user_name)
            
            return result
        except Exception as e:
            logger.error(f"Store {store_id}: Error clicking conversation by name: {e}")
            return False


    async def _is_user_in_sidebar(self, page: Page, user_name: str) -> bool:
        """Check if a user still exists in the sidebar conversation list.
        
        Returns True if the user is found in sidebar, False otherwise.
        """
        try:
            normalized = self._normalize_user_name(user_name)
            _emoji_re = EMOJI_PATTERN_JS
            result = await page.evaluate(f"""
                (userName) => {{
                    const normalizedName = userName.replace(/[，。！？、；：""''（）《》…—~.,!?;:\\'\"()[]{{}}]+$/g, '').replace(/[{_emoji_re}]+$/gu, '');
                    const sidebar = document.querySelector('[class*="sidebar"], [class*="session-list"], [class*="conversation-list"]');
                    if (!sidebar) return false;
                    const items = sidebar.querySelectorAll('div');
                    for (const item of items) {{
                        const text = item.innerText || item.textContent || '';
                        if (text.includes(userName) || (normalizedName && text.includes(normalizedName))) {{
                            const hasAvatar = item.querySelector('img') !== null || item.outerHTML.includes('avatar');
                            if (hasAvatar) return true;
                        }}
                    }}
                    return false;
                }}
            """, user_name)
            return bool(result)
        except Exception:
            # If check fails, assume user still exists to be safe
            return True


    async def _verify_current_conversation_user(self, page: Page, expected_user: str, store_id: str = "unknown") -> bool:
        """Verify that the current conversation is with the expected user.

        Returns True if we're in the correct conversation, False otherwise.
        """
        try:
            # Try to find the user name in the chat header with expanded selectors
            header_selectors = [
                '[class*="chat-header"]',
                '[class*="conversation-header"]',
                '[class*="header"] [class*="title"]',
                '[class*="header"]',
                '[class*="title"]',
                '[class*="nickname"]',
                '[class*="user-name"]',
                '[class*="user-info"]',
                '[class*="name"]',
                '[class*="user"]',
                # Douyin-specific selectors
                '[class*="cs-header"]',
                '[class*="im-header"]',
                '[class*="session-title"]',
                '[class*="conv-title"]',
            ]
            
            found_texts = []  # Collect all found texts for debugging
            
            for selector in header_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for elem in elements:
                        text = await elem.inner_text()
                        text_stripped = text.strip()
                        if text_stripped and len(text_stripped) < 100:  # Avoid huge texts
                            found_texts.append(f"{selector}: {text_stripped[:50]}")
                        if expected_user in text:
                            # Found user in selector
                            return True
                        # Loosen matching: also try normalized name (strip trailing punctuation)
                        normalized = self._normalize_user_name(expected_user)
                        if normalized and normalized != expected_user and normalized in text:
                            logger.debug(f"Verification matched normalized name '{normalized}' for '{expected_user}'")
                            return True
                except:
                    continue
            
            if found_texts:
                logger.debug(f"Store {store_id}: 验证 {expected_user} 未通过，CSS找到文本: {found_texts[:10]}")
            
            # Fallback: Use JavaScript for more comprehensive check (with timeout)
            _emoji_re = EMOJI_PATTERN_JS
            try:
                result = await asyncio.wait_for(
                    page.evaluate(f"""
                (userName) => {{
                    const normalizedName = userName.replace(/[，。！？、；：""''（）《》…—~.,!?;:\\'\"()[]{{}}]+$/g, '').replace(/[{_emoji_re}]+$/gu, '');
                    
                    // Check various header areas
                    const selectors = [
                        '[class*="chat-header"]',
                        '[class*="conversation-header"]',
                        '[class*="header"] [class*="title"]',
                        '[class*="header"]',
                        '[class*="title"]',
                        '[class*="nickname"]',
                        '[class*="user-name"]',
                        '[class*="user-info"]',
                        '[class*="name"]',
                        '[class*="user"]',
                        '[class*="cs-header"]',
                        '[class*="im-header"]',
                        '[class*="session-title"]',
                        '[class*="conv-title"]',
                        'h1', 'h2', 'h3', 'h4',
                    ];
                    
                    const foundTexts = [];
                    
                    for (const sel of selectors) {{
                        const elems = document.querySelectorAll(sel);
                        for (const elem of elems) {{
                            const text = (elem.innerText || elem.textContent || '').trim();
                            if (text && text.length < 100) {{
                                foundTexts.push(` ${{sel}}: ${{text.substring(0, 50)}}`);
                            }}
                            if (text.includes(userName) || (normalizedName && text.includes(normalizedName))) {{
                                return {{ found: true, text: text, selector: sel }};
                            }}
                        }}
                    }}
                    
                    // Check right panel for user info
                    const rightPanel = document.querySelector('[class*="right"], [class*="right-panel"], [class*="user-panel"], [class*="sidebar-right"]');
                    if (rightPanel) {{
                        const text = rightPanel.innerText || '';
                        if (text.includes(userName) || (normalizedName && text.includes(normalizedName))) {{
                            return {{ found: true, text: text.substring(0, 100), selector: 'right-panel' }};
                        }}
                    }}
                    
                    // Check main content area (in case header doesn't have user name)
                    const mainContent = document.querySelector('[class*="main"], [class*="content"], [class*="chat-area"], [class*="conversation-area"]');
                    if (mainContent) {{
                        // Look for user name in any element
                        const allElements = mainContent.querySelectorAll('*');
                        for (const el of allElements) {{
                            const text = (el.innerText || el.textContent || '').trim();
                            if (text === userName || text.startsWith(userName + '\\n') ||
                                (normalizedName && (text === normalizedName || text.startsWith(normalizedName + '\\n')))) {{
                                return {{ found: true, text: text.substring(0, 100), selector: 'main-content' }};
                            }}
                        }}
                    }}
                    
                    return {{ found: false, checkedSelectors: selectors.length, foundTexts: foundTexts.slice(0, 10) }};
                }}
            """, expected_user),
                    timeout=8.0
                )
            except asyncio.TimeoutError:
                logger.warning(f"Store {store_id}: _verify_current_conversation_user JS fallback 超时 (8s)")
                return False
            
            if result.get('found'):
                return True
            else:
                # Log what we found for debugging
                found_texts_js = result.get('foundTexts', [])
                logger.debug(f"Store {store_id}: 验证 {expected_user} 未通过，JS找到文本: {found_texts_js}")
                return False
                
        except Exception as e:
            # Error verifying conversation user
            return False


    async def _extract_user_id_from_conv(self, conv, page) -> Optional[str]:
        """Extract user ID from conversation element."""
        try:
            # Try to get user ID from data attributes
            for attr in ['data-user-id', 'data-conversation-id', 'data-id', 'data-uid']:
                user_id = await conv.get_attribute(attr)
                if user_id:
                    return user_id

            # Try to find from inner elements
            id_selectors = ['[data-user-id]', '[data-uid]', '[data-id]']
            for id_sel in id_selectors:
                try:
                    id_elem = await conv.query_selector(id_sel)
                    if id_elem:
                        for attr in ['data-user-id', 'data-uid', 'data-id']:
                            user_id = await id_elem.get_attribute(attr)
                            if user_id:
                                return user_id
                except:
                    continue

            # Try JavaScript extraction
            return await page.evaluate("""
                (element) => {
                    const attrs = ['data-user-id', 'data-uid', 'data-id', 'data-conversation-id', 'data-imid'];
                    for (const attr of attrs) {
                        if (element.hasAttribute(attr)) {
                            return element.getAttribute(attr);
                        }
                        const child = element.querySelector('[' + attr + ']');
                        if (child) {
                            return child.getAttribute(attr);
                        }
                    }
                    return null;
                }
            """, conv)
        except:
            return None


    async def _has_unread_indicator(self, conv_element) -> bool:
        """Check if a conversation element has an unread indicator.
        
        Returns True if any of these are found:
        1. Badge with NUMBER (1, 2, 3, ..., 99, +)
        2. Red dot indicator (small red circular element)
        3. "未读" keyword in the element text
        
        Douyin uses animated number components where the badge text may contain
        all digits 0-9. We check for small elements (16x16) with red background.
        """
        try:
            result = await conv_element.evaluate("""
                (el) => {
                    // Strategy 1: Look for Douyin badge components with red background
                    // The actual unread badge is typically 16x16px with rgb(240, 67, 48) bg
                    const badgeSelectors = [
                        '.byted-badge-text',
                        '.byted-animated-number',
                        '.byted-badge-sup',
                        '[class*="supCls"]'
                    ];
                    
                    for (const selector of badgeSelectors) {
                        const badges = el.querySelectorAll(selector);
                        
                        for (const badge of badges) {
                            const style = window.getComputedStyle(badge);
                            const rect = badge.getBoundingClientRect();
                            
                            // Must be visible and have reasonable size (badge size)
                            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                                continue;
                            }
                            if (rect.width === 0 || rect.height === 0) continue;
                            
                            // Check for red background color (Douyin red)
                            const bg = style.backgroundColor || '';
                            const isRedBg = bg.includes('240, 67') || bg.includes('255') || 
                                           bg.includes('red') || bg.includes('244') ||
                                           bg.includes('250');
                            
                            // For badges around 16x16 with red bg, it's likely an unread badge
                            if (isRedBg && rect.width <= 25 && rect.height <= 25 && rect.width >= 10) {
                                // Try to get the actual number from aria-label or text
                                let count = badge.getAttribute('aria-label');
                                if (!count) {
                                    // For animated numbers, get text and extract first digit sequence
                                    let text = (badge.innerText || badge.textContent || '').trim();
                                    // Remove whitespace and newlines, get first number
                                    const match = text.replace(/\\s/g, '').match(/^[0-9]+/);
                                    if (match) count = match[0];
                                }
                                
                                if (count && /^[0-9]+$/.test(count)) {
                                    return {
                                        hasUnread: true,
                                        reason: 'douyin_red_badge',
                                        text: count,
                                        size: `${Math.round(rect.width)}x${Math.round(rect.height)}`
                                    };
                                }
                                
                                // Even if we can't extract number, red badge means unread
                                if (isRedBg) {
                                    return {
                                        hasUnread: true,
                                        reason: 'douyin_red_badge_no_number',
                                        size: `${Math.round(rect.width)}x${Math.round(rect.height)}`
                                    };
                                }
                            }
                        }
                    }
                    
                    // Strategy 2: Check for '未读' keyword anywhere
                    const allText = el.innerText || '';
                    if (allText.includes('未读')) {
                        return {hasUnread: true, reason: 'unread_keyword'};
                    }
                    
                    return {hasUnread: false};
                }
            """)
            
            if result and result.get('hasUnread'):
                # Found unread indicator
                return True
            
            return False
            
        except Exception as e:
            logger.warning(f"Error checking unread indicator: {e}")
            return False

