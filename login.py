#!/usr/bin/env python3
"""
抖音来客登录脚本

使用方法:
    python login.py

功能:
    1. 自动打开抖音来客登录页面
    2. 支持扫码/手机号/Cookie登录
    3. 登录成功后自动获取Cookie
    4. 保存到配置文件 config/accounts.yaml
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Optional, Dict, Any

import yaml
from playwright.async_api import async_playwright, Page, Browser


# 配置路径
CONFIG_DIR = Path(__file__).parent / "config"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.yaml"

# 抖音来客相关URL
KELE_URL = "https://life.douyin.com/"
LOGIN_URL = "https://life.douyin.com/"


class KeleLogin:
    """抖音来客登录工具"""
    
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        
    async def __aenter__(self):
        """异步上下文管理器入口"""
        self.playwright = await async_playwright().start()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        if self.browser:
            await self.browser.close()
        await self.playwright.stop()
    
    async def launch_browser(self, headless: bool = False):
        """启动浏览器"""
        print("正在启动浏览器...")
        self.browser = await self.playwright.chromium.launch(
            headless=headless,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = await self.browser.new_context(
            viewport={'width': 1280, 'height': 800}
        )
        self.page = await context.new_page()
        
        # 隐藏webdriver特征
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        print("浏览器启动完成")
    
    async def login_by_qrcode(self) -> Optional[str]:
        """
        使用二维码登录
        
        Returns:
            登录成功返回cookie字符串，失败返回None
        """
        print("\n=== 二维码登录 ===")
        print("1. 正在打开登录页面...")
        
        await self.page.goto(LOGIN_URL, wait_until="networkidle")
        await asyncio.sleep(3)
        
        # 等待二维码出现
        print("2. 等待二维码加载...")
        try:
            # 尝试多种可能的二维码选择器
            qr_selectors = [
                'img[src*="qrcode"]',
                '.qrcode-img',
                '[class*="qrcode"] img',
                'canvas',
                '.byted-qrcode-image'
            ]
            
            qr_found = False
            for selector in qr_selectors:
                try:
                    await self.page.wait_for_selector(selector, timeout=5000)
                    qr_found = True
                    print(f"   找到二维码元素: {selector}")
                    break
                except:
                    continue
            
            if not qr_found:
                print("   未自动找到二维码，请查看浏览器窗口手动扫码")
            
        except Exception as e:
            print(f"   等待二维码超时: {e}")
        
        # 等待用户扫码登录
        print("\n3. 请使用抖音APP扫描二维码登录")
        print("   (等待5分钟，请尽快扫码...)")
        
        # 循环检查登录状态
        for i in range(300):  # 5分钟 = 300秒
            await asyncio.sleep(1)
            
            # 检查是否已登录（通过URL或特定元素判断）
            current_url = self.page.url
            if "life.douyin.com" in current_url and "login" not in current_url.lower():
                print(f"   检测到登录成功！")
                break
                
            # 检查是否有用户信息元素
            try:
                user_element = await self.page.query_selector('[class*="user-name"], [class*="avatar"], .header-user')
                if user_element:
                    print(f"   检测到用户元素，登录成功！")
                    break
            except:
                pass
            
            # 每分钟显示一次剩余时间
            if (i + 1) % 60 == 0:
                remaining = 5 - (i + 1) // 60
                print(f"   已等待 {i+1} 秒，还剩 {remaining} 分钟...")
        else:
            print("\n   登录超时 (5分钟)")
            return None
        
        # 获取cookie
        print("4. 正在获取登录凭证...")
        cookies = await self._get_cookies()
        
        if cookies:
            print(f"   成功获取 {len(cookies)} 个cookie")
            return self._format_cookies(cookies)
        else:
            print("   获取cookie失败")
            return None
    
    async def login_by_phone(self, phone: str) -> Optional[str]:
        """
        使用手机号登录
        
        Args:
            phone: 手机号
            
        Returns:
            登录成功返回cookie字符串，失败返回None
        """
        print(f"\n=== 手机号登录 ({phone}) ===")
        print("1. 正在打开登录页面...")
        
        await self.page.goto(LOGIN_URL, wait_until="networkidle")
        await asyncio.sleep(2)
        
        # 尝试切换到手机号登录
        print("2. 尝试切换到手机号登录...")
        try:
            # 点击切换到手机号登录
            phone_tab_selectors = [
                'text=手机登录',
                'text=手机号登录',
                '[class*="phone"]',
                'div:has-text("手机号")'
            ]
            
            for selector in phone_tab_selectors:
                try:
                    await self.page.click(selector, timeout=3000)
                    print(f"   已切换到手机号登录")
                    await asyncio.sleep(1)
                    break
                except:
                    continue
        except Exception as e:
            print(f"   切换登录方式失败: {e}")
        
        # 输入手机号
        print("3. 输入手机号...")
        try:
            phone_input_selectors = [
                'input[type="tel"]',
                'input[placeholder*="手机"]',
                'input[name="phone"]',
                '[class*="phone"] input'
            ]
            
            for selector in phone_input_selectors:
                try:
                    await self.page.fill(selector, phone, timeout=3000)
                    print(f"   已输入手机号")
                    break
                except:
                    continue
        except Exception as e:
            print(f"   输入手机号失败: {e}")
        
        # 等待用户输入验证码
        print("\n4. 请手动输入验证码并完成登录")
        print("   (等待5分钟，请尽快完成...)")
        
        # 循环检查登录状态
        for i in range(300):  # 5分钟 = 300秒
            await asyncio.sleep(1)
            
            current_url = self.page.url
            if "life.douyin.com" in current_url and "login" not in current_url.lower():
                print(f"   检测到登录成功！")
                break
            
            # 每分钟显示一次剩余时间
            if (i + 1) % 60 == 0:
                remaining = 5 - (i + 1) // 60
                print(f"   已等待 {i+1} 秒，还剩 {remaining} 分钟...")
        else:
            print("\n   登录超时 (5分钟)")
            return None
        
        # 获取cookie
        print("5. 正在获取登录凭证...")
        cookies = await self._get_cookies()
        
        if cookies:
            print(f"   成功获取 {len(cookies)} 个cookie")
            return self._format_cookies(cookies)
        else:
            print("   获取cookie失败")
            return None
    
    async def _get_cookies(self) -> list:
        """获取当前页面的所有cookie"""
        try:
            context = self.page.context
            cookies = await context.cookies()
            return cookies
        except Exception as e:
            print(f"获取cookie失败: {e}")
            return []
    
    def _format_cookies(self, cookies: list) -> str:
        """将cookie列表格式化为字符串"""
        # 只保留关键cookie
        important_keys = [
            'sessionid', 'sessionid_ss', 'sid_guard', 'sid_tt',
            'uid_tt', 'uid_tt_ss', 'ssid_ucp_v1', 'passport_csrf_token'
        ]
        
        cookie_dict = {}
        for cookie in cookies:
            name = cookie.get('name', '')
            if name in important_keys or any(key in name.lower() for key in important_keys):
                cookie_dict[name] = cookie.get('value', '')
        
        # 如果没有关键cookie，使用所有非空cookie
        if not cookie_dict:
            for cookie in cookies:
                name = cookie.get('name', '')
                value = cookie.get('value', '')
                if name and value:
                    cookie_dict[name] = value
        
        # 格式化为字符串: key=value; key2=value2
        cookie_str = '; '.join([f"{k}={v}" for k, v in cookie_dict.items()])
        return cookie_str
    
    def _parse_cookie_to_dict(self, cookie_str: str) -> Dict[str, str]:
        """将cookie字符串解析为字典"""
        result = {}
        if not cookie_str:
            return result
            
        for item in cookie_str.split(';'):
            item = item.strip()
            if '=' in item:
                key, value = item.split('=', 1)
                result[key.strip()] = value.strip()
        return result


def load_existing_accounts() -> list:
    """加载现有的账户配置"""
    if not ACCOUNTS_FILE.exists():
        return []
    
    try:
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            if data and 'stores' in data:
                return data['stores']
    except Exception as e:
        print(f"读取现有配置失败: {e}")
    
    return []


def save_accounts(accounts: list):
    """保存账户配置到YAML文件"""
    # 确保配置目录存在
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    # 构建YAML数据
    data = {'stores': accounts}
    
    # 保存到文件
    with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False)
    
    print(f"\n配置已保存到: {ACCOUNTS_FILE}")


def get_next_store_id(existing_accounts: list) -> str:
    """获取下一个店铺ID"""
    if not existing_accounts:
        return "store_001"
    
    # 提取现有ID中的数字
    max_num = 0
    for account in existing_accounts:
        store_id = account.get('store_id', '')
        match = re.search(r'(\d+)', store_id)
        if match:
            num = int(match.group(1))
            max_num = max(max_num, num)
    
    return f"store_{max_num + 1:03d}"


async def main():
    """主函数"""
    print("=" * 60)
    print("抖音来客自动登录工具")
    print("=" * 60)
    
    # 选择登录方式
    print("\n请选择登录方式:")
    print("1. 二维码登录 (推荐)")
    print("2. 手机号登录")
    print("3. 手动输入Cookie")
    
    choice = input("\n请输入选项 (1/2/3): ").strip() or "1"
    
    # 创建登录实例
    async with KeleLogin() as login:
        cookie_str = None
        
        if choice == "1":
            # 二维码登录
            await login.launch_browser(headless=False)
            cookie_str = await login.login_by_qrcode()
            
        elif choice == "2":
            # 手机号登录
            phone = input("请输入手机号: ").strip()
            if not phone:
                print("手机号不能为空")
                return
            
            await login.launch_browser(headless=False)
            cookie_str = await login.login_by_phone(phone)
            
        elif choice == "3":
            # 手动输入Cookie
            print("\n请从浏览器开发者工具中复制Cookie字符串")
            print("(按Ctrl+V粘贴，然后按Enter)")
            cookie_str = input("Cookie: ").strip()
            
        else:
            print("无效的选项")
            return
        
        if not cookie_str:
            print("\n登录失败，未获取到Cookie")
            return
        
        # 显示获取到的cookie（部分隐藏）
        print("\n" + "=" * 60)
        print("获取到的Cookie (部分隐藏):")
        display_cookie = cookie_str[:50] + "..." if len(cookie_str) > 50 else cookie_str
        print(display_cookie)
        print("=" * 60)
        
        # 加载现有配置
        existing_accounts = load_existing_accounts()
        
        # 输入店铺信息
        print("\n请输入店铺信息:")
        store_id = get_next_store_id(existing_accounts)
        name = input(f"店铺名称 (默认: 店铺{len(existing_accounts)+1}): ").strip() or f"店铺{len(existing_accounts)+1}"
        default_api = "http://localhost:8000/api/chat/reply"
        api_endpoint = input(f"API地址 (默认: {default_api}): ").strip() or default_api
        
        # 创建新账户配置
        new_account = {
            'store_id': store_id,
            'name': name,
            'login_type': 'cookie',
            'cookies': cookie_str,
            'api_endpoint': api_endpoint,
            'api_timeout': 10,
            'reply_delay': 1.0,
            'enabled': True,
            'headless': True
        }
        
        # 检查是否已存在相同cookie的账户
        for i, account in enumerate(existing_accounts):
            if account.get('cookies') == cookie_str:
                print(f"\n警告: 该Cookie已存在于账户 {account.get('store_id')} 中")
                update = input("是否更新现有账户? (y/n): ").strip().lower()
                if update == 'y':
                    existing_accounts[i] = {**account, **new_account}
                    print(f"已更新账户: {account.get('store_id')}")
                break
        else:
            # 添加新账户
            existing_accounts.append(new_account)
            print(f"\n已添加新账户: {store_id}")
        
        # 保存配置
        save_accounts(existing_accounts)
        
        print("\n" + "=" * 60)
        print("登录配置完成!")
        print(f"配置文件: {ACCOUNTS_FILE}")
        print("\n现在可以运行主程序:")
        print("  python main.py")
        print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n程序被用户中断")
        sys.exit(0)
    except Exception as e:
        print(f"\n程序出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
