#!/usr/bin/env python3
"""
Script to extract and save cookies from a logged-in store session.
Run this after successfully logging in to save cookies for future use.
"""
import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright
from config import settings, StoreConfig


async def extract_cookies(store_id: str = None):
    """Extract cookies from a logged-in browser session."""
    print("="*60)
    print(" EXTRACT COOKIES FROM LOGGED-IN SESSION ")
    print("="*60)

    # Find store config
    store_config = None
    if store_id:
        for s in settings.stores:
            if s.store_id == store_id:
                store_config = s
                break

    if not store_config:
        # Auto-select first enabled store
        for s in settings.stores:
            if s.enabled:
                store_config = s
                break

        # If still no store, use first one
        if not store_config and settings.stores:
            store_config = settings.stores[0]
        elif not store_config:
            print("No stores configured!")
            return

    print(f"\nExtracting cookies for: {store_config.name} ({store_config.store_id})")
    print("This will open a browser. Please:")
    print("1. Wait for the page to load")
    print("2. Login if not already logged in")
    print("3. The cookies will be automatically extracted and saved")
    print("4. Press Ctrl+C when done\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        # Navigate to Douyin Kele
        await page.goto("https://life.douyin.com/", wait_until="load")

        print("Browser opened. Please login if needed.")
        print("Press Enter when you're logged in and want to save cookies...")
        print("(Or wait 60 seconds for auto-extraction)\n")

        # Wait for user to login
        try:
            await page.wait_for_selector('[class*="avatar"], [class*="dashboard"], .im-list', timeout=60000)
            print("[OK] Login detected!")
        except:
            print("[Timeout] Extracting cookies anyway...")

        # Extract cookies
        cookies = await context.cookies()

        # Format as cookie string
        cookie_dict = {c['name']: c['value'] for c in cookies if 'douyin.com' in c.get('domain', '')}
        cookie_string = '; '.join(f"{k}={v}" for k, v in cookie_dict.items())

        print("\n" + "="*60)
        print(" COOKIES EXTRACTED ")
        print("="*60)
        print(f"\nStore: {store_config.name}")
        print(f"Store ID: {store_config.store_id}")
        print(f"\nCookie String ({len(cookie_string)} chars):")
        print(cookie_string[:200] + "..." if len(cookie_string) > 200 else cookie_string)

        # Save to file
        cookie_file = f"storage/cookies_{store_config.store_id}.txt"
        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write(cookie_string)
        print(f"\n[OK] Cookies saved to: {cookie_file}")

        # Also print full JSON for debugging
        cookie_json = json.dumps(cookie_dict, indent=2, ensure_ascii=False)
        print(f"\nCookie Details:")
        print(cookie_json)

        # Update accounts.yaml suggestion
        print("\n" + "="*60)
        print(" UPDATE YOUR accounts.yaml: ")
        print("="*60)
        print(f"""
stores:
  - store_id: "{store_config.store_id}"
    name: "{store_config.name}"
    login_type: "cookie"
    cookies: "{cookie_string[:100]}..."
    # ... rest of config
        """)

        await browser.close()


if __name__ == "__main__":
    store_id = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        asyncio.run(extract_cookies(store_id))
    except KeyboardInterrupt:
        print("\n\nExited by user")
        sys.exit(0)
