import time
import urllib.parse
import re
from .core import BaseTool

class BrowserTool(BaseTool):
    name = "browser_tool"
    description = "Automated web browser for searching the internet or reading web pages. Converts pages to clean Markdown for easy reading. Actions: 'search' (to find info on Bing/Baidu), 'goto' (to read a specific URL)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "goto"],
                "description": "'search' to look up a query, 'goto' to read a specific URL."
            },
            "query": {
                "type": "string",
                "description": "The search query (required if action is 'search')."
            },
            "url": {
                "type": "string",
                "description": "The full URL to visit (required if action is 'goto')."
            }
        },
        "required": ["action"]
    }

    def run(self, action: str, query: str = "", url: str = "", **kwargs) -> str:
        from playwright.sync_api import sync_playwright
        import markdownify

        def clean_html_to_markdown(html_content):
            """暴力清洗 HTML：先用正则干掉所有 style/script，再转 Markdown"""
            if not html_content:
                return ""
                
            # 1. 暴力正则剥离所有 <style> 和 <script> 及其内部内容（忽略大小写，跨行匹配）
            html_content = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', html_content, flags=re.IGNORECASE | re.DOTALL)
            
            # 2. 使用 markdownify 提取正文，丢弃导航栏、底部等干扰元素
            md = markdownify.markdownify(
                html_content, 
                heading_style="ATX", 
                strip=['nav', 'footer', 'iframe', 'header', 'aside']
            )
            
            # 3. 压缩连续的多余空白行（超过2个空行压缩为2个）
            md = re.sub(r'\n{3,}', '\n\n', md)
            return md.strip()

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={'width': 1280, 'height': 800}
                )
                page = context.new_page()

                # 拦截图片、字体、媒体，极大加快网页加载速度
                page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())

                if action == "search":
                    if not query:
                        return "Error: 'query' parameter is required for search action."
                    
                    # 优先使用 Bing (Bing 对 AI 爬取更友好，结果 DOM 结构比百度干净)
                    search_url_bing = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
                    search_url_baidu = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}"
                    
                    try:
                        # 增加 timeout 到 20 秒
                        page.goto(search_url_bing, wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(1000) # 给一点时间渲染动态内容
                    except Exception as e:
                        # Fallback 机制：如果 Bing 超时或报错，切换到百度
                        print(f"[BrowserTool] Bing timeout/error, falling back to Baidu... ({str(e)})")
                        page.goto(search_url_baidu, wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(1500)
                    
                    # 抓取整个 body 并强力清洗
                    html_content = page.inner_html('body')
                    md_text = clean_html_to_markdown(html_content)
                    
                    browser.close()
                    # 截断超长内容，防 Token 爆炸
                    return f"=== Search Results for '{query}' ===\n{md_text[:8000]}"

                elif action == "goto":
                    if not url:
                        return "Error: 'url' parameter is required for goto action."
                    if not url.startswith("http"):
                        url = "https://" + url

                    # 增加 timeout 到 20 秒
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    
                    # 等待异步内容（如 React/Vue）挂载
                    try:
                        page.wait_for_timeout(1500)
                    except:
                        pass
                    
                    # 优先级抓取策略：先找文章主体，找不到再抓全局
                    html_content = ""
                    for selector in ['article', 'main', '#content', '.content', '.post', 'body']:
                        elements = page.query_selector_all(selector)
                        if elements:
                            html_content = elements[0].inner_html()
                            break
                    
                    md_text = clean_html_to_markdown(html_content)
                    
                    browser.close()
                    # 限制单页最大返回长度为 15000 字符
                    result = md_text[:15000]
                    if len(md_text) > 15000:
                        result += "\n\n... [Content Truncated due to length] ..."
                    return f"=== Page Content of {url} ===\n{result}"
                    
                else:
                    return f"Error: Unknown action '{action}'"

        except Exception as e:
            return f"Browser Execution Error: {str(e)}"