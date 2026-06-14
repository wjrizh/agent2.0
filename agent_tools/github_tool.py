"""
github_tool.py — GitHub 仓库爬取与评估工具

基于 Playwright 浏览器模拟真人访问 GitHub Search，支持：
- 按关键词搜索仓库
- 获取名称、Star 数、语言、描述
- 自动分析项目价值
- 翻页采用"最快接近算法"，不逐步点 Next
- 无需 API Token，无 rate limit
"""

import urllib.parse
import json
import re
import os
import time
import random
import subprocess
import signal
from .core import BaseTool

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ── 复用 browser_tool 的基础设施 ──────────────────────────

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]


class XvfbManager:
    """Xvfb 虚拟显示器进程管理器"""

    def __init__(self, display=":99", size="1366x768x24"):
        self.display = display
        self.size = size
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", self.size, "-ac"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        os.environ["DISPLAY"] = self.display
        time.sleep(0.5)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.proc:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.proc.wait()


class HumanSimulator:
    """极简拟人交互（github_tool 专用，仅需基本滚动）"""

    @staticmethod
    def random_scroll(page, min_scroll=100, max_scroll=400, times=1):
        for _ in range(times):
            scroll_amount = random.randint(min_scroll, max_scroll)
            page.mouse.wheel(0, scroll_amount)
            page.wait_for_timeout(random.randint(80, 200))
        if random.random() > 0.6:
            page.mouse.wheel(0, -random.randint(50, 200))
            page.wait_for_timeout(random.randint(150, 350))


# ── 核心 Tool ──────────────────────────────────────────────


