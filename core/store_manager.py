"""Store account manager for handling multiple store credentials."""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List

from playwright.async_api import async_playwright, BrowserContext, Page, Browser

from config import StoreConfig, settings
from utils.logger import get_logger
from utils.helpers import parse_cookie_string

logger = get_logger(__name__)


class StoreStatus(Enum):
    """Store connection status."""
    OFFLINE = "offline"
    CONNECTING = "connecting"
    ONLINE = "online"
    ERROR = "error"
    RECONNECTING = "reconnecting"


@dataclass
class Store:
    """Store instance with browser context."""
    config: StoreConfig
    status: StoreStatus = StoreStatus.OFFLINE
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    browser: Optional[Browser] = None
    last_activity: Optional[datetime] = None
    error_count: int = 0
    total_messages: int = 0
    total_replies: int = 0
    metadata: Dict = field(default_factory=dict)
    # ===== 关键修复: 页面级锁 =====
    # 用于保护所有页面操作，确保同一时刻只有一个协程可以操作页面
    _page_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def store_id(self) -> str:
        return self.config.store_id

    @property
    def is_online(self) -> bool:
        return self.status == StoreStatus.ONLINE and self.page is not None

    def update_status(self, status: StoreStatus, error: Optional[str] = None) -> None:
        """Update store status."""
        old_status = self.status
        self.status = status
        self.last_activity = datetime.now()

        if status == StoreStatus.ERROR:
            self.error_count += 1

        logger.info(f"Store {self.store_id} status: {old_status.value} -> {status.value}")
        if error:
            logger.error(f"Store {self.store_id} error: {error}")


