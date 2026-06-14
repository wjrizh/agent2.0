import os
import time
import urllib.parse
import re
import random
import subprocess
import signal
import json
from .core import BaseTool

# 如果缺少库，请在服务器执行： pip install playwright-stealth markdownify
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth # 🚀 修复点 1：导入 Stealth 类
import markdownify

# ==========================================
# 模块 1：基础设施与环境常量
# ==========================================

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]

# 1x1 透明 GIF，用于拦截并 Mock 图片请求
TRANSPARENT_GIF = b"R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
SESSION_DIR = "./browser_sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

class XvfbManager:
    """Xvfb 虚拟显示器进程管理器"""
    def __init__(self, display=":99", size="1366x768x24"):
        self.display = display
        self.size = size
        self.proc = None

    def __enter__(self):
        # preexec_fn=os.setsid 确保子进程拥有独立进程组，方便 killpg 一键清理
        self.proc = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", self.size, "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        os.environ["DISPLAY"] = self.display
        time.sleep(0.5) # 给 Xvfb 留出启动时间
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.proc:
            try:
                # 杀死整个进程组，绝对不留僵尸进程
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.proc.wait()


# ==========================================
# 模块 2：拟人交互引擎 (Human Simulator)
# ==========================================

class HumanSimulator:
    @staticmethod
    def bezier_curve(start, end, control, steps):
        """三次贝塞尔曲线插值"""
        points = []
        for i in range(steps):
            t = i / (steps - 1)
            x = (1-t)**3 * start[0] + 3*(1-t)**2*t * control[0] + 3*(1-t)*t**2 * end[0] + t**3 * end[0]
            y = (1-t)**3 * start[1] + 3*(1-t)**2*t * control[1] + 3*(1-t)*t**2 * end[1] + t**3 * end[1]
            x += random.uniform(-3, 3) # 增加微弱的肌肉抖动
            y += random.uniform(-3, 3)
            points.append((x, y))
        return points

    @staticmethod
    def move_mouse(page, target_locator):
        """带有过冲回正的贝塞尔鼠标滑动"""
        try:
            box = target_locator.bounding_box(timeout=5000)
            if not box: return
            target_x = box["x"] + box["width"] / 2
            target_y = box["y"] + box["height"] / 2
        except Exception:
            return

        start_x, start_y = random.randint(100, 400), random.randint(50, 300)
        page.mouse.move(start_x, start_y)
        
        control_x = random.randint(int(min(start_x, target_x)), int(max(start_x, target_x)))
        control_y = random.randint(int(min(start_y, target_y)), int(max(start_y, target_y)))
        
        steps = random.randint(5, 8)
        points = HumanSimulator.bezier_curve((start_x, start_y), (target_x, target_y), (control_x, control_y), steps)
        
        for x, y in points:
            page.mouse.move(x, y)
            page.wait_for_timeout(random.randint(5, 20))
        
        # 30% 概率触发过冲回正
        if random.random() > 0.7:
            overshoot_x = target_x + random.randint(-15, 15)
            overshoot_y = target_y + random.randint(-15, 15)
            page.mouse.move(overshoot_x, overshoot_y)
            page.wait_for_timeout(random.randint(15, 40))
            page.mouse.move(target_x, target_y)

    @staticmethod
    def type_text(page, locator, text):
        """高熵值拟人输入（带思考停顿）"""
        locator.fill("") # 先清空
        for char in text:
            page.keyboard.type(char, delay=random.randint(15, 80))
            # 10% 概率思考停顿
            if random.random() > 0.90:
                page.wait_for_timeout(random.randint(80, 200))

    @staticmethod
    def random_scroll(page, min_scroll=100, max_scroll=400, times=1):
        """视觉浏览模拟（扫视页面）"""
        for _ in range(times):
            scroll_amount = random.randint(min_scroll, max_scroll)
            page.mouse.wheel(0, scroll_amount)
            page.wait_for_timeout(random.randint(80, 200))
        # 有时往回滚一点点
        if random.random() > 0.6:
            page.mouse.wheel(0, -random.randint(50, 200))
            page.wait_for_timeout(random.randint(150, 350))