class GitHubTool(BaseTool):
    name = "github_tool"
    description = (
        "Search and retrieve GitHub repositories. "
        "Supports keyword search, returns structured repo metadata (name, stars, language, description, README summary). "
        "Automatically evaluates project relevance and value."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query keywords (e.g., 'AI agent tool use', 'browser automation python').",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of repos to return (1-30, default 10).",
            },
            "sort": {
                "type": "string",
                "enum": ["stars", "updated", "forks"],
                "description": "Sort by: 'stars' (default), 'updated', or 'forks'.",
            },
            "language": {
                "type": "string",
                "description": "Filter by programming language (e.g., 'python', 'typescript'). Optional.",
            },
            "start_page": {
                "type": "integer",
                "description": "Page number to start from (1-based, default 1).",
            },
            "source": {
                "type": "string",
                "enum": ["github", "gitee"],
                "description": "Search source: 'github' (default) or 'gitee'.",
            },
        },
        "required": ["query"],
    }
    timeout = 45

    def run(
        self,
        query: str,
        max_results: int = 10,
        sort: str = "stars",
        language: str = "",
        start_page: int = 1,
        source: str = "github",
        **kwargs,
    ) -> str:
        if max_results < 1:
            max_results = 10
        if max_results > 30:
            max_results = 30

        encoded_query = urllib.parse.quote(query)
        if source == "gitee":
            base_url = f"https://gitee.com/explore/all?q={encoded_query}&order={sort if sort != 'forks' else 'starred'}"
            if language:
                base_url += f"&lang={urllib.parse.quote(language)}"
        else:
            base_url = f"https://github.com/search?q={encoded_query}&type=repositories&s={sort}&o=desc"
            if language:
                base_url += f"+language:{urllib.parse.quote(language)}"

        try:
            with XvfbManager() as _:
                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=False,
                        args=[
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-blink-features=AutomationControlled",
                            "--disable-infobars",
                            "--window-size=1366,768",
                        ],
                    )

                    context = browser.new_context(
                        viewport={"width": 1366, "height": 768},
                        user_agent=random.choice(UA_POOL),
                    )
                    context.add_init_script(
                        """
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        delete navigator.__proto__.webdriver;
                    """
                    )

                    page = context.new_page()
                    Stealth().apply_stealth_sync(page)
                    page.route(
                        "**/*",
                        lambda route: (
                            route.abort()
                            if route.request.resource_type in ("image", "media", "font")
                            else route.continue_()
                        ),
                    )

                    # 初始加载（Gitee 无需镜像降级，GitHub 需要）
                    if source == "gitee":
                        page.goto(base_url, wait_until="networkidle", timeout=30000)
                        page.wait_for_timeout(random.randint(1000, 1500))
                        HumanSimulator.random_scroll(page, times=1)
                    else:
                        loaded = False
                        for attempt_url in [base_url, f"https://ghproxy.com/{base_url}"]:
                            try:
                                page.goto(attempt_url, wait_until="domcontentloaded", timeout=15000)
                                loaded = True
                                break
                            except Exception:
                                continue
                        if not loaded:
                            browser.close()
                            return "Error: GitHub is unreachable. Try again later."
                        page.wait_for_timeout(random.randint(800, 1200))
                        HumanSimulator.random_scroll(page, times=1)

                    # 如果指定了起始页码，跳到目标页
                    if start_page > 1:
                        if source == "gitee":
                            self._fastest_approach_gitee(page, start_page)
                        else:
                            self._fastest_approach(page, start_page)

                    all_infos = []  # 收集解析后的仓库信息
                    current_page_num = start_page if start_page > 1 else 1

                    while len(all_infos) < max_results:
                        if source == "gitee":
                            # Gitee 探索页解析
                            # 等待页面完全渲染
                            try:
                                page.wait_for_selector('.explore-projects__detail-list', timeout=8000)
                            except Exception:
                                pass
                            cards = page.query_selector_all('.explore-projects__detail-list > div')
                            # 过滤：只保留包含 h3 a 的真正仓库卡片
                            filtered_cards = []
                            for div in cards:
                                if div.query_selector('h3 a'):
                                    filtered_cards.append(div)
                            if not filtered_cards:
                                break

                            # 收集已有仓库名用于去重
                            seen_names = {i['name'] for i in all_infos}
                            for card in filtered_cards:
                                if len(all_infos) >= max_results:
                                    break
                                info = self._parse_gitee_card(card, page)
                                if info and info['name'] not in seen_names:
                                    all_infos.append(info)
                                    seen_names.add(info['name'])
                        else:
                            # GitHub 搜索页解析
                            try:
                                page.wait_for_selector('[data-testid="results-list"]', timeout=8000)
                            except Exception:
                                pass

                            cards = []
                            for sel in [
                                '[data-testid="results-list"] > div',
                                '.repo-list-item',
                                '.search-result-item',
                            ]:
                                cards = page.query_selector_all(sel)
                                if cards:
                                    break

                            if not cards:
                                body_text = page.inner_text("body")
                                if "Sign in to GitHub" in body_text or "login" in body_text.lower():
                                    return "Error: GitHub requires login for this search."
                                break

                            for card in cards:
                                if len(all_infos) >= max_results:
                                    break
                                info = self._parse_repo_card(card, page)
                                if info:
                                    all_infos.append(info)

                        if len(all_infos) >= max_results:
                            break

                        # 翻页
                        target_page = current_page_num + 1
                        success = self._fastest_approach_gitee(page, target_page) if source == "gitee" else self._fastest_approach(page, target_page)
                        if not success:
                            break
                        current_page_num = target_page

                    browser.close()

                    if not all_infos:
                        return f"No repositories found for query '{query}'."

                    # 组装输出
                    source_label = "Gitee" if source == "gitee" else "GitHub"
                    lines = [
                        f"=== {source_label} Search Results: '{query}' ===",
                        f"Showing top {len(all_infos)} results (sorted by {sort})\n",
                    ]
                    for idx, info in enumerate(all_infos, 1):
                        rating = self._rate_project(
                            info["stars"],
                            info["language"],
                            info["description"],
                            info.get("topics", []),
                            query,
                        )
                        lines.append(f"## {idx}. [{info['name']}]({info['url']})")
                        lines.append(
                            f"   ⭐ {info['stars']:,} stars | 🍴 {info['forks']:,} forks | 💻 {info['language']} | 📅 Updated: {info['updated']}"
                        )
                        lines.append(f"   License: {info['license']} | Created: {info['created']}")
                        lines.append(f"   Description: {info['description'][:200]}")
                        if info.get("topics"):
                            lines.append(f"   Topics: {', '.join(info['topics'][:10])}")
                        lines.append(f"   📊 Assessment: {rating}")
                        lines.append("")

                    return "\n".join(lines)

        except Exception as e:
            return f"Error: Failed to search GitHub via browser: {str(e)}"

    def _fastest_approach(self, page, target_page: int) -> bool:
        """
        最快接近算法：扫描当前分页器上所有可点击的页码数字，
        选择离 target_page 最近的一个，直接点击跳转。
        不依赖于写死的 CSS 选择器，只依赖于 a[href*="&p="] 的文本内容。
        """
        for attempt in range(8):  # 最多尝试 8 跳，防止死循环
            # 提取当前页所有分页链接
            page_links = page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*="&p="]');
                return Array.from(links).map(a => ({
                    text: a.innerText.trim(),
                    href: a.getAttribute('href')
                }));
            }""")

            # 收集所有明确的数字页码
            available_pages = set()
            next_href = None
            prev_href = None
            for link in page_links:
                text = link["text"]
                # 纯数字页码
                if text.isdigit():
                    available_pages.add(int(text))
                elif "next" in text.lower() or ">" in text or "»" in text:
                    next_href = link["href"]
                elif "previous" in text.lower() or "<" in text or "«" in text:
                    prev_href = link["href"]

            if target_page in available_pages:
                # 直接点目标
                self._click_page_number(page, target_page)
                return True

            if not available_pages:
                # 没有数字页码，尝试用 Next/Previous 兜底
                if target_page > 1 and next_href:
                    page.goto(next_href, wait_until="domcontentloaded", timeout=20000)
                elif prev_href:
                    page.goto(prev_href, wait_until="domcontentloaded", timeout=20000)
                else:
                    return False
            else:
                # 找最近页码
                best = min(available_pages, key=lambda p: abs(p - target_page))
                if best == target_page:
                    self._click_page_number(page, target_page)
                    return True
                # 点击最佳页码
                self._click_page_number(page, best)

            page.wait_for_timeout(random.randint(800, 1200))
            HumanSimulator.random_scroll(page, times=1)

        return False

    def _fastest_approach_gitee(self, page, target_page: int) -> bool:
        """Gitee 探索页分页器翻页（page= 参数）"""
        for attempt in range(8):
            page_links = page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*="page="]');
                return Array.from(links).map(a => ({
                    text: a.innerText.trim(),
                    href: a.getAttribute('href')
                }));
            }""")

            available_pages = set()
            for link in page_links:
                text = link["text"]
                if text.isdigit():
                    available_pages.add(int(text))

            if target_page in available_pages:
                self._click_page_number_gitee(page, target_page)
                return True

            if not available_pages:
                return False

            best = min(available_pages, key=lambda p: abs(p - target_page))
            self._click_page_number_gitee(page, best)
            page.wait_for_timeout(random.randint(800, 1200))
            HumanSimulator.random_scroll(page, times=1)

        return False

    def _click_page_number_gitee(self, page, page_num: int):
        """点击 Gitee 探索页分页器的数字页码"""
        page.evaluate(
            f"""(num) => {{
                const links = document.querySelectorAll('a[href*="page="]');
                for (let a of links) {{
                    if (a.innerText.trim() === String(num)) {{
                        a.click();
                        return true;
                    }}
                }}
            }}""",
            page_num,
        )
        page.wait_for_load_state("domcontentloaded", timeout=15000)

    def _click_page_number(self, page, page_num: int):
        """在当前页面中点击指定数字的页码链接"""
        page.evaluate(
            f"""(num) => {{
                const links = document.querySelectorAll('a[href*="&p="]');
                for (let a of links) {{
                    if (a.innerText.trim() === String(num)) {{
                        a.click();
                        return true;
                    }}
                }}
                // 降级：直接修改 location
                const url = new URL(window.location.href);
                url.searchParams.set('p', num);
                window.location.href = url.toString();
            }}""",
            page_num,
        )
        page.wait_for_load_state("domcontentloaded", timeout=15000)

    def _parse_repo_card(self, card, page) -> dict | None:
        """从单个仓库卡片 HTML 中提取结构化信息（基于 GitHub 2025+ 真实 DOM）"""
        info: dict = {
            "name": "Unknown", "url": "",
            "stars": 0, "forks": 0, "language": "N/A",
            "description": "(No description)", "license": "Unknown",
            "updated": "", "created": "", "topics": [],
        }

        # 1. 仓库名 + URL
        name_a = card.query_selector('div.search-title a')
        if not name_a:
            name_a = card.query_selector('a[data-testid="results-list-item-link"]')
        if not name_a:
            for a in card.query_selector_all('a'):
                href = (a.get_attribute('href') or '').split('?')[0]
                if re.match(r'^/[^/]+/[^/?#]+$', href):
                    name_a = a
                    break
        if name_a:
            raw = (name_a.get_attribute('href') or '').strip('/')
            m = re.search(r'([^/]+/[^/?#]+)', raw)
            if m:
                info["name"] = m.group(1)
                info["url"] = f"https://github.com/{m.group(1)}"

        # 2. 描述
        desc_el = card.query_selector('div.Content-module__Content__mHmep span')
        if not desc_el:
            desc_el = card.query_selector('[data-testid="results-list-item-description"]')
        if desc_el:
            info["description"] = desc_el.inner_text().strip()[:300]

        # 3. Topics
        topic_els = card.query_selector_all(
            'div.TokenList-module__tokenList__zbitn a, a[data-octo-click*="topic"]'
        )
        info["topics"] = [t.inner_text().strip() for t in topic_els if t.inner_text().strip()]

        # 4. 语言
        lang_el = card.query_selector('span[aria-label$=" language"]')
        if lang_el:
            aria = lang_el.get_attribute('aria-label') or ''
            info["language"] = aria.replace(' language', '').strip()

        # 5. 星数
        star_a = card.query_selector('a[aria-label*="stars"]')
        if star_a:
            aria = star_a.get_attribute('aria-label') or ''
            sm = re.search(r'([\d,]+)\s*stars?', aria, re.IGNORECASE)
            if sm:
                info["stars"] = int(sm.group(1).replace(',', ''))

        # 6. Forks
        fork_a = card.query_selector('a[aria-label*="forks"]')
        if fork_a:
            aria = fork_a.get_attribute('aria-label') or ''
            fm = re.search(r'([\d,]+)\s*forks?', aria, re.IGNORECASE)
            if fm:
                info["forks"] = int(fm.group(1).replace(',', ''))

        # 7. 更新时间
        for li in card.query_selector_all('ul.Footer-module__footer__kjBR4 li'):
            text = li.inner_text().strip()
            if 'Updated' in text:
                info["updated"] = text.replace('Updated', '').strip()
                break

        return info

    def _parse_gitee_card(self, card, page) -> dict | None:
        """从 Gitee 探索页单个仓库卡片提取结构化信息"""
        info: dict = {
            "name": "Unknown", "url": "",
            "stars": 0, "forks": 0, "language": "N/A",
            "description": "(No description)", "license": "Unknown",
            "updated": "", "created": "", "topics": [],
        }

        # 1. 仓库名 + URL (h3 里的 a)
        name_a = card.query_selector('h3 a') or card.query_selector('a[href*="/"]')
        if name_a:
            href = (name_a.get_attribute('href') or '').strip('/')
            info["name"] = href
            info["url"] = f"https://gitee.com/{href}"

        # 2. 描述（精确选择器: div.project-desc）
        text_all = card.inner_text() if card else ""
        desc_el = card.query_selector('[class*="project-desc"]')
        if not desc_el:
            desc_el = card.query_selector('p')
        if desc_el:
            info["description"] = desc_el.inner_text().strip()[:300]
        else:
            # 降级：从文本行中提取，排除仓库名、数字、短标签
            name = info.get("name", "")
            lines = [l.strip() for l in text_all.split('\n') if l.strip()]
            for line in lines:
                if len(line) > 30 and line != name and '/' not in line and not re.match(r'^[\d,.]+[Kk]?$', line):
                    info["description"] = line[:300]
                    break

        # 3. 星数 (格式: "49K" 或 "1.2K" 或 "123")
        star_match = re.search(r'(\d+[\d.]*)\s*[Kk]?', text_all)
        if star_match:
            val = star_match.group(1)
            if 'K' in star_match.group(0) or 'k' in star_match.group(0):
                info["stars"] = int(float(val) * 1000)
            else:
                info["stars"] = int(val.replace(',', ''))

        # 4. 语言（优先用 Gitee 原生 lang= 链接，精确无歧义）
        lang_a = card.query_selector('a[href*="lang="]')
        if lang_a:
            info["language"] = lang_a.inner_text().strip()
        else:
            lang_keywords = ["Python","JavaScript","TypeScript","Java","Go","Rust","C++","C#","Ruby","PHP","Swift","Kotlin","Shell","HTML","CSS","Dart","Vue"]
            for word in lang_keywords:
                if word in text_all:
                    info["language"] = word
                    break

        # 5. 更新时间（格式: "7天前" / "1小时前" / "2025-01-01"）
        time_match = re.search(r'(\d+天前|\d+小时前|\d+分钟前|\d{4}-\d{2}-\d{2})', text_all)
        if time_match:
            info["updated"] = time_match.group(1)

        return info

    def _rate_project(
        self, stars: int, lang: str, description: str, topics: list, query: str
    ) -> str:
        """自动评估项目与查询的相关性和价值"""
        desc_lower = description.lower()
        query_lower = query.lower()
        topics_lower = [t.lower() for t in topics]

        relevance = 0
        for word in query_lower.split():
            if word in desc_lower:
                relevance += 1
            if any(word in t for t in topics_lower):
                relevance += 1

        if stars >= 10000 and relevance >= 2:
            return "🔥 极高关注度 + 高相关性，强烈推荐关注"
        elif stars >= 5000:
            return "⭐ 高关注度项目，值得深入研究"
        elif stars >= 1000:
            return "✨ 中等关注度，有一定社区基础"
        elif stars >= 100:
            return "🔍 新兴项目，可以持续观察"
        else:
            return "🌱 较新或小众项目，具体价值需进一步评估"