#!/usr/bin/env python3
"""
初始化万方登录态
和知网一样用 Playwright Persistent Context，登录一次后自动保存
"""
import os

PROFILE_DIR = os.path.expanduser("~/.agent_wanfang_profile")
WANFANG_LOGIN_URL = "https://www.wanfangdata.com.cn/"

print("=" * 60)
print("万方数据 登录初始化")
print("=" * 60)
print()
print(f"Profile 目录: {PROFILE_DIR}")
print()
print("即将打开浏览器窗口，请手动完成登录：")
print("  1. 用你的万方账号登录（或机构账号）")
print("  2. 登录成功后，登录按钮消失/用户名出现即完成")
print("  3. 按 Ctrl+C 关闭浏览器，登录态自动保存")
print()

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        viewport={"width": 1280, "height": 800},
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    )
    
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(WANFANG_LOGIN_URL, wait_until="domcontentloaded")
    
    print(">>> 请在弹出的浏览器中完成登录...")
    print(">>> 登录成功后按 Ctrl+C 退出\n")
    
    try:
        while True:
            import time
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在关闭浏览器并保存登录态...")
        context.close()

print()
print("=" * 60)
if os.path.isdir(PROFILE_DIR):
    print(f"✅ 万方登录态已保存到: {PROFILE_DIR}")
    print("   后续可复用此登录态搜索和下载万方论文")
else:
    print("❌ 登录态保存失败")
print("=" * 60)