# ==========================================
# 模块 3：核心 Tool 业务逻辑
# ==========================================

class BrowserTool(BaseTool):
    name = "browser_tool"
    description = "Automated web browser for searching or reading web pages with extreme human-like evasion. Actions: 'search' (Bing/Baidu), 'goto' (read specific URL), 'search_magnet', 'sniff'. Supports 'session_id' for persistence."
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "goto", "search_magnet", "sniff"]},
            "query": {"type": "string"},
            "url": {"type": "string"},
            "page": {"type": "integer"},
            "session_id": {"type": "string", "description": "Optional ID to persist cookies/storage across calls."},
            "click_selector": {"type": "string"},
            "target_pattern": {"type": "string"}
        },
        "required": ["action"]
    }

    def _clean_html_to_markdown(self, html_content):
        if not html_content: return ""
        html_content = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', html_content, flags=re.IGNORECASE | re.DOTALL)
        html_content = re.sub(r'<img[^>]*src="data:image/[^"]*"[^>]*/?>', '', html_content, flags=re.IGNORECASE)
        md = markdownify.markdownify(
            html_content, 
            heading_style="ATX", 
            strip=['nav', 'footer', 'iframe', 'header', 'aside']
        )
        md = re.sub(r'\n{3,}', '\n\n', md)
        return md.strip()

    def _route_interceptor(self, route):
        """精细化资源路由"""
        rt = route.request.resource_type
        if rt == "image":
            # Mock 1x1 透明图片
            route.fulfill(body=TRANSPARENT_GIF, content_type="image/gif")
        elif rt == "media":
            route.abort() # 阻断大体积音视频
        else:
            route.continue_()

    def _human_search(self, page, engine, query):
        """会话预热与拟人化搜索主流程"""
        if engine == "bing":
            page.goto("https://www.bing.com/?cc=us&setmkt=en-US&setlang=en-us", wait_until="domcontentloaded", timeout=20000)
            search_box = page.locator('#sb_form_q').first
        else:
            page.goto("https://www.baidu.com", wait_until="domcontentloaded", timeout=20000)
            search_box = page.locator('#kw').first

        page.wait_for_timeout(random.randint(300, 500))
        
        # 1. 模拟打开首页后的扫视
        HumanSimulator.random_scroll(page, times=1)

        # 2. 贝塞尔鼠标滑向搜索框并点击
        HumanSimulator.move_mouse(page, search_box)
        search_box.click()
        page.wait_for_timeout(random.randint(150, 300))
        
        # 3. 高熵值输入关键词
        HumanSimulator.type_text(page, search_box, query)

        # 4. 物理回车提交
        page.wait_for_timeout(random.randint(80, 200))
        page.keyboard.press("Enter")
        
        # 5. 等待渲染并向下浏览结果
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(random.randint(500, 800))
        HumanSimulator.random_scroll(page, times=1)

    def run(self, action: str, query: str = "", url: str = "", page: int = 1, session_id: str = None, **kwargs) -> str:
        state_path = os.path.join(SESSION_DIR, f"{session_id}.json") if session_id else None

        try:
            with XvfbManager() as _:
                with sync_playwright() as p:
                    # 强行指定 UA
                    current_ua = random.choice(UA_POOL)
                    
                    browser = p.chromium.launch(
                        headless=False,
                        args=[
                            '--no-sandbox', 
                            '--disable-setuid-sandbox',
                            '--disable-blink-features=AutomationControlled',
                            '--disable-infobars',
                            '--window-size=1366,768'
                        ]
                    )
                    
                    # 状态恢复 (Layer 2 机制)
                    context_kwargs = {
                        "viewport": {'width': 1366, 'height': 768},
                        "user_agent": current_ua
                    }
                    if state_path and os.path.exists(state_path):
                        context_kwargs["storage_state"] = state_path

                    context = browser.new_context(**context_kwargs)
                    
                    # 双重 Stealth 注入：拦截动态环境泄漏
                    context.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        delete navigator.__proto__.webdriver;
                    """)
                    
                    page_instance = context.new_page()
                    
                    # 🚀 修复点 2：使用新的 Stealth 类方法应用隐身策略
                    Stealth().apply_stealth_sync(page_instance)
                    
                    page_instance.route("**/*", self._route_interceptor)

                    result_output = ""

                    # ==================== Action: Search ====================
                    if action == "search":
                        if not query: return "Error: 'query' required."
                        current_page = max(1, page)

                        html_content = ""
                        try:
                            self._human_search(page_instance, "bing", query)
                            
                            # 翻页逻辑复用
                            if current_page > 1:
                                for current_idx in range(1, current_page):
                                    next_page_num = current_idx + 1
                                    smart_paginate_js = f"""(nextNum) => {{
                                        let links = Array.from(document.querySelectorAll('a[href]'));
                                        for (let a of links) {{
                                            let text = a.innerText.trim();
                                            if (text === String(nextNum) || text === `[${{nextNum}}]` || text === `-${{nextNum}}-`) return a.href;
                                        }}
                                        let nextKeywords = ['下一页', 'next', '»', '›', '>', '▶'];
                                        for (let a of links) {{
                                            let text = a.innerText.trim().toLowerCase();
                                            let title = (a.title || '').toLowerCase();
                                            if (nextKeywords.includes(text) || nextKeywords.some(k => title === k) || (a.className || '').toLowerCase().includes('next')) {{
                                                if (!text.includes('末页') && !text.includes('last')) return a.href;
                                            }}
                                        }}
                                        let fallback = document.querySelector('a[rel="next"], .next a');
                                        if (fallback) return fallback.href;
                                        return null;
                                    }}"""
                                    href = page_instance.evaluate(smart_paginate_js, next_page_num)
                                    if href:
                                        if not href.startswith("http"):
                                            parsed = urllib.parse.urlparse(page_instance.url)
                                            href = f"{parsed.scheme}://{parsed.netloc}{href if href.startswith('/') else '/' + href}"
                                        page_instance.goto(href, wait_until="domcontentloaded", timeout=15000)
                                        page_instance.wait_for_timeout(random.randint(500, 800))
                                        HumanSimulator.random_scroll(page_instance, times=1)
                                    else:
                                        break

                            try:
                                page_instance.wait_for_selector("#b_results", timeout=3000)
                                html_content = page_instance.inner_html('#b_results')
                            except:
                                html_content = page_instance.inner_html('body')

                        except Exception as e:
                            print(f"[BrowserTool] Bing 失败，切百度 ({str(e)})")
                            self._human_search(page_instance, "baidu", query)
                            try:
                                page_instance.wait_for_selector("#content_left", timeout=3000)
                                html_content = page_instance.inner_html('#content_left')
                            except:
                                html_content = page_instance.inner_html('body')

                        md_text = self._clean_html_to_markdown(html_content)[:12000]
                        result_output = (
                            f"=== Search Results for '{query}' (Page {current_page}) ===\n{md_text}\n---\n"
                            f"[System Directive]: 你可以使用 action='search' 及 page={current_page + 1} 继续翻页。"
                        )

                    # ==================== Action: Goto ====================
                    elif action == "goto":
                        if not url: return "Error: 'url' required."
                        if not url.startswith("http"): url = "https://" + url
                        
                        page_instance.goto(url, wait_until="domcontentloaded", timeout=20000)
                        page_instance.wait_for_timeout(random.randint(500, 800))

                        current_page = max(1, page)
                        if current_page > 1:
                            for step in range(current_page - 1):
                                next_js = """() => {
                                    let links = Array.from(document.querySelectorAll('a[href]'));
                                    let nextKeywords = ['下一页', 'next', '»', '›', 'Next'];
                                    for (let a of links) {
                                        let text = a.innerText.trim().toLowerCase();
                                        let title = (a.title || '').toLowerCase();
                                        if (nextKeywords.some(k => text === k || title === k)) {
                                            if (!text.includes('末页') && !text.includes('last')) return a.href;
                                        }
                                    }
                                    let fb = document.querySelector('a[rel="next"], .pagination .next a, .pager .next a');
                                    return fb ? fb.href : null;
                                }"""
                                href = page_instance.evaluate(next_js)
                                if href:
                                    if not href.startswith("http"):
                                        parsed = urllib.parse.urlparse(page_instance.url)
                                        href = f"{parsed.scheme}://{parsed.netloc}{href if href.startswith('/') else '/' + href}"
                                    page_instance.goto(href, wait_until="domcontentloaded", timeout=15000)
                                    page_instance.wait_for_timeout(random.randint(500, 800))
                                else:
                                    break

                        # 模拟深度阅读
                        HumanSimulator.random_scroll(page_instance, times=random.randint(2, 4))
                        
                        # 提取导航雷达
                        nav_links_js = """() => {
                            let links = []; let seen = new Set();
                            let selectors = ['nav', 'aside', '.sidebar', '#sidebar', '.menu', '.pagination', '.toc'];
                            document.querySelectorAll(selectors.join(', ')).forEach(container => {
                                container.querySelectorAll('a').forEach(a => {
                                    let text = (a.innerText || a.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
                                    if (text && a.href && a.href.startsWith('http') && text.length > 1 && text.length < 60) {
                                        if (!seen.has(a.href)) { seen.add(a.href); links.push(`- [${text}](${a.href})`); }
                                    }
                                });
                            });
                            return links.slice(0, 45);
                        }"""
                        extracted_nav_links = page_instance.evaluate(nav_links_js)

                        # 暴力清洗噪音
                        page_instance.evaluate("""() => {
                            ['header', 'footer', 'nav', 'aside', '.sidebar', '.ads', '#cookie-banner', '.pagination'].forEach(selector => {
                                document.querySelectorAll(selector).forEach(el => el.remove());
                            });
                        }""")
                        
                        html_content = ""
                        for selector in ['article', 'main', '#content', '.content', '.post', 'body']:
                            elements = page_instance.query_selector_all(selector)
                            if elements:
                                html_content = elements[0].inner_html()
                                break
                        
                        md_text = self._clean_html_to_markdown(html_content)[:16000]
                        nav_section = "\n\n=== 🗺️ 网页导航雷达 ===\n" + "\n".join(extracted_nav_links) if extracted_nav_links else ""
                        result_output = f"=== Page Content of {url} ===\n{md_text}{nav_section}"

                    # ==================== Action: Sniff ====================
                    elif action == "sniff":
                        click_selector = kwargs.get("click_selector", "")
                        target_pattern = kwargs.get("target_pattern", "")
                        sniff_timeout = kwargs.get("timeout", 5000)
                        
                        if not url or not target_pattern:
                            return "Error: 'url' and 'target_pattern' are required."
                        if not url.startswith("http"): url = "https://" + url

                        captured_urls = set()

                        def is_match(text):
                            if not text: return False
                            try:
                                if re.search(target_pattern, text, re.IGNORECASE): return True
                            except re.error:
                                pass
                            return target_pattern.lower() in text.lower()

                        context.on("request", lambda req: captured_urls.add(req.url) if is_match(req.url) else None)

                        def handle_response(res):
                            if is_match(res.url): captured_urls.add(res.url)
                            loc = res.headers.get("location") or res.headers.get("Location")
                            if loc and is_match(loc): captured_urls.add(loc)
                        context.on("response", handle_response)

                        def handle_download(dl):
                            if is_match(dl.url): captured_urls.add(dl.url)
                            if dl.suggested_filename and is_match(dl.suggested_filename):
                                captured_urls.add(f"{dl.url} (文件: {dl.suggested_filename})")
                        page_instance.on("download", handle_download)

                        context.on("page", lambda p: captured_urls.add(p.url) if is_match(p.url) else None)

                        page_instance.goto(url, wait_until="domcontentloaded", timeout=20000)
                        page_instance.wait_for_timeout(random.randint(500, 1000))
                        HumanSimulator.random_scroll(page_instance, times=1)

                        if click_selector:
                            actual_selector = click_selector if any(c in click_selector for c in ['#', '.', '[', '>', ' ', ':']) or click_selector.startswith('text=') else f'text="{click_selector}"'
                            
                            try:
                                target_btn = page_instance.locator(actual_selector).first
                                target_btn.wait_for(state="attached", timeout=3000)

                                found_in_dom = None
                                for attr in ['data-url', 'data-href', 'data-link', 'data-download', 'href', 'url']:
                                    try:
                                        val = target_btn.get_attribute(attr)
                                        if is_match(val):
                                            found_in_dom = val
                                            if not found_in_dom.startswith("http") and not found_in_dom.startswith("magnet:") and not found_in_dom.startswith("blob:"):
                                                parsed = urllib.parse.urlparse(page_instance.url)
                                                found_in_dom = f"{parsed.scheme}://{parsed.netloc}{found_in_dom if found_in_dom.startswith('/') else '/' + found_in_dom}"
                                            break
                                    except: pass

                                if found_in_dom:
                                    captured_urls.add(found_in_dom)
                                else:
                                    target_btn.scroll_into_view_if_needed()
                                    page_instance.wait_for_timeout(500)
                                    HumanSimulator.move_mouse(page_instance, target_btn)
                                    target_btn.click(force=True)
                            except Exception as e:
                                return f"=== 🕵️ 嗅探异常 ===\n无法定位或点击选择器 '{click_selector}'，详细错误: {str(e)}"

                        try:
                            page_instance.wait_for_load_state('networkidle', timeout=sniff_timeout)
                        except:
                            page_instance.wait_for_timeout(sniff_timeout)

                        if captured_urls:
                            result_list = "\n".join([f"- {u}" for u in captured_urls])
                            mode_text = "主动点击" if click_selector else "被动监听"
                            result_output = f"=== 🕵️ 嗅探成功 ({mode_text}) ===\n目标: {url}\n捕获链接:\n{result_list}"
                        else:
                            result_output = f"=== 🕵️ 嗅探无结果 ===\n未拦截到匹配 '{target_pattern}' 的请求或响应。"
                        if not url.startswith("http"): url = "https://" + url

                        captured_urls = set()
                        context.on("request", lambda req: captured_urls.add(req.url) if target_pattern.lower() in req.url.lower() else None)

                        page_instance.goto(url, wait_until="domcontentloaded", timeout=20000)
                        page_instance.wait_for_timeout(random.randint(500, 1000))
                        HumanSimulator.random_scroll(page_instance, times=1)

                        actual_selector = click_selector if any(c in click_selector for c in ['#', '.', '[', '>', ' ', ':']) or click_selector.startswith('text=') else f'text="{click_selector}"'
                        target_btn = page_instance.locator(actual_selector).first

                        # DOM 静默提取
                        found_in_dom = None
                        for attr in ['data-url', 'data-href', 'data-link', 'data-download', 'href', 'url']:
                            try:
                                val = target_btn.get_attribute(attr)
                                if val and target_pattern.lower() in val.lower():
                                    found_in_dom = val
                                    if not found_in_dom.startswith("http") and not found_in_dom.startswith("magnet:"):
                                        parsed = urllib.parse.urlparse(page_instance.url)
                                        found_in_dom = f"{parsed.scheme}://{parsed.netloc}{found_in_dom if found_in_dom.startswith('/') else '/' + found_in_dom}"
                                    break
                            except: pass

                        if found_in_dom:
                            captured_urls.add(found_in_dom)
                        else:
                            # 没找到则强制物理点击嗅探
                            target_btn.scroll_into_view_if_needed()
                            page_instance.wait_for_timeout(500)
                            HumanSimulator.move_mouse(page_instance, target_btn)
                            target_btn.click(force=True)
                            page_instance.wait_for_timeout(4000)

                        if captured_urls:
                            result_list = "\n".join([f"- {u}" for u in captured_urls])
                            result_output = f"=== 🕵️ 嗅探成功 ===\n目标: {url}\n捕获链接:\n{result_list}"
                        else:
                            result_output = f"=== 🕵️ 嗅探无结果 ===\n未拦截到包含 '{target_pattern}' 的请求。"

                    # ==================== 状态保存与结束 ====================
                    if state_path:
                        context.storage_state(path=state_path)

                    browser.close()
                    return result_output

        except Exception as e:
            return f"Browser Execution Error: {str(e)}"