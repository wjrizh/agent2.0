import time
import urllib.parse
import re
from .core import BaseTool

class BrowserTool(BaseTool):
    name = "browser_tool"
    description = "Automated web browser for searching the internet or reading web pages. Converts pages to clean Markdown for easy reading. Actions: 'search' (Bing/Baidu with pagination), 'goto' (read specific URL)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "goto", "search_magnet", "sniff"],
                "description": "'search' (Bing/Baidu), 'goto' (read a URL), 'search_magnet' (high-security forms), 'sniff' (click an element and intercept dynamic network requests like downloads/videos)."
            },
            "query": {
                "type": "string",
                "description": "The search query (required for 'search' and 'search_magnet')."
            },
            "url": {
                "type": "string",
                "description": "The full URL to visit (required for 'goto', 'search_magnet', and 'sniff')."
            },
            "page": {
                "type": "integer",
                "description": "Page number for search results (default is 1)."
            },
            "click_selector": {
                "type": "string",
                "description": "The CSS or text selector of the button to click (required for 'sniff'). e.g., 'text=\"Deb\"' or '#download-btn'"
            },
            "target_pattern": {
                "type": "string",
                "description": "The keyword or file extension to look for in intercepted network requests (required for 'sniff'). e.g., '.deb', '.mp4', 'magnet:'"
            }
        },
        "required": ["action"]
    }

    def run(self, action: str, query: str = "", url: str = "", page: int = 1, **kwargs) -> str:
        from playwright.sync_api import sync_playwright
        import markdownify

        def clean_html_to_markdown(html_content):
            """暴力清洗 HTML：先用正则干掉所有 style/script，再转 Markdown"""
            if not html_content:
                return ""
            
            html_content = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', html_content, flags=re.IGNORECASE | re.DOTALL)
            
            md = markdownify.markdownify(
                html_content, 
                heading_style="ATX", 
                strip=['nav', 'footer', 'iframe', 'header', 'aside']
            )
            
            md = re.sub(r'\n{3,}', '\n\n', md)
            return md.strip()

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={'width': 1280, 'height': 800}
                )
                
                # 抹除无头浏览器特征，伪装成真实 Chrome
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.navigator.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
                """)
                
                page_instance = context.new_page()
                page_instance.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())

                if action == "search":
                    if not query:
                        return "Error: 'query' parameter is required for search action."
                    
                    current_page = max(1, page)
                    base_bing_url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
                    base_baidu_url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}"

                    def navigate_and_paginate(base_url, target_page):
                        page_instance.goto(base_url, wait_until="domcontentloaded", timeout=20000)
                        page_instance.wait_for_timeout(1000)

                        if target_page > 1:
                            # current_idx 表示当前所在的页码
                            for current_idx in range(1, target_page):
                                next_page_num = current_idx + 1
                                
                                # 💡 终极自适应翻页逻辑：注入 JS 智能探测下一页 URL
                                smart_paginate_js = f"""(nextNum) => {{
                                    let links = Array.from(document.querySelectorAll('a[href]'));
                                    
                                    // 策略 1：精准匹配目标页码数字 (例如找 "2", "3")。最高效，绝不误判。
                                    for (let a of links) {{
                                        let text = a.innerText.trim();
                                        // 兼容常见的数字包裹格式
                                        if (text === String(nextNum) || text === `[${{nextNum}}]` || text === `-${{nextNum}}-`) {{
                                            return a.href;
                                        }}
                                    }}
                                    
                                    // 策略 2：匹配常见的"下一页"符号和中英文关键字
                                    let nextKeywords = ['下一页', 'next', '»', '›', '>', '▶', '下页', 'forward', 'next page'];
                                    for (let a of links) {{
                                        let text = a.innerText.trim().toLowerCase();
                                        let title = (a.title || '').toLowerCase();
                                        let aria = (a.getAttribute('aria-label') || '').toLowerCase();
                                        let className = (a.className || '').toLowerCase();
                                        
                                        // 只要文本、标题、辅助标签或类名命中，就认为是下一页
                                        if (nextKeywords.includes(text) || 
                                            nextKeywords.some(k => title === k || aria === k) ||
                                            className.includes('next')) {{
                                            
                                            // 防误判：确保它不是跳转到尾页的 "Last" 或 "末页" 或 "»»"
                                            if (!text.includes('末页') && !text.includes('last') && text !== '»»') {{
                                                return a.href;
                                            }}
                                        }}
                                    }}
                                    
                                    // 策略 3：兜底原生选择器
                                    let fallback = document.querySelector('a[rel="next"], .next a, .pagination-next a, .pager .next a');
                                    if (fallback) return fallback.href;
                                    
                                    return null;
                                }}"""
                                
                                href = page_instance.evaluate(smart_paginate_js, next_page_num)
                                
                                if href:
                                    # 处理相对路径转换为绝对路径
                                    if not href.startswith("http"):
                                        from urllib.parse import urlparse
                                        parsed = urlparse(page_instance.url)
                                        href = f"{parsed.scheme}://{parsed.netloc}{href if href.startswith('/') else '/' + href}"
                                    
                                    print(f"[BrowserTool] 成功获取第 {next_page_num} 页链接: {href}，准备跳转...")
                                    page_instance.goto(href, wait_until="domcontentloaded", timeout=15000)
                                    page_instance.wait_for_timeout(1500) # 给一点渲染缓冲时间
                                else:
                                    print(f"[BrowserTool] 网页中未找到第 {next_page_num} 页的翻页入口，可能已到底部。")
                                    break

                    try:
                        navigate_and_paginate(base_bing_url, current_page)
                        try:
                            page_instance.wait_for_selector("#b_results", timeout=3000)
                            html_content = page_instance.inner_html('#b_results')
                        except:
                            html_content = page_instance.inner_html('body')
                    except Exception as e:
                        print(f"[BrowserTool] Bing 翻页或加载超时，切换到 Baidu... ({str(e)})")
                        navigate_and_paginate(base_baidu_url, current_page)
                        try:
                            page_instance.wait_for_selector("#content_left", timeout=3000)
                            html_content = page_instance.inner_html('#content_left')
                        except:
                            html_content = page_instance.inner_html('body')
                    
                    md_text = clean_html_to_markdown(html_content)
                    browser.close()
                    
                    result_text = md_text[:12000]
                    return (
                        f"=== Search Results for '{query}' (Page {current_page}) ===\n"
                        f"{result_text}\n"
                        f"---\n"
                        f"[System Directive to AI]: 这只是第 {current_page} 页的结果。如果在上述内容中没有找到满意的答案，"
                        f"请使用 action='search' 并将 'page' 参数设置为 {current_page + 1} 继续翻页查找！"
                    )

                elif action == "search_magnet":
                    if not query:
                        return "Error: 'query' parameter is required for search_magnet."
                    if not url:
                        url = "https://ddcl.me"
                    
                    if not url.startswith("http"):
                        url = "https://" + url
                        
                    # 提取根域名，确保从首页开始预热
                    from urllib.parse import urlparse
                    parsed_url = urlparse(url)
                    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                    encoded_query = urllib.parse.quote(query)

                    # ========== 🚀 智能 URL 拼接快速通道 ==========
                    # 优先尝试直接拼接搜索 URL，秒级响应，绕开 networkidle 超时
                    url_patterns = [
                        f"{base_url}/search?keyword={encoded_query}",
                        f"{base_url}/search?q={encoded_query}",
                        f"{base_url}/?q={encoded_query}",
                        f"{base_url}/so/{encoded_query}.html",
                        f"{base_url}/search/{encoded_query}_ctime_1.html",
                        f"{base_url}/index.php?r=l&kw={encoded_query}",
                    ]
                    
                    fast_result = None
                    for candidate_url in url_patterns:
                        try:
                            page_instance.goto(candidate_url, wait_until="domcontentloaded", timeout=12000)
                            page_instance.wait_for_timeout(1500)
                            body_text = page_instance.inner_text('body')[:500]
                            # 验证：页面必须包含搜索关键词且没有报错
                            if query[:2] in body_text and "error" not in body_text.lower() and "not found" not in body_text.lower():
                                print(f"[BrowserTool] 🚀 URL 拼接成功: {candidate_url}")
                                fast_result = page_instance.inner_html('body')
                                break
                        except Exception:
                            continue
                    
                    if fast_result:
                        # 快速通道命中，跳过表单提交流程
                        md_text = clean_html_to_markdown(fast_result)
                        browser.close()
                        result = md_text[:16000]
                        if len(md_text) > 16000:
                            result += "\n\n... [Content Truncated due to length] ..."
                        return (
                            f"=== Magnet Search Results for '{query}' on {base_url} ===\n{result}\n"
                            f"---\n"
                            f"[System Directive to AI]: 你已经成功在目标网站执行了搜索。请从上方的正文中寻找你需要的资源链接。"
                        )
                    # ========== 快速通道未命中，回退到表单输入 ==========

                    print(f"[BrowserTool] URL 拼接未命中，正在对高防站点 {base_url} 进行会话预热...")
                    
                    # 1. 访问首页，建立 Session 信任并过 WAF
                    try:
                        page_instance.goto(base_url, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass  # 加载不完也继续
                    page_instance.wait_for_timeout(2000)
                    
                    # 2. 寻找搜索框
                    # 涵盖了绝大多数原生网站、现代前端框架和无障碍规范的搜索框特征
                    search_input_selectors = [
                        # 原生 HTML 与通用属性
                        'input[type="text"]', 'input[type="search"]', 
                        'input[name="keyword"]', 'input[name="q"]', 'input[name="search"]',
                        'input[placeholder*="搜索"]', 'input[placeholder*="search" i]', 
                        '.search-input', '#search', '#search-input',
                        '[aria-label*="搜索"]', '[aria-label*="search" i]', # 现代无障碍标准 (A11y)
                        
                        # 现代前端框架穿透 (Vue/React/UI库)
                        'input.el-input__inner',       # Element UI / Element Plus (Vue)
                        'input.ant-input',             # Ant Design (React/Vue)
                        'input.n-input__input-el',     # Naive UI (Vue)
                        'input.MuiInputBase-input',    # Material-UI / MUI (React)
                        'input.v-field__input',        # Vuetify (Vue)
                        '.q-field__native',            # Quasar (Vue)
                    ]
                    
                    input_found = False
                    for selector in search_input_selectors:
                        if page_instance.query_selector(selector):
                            print(f"[BrowserTool] 找到搜索框: {selector}，开始模拟人类输入...")
                            # 3. 拟人化输入：先清空，再带有延迟地逐字敲击
                            page_instance.fill(selector, "")
                            page_instance.wait_for_timeout(300)
                            page_instance.type(selector, query, delay=150) # delay=150ms 模拟真实打字速度
                            input_found = True
                            break
                            
                    if not input_found:
                        return f"Error: Could not find search input box on {base_url}. The site structure might be too complex."
                        
                    # 4. 模拟物理回车，触发原生的 Submit 事件（这自带 isTrusted=true 标记，极其防封）
                    page_instance.wait_for_timeout(500)
                    page_instance.keyboard.press("Enter")
                    
                    # 5. 等待搜索结果页面加载
                    print("[BrowserTool] 回车已触发，等待结果页加载...")
                    try:
                        page_instance.wait_for_load_state("networkidle", timeout=20000)
                    except:
                        pass # 有些网站永远不会 idle，强制通过
                    page_instance.wait_for_timeout(3000) # 关键：给前端异步渲染（Vue/React）列表留出时间

                    # 6. 使用我们强大的分离式导航雷达和清洗机制（复用 goto 的逻辑）
                    nav_links_js = """() => {
                        let links = [];
                        let seen = new Set();
                        let selectors = ['nav', 'aside', '.sidebar', '#sidebar', '.menu', '#menu', '.tabs', '.pagination', '.toc', '#toc', '.pager', '.next', '.prev', '.magnet', '.download'];
                        
                        document.querySelectorAll(selectors.join(', ')).forEach(container => {
                            container.querySelectorAll('a').forEach(a => {
                                let text = a.innerText.trim() || a.getAttribute('title') || '';
                                text = text.replace(/\\s+/g, ' ').trim();
                                let href = a.href;
                                if (text && href && (href.startsWith('http') || href.startsWith('magnet:')) && text.length > 1 && text.length < 100) {
                                    if (!seen.has(href)) {
                                        seen.add(href);
                                        links.push(`- [${text}](${href})`);
                                    }
                                }
                            });
                        });
                        return links.slice(0, 50);
                    }"""
                    extracted_nav_links = page_instance.evaluate(nav_links_js)

                    # 暴力拆除噪音
                    page_instance.evaluate("""() => {
                        const garbageSelectors = ['header', 'footer', 'nav', 'aside', '.sidebar', '#sidebar', '.ads', '#cookie-banner', '.pagination', '.toc'];
                        garbageSelectors.forEach(selector => {
                            document.querySelectorAll(selector).forEach(el => el.remove());
                        });
                    }""")
                    
                    html_content = ""
                    for selector in ['article', 'main', '#content', '.content', '.post', '.list', 'body']:
                        elements = page_instance.query_selector_all(selector)
                        if elements:
                            html_content = elements[0].inner_html()
                            break
                            
                    md_text = clean_html_to_markdown(html_content)
                    browser.close()
                    
                    result = md_text[:16000] 
                    if len(md_text) > 16000:
                        result += "\n\n... [Content Truncated due to length] ..."
                        
                    nav_section = ""
                    if extracted_nav_links:
                        nav_section = "\n\n=== 🗺️ 网页导航雷达 (包含磁力链接/分页) ===\n" + "\n".join(extracted_nav_links)

                    return (
                        f"=== Magnet Search Results for '{query}' on {base_url} ===\n{result}"
                        f"{nav_section}\n"
                        f"---\n"
                        f"[System Directive to AI]: 你已经成功在目标网站执行了搜索。请从上方的正文或『网页导航雷达』中寻找你需要的资源链接。如果有详情页链接，你可以继续使用 'goto' 深入查看！"
                    )

                elif action == "goto":
                    if not url:
                        return "Error: 'url' parameter is required for goto action."
                    if not url.startswith("http"):
                        url = "https://" + url

                    current_page = max(1, page)

                    # 🚀 翻页逻辑：如果 page > 1，从第 1 页开始逐页点击"下一页"
                    page_instance.goto(url, wait_until="domcontentloaded", timeout=20000)
                    page_instance.wait_for_timeout(1000)

                    if current_page > 1:
                        for step in range(current_page - 1):
                            next_selectors = "a[rel='next'], a.sb_pagN, a[title='下一页'], a[title='Next page'], a:has-text('»'), a:has-text('›'), a:has-text('Next'), a[aria-label*='next' i], a[aria-label*='下一' i], a.n:has-text('下一页'), a:has-text('下一页'), .pager .next a, .pagination .next a"
                            next_btn = page_instance.query_selector(next_selectors)
                            if next_btn:
                                href = next_btn.get_attribute("href")
                                if href:
                                    if not href.startswith("http"):
                                        from urllib.parse import urlparse
                                        parsed = urlparse(page_instance.url)
                                        href = f"{parsed.scheme}://{parsed.netloc}{href if href.startswith('/') else '/' + href}"
                                    page_instance.goto(href, wait_until="domcontentloaded", timeout=15000)
                                    page_instance.wait_for_timeout(1200)
                                else:
                                    next_btn.click()
                                    page_instance.wait_for_timeout(3000)
                            else:
                                print("[BrowserTool] goto: 未找到下一页按钮，翻页终止。")
                                break
                    
                    # 💡 核心升级 1：在破坏 DOM 之前，启用“导航雷达”提取侧边栏、顶部菜单和翻页按钮
                    nav_links_js = """() => {
                        let links = [];
                        let seen = new Set();
                        // 精准锁定网站的骨架区域：导航栏、侧边栏、菜单、选项卡、翻页器、目录
                        let selectors = ['nav', 'aside', '.sidebar', '#sidebar', '.menu', '#menu', '.tabs', '.pagination', '.toc', '#toc', '.pager', '.next', '.prev'];
                        
                        document.querySelectorAll(selectors.join(', ')).forEach(container => {
                            container.querySelectorAll('a').forEach(a => {
                                let text = a.innerText.trim() || a.getAttribute('title') || a.getAttribute('aria-label') || '';
                                text = text.replace(/\\s+/g, ' ').trim();
                                let href = a.href;
                                
                                // 过滤掉无效链接、纯锚点和过长的垃圾文本
                                if (text && href && href.startsWith('http') && text.length > 1 && text.length < 60) {
                                    if (!seen.has(href)) {
                                        seen.add(href);
                                        links.push(`- [${text}](${href})`);
                                    }
                                }
                            });
                        });
                        return links.slice(0, 45); // 最多提取 45 个高质量导航链接
                    }"""
                    extracted_nav_links = page_instance.evaluate(nav_links_js)

                    # 💡 核心升级 2：雷达扫描完毕，开始暴力拆除噪音节点，保全最干净的正文
                    page_instance.evaluate("""() => {
                        const garbageSelectors = ['header', 'footer', 'nav', 'aside', '.sidebar', '#sidebar', '.ads', '#cookie-banner', '.pagination', '.toc'];
                        garbageSelectors.forEach(selector => {
                            document.querySelectorAll(selector).forEach(el => el.remove());
                        });
                    }""")
                    
                    try:
                        page_instance.wait_for_timeout(1500)
                    except:
                        pass
                    
                    html_content = ""
                    for selector in ['article', 'main', '#content', '.content', '.post', 'body']:
                        elements = page_instance.query_selector_all(selector)
                        if elements:
                            html_content = elements[0].inner_html()
                            break
                    
                    md_text = clean_html_to_markdown(html_content)
                    browser.close()
                    
                    # 组装正文
                    result = md_text[:16000] 
                    if len(md_text) > 16000:
                        result += "\n\n... [Content Truncated due to length] ..."
                        
                    # 💡 核心升级 3：组装全局雷达图
                    nav_section = ""
                    if extracted_nav_links:
                        nav_section = "\n\n=== 🗺️ 网页导航雷达 (侧边栏/顶部标签/翻页) ===\n" + "\n".join(extracted_nav_links)

                    page_label = f" (Page {current_page})" if current_page > 1 else ""
                    return (
                        f"=== Page Content of {url}{page_label} ===\n{result}"
                        f"{nav_section}\n"
                        f"---\n"
                        f"[System Directive to AI]: 你正在阅读 {url} 的正文内容（第 {current_page} 页）。如果你发现该页面没有你要找的具体答案（例如只是一个目录页），或者你需要阅读下一章、切换到其他标签页，**请直接查看上方的『网页导航雷达』**。从雷达中挑选你需要的章节/标签的 URL，再次调用 action='goto' 前往目标页面！或者使用 page={current_page + 1} 翻到下一页。"
                    )

                elif action == "sniff":
                    click_selector = kwargs.get("click_selector")
                    target_pattern = kwargs.get("target_pattern")
                    
                    if not url or not click_selector or not target_pattern:
                        return "Error: 'url', 'click_selector', and 'target_pattern' are required for sniff action."
                    if not url.startswith("http"):
                        url = "https://" + url

                    print(f"[BrowserTool] 正在嗅探模式下访问 {url} ...")
                    print(f"[BrowserTool] 目标操作: 点击 {click_selector}，拦截特征包含 '{target_pattern}' 的请求")

                    captured_urls = set()

                    # 定义拦截回调函数
                    def handle_request(request):
                        try:
                            # 忽略大小写进行模式匹配
                            if target_pattern.lower() in request.url.lower():
                                captured_urls.add(request.url)
                        except:
                            pass

                    # 💡 核心：绑定到 context 而非 page，这样即使点击触发了新标签页(Target="_blank")也能拦截到！
                    context.on("request", handle_request)

                    # 访问页面
                    page_instance.goto(url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        page_instance.wait_for_timeout(2000) # 给予框架初始化时间
                    except:
                        pass

                    # 💡 优化：更智能的 locator 判定逻辑
                    try:
                        # 自动感知：如果 selector 没有 CSS 常见的特征
                        if not any(char in click_selector for char in ['#', '.', '[', '>', ' ', ':']) \
                           and not click_selector.startswith('text='):
                            actual_selector = f'text="{click_selector}"'
                        else:
                            actual_selector = click_selector
                        
                        print(f"[BrowserTool] 自动解析定位器: {click_selector} -> {actual_selector}")
                        
                        target_btn = page_instance.locator(actual_selector).first
                        
                        # ==================================================
                        # 💡 核心新增：DOM 属性静默提取 (DOM Sniffing)
                        # 在暴力点击前，先检查元素本身是否携带了隐藏的真实链接
                        # ==================================================
                        found_in_dom = None
                        attributes_to_check = ['data-url', 'data-href', 'data-link', 'data-download', 'href', 'url']
                        
                        for attr in attributes_to_check:
                            try:
                                val = target_btn.get_attribute(attr)
                                # 忽略大小写匹配扩展名（例如匹配到 .deb, .exe 等）
                                if val and target_pattern.lower() in val.lower():
                                    found_in_dom = val
                                    # 处理相对路径转换为绝对路径
                                    if not found_in_dom.startswith("http") and not found_in_dom.startswith("magnet:"):
                                        from urllib.parse import urlparse
                                        parsed = urlparse(page_instance.url)
                                        found_in_dom = f"{parsed.scheme}://{parsed.netloc}{found_in_dom if found_in_dom.startswith('/') else '/' + found_in_dom}"
                                    break
                            except:
                                pass
                        
                        if found_in_dom:
                            print(f"[BrowserTool] 🎯 命中 DOM 属性提取，无需点击即可获取真实链接: {found_in_dom}")
                            captured_urls.add(found_in_dom)
                        else:
                            # ==================================================
                            # 回退机制：如果没在 DOM 属性中找到，强制触发点击监听网络
                            # ==================================================
                            target_btn.scroll_into_view_if_needed(timeout=2000)
                            
                            target_btn.click(force=True, timeout=5000)
                            print("[BrowserTool] 未发现直接属性，已强制触发点击，正在监听网络请求 (等待 4 秒)...")
                            
                            page_instance.wait_for_timeout(4000)
                            
                    except Exception as e:
                        # 💡 额外增益：即使发生异常，也顺手输出该元素的 outerHTML 供人肉提取
                        return f"Error: 无法处理元素 '{actual_selector}'。尝试解析出来的 HTML 片段供您参考: {str(e)[:300]}"

                    browser.close()

                    # 汇总结果返回给大模型
                    if captured_urls:
                        result_list = "\n".join([f"- {u}" for u in captured_urls])
                        return (
                            f"=== 🕵️ 通用网络嗅探成功 ===\n"
                            f"目标网址: {url}\n"
                            f"触发动作: Clicked '{click_selector}'\n"
                            f"匹配特征: '{target_pattern}'\n"
                            f"抓取到的隐藏真实链接:\n{result_list}\n"
                            f"---\n"
                            f"[System Directive to AI]: 你已成功抓取到底层动态链接，请直接使用上述链接执行后续任务（如使用 download_tool 下载）。"
                        )
                    else:
                        return (
                            f"=== 🕵️ 嗅探无结果 ===\n"
                            f"动作 Clicked '{click_selector}' 已执行，但未能拦截到任何包含 '{target_pattern}' 的网络请求。\n"
                            f"可能原因：1. 按钮选择器不准确。2. 资源不是通过网络请求下发（而是写入 DOM）。3. 需要更长的加载时间。"
                        )
                    
                else:
                    return f"Error: Unknown action '{action}'"

        except Exception as e:
            return f"Browser Execution Error: {str(e)}"