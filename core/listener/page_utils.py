"""Page navigation utilities for IM listener — popup closing, IM entry, refresh."""

import asyncio
import re
import time
from typing import Optional

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


class PageUtilsMixin:
    """Mixin providing page utils methods for MessageListener."""

    async def _navigate_to_im(self, store: Store) -> None:
        """Navigate to the IM conversation page via 在线咨询 button or direct URL."""
        page = store.page
        if not page:
            raise Exception("Page not available")

        # First check if we're already on IM page
        current_url = page.url
        # Current URL check
        if "cs/web" in current_url or "im" in current_url.lower() or "accountId" in current_url:
            # Already on IM page
            # Make sure we're in 抖音私信 tab
            try:
                await page.evaluate("""
                    () => {
                        const elements = document.querySelectorAll('*');
                        for (const el of elements) {
                            if (el.textContent && el.textContent.includes('抖音私信')) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                await asyncio.sleep(1)
            except Exception:
                pass
            # Close any popup dialogs and return
            await self._close_popup_dialogs(store, page)
            return

        # NOTE: Direct URL navigation is disabled - relying on button click instead
        # This is more reliable as the IM URL may change or become invalid
        im_url = store.config.im_url or store.metadata.get('im_url')
        # NOTE: IM URL is stored but not used for direct navigation
        _ = im_url  # Avoid unused variable warning

        # Make sure we're on the main page first
        if "life.douyin.com" not in current_url:
            # Navigating to main page first
            await page.goto("https://life.douyin.com/", wait_until="load")
            await asyncio.sleep(3)

        # Navigate to main page first to ensure proper auth flow
        # Navigating to main page first
        await page.goto("https://life.douyin.com/", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Check again if we're now on IM page (after navigation)
        current_url = page.url
        if "cs/web" in current_url or "im" in current_url.lower():
            # Already on IM page after navigation
            await self._close_popup_dialogs(store, page)
            return

        # Try to find and click 在线咨询 button to enter IM page with proper auth
        # Looking for 在线咨询 button
        await asyncio.sleep(5)  # Wait longer for page to fully load

        button_clicked = False
        
        # ===== 关键修复：先检测是否是咨询中控台页面 =====
        # 如果直接走 Strategy 1-4，"text=咨询"等选择器会误点击"咨询中控台"页面上的标题/菜单，
        # 导致跳过真正的"顾客咨询"入口
        try:
            is_consult_center = await page.evaluate("""
                () => {
                    const pageText = document.body.innerText || '';
                    return pageText.includes('咨询中控台') || 
                           pageText.includes('用户触达') || 
                           pageText.includes('促进发问') ||
                           pageText.includes('接待提效');
                }
            """)
            
            if is_consult_center:
                logger.info(f"Store {store.store_id}: 检测到咨询中控台页面，直接点击右上角'顾客咨询'进入 IM...")
                
                consult_clicked = await page.evaluate("""
                    () => {
                        // 方法1: 顶部导航栏区域
                        const topNavs = document.querySelectorAll('header div, header span, header a, [class*="header"] div, [class*="header"] span, [class*="header"] a, [class*="nav"] div, [class*="nav"] span, [class*="nav"] a');
                        for (const nav of topNavs) {
                            const text = (nav.innerText || '').trim();
                            if (text.startsWith('顾客咨询')) {
                                nav.click();
                                return {success: true, method: 'top_nav', text: text};
                            }
                        }
                        // 方法2: fallback - 按页面顶部位置定位
                        const allTopElements = document.querySelectorAll('div, span, a');
                        for (const nav of allTopElements) {
                            const rect = nav.getBoundingClientRect();
                            if (rect.top < 80 && rect.width > 0 && rect.height > 0) {
                                const text = (nav.innerText || '').trim();
                                if (text.startsWith('顾客咨询')) {
                                    nav.click();
                                    return {success: true, method: 'top_nav_fallback', text: text};
                                }
                            }
                        }
                        return {success: false};
                    }
                """)
                
                if consult_clicked and consult_clicked.get('success'):
                    button_clicked = True
                    logger.info(f"Store {store.store_id}: 点击'顾客咨询'成功: {consult_clicked.get('method')} - {consult_clicked.get('text', '')}")
                    await asyncio.sleep(3)
                else:
                    logger.warning(f"Store {store.store_id}: 咨询中控台页面未找到'顾客咨询'入口")
        except Exception as e:
            logger.warning(f"Store {store.store_id}: 咨询中控台检测失败: {e}")
        
        # Strategy 1: Use Playwright's text selector to find and click 在线咨询
        if not button_clicked:
            try:
                # Try multiple text variations
                text_selectors = ['text=在线咨询', 'text=咨询', 'text=在线']
                for text_sel in text_selectors:
                    try:
                        consult_button = await page.query_selector(text_sel)
                        if consult_button:
                            is_visible = await consult_button.is_visible()
                            if is_visible:
                                await consult_button.click()
                                button_clicked = True
                                # Clicked button using selector
                                await asyncio.sleep(1)
                                break
                    except:
                        continue
            except Exception as e:
                # Strategy 1 failed
                pass
        
        # Strategy 2: Try to find button by partial text match with more options
        if not button_clicked:
            try:
                # Get all buttons and check their text
                buttons = await page.query_selector_all('button, a, [role="button"]')
                for btn in buttons:
                    try:
                        text = await btn.inner_text()
                        if text and ('在线咨询' in text or '咨询' in text or '客服' in text):
                            is_visible = await btn.is_visible()
                            if is_visible:
                                await btn.click()
                                button_clicked = True
                                # Clicked button with text
                                await asyncio.sleep(1)
                                break
                    except:
                        continue
            except Exception as e:
                # Strategy 2 failed
                pass
        
        # Strategy 3: Use JavaScript with comprehensive search
        if not button_clicked:
            try:
                result = await page.evaluate("""
                    () => {
                        // Search for 在线咨询 text in the entire document
                        const allElements = document.querySelectorAll('*');
                        const candidates = [];
                        
                        for (const elem of allElements) {
                            const text = (elem.textContent || '').trim();
                            if (text === '在线咨询' || text === '咨询' || text.includes('在线咨询')) {
                                const rect = elem.getBoundingClientRect();
                                // Must be visible
                                if (rect.width > 0 && rect.height > 0) {
                                    // Score by position (prefer top area) and exact match
                                    let score = 0;
                                    if (text === '在线咨询') score += 100;
                                    if (text.includes('在线咨询')) score += 50;
                                    if (rect.top < 100) score += 30;  // Top header area
                                    if (rect.top < 200) score += 10;  // Upper area
                                    
                                    candidates.push({
                                        element: elem,
                                        text: text,
                                        score: score,
                                        rect: rect
                                    });
                                }
                            }
                        }
                        
                        // Sort by score
                        candidates.sort((a, b) => b.score - a.score);
                        
                        // Try to click the highest scored element
                        for (const candidate of candidates.slice(0, 3)) {
                            let clickable = candidate.element;
                            // Find clickable parent or self
                            let depth = 0;
                            while (clickable && clickable !== document.body && depth < 5) {
                                const tag = clickable.tagName;
                                const role = clickable.getAttribute('role');
                                const style = window.getComputedStyle(clickable);
                                
                                if (tag === 'BUTTON' || tag === 'A' || role === 'button' || 
                                    style.cursor === 'pointer' || clickable.onclick) {
                                    clickable.click();
                                    return {
                                        success: true, 
                                        method: 'javascript_click', 
                                        text: candidate.text,
                                        score: candidate.score,
                                        tag: clickable.tagName
                                    };
                                }
                                clickable = clickable.parentElement;
                                depth++;
                            }
                            
                            // Try clicking the element itself
                            candidate.element.click();
                            return {
                                success: true, 
                                method: 'direct_click', 
                                text: candidate.text,
                                score: candidate.score
                            };
                        }
                        
                        // Last resort: try to find by data attributes
                        const dataElements = document.querySelectorAll('[data-log-name*="咨询"], [data-log-name*="客服"], [data-log-module*="咨询"]');
                        for (const el of dataElements) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                el.click();
                                return {success: true, method: 'data_attribute', name: el.getAttribute('data-log-name')};
                            }
                        }
                        
                        return {success: false, candidates: candidates.length};
                    }
                """)
                if result and result.get('success'):
                    button_clicked = True
                    # Clicked 在线咨询 button
                else:
                    logger.warning(f"Store {store.store_id}: JavaScript click failed, candidates found: {result.get('candidates', 0)}")
            except Exception as e:
                logger.error(f"Strategy 3 (JavaScript) failed: {e}")

        # Strategy 4: Look for elements with specific attributes in header (fallback)
        if not button_clicked:
            try:
                result = await page.evaluate("""
                    () => {
                        // Check header area for IM-related elements
                        const headerElements = document.querySelectorAll('header [class*="consult"], header [class*="im"], header [class*="online"], [class*="header"] [class*="consult"], [class*="header"] [class*="im"]');
                        for (const elem of headerElements) {
                            const rect = elem.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                elem.click();
                                return {success: true, className: elem.className};
                            }
                        }
                        return {success: false};
                    }
                """)
                if result and result.get('success'):
                    button_clicked = True
                    # Clicked 在线咨询 button by class
            except Exception as e:
                # Strategy 4 failed
                pass

        if not button_clicked:
            logger.warning(f"Store {store.store_id}: Could not find 在线咨询 button, will try alternative methods...")

        # Button was clicked - wait for IM page to open in new tab
        # Waiting for IM interface to load
        await asyncio.sleep(5)  # Wait for page transition

        # Check if a new page/tab opened (IM opens in new window)
        context = page.context
        pages = context.pages
        # Total pages in context

        # If more than one page, the IM opened in a new tab
        if len(pages) > 1:
            # Find the IM page (check for 'cs/web' in URL)
            for p in pages:
                url = p.url
                # Found page with URL
                if "cs/web" in url:
                    # Switch to IM page
                    # Switching to IM page
                    store.page = p
                    page = p
                    # Record this URL for future direct navigation
                    if "accountId" in url and not store.metadata.get('im_url'):
                        store.metadata['im_url'] = url
                        # Recorded IM URL for future use
                    break

        # Take screenshot to see what loaded
        await page.screenshot(path=f"storage/debug_im_loading_{store.store_id}.png")

        # Check current URL
        current_url = page.url
        # Current URL after click

        # If still not on IM page after clicking entry, log error and let outer loop retry
        if "cs/web" not in current_url:
            logger.error(f"Store {store.store_id}: IM page did not open after clicking entry. Current URL: {current_url}")
            raise Exception("IM page did not open")
        
        # ===== 系统异常检测与自动恢复 =====
        try:
            is_system_error = await page.evaluate("""
                () => {
                    const pageText = document.body.innerText || '';
                    return pageText.includes('系统异常') && pageText.includes('请刷新页面或退出后重新进入IM工作台');
                }
            """)
            
            if is_system_error:
                logger.warning(f"Store {store.store_id}: 检测到 IM 系统异常页面，尝试自动恢复...")
                
                # 尝试点击"刷新"按钮
                refresh_clicked = await page.evaluate("""
                    () => {
                        const buttons = document.querySelectorAll('button, div[role="button"], a');
                        for (const btn of buttons) {
                            const text = (btn.innerText || '').trim();
                            if (text === '刷新') {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                
                if refresh_clicked:
                    logger.info(f"Store {store.store_id}: 已点击刷新按钮，等待页面恢复...")
                    await asyncio.sleep(5)
                    
                    # 刷新后再检查一次
                    still_error = await page.evaluate("""
                        () => {
                            const pageText = document.body.innerText || '';
                            return pageText.includes('系统异常');
                        }
                    """)
                    
                    if still_error:
                        logger.warning(f"Store {store.store_id}: 刷新后仍为系统异常，尝试返回主页面重新进入...")
                        # 尝试点击"退出"
                        await page.evaluate("""
                            () => {
                                const buttons = document.querySelectorAll('button, div[role="button"]');
                                for (const btn of buttons) {
                                    const text = (btn.innerText || '').trim();
                                    if (text === '退出') {
                                        btn.click();
                                        return true;
                                    }
                                }
                                return false;
                            }
                        """)
                        await asyncio.sleep(3)
                        # 重新导航到主页面
                        await page.goto("https://life.douyin.com/", wait_until="domcontentloaded")
                        await asyncio.sleep(3)
                        # 再次尝试进入 IM（递归或重新执行策略）
                        # 这里让外层循环重试
                        raise Exception("System error page, need retry from main page")
                else:
                    logger.warning(f"Store {store.store_id}: 未找到刷新按钮")
        except Exception as e:
            if "System error page" in str(e):
                raise
            logger.warning(f"Store {store.store_id}: 系统异常检测失败: {e}")

        # Wait for conversation list to load (with shorter timeout)
        # Note: These selectors need to be updated based on actual page structure
        conversation_selectors = [
            '[class*="conversation"]',
            'text=当前咨询',
            '[class*="session"]',
        ]

        im_loaded = False
        for selector in conversation_selectors:
            try:
                await page.wait_for_selector(selector, timeout=5000)  # 5秒超时
                # IM page loaded with selector
                im_loaded = True
                break
            except:
                continue

        if not im_loaded:
            # Check if we're on the IM page by checking URL and simple body text
            try:
                # Quick check via JavaScript
                has_im_content = await page.evaluate("""
                    () => {
                        return document.body.innerText.includes('抖音私信') ||
                               document.body.innerText.includes('消息') ||
                               document.querySelector('[class*="conversation"]') !== null;
                    }
                """)
                if has_im_content:
                    # IM page loaded (detected via JS)
                    im_loaded = True
                else:
                    logger.warning(f"Store {store.store_id}: IM page selectors not found, but continuing anyway")
                    im_loaded = True  # Continue anyway since URL is correct
            except Exception as e:
                logger.warning(f"Store {store.store_id}: Error checking IM page: {e}, continuing anyway")
                im_loaded = True  # Continue anyway

        # Click on "抖音私信" to enter the actual message interface
        # Looking for 抖音私信 button
        await asyncio.sleep(2)

        private_message_selectors = [
            'text=抖音私信',
            'button:has-text("私信")',
            'div:has-text("私信")',
            '[class*="private"]:visible',
            '[class*="message"]:visible',
            'div[role="button"]:has-text("私信")',
        ]

        pm_clicked = False
        for selector in private_message_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for elem in elements:
                    try:
                        is_visible = await elem.is_visible()
                        if is_visible:
                            await elem.click()
                            pm_clicked = True
                            # Clicked 抖音私信 button
                            await asyncio.sleep(3)
                            break
                    except:
                        continue
                if pm_clicked:
                    break
            except Exception as e:
                # PM selector failed
                continue

        if not pm_clicked:
            logger.warning(f"Store {store.store_id}: Could not find 抖音私信 button, may already be on message page")

        # Close any popup dialogs
        await self._close_popup_dialogs(store, page)


    async def _close_popup_dialogs(self, store: Store, page: Page) -> None:
        """Close any popup/modal dialogs on the page."""
        # Closing any popup dialogs
        popup_closed = False
        try:
            # Try multiple strategies to close popups
            # Strategy 1: Direct click on specific buttons
            close_button_texts = ['我知道了', '关闭', '取消', '暂不', 'skip', 'Skip']
            for text in close_button_texts:
                try:
                    # Use page.evaluate to click via JS to avoid overlay issues
                    clicked = await page.evaluate(f"""
                        () => {{
                            const buttons = document.querySelectorAll('button, div[role="button"]');
                            for (const btn of buttons) {{
                                if (btn.textContent.includes('{text}')) {{
                                    btn.click();
                                    return true;
                                }}
                            }}
                            return false;
                        }}
                    """)
                    if clicked:
                        # Closed popup by clicking
                        popup_closed = True
                        await asyncio.sleep(1)
                        break
                except:
                    continue

            # Strategy 2: Try to click close icon
            if not popup_closed:
                try:
                    await page.evaluate("""
                        () => {
                            const closeIcons = document.querySelectorAll('.byted-modal-close, [class*="close"], .icon-close');
                            for (const icon of closeIcons) {
                                if (icon.offsetParent !== null) {
                                    icon.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                    # Closed popup via close icon
                    popup_closed = True
                    await asyncio.sleep(1)
                except:
                    pass

            # Strategy 2.5: Close the "开启智能机器人" promotion modal
            if not popup_closed:
                try:
                    clicked = await page.evaluate("""
                        () => {
                            const closeBtn = document.querySelector('span[class*="life-im-pc-modal-close-icon"]');
                            if (closeBtn && closeBtn.offsetParent !== null) {
                                closeBtn.click();
                                return true;
                            }
                            return false;
                        }
                    """)
                    if clicked:
                        logger.info(f"Store {store.store_id}: 关闭了智能机器人推广弹窗")
                        popup_closed = True
                        await asyncio.sleep(1)
                except:
                    pass

            # Strategy 3: Press Escape key
            if not popup_closed:
                try:
                    await page.keyboard.press('Escape')
                    # Pressed Escape to close popup
                    await asyncio.sleep(1)
                except:
                    pass

        except Exception as e:
            # Error closing popup
            pass

        # Take final screenshot
        await page.screenshot(path=f"storage/debug_im_final_{store.store_id}.png")
        # IM navigation complete


    async def _check_and_refresh_page(self, store: Store) -> None:
        """Check if page needs refresh and perform refresh if needed.
        
        This prevents memory leaks and keeps the page responsive during long-running sessions.
        Only refreshes when:
        1. page_refresh_interval is configured (> 0)
        2. Interval has passed since last refresh
        3. Not currently processing any user (to avoid interrupting replies)
        
        NOTE: After refresh, all locks and conversation states are cleared to ensure clean state.
        """
        refresh_interval = store.config.page_refresh_interval
        
        # Skip if refresh is disabled (0 or negative)
        if refresh_interval <= 0:
            return

        # Skip if currently processing a user
        processing_user = self.get_processing_user(store.store_id)
        if processing_user:
            logger.debug(f"[跳过刷新] 店铺: {store.store_id}, 正在处理 {processing_user}")
            return

        # ===== 关键: 检查是否有未处理完的消息或新消息 =====
        # 1. 检查是否有待处理消息（Handler 队列非空）
        if self.is_any_user_processing(store.store_id):
            logger.debug(f"[跳过刷新] 店铺: {store.store_id}, Handler 队列非空，有未处理完的消息")
            return

        # 2. 检查是否有待处理的 pending 消息
        has_pending = False
        for key in self._pending_messages:
            if key.startswith(f"{store.store_id}_") and self._pending_messages[key]:
                has_pending = True
                break
        if has_pending:
            logger.debug(f"[跳过刷新] 店铺: {store.store_id}, 有待处理消息")
            return

        # 3. 检查侧边栏是否有未读消息
        page = store.page
        if page:
            try:
                unread_conv = await asyncio.wait_for(
                    self._find_first_unread_conversation(page, store),
                    timeout=5.0
                )
                if unread_conv:
                    logger.info(f"[跳过刷新] 店铺: {store.store_id}, 侧边栏有未读消息 ({unread_conv.get('user_name', '?')})")
                    return
            except asyncio.TimeoutError:
                logger.debug(f"[跳过刷新] 店铺: {store.store_id}, 侧边栏扫描超时，继续刷新检查")
            except Exception as e:
                logger.debug(f"[跳过刷新] 店铺: {store.store_id}, 侧边栏扫描异常: {e}")

        now = time.time()
        last_refresh = self._last_page_refresh.get(store.store_id, 0)
        
        # Check if it's time to refresh
        if now - last_refresh < refresh_interval:
            return
        
        # Perform page refresh
        reload_ok = False
        try:
            logger.info(f"[页面刷新] 店铺: {store.store_id}, 距离上次刷新: {int(now - last_refresh)}秒")
            
            page = store.page
            if not page:
                logger.warning(f"[页面刷新] 店铺: {store.store_id}, 页面不存在，跳过刷新")
                return
            
            # Perform reload (stays on current page)
            # Use domcontentloaded instead of networkidle to avoid timeout on pages with persistent background requests
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                reload_ok = True
                logger.info(f"[页面刷新] 店铺: {store.store_id}, 页面已刷新")
                await asyncio.sleep(2)  # Give page time to stabilize after DOM ready
            except Exception as e:
                # If timeout occurred but navigation already happened, treat as success
                err_str = str(e)
                if "Timeout" in err_str and "navigated to" in err_str:
                    logger.warning(f"[页面刷新] 店铺: {store.store_id}, 导航已完成但等待超时，继续执行状态清理")
                    reload_ok = True
                else:
                    logger.error(f"[页面刷新] 店铺: {store.store_id}, 刷新失败: {e}")
        except Exception as e:
            logger.error(f"[页面刷新] 店铺: {store.store_id}, 刷新过程异常: {e}")
        finally:
            # ===== CRITICAL: Clear conversation states after refresh =====
            # Always clear conversation-level states (these are Listener-internal).
            # But only clear processing_user lock if it hasn't been taken by Handler
            # since our check at the top of this method (to avoid racing with Handler).

            # Check if Handler took the lock since our check
            current_processing = self.get_processing_user(store.store_id)
            if current_processing is not None:
                # Handler took the lock — don't touch it, but still clear conversation states
                logger.info(f"[页面刷新] 店铺: {store.store_id}, Handler 正在处理 {current_processing}，保留锁")
            else:
                # Safe to clear — no one took the lock
                logger.debug(f"[页面刷新] 店铺: {store.store_id}, 无处理中用户，清除锁")

            # Always clear current conversation tracking (stale after refresh)
            old_conversation = self._current_conversation.get(store.store_id)
            self._current_conversation[store.store_id] = None
            if old_conversation:
                logger.debug(f"[页面刷新] 店铺: {store.store_id}, 已清除会话跟踪: {old_conversation}")

            # Clear conversation message counts
            self._conversation_message_counts[store.store_id] = 0

            # Clear last message info for this store
            pending_keys_to_remove = []
            for key in self._last_message_info:
                if key.startswith(f"{store.store_id}_"):
                    pending_keys_to_remove.append(key)
            for key in pending_keys_to_remove:
                del self._last_message_info[key]
            if pending_keys_to_remove:
                logger.debug(f"[页面刷新] 店铺: {store.store_id}, 已清除 {len(pending_keys_to_remove)} 条消息状态")

            if reload_ok:
                logger.info(f"[页面刷新] 店铺: {store.store_id}, 刷新完成，状态已重置")

            # Close any popup dialogs after refresh
            try:
                page = store.page
                if page:
                    await self._close_popup_dialogs(store, page)
            except Exception:
                pass

            # Update last refresh time to avoid continuous retry on persistent errors
            self._last_page_refresh[store.store_id] = time.time()


    async def _detect_current_conversation_user(self, page: Page, store_id: str) -> Optional[str]:
        """Detect the currently open conversation user from the page.
        
        This is called on first run to handle the case where a conversation
        is already open by default when entering the IM page.
        
        Returns the user name if a conversation is detected, None otherwise.
        """
        try:
            # Use JavaScript to find user name in the chat header or right panel
            # Note: We use separate string to avoid escaping issues
            js_code = """
            () => {
                // Try multiple selectors for user name
                const selectors = [
                    '[class*="user-info"] [class*="name"]',
                    '[class*="user-info"] h1',
                    '[class*="user-detail"] [class*="name"]',
                    '[class*="user-panel"] [class*="name"]',
                    '[class*="chat-header"] [class*="title"]',
                    '[class*="conversation-header"] [class*="name"]',
                    '[class*="header"] [class*="nickname"]',
                    '[class*="chat-title"]',
                    '[class*="nickname"]',
                    '[class*="user-name"]',
                    '[class*="right"] h1',
                    '[class*="right"] [class*="title"]'
                ];
                
                // Helper to check if text is a time format (relative time)
                const isTimeFormat = (t) => {
                    return /^\\d+分钟前?$/.test(t) || /^\\d+小时前?$/.test(t) || /^\\d+天前?$/.test(t) ||
                           /^\\d+周前?$/.test(t) || /^\\d+月前?$/.test(t) || /^\\d+年前?$/.test(t) ||
                           /^刚刚$/.test(t) ||
                           /^-?\\d+秒$/.test(t) || /^-?\\d+秒前$/.test(t) ||
                           /^<\\d+分钟?$/.test(t) || /^<\\d+小时?$/.test(t) || /^<\\d+天?$/.test(t) ||
                           /^<\\d+$/.test(t) ||
                           /^\\d+分钟内?$/.test(t) || /^\\d+小时内?$/.test(t);
                    // NOTE: HH:MM and YYYY-MM-DD are NOT filtered - could be username
                    // NOTE: Single digits are NOT filtered - could be username
                };
                
                for (const sel of selectors) {
                    const elems = document.querySelectorAll(sel);
                    for (const elem of elems) {
                        const text = (elem.innerText || '').trim();
                        if (text && text.length >= 1 && text.length < 20
                            && text.indexOf('抖音') === -1
                            && text.indexOf('私信') === -1
                            && text.indexOf('咨询') === -1
                            && !isTimeFormat(text)) {
                            return text;
                        }
                    }
                }
                
                // Fallback: look for any text in right panel
                const rightPanel = document.querySelector('[class*="right"], [class*="right-panel"], [class*="user-panel"]');
                if (rightPanel) {
                    const allText = rightPanel.innerText || '';
                    const lines = allText.split('\\n').map(l => l.trim()).filter(l => l);
                    for (const line of lines) {
                        if (line.length >= 1 && line.length < 20
                            && line.indexOf('抖音') === -1
                            && line.indexOf('私信') === -1
                            && line.indexOf('分钟') === -1
                            && line.indexOf('小时') === -1
                            && !isTimeFormat(line)) {
                            return line;
                        }
                    }
                }
                
                return null;
            }
            """
            
            result = await page.evaluate(js_code)
            
            if result:
                # CRITICAL: Filter out time formats that might have slipped through
                if self._is_time_format(result):
                    logger.warning(f"[检测会话] 店铺: {store_id}, 检测到时间格式: '{result}', 忽略")
                    return None
                logger.debug(f"[检测会话] 店铺: {store_id}, 用户: {result}")
                return result
            
            return None
            
        except Exception as e:
            logger.warning(f"[检测默认会话] 店铺: {store_id}, 检测失败: {e}")
            return None

