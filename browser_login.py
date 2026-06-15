#!/usr/bin/env python3
import argparse, os, sys
# 引入 Error 以精确捕获 Playwright 抛出的异常
from playwright.sync_api import sync_playwright, Error as PlaywrightError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(SCRIPT_DIR, "browser_sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

def main():
    parser = argparse.ArgumentParser(description="headful browser login")
    parser.add_argument("--url", required=True)
    parser.add_argument("--session-name", required=True)
    args = parser.parse_args()
    
    url = args.url
    if not url.startswith("http"):
        url = "https://" + url
        
    state_path = os.path.join(SESSION_DIR, f"{args.session_name}.json")
    
    print(f"Opening browser: {url}")
    print(f"Credentials will be saved to: {state_path}")
    print("=" * 60)
    print("Complete login (including captcha) in the browser window.")
    print("Press Enter in THIS terminal after login to save credentials.")
    print("=" * 60)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1366,768"
            ]
        )
        vp = {"width": 1366, "height": 768}
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
        context = browser.new_context(viewport=vp, user_agent=ua)
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "delete navigator.__proto__.webdriver;"
        )
        page = context.new_page()
        
        print(f"Loading URL: {url} ...")
        try:
            # 增加超时错误捕获
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            print("Browser opened successfully.")
        except PlaywrightError as e:
            print(f"\n❌ Error: Failed to load the page (Network Timeout or invalid URL).")
            print(f"Details: {e}")
            browser.close()
            sys.exit(1)

        print("\nSwitch to the browser window to login...")
        try:
            input("Press Enter here AFTER login is complete to save credentials...")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled by user.")
            browser.close()
            sys.exit(0)
            
        try:
            # 尝试保存状态，如果浏览器已被用户手动关闭，会抛出 TargetClosedError 或一般 Error
            context.storage_state(path=state_path)
            print(f"✅ Saved credentials successfully: {state_path}")
        except PlaywrightError:
            print("\n❌ Error: The browser window was closed before saving. Credentials NOT saved.")
        except Exception as e:
            print(f"\n❌ Unexpected error saving credentials: {e}")
        finally:
            try:
                browser.close()
            except Exception:
                pass # 忽略关闭时的异常

if __name__ == "__main__":
    main()