class StoreManager:
    """Manager for multiple store accounts."""

    def __init__(self):
        self.stores: Dict[str, Store] = {}
        self._playwright = None
        self._browser_pool = None

    async def initialize(self) -> None:
        """Initialize the playwright instance."""
        self._playwright = await async_playwright().start()
        logger.info("Playwright initialized")

    async def shutdown(self) -> None:
        """Shutdown all stores and playwright."""
        # Close all store contexts
        for store in self.stores.values():
            await self._close_store(store)

        # Stop playwright
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        logger.info("StoreManager shutdown complete")

    async def add_store(self, config: StoreConfig) -> Store:
        """Add a store and initialize its browser context."""
        if config.store_id in self.stores:
            logger.warning(f"Store {config.store_id} already exists, removing old instance")
            await self._close_store(self.stores[config.store_id])

        store = Store(config=config)
        self.stores[config.store_id] = store

        if config.enabled:
            await self._initialize_store(store)

        return store

    async def remove_store(self, store_id: str) -> None:
        """Remove a store."""
        if store_id in self.stores:
            await self._close_store(self.stores[store_id])
            del self.stores[store_id]
            logger.info(f"Store {store_id} removed")

    async def _initialize_store(self, store: Store) -> None:
        """Initialize browser context for a store."""
        store.update_status(StoreStatus.CONNECTING)

        try:
            # Launch browser with minimal args to avoid crashes
            # Use system Chrome to avoid downloading Chromium
            browser = await self._playwright.chromium.launch(
                headless=store.config.headless,
                channel='chrome',
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--no-first-run',
                    '--no-zygote',
                    '--disable-gpu',
                ]
            )
            store.browser = browser

            # Create context with cookies if available
            context_options = {
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }

            context = await browser.new_context(**context_options)
            store.context = context

            # Add cookies if login type is cookie
            if store.config.login_type == "cookie" and store.config.cookies:
                cookies = []
                cookie_dict = parse_cookie_string(store.config.cookies)
                for name, value in cookie_dict.items():
                    cookies.append({
                        "name": name,
                        "value": value,
                        "domain": ".douyin.com",
                        "path": "/",
                    })
                await context.add_cookies(cookies)
                logger.info(f"Store {store.store_id}: Added {len(cookies)} cookies to context")

            # Create page and navigate to login
            page = await context.new_page()
            store.page = page

            # Inject stealth script to avoid detection
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
            """)

            # Navigate to Douyin Kele
            logger.info(f"Store {store.store_id}: Navigating to {settings.kele_login_url}")
            try:
                # Use networkidle to ensure page is fully loaded
                await page.goto(settings.kele_login_url, wait_until="networkidle", timeout=60000)
            except Exception as e:
                logger.warning(f"Store {store.store_id}: Navigation timeout, continuing anyway: {e}")
            
            # Wait for page content to load
            logger.info(f"Store {store.store_id}: Waiting for page content to load...")
            await asyncio.sleep(5)
            
            # Check if page actually loaded content
            try:
                body_content = await page.evaluate("() => document.body.innerText.length")
                logger.info(f"Store {store.store_id}: Page body content length: {body_content}")
                if body_content < 100:
                    logger.warning(f"Store {store.store_id}: Page appears to be blank, retrying...")
                    # Retry navigation
                    await page.reload(wait_until="networkidle")
                    await asyncio.sleep(5)
            except Exception as e:
                logger.warning(f"Store {store.store_id}: Could not check page content: {e}")

            # Check if already logged in
            if await self._check_login_status(store):
                store.update_status(StoreStatus.ONLINE)
                logger.info(f"Store {store.store_id}: Logged in successfully")
            else:
                if store.config.login_type == "qrcode":
                    await self._handle_qrcode_login(store)
                elif store.config.login_type == "phone":
                    await self._handle_phone_login(store)
                else:
                    store.update_status(StoreStatus.ERROR, "Cookie login failed")

        except Exception as e:
            store.update_status(StoreStatus.ERROR, str(e))
            logger.exception(f"Store {store.store_id}: Initialization failed")

    async def _close_store(self, store: Store) -> None:
        """Close a store's browser context."""
        if store.context:
            await store.context.close()
            store.context = None
        if store.browser:
            await store.browser.close()
            store.browser = None
        store.page = None
        store.update_status(StoreStatus.OFFLINE)

    async def _check_login_status(self, store: Store) -> bool:
        """Check if the store is logged in.

        Returns:
            True if logged in
        """
        try:
            page = store.page
            if not page:
                return False

            # Wait a bit for page to stabilize
            await asyncio.sleep(3)

            # Method 1: Check if URL indicates logged in state
            current_url = page.url
            if "life.douyin.com" in current_url and "login" not in current_url.lower():
                # Check if we can find common logged-in page elements
                logged_in_indicators = [
                    '[class*="avatar"]',  # User avatar
                    '[class*="user-name"]',  # User name
                    '[class*="dashboard"]',  # Dashboard
                    '[class*="header"]',  # Header area
                    'nav',  # Navigation
                    '[class*="nav"]',  # Navigation classes
                    'button',  # Any button (login page usually has few buttons)
                    '[class*="menu"]',  # Menu elements
                ]
                
                for selector in logged_in_indicators:
                    try:
                        elements = await page.query_selector_all(selector)
                        if len(elements) > 0:
                            logger.info(f"Store {store.store_id}: Login detected via selector: {selector} ({len(elements)} elements)")
                            return True
                    except:
                        continue

            # Method 2: Check if we see login form elements (if found, not logged in)
            login_form_indicators = [
                'input[type="tel"]',  # Phone input
                'input[placeholder*="手机"]',  # Phone placeholder
                'text=手机号登录',  # Login text
                'text=验证码',  # Verification code
                '[class*="login-form"]',  # Login form
                '[class*="qrcode"]',  # QR code (login page)
            ]
            
            login_elements_found = 0
            for selector in login_form_indicators:
                try:
                    elements = await page.query_selector_all(selector)
                    visible_count = 0
                    for el in elements:
                        if await el.is_visible():
                            visible_count += 1
                    if visible_count > 0:
                        login_elements_found += 1
                        logger.debug(f"Store {store.store_id}: Login form indicator found: {selector}")
                except:
                    continue
            
            # If we found multiple login indicators, we're not logged in
            if login_elements_found >= 2:
                logger.info(f"Store {store.store_id}: Not logged in (found {login_elements_found} login indicators)")
                return False

            # Method 3: Check via JavaScript for page content
            try:
                has_logged_in_content = await page.evaluate("""
                    () => {
                        // Check for common logged-in page elements
                        const hasAvatar = document.querySelector('[class*="avatar"]') !== null;
                        const hasUserMenu = document.querySelector('[class*="user-menu"], [class*="account"]') !== null;
                        const hasNav = document.querySelector('nav, [class*="nav"], [class*="header"]') !== null;
                        const hasContent = document.querySelectorAll('div').length > 50;  // Rich content indicates logged-in page
                        
                        // Check for login form indicators
                        const hasPhoneInput = document.querySelector('input[type="tel"], input[placeholder*="手机"]') !== null;
                        const hasQrcode = document.querySelector('[class*="qrcode"]') !== null;
                        const hasLoginText = document.body.innerText.includes('手机号登录') || 
                                            document.body.innerText.includes('验证码登录');
                        
                        return {
                            loggedIn: hasAvatar || hasUserMenu || (hasNav && hasContent),
                            indicators: {hasAvatar, hasUserMenu, hasNav, hasContent},
                            loginForm: {hasPhoneInput, hasQrcode, hasLoginText}
                        };
                    }
                """)
                
                if has_logged_in_content.get('loggedIn') and not has_logged_in_content.get('loginForm', {}).get('hasPhoneInput'):
                    logger.info(f"Store {store.store_id}: Login detected via JavaScript check")
                    return True
                    
            except Exception as e:
                logger.debug(f"Store {store.store_id}: JavaScript login check failed: {e}")

            # Method 4: If page loaded successfully with douyin.com domain and no obvious login indicators
            # Assume we're logged in (relaxed check)
            if "life.douyin.com" in current_url:
                logger.info(f"Store {store.store_id}: Assuming logged in (page loaded on douyin domain)")
                return True

            return False

        except Exception as e:
            logger.error(f"Store {store.store_id}: Login check failed: {e}")
            return False

    async def _handle_qrcode_login(self, store: Store) -> None:
        """Handle QR code login flow."""
        logger.info(f"Store {store.store_id}: Waiting for QR code scan...")

        try:
            page = store.page

            # Wait for QR code to appear
            # Note: These selectors need to be adjusted based on actual page
            qr_selector = '[class*="qrcode"] img, .qrcode-image, [data-e2e="qrcode"]'
            await page.wait_for_selector(qr_selector, timeout=30000)

            # Take screenshot of QR code for user to scan
            qr_element = await page.query_selector(qr_selector)
            if qr_element:
                screenshot_path = f"storage/qrcode_{store.store_id}.png"
                await qr_element.screenshot(path=screenshot_path)
                logger.info(f"Store {store.store_id}: QR code saved to {screenshot_path}")
                print(f"\n{'='*50}")
                print(f"请扫描二维码登录店铺: {store.config.name}")
                print(f"QR Code saved to: {screenshot_path}")
                print(f"{'='*50}\n")

            # Wait for login to complete
            max_wait = 120  # 2 minutes
            for i in range(max_wait):
                if await self._check_login_status(store):
                    store.update_status(StoreStatus.ONLINE)
                    logger.info(f"Store {store.store_id}: QR code login successful")
                    return
                await asyncio.sleep(1)

            store.update_status(StoreStatus.ERROR, "QR code login timeout")

        except Exception as e:
            store.update_status(StoreStatus.ERROR, f"QR code login failed: {e}")

    async def _handle_phone_login(self, store: Store) -> None:
        """Handle phone SMS login flow."""
        logger.info(f"Store {store.store_id}: Starting phone login...")

        if not store.config.phone:
            store.update_status(StoreStatus.ERROR, "Phone number not configured")
            return

        try:
            page = store.page

            # Take screenshot for debugging
            debug_screenshot = f"storage/debug_login_{store.store_id}.png"
            await page.screenshot(path=debug_screenshot)
            logger.info(f"Store {store.store_id}: Login page screenshot saved to {debug_screenshot}")

            # Wait for phone login tab/button and click it
            # Note: These selectors need to be adjusted based on actual page
            phone_tab_selectors = [
                'text=手机号登录',
                'text=短信登录',
                'text=手机登录',
                '[class*="phone"]',
                '[class*="sms"]',
                'button:has-text("手机")',
                'div[role="tab"]:has-text("手机")',
                '.login-tab:has-text("手机")',
            ]

            phone_tab_clicked = False
            for selector in phone_tab_selectors:
                try:
                    tab = await page.wait_for_selector(selector, timeout=3000)
                    if tab:
                        await tab.click()
                        phone_tab_clicked = True
                        logger.info(f"Store {store.store_id}: Clicked phone login tab with selector: {selector}")
                        await asyncio.sleep(3)  # Wait for form to load
                        break
                except Exception as e:
                    logger.debug(f"Selector {selector} not found: {e}")
                    continue

            if not phone_tab_clicked:
                logger.warning(f"Store {store.store_id}: Could not find phone login tab, may already be on phone login page")
                # Try to find phone input directly - might already be on phone login page

            # Wait longer for phone login form to appear after clicking tab
            await asyncio.sleep(3)

            # Take screenshot after clicking phone tab
            await page.screenshot(path=f"storage/debug_phone_tab_{store.store_id}.png")

            # Fill phone number - try more generic selectors
            phone_input_selectors = [
                'input[type="tel"]',
                'input[type="text"][placeholder*="手机"]',
                'input[type="text"][placeholder*="电话"]',
                'input[placeholder*="手机"]',
                'input[placeholder*="phone"]',
                'input[placeholder*="手机号"]',
                'input[name*="phone"]',
                'input[name*="mobile"]',
                '[class*="phone"] input[type="text"]',
                '[class*="phone"] input[type="tel"]',
                'form input:first-of-type',  # First input in form
                'input[maxlength="11"]',  # Phone number is typically 11 digits
                'input',  # Any input as last resort
            ]

            phone_input = None
            used_selector = None
            for selector in phone_input_selectors:
                try:
                    # Try to find visible input
                    elements = await page.query_selector_all(selector)
                    for elem in elements:
                        is_visible = await elem.is_visible()
                        if is_visible:
                            phone_input = elem
                            used_selector = selector
                            break
                    if phone_input:
                        break
                except Exception as e:
                    logger.debug(f"Phone input selector {selector} failed: {e}")
                    continue

            if not phone_input:
                # List all inputs for debugging
                all_inputs = await page.query_selector_all('input')
                logger.error(f"Store {store.store_id}: Could not find phone input field. Found {len(all_inputs)} total inputs on page.")
                for i, inp in enumerate(all_inputs[:5]):
                    try:
                        input_type = await inp.get_attribute('type')
                        input_placeholder = await inp.get_attribute('placeholder')
                        input_name = await inp.get_attribute('name')
                        input_class = await inp.get_attribute('class')
                        logger.error(f"  Input {i}: type={input_type}, placeholder={input_placeholder}, name={input_name}, class={input_class}")
                    except:
                        pass
                store.update_status(StoreStatus.ERROR, "Could not find phone input field - check debug screenshot in storage/ folder")
                return

            logger.info(f"Store {store.store_id}: Found phone input using selector: {used_selector}")

            # Clear and enter phone number
            await phone_input.click()
            await phone_input.fill("")
            await asyncio.sleep(0.5)
            await phone_input.type(store.config.phone, delay=50)  # Type slowly like human
            logger.info(f"Store {store.store_id}: Entered phone number {store.config.phone}")

            # Click get verification code button
            code_btn_selectors = [
                'text=获取验证码',
                'text=发送验证码',
                'button:has-text("获取")',
                'button:has-text("发送")',
                '[class*="code-btn"]',
                '[class*="verify-btn"]',
                '[class*="send-btn"]',
                'button:has-text("验证码")',
                'button[type="button"]',  # Generic button
            ]

            code_btn_clicked = False
            used_btn_selector = None
            for selector in code_btn_selectors:
                try:
                    buttons = await page.query_selector_all(selector)
                    for btn in buttons:
                        is_visible = await btn.is_visible()
                        is_enabled = await btn.is_enabled()
                        if is_visible and is_enabled:
                            await btn.click()
                            code_btn_clicked = True
                            used_btn_selector = selector
                            logger.info(f"Store {store.store_id}: Clicked verification code button using selector: {selector}")
                            await asyncio.sleep(2)  # Wait for SMS to be sent
                            break
                    if code_btn_clicked:
                        break
                except Exception as e:
                    logger.debug(f"Code button selector {selector} failed: {e}")
                    continue

            if not code_btn_clicked:
                logger.warning(f"Store {store.store_id}: Could not find verification code button - user may need to click manually")
                # Continue anyway - user might click manually

            # Prompt user for verification code
            print(f"\n{'='*60}")
            print(f" 手机验证码登录 - 店铺: {store.config.name}")
            print(f"{'='*60}")
            print(f" 手机号: {store.config.phone}")
            if code_btn_clicked:
                print(f" 验证码已发送，请查看手机...")
            else:
                print(f" 请手动点击'获取验证码'按钮...")
            print(f" 请在浏览器窗口中输入验证码")
            print(f"{'='*60}\n")

            # Take screenshot after sending code
            await page.screenshot(path=f"storage/debug_waiting_code_{store.store_id}.png")

            # Wait for SMS code input - user will enter it manually in browser
            # We just need to detect when login is successful
            logger.info(f"Store {store.store_id}: Waiting for user to enter verification code...")

            max_wait = 300  # 5 minutes to enter code
            for i in range(max_wait):
                # Check if already logged in
                if await self._check_login_status(store):
                    store.update_status(StoreStatus.ONLINE)
                    logger.info(f"Store {store.store_id}: Phone login successful")
                    return

                # Every 30 seconds, remind user
                if i % 30 == 0 and i > 0:
                    print(f"  等待验证码中... ({i}秒)")

                await asyncio.sleep(1)

            store.update_status(StoreStatus.ERROR, "Phone login timeout - user did not enter code in time")

        except Exception as e:
            store.update_status(StoreStatus.ERROR, f"Phone login failed: {e}")
            logger.exception(f"Store {store.store_id}: Phone login error")

    async def refresh_store(self, store_id: str) -> bool:
        """Refresh a store's connection.

        Returns:
            True if successful
        """
        if store_id not in self.stores:
            logger.error(f"Store {store_id} not found")
            return False

        store = self.stores[store_id]
        await self._close_store(store)
        await asyncio.sleep(2)
        await self._initialize_store(store)

        return store.is_online

    def get_store(self, store_id: str) -> Optional[Store]:
        """Get a store by ID."""
        return self.stores.get(store_id)

    def get_online_stores(self) -> List[Store]:
        """Get all online stores."""
        return [s for s in self.stores.values() if s.is_online]

    def get_all_stores(self) -> List[Store]:
        """Get all stores."""
        return list(self.stores.values())

    async def health_check(self) -> Dict[str, any]:
        """Check health of all stores."""
        results = {
            "total": len(self.stores),
            "online": 0,
            "offline": 0,
            "error": 0,
            "stores": {}
        }

        for store_id, store in self.stores.items():
            if store.is_online:
                results["online"] += 1
            elif store.status == StoreStatus.OFFLINE:
                results["offline"] += 1
            else:
                results["error"] += 1

            results["stores"][store_id] = {
                "status": store.status.value,
                "error_count": store.error_count,
                "total_messages": store.total_messages,
                "total_replies": store.total_replies,
            }

        return results
