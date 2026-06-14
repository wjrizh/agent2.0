"""
paper_tool.py — 全球多源论文搜索与解析工具

支持以下学术搜索引擎：
- arXiv (物理、数学、CS 为主，官方 XML API)
- Semantic Scholar (全学科 2.33亿篇，免费 API)
- OpenAlex (全学科 2.6亿篇，完全免费开放 API)
- CORE.ac.uk (全球开放获取论文)
- CNKI 知网 (Playwright 浏览器抓取)
- Wanfang 万方 (Playwright 浏览器抓取)

所有源按优先级并发搜索，自动去重、合并结果、生成洞察。
"""

import os
import re
import time
import random
import urllib.parse
import xml.etree.ElementTree as ET
import concurrent.futures
from abc import ABC, abstractmethod
from contextlib import contextmanager

from .core import BaseTool

# ==================== 全局 Playwright 上下文管理器 ====================

@contextmanager
def managed_browser(profile_dir: str, accept_downloads: bool = False, block_resources: bool = True):
    """
    统一管理 Xvfb 和 Playwright 上下文的生命周期。
    所有 CNKI/Wanfang 的浏览器操作都通过此函数获取 page 对象。
    """
    from pyvirtualdisplay import Display
    from playwright.sync_api import sync_playwright

    profile_path = os.path.expanduser(profile_dir)
    if not os.path.isdir(profile_path):
        raise ValueError(f"Session directory not found: {profile_path}")

    with Display(visible=0, size=(1280, 800)):
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                headless=False,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                accept_downloads=accept_downloads
            )

            page = context.pages[0] if context.pages else context.new_page()

            if block_resources:
                page.route("**/*", lambda route: route.abort()
                    if route.request.resource_type in ["image", "media", "font"]
                    else route.continue_())

            try:
                yield page, context
            finally:
                context.close()


# ==================== 学术数据源基类 (策略模式 + 模板方法) ====================

class BaseAcademicSource(ABC):
    """学术数据源抽象基类。子类只需实现搜索钩子和三个具体业务接口。"""
    profile_dir: str = ""
    source_name: str = ""

    @abstractmethod
    def _search_url(self, query: str) -> str: ...
    @abstractmethod
    def _wait_selector(self) -> str: ...
    @abstractmethod
    def _item_selector(self) -> str: ...
    @abstractmethod
    def _parse_item(self, item, query: str) -> dict: ...
    @abstractmethod
    def _next_page_selectors(self) -> list: ...

    def _check_blocked(self, page) -> str | None:
        return None

    def _extract_total_pages(self, page) -> str:
        """从分页器提取总页数，子类可覆盖。"""
        return ""

    @abstractmethod
    def _jump_to_page(self, page, target_page): ...

    def _apply_search_filters(self, page, query: str = ""):
        """钩子：子类可覆盖以应用额外搜索过滤（如语种）。默认不操作。"""
        pass

    @abstractmethod
    def fetch_detail(self, detail_url: str) -> dict: ...
    @abstractmethod
    def download_pdf(self, detail_url: str, pdf_url: str, save_path: str) -> str: ...
    @abstractmethod
    def cite(self, detail_url: str) -> str: ...

    def search(self, query: str, max_results: int, page_start: int = 1) -> list:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        papers = []
        total_pages = ""  # 提前初始化变量
        
        try:
            with managed_browser(self.profile_dir, accept_downloads=False, block_resources=True) as (page, _):
                page.goto(self._search_url(query), wait_until="domcontentloaded", timeout=25000)
                
                if self._check_blocked(page):
                    return {"papers": papers, "total_pages": total_pages}
                
                page.wait_for_selector(self._wait_selector(), timeout=15000)
                time.sleep(random.uniform(1.5, 3.0))
                
                # 先过滤再跳页：万方过滤会AJAX刷新重置到第1页
                self._apply_search_filters(page, query)
                page.wait_for_selector(self._wait_selector(), timeout=15000)
                time.sleep(random.uniform(1.5, 3.0))
                
                # 在过滤完成后提取总页数（万方过滤会改变页数）
                total_pages = self._extract_total_pages(page)
                
                # 跳到指定起始页
                self._jump_to_page(page, page_start)
                
                time.sleep(2.5)
                seen_pids = set()
                current_pager_page = 1
                while len(papers) < max_results:
                    items = page.locator(self._item_selector()).all()
                    added_in_current_page = 0
                    for item in items:
                        if len(papers) >= max_results:
                            break
                        try:
                            parsed = self._parse_item(item, query)
                            pid = parsed.get("id", "")
                            if parsed and pid and pid not in seen_pids:
                                seen_pids.add(pid)
                                papers.append(parsed)
                                added_in_current_page += 1
                        except Exception:
                            continue
                    if len(papers) < max_results and len(items) > 0:
                        next_btn = None
                        for sel in self._next_page_selectors():
                            btn = page.locator(sel).first
                            if btn.is_visible() and "disabled" not in (btn.get_attribute("class") or ""):
                                next_btn = btn
                                break
                        if next_btn:
                            current_pager_page += 1
                            time.sleep(random.uniform(2.0, 4.0))
                            next_btn.click()
                            page.wait_for_selector(self._wait_selector(), timeout=15000)
                            time.sleep(random.uniform(1.5, 3.0))
                        else:
                            break
                    else:
                        break
        except PlaywrightTimeout:
            pass
        except Exception:
            pass
            
        return {"papers": papers[:max_results], "total_pages": total_pages}


# ==================== 万方数据源 ====================

class WanfangSource(BaseAcademicSource):
    profile_dir = "~/.agent_wanfang_profile"
    source_name = "万方"

    def _search_url(self, query: str) -> str:
        return f"https://s.wanfangdata.com.cn/paper?q={urllib.parse.quote(query)}"
    def _wait_selector(self) -> str:
        return ".title-area .title"
    def _item_selector(self) -> str:
        return ".normal-list"
    def _next_page_selectors(self) -> list:
        return [".ctrl-btn:not(.occupying)", "a:has-text('>')", ".pager:not(.active)", ".bottom-pagination .ctrl-btn:not(.occupying)"]
    def _check_blocked(self, page) -> str | None:
        if page.locator(".error-page, .not-found").count() > 0:
            return "万方搜索返回异常页面"
        return None

    def _jump_to_page(self, page, target_page):
        """万方最远页码跳跃策略"""
        while target_page > 1:
            pagers = page.locator(".pager").all()
            if not pagers:
                break
            last_visible = int(pagers[-1].inner_text().strip())
            if last_visible >= target_page:
                tgt = page.locator(f".pager:has-text('{target_page}')").first
                if tgt.count() > 0 and tgt.is_visible():
                    tgt.click()
                    page.wait_for_selector(self._wait_selector(), timeout=15000)
                    time.sleep(random.uniform(1.5, 3.0))
                break
            pagers[-1].click()
            page.wait_for_selector(self._wait_selector(), timeout=15000)
            time.sleep(random.uniform(1.5, 3.0))

    def _extract_total_pages(self, page) -> str:
        try:
            info = page.locator(".page-number").first.inner_text()
            return info.split("/")[-1].strip()
        except Exception:
            return ""

    def _apply_search_filters(self, page, query: str = ""):
        """点击万方左侧语种过滤→英文→确定，等待 AJAX 刷新。中文查询跳过。"""
        # 查询含中文字符则跳过英文过滤
        if any('\u4e00' <= c <= '\u9fff' for c in query):
            return
        # 步骤1：点击"英文"
        for sel in ["span:has-text('英文')", ".facet-item:has-text('英文')"]:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click()
                time.sleep(0.5)
                break
        # 步骤2：点击"确定"按钮提交过滤 (万方用 span.fixed-btn-submit)
        for sel in [".fixed-btn-submit", "span:has-text('确定')", "button:has-text('确定')"]:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                page.wait_for_timeout(4000)
                return

    def _parse_item(self, item, query: str) -> dict:
        tl = item.locator(".title").first
        title = tl.inner_text().strip() if tl.count() > 0 else ""
        if not title or len(title) < 3:
            return {}
        # 先提取PID——只要有标题+PID就认为是有效条目
        pid = ""
        idl = item.locator(".title-id-hidden").first
        if idl.count() > 0:
            pid = idl.inner_text().strip()
        if not pid:
            # 无PID的条目丢弃（可能是占位符）
            return {}
        if title and len(title) > 2 and title[0].isdigit() and '.' in title[:4]:
            title = title.split(".", 1)[-1].strip()

        authors = "Unknown"
        aa = item.locator(".author-area").first
        if aa.count() > 0:
            spans = aa.locator("span.authors").all()
            if spans:
                nl = [s.inner_text().strip() for s in spans
                      if not re.match(r'^\d{4}年\d*期$', s.inner_text().strip())
                      and not re.match(r'^\d+$', s.inner_text().strip())
                      and s.inner_text().strip() not in ("等", "")]
                if nl:
                    authors = ", ".join(nl)
            # 英文兼容：回退提取整个 author-area 文本并清洗
            if authors == "Unknown":
                raw_text = aa.inner_text().strip()
                nl = [x.strip() for x in raw_text.split('\n') if x.strip() and not x.strip().isdigit()]
                if nl:
                    authors = ", ".join(nl)

        source = ""
        # 英文兼容：增加外文期刊常见的备用 class
        src = item.locator(".periodical-title, .source-title, .publisher").first
        if src.count() > 0:
            source = src.inner_text().strip()

        it = item.inner_text()
        # 英文兼容：弃用必须带"年"字的正则，改为捕获四位数字年份
        ym = re.findall(r'\b(19\d{2}|20\d{2})\b', it)
        year = ym[0] if ym else ""

        abstract = ""
        # 英文兼容：扩展可能出现的 summary class
        ab = item.locator(".abstract-area, .summary").first
        if ab.count() > 0:
            abstract = ab.inner_text().strip()[:500]

        pid = ""
        idl = item.locator(".title-id-hidden").first
        if idl.count() > 0:
            pid = idl.inner_text().strip()

        if pid:
            if pid.startswith("patent_ZL_") or pid.startswith("periodical_qk3"):
                for pre, pt in [("patent_ZL_", "patent"), ("periodical_qk3", "periodical")]:
                    if pid.startswith(pre):
                        url = f"https://d.wanfangdata.com.cn/{pt}/{pid}"
                        break
                else:
                    url = self._search_url(query)
            elif "_" in pid:
                pt, pv = pid.split("_", 1)
                url = f"https://d.wanfangdata.com.cn/{pt}/{pv}"
            else:
                url = self._search_url(query)
        else:
            url = self._search_url(query)

        return {
            "title": title, "authors": authors, "id": pid or title,
            "url": url, "year": year,
            "abstract": abstract or "需要进入详情页获取",
            "source": f"万方 - {source}" if source else "万方"
        }

    def fetch_detail(self, detail_url: str) -> dict:
        result = {"abstract": "", "keywords": "", "pdf_url": ""}
        try:
            with managed_browser(self.profile_dir) as (page, _):
                page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(3)
                for sel in [".abstract-text", ".detail-abstract", "[class*='abstract']"]:
                    el = page.query_selector(sel)
                    if el:
                        result["abstract"] = el.inner_text().strip()[:1000]
                        break
                for sel in [".keywords-area", "[class*='keyword']"]:
                    el = page.query_selector(sel)
                    if el:
                        result["keywords"] = el.inner_text().strip()[:300]
                        break
                pdf_el = page.query_selector("a[href*='oss.wanfangdata.com.cn/file/download']")
                if pdf_el:
                    result["pdf_url"] = pdf_el.get_attribute("href") or ""
        except Exception:
            pass
        return result

    def download_pdf(self, detail_url: str = "", pdf_url: str = "", save_path: str = "") -> str:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        if not save_path:
            save_path = os.path.expanduser(f"~/桌面/论文/wanfang_paper_{int(time.time())}.pdf")
        save_path = os.path.expanduser(save_path)
        if not pdf_url and detail_url:
            d = self.fetch_detail(detail_url)
            pdf_url = d.get("pdf_url", "")
        if not pdf_url:
            return "Error: 无法获取万方 PDF 下载链接。"
        try:
            with managed_browser(self.profile_dir, accept_downloads=True, block_resources=False) as (page, _):
                ok = False
                try:
                    with page.expect_download(timeout=35000) as di:
                        try:
                            page.goto(pdf_url, timeout=15000)
                        except Exception:
                            pass
                    di.value.save_as(save_path)
                    ok = True
                except PlaywrightTimeout:
                    if "my.wanfangdata.com.cn/user/transaction" in page.url:
                        return "Error: 万方机构登录态可能已失效。"
                    return f"Error: 未触发下载流。URL: {page.url}"
                if ok and os.path.exists(save_path) and os.path.getsize(save_path) > 100000:
                    return f"✅ 万方 PDF 下载完成: {save_path} ({os.path.getsize(save_path) / (1024*1024):.1f} MB)"
                return "⚠️ PDF 下载失败。"
        except Exception as e:
            return f"Error: 万方 PDF 下载异常: {str(e)}"

    def cite(self, detail_url: str) -> str:
        try:
            with managed_browser(self.profile_dir, block_resources=False) as (page, _):
                page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(5)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)
                ok = page.evaluate("""
                    () => {
                        const all = document.querySelectorAll('*');
                        for (const el of all) {
                            if (el.innerText && el.innerText.trim() === '引用') {
                                el.click(); return true;
                            }
                        }
                        return false;
                    }
                """)
                if not ok:
                    return "Error: 未找到万方引用按钮。"
                time.sleep(3)
                gbt = ""
                for d in page.query_selector_all("[class*='export-reference']"):
                    t = d.inner_text().strip()
                    if "参考文献" in t or "复制" in t or len(t) < 20:
                        continue
                    if "[J]" in t or "[D]" in t or "[M]" in t or "[C]" in t:
                        gbt = t
                        break
                    if not gbt:
                        gbt = t
                if not gbt:
                    ge = page.query_selector(".export-reference-GB")
                    if ge:
                        gbt = ge.inner_text().strip()
                if gbt:
                    return f"=== 万方 引用格式 (GB/T 7714) ===\n{gbt}"
                return "Error: 未能提取到引用格式。"
        except Exception as e:
            return f"Error: 万方引用提取失败: {str(e)}"


# ==================== 知网数据源 ====================

class CNKISource(BaseAcademicSource):
    profile_dir = "~/.agent_cnki_profile"
    source_name = "CNKI"

    def _search_url(self, query: str) -> str:
        url = f"https://kns.cnki.net/kns8s/defaultresult/index?kw={urllib.parse.quote(query)}"
        lang = "CHS" if any('\u4e00' <= c <= '\u9fff' for c in query) else "EN"
        return f"{url}&dblang={lang}"
    def _wait_selector(self) -> str:
        return "table.result-table-list"
    def _item_selector(self) -> str:
        return "table.result-table-list tbody tr"
    def _next_page_selectors(self) -> list:
        return [".pagesnums:has-text('下一页')", "a:has-text('下一页')", "a[title='下一页']"]

    def _extract_total_pages(self, page) -> str:
        try:
            body = page.inner_text("body")
            m = __import__('re').search(r'共\s*找到\s*([\d,]+)\s*条\s*结果', body)
            if m:
                total = int(m.group(1).replace(',', ''))
                return str(max(1, total // 20))  # 至少1页
            return "1"  # 兜底：结果少时不显示总页数，默认1页
        except Exception:
            return "1"

    def _jump_to_page(self, page, target_page):
        """知网最远页码跳跃策略"""
        while target_page > 1:
            nums = page.locator(".pagesnums").all()
            visible_nums = []
            for n in nums:
                if n.is_visible() and n.inner_text().strip().isdigit():
                    visible_nums.append(int(n.inner_text().strip()))
            if not visible_nums:
                break
            last = max(visible_nums)
            if last >= target_page:
                tgt = page.locator(f".pagesnums:has-text('{target_page}')").first
                if tgt.count() > 0 and tgt.is_visible():
                    tgt.click()
                    page.wait_for_selector(self._wait_selector(), timeout=15000)
                    time.sleep(random.uniform(1.5, 3.0))
                break
            last_el = None
            for n in nums:
                if n.is_visible() and n.inner_text().strip() == str(last):
                    last_el = n
                    break
            if last_el:
                last_el.click()
                page.wait_for_selector(self._wait_selector(), timeout=15000)
                time.sleep(random.uniform(1.5, 3.0))

    def _check_blocked(self, page) -> str | None:
        if "安全验证" in page.title() or "验证码" in page.title():
            return "触发知网安全验证"
        return None

    def _apply_search_filters(self, page, query: str = ""):
        """知网语言切换：根据查询内容点击'中文'或'外文'按钮恢复Cookie"""
        target = "中文" if any('\u4e00' <= c <= '\u9fff' for c in query) else "外文"
        for el in page.query_selector_all(".switch-ChEn a"):
            t = el.inner_text().strip()
            if t == target and el.is_visible():
                el.click()
                page.wait_for_selector(self._wait_selector(), timeout=15000)
                time.sleep(random.uniform(1.5, 3.0))
                return

    def _parse_item(self, item, query: str) -> dict:
        tl = item.locator(".name a").first
        if tl.count() == 0:
            return {}
        title = tl.inner_text().strip()
        url_suffix = tl.get_attribute("href") or ""
        url = f"https://kns.cnki.net{url_suffix}" if url_suffix.startswith("/") else url_suffix
        paper_id = ""
        if "FileName=" in url:
            paper_id = url.split("FileName=")[1].split("&")[0]
        elif "DbCode=" in url:
            paper_id = url.split("DbCode=")[1].split("&")[0]
        if not paper_id:
            paper_id = url  # 直接使用完整URL作为唯一ID
        authors = "Unknown"
        al = item.locator(".author").first
        if al.count() > 0:
            authors = al.inner_text().strip().replace("\n", " ")
        source = "CNKI"
        sl = item.locator(".source a").first
        if sl.count() > 0:
            source = sl.inner_text().strip()
        year = ""
        dl = item.locator(".date").first
        if dl.count() > 0:
            year = dl.inner_text().strip()[:4]
        return {
            "title": title, "authors": authors, "id": paper_id,
            "url": url, "year": year,
            "abstract": "需要进入详情页获取",
            "source": f"CNKI - {source}"
        }

    def fetch_detail(self, detail_url: str) -> dict:
        result = {"abstract": "", "keywords": "", "pdf_url": ""}
        try:
            with managed_browser(self.profile_dir) as (page, _):
                page.goto(detail_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(random.uniform(3.0, 5.0))
                for sel in [".abstract-text", "#abstract", ".abstract", "[class*='abstract']", ".row-abstract", "div.abstract"]:
                    el = page.query_selector(sel)
                    if el:
                        result["abstract"] = el.inner_text().strip()[:1000]
                        break
                for sel in [".keywords", "#keywords", "[class*='keyword']", ".kw-group", ".kw-main"]:
                    el = page.query_selector(sel)
                    if el:
                        result["keywords"] = el.inner_text().strip()[:300]
                        break
        except Exception:
            pass
        return result

    def download_pdf(self, detail_url: str = "", pdf_url: str = "", save_path: str = "") -> str:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        if not save_path:
            save_path = os.path.expanduser(f"~/桌面/论文/cnki_paper_{int(time.time())}.pdf")
        save_path = os.path.expanduser(save_path)
        if not detail_url:
            return "Error: 知网 PDF 下载必须提供 detail_url。"
        try:
            with managed_browser(self.profile_dir, accept_downloads=True) as (page, _):
                page.goto(detail_url, wait_until="domcontentloaded", timeout=25000)
                time.sleep(random.uniform(3.0, 5.0))
                if "安全验证" in page.title():
                    return "Error: 知网触发了安全验证页面。"
                pdf_btn = page.query_selector("a:has-text('PDF下载')")
                if not pdf_btn:
                    return "未在详情页找到 PDF下载 按钮。"
                with page.expect_download(timeout=45000) as di:
                    pdf_btn.click()
                di.value.save_as(save_path)
                if os.path.exists(save_path) and os.path.getsize(save_path) > 100000:
                    return f"✅ PDF 下载完成: {save_path} ({os.path.getsize(save_path) / (1024*1024):.1f} MB)"
                if os.path.exists(save_path) and os.path.getsize(save_path) < 100000:
                    os.remove(save_path)
                    return "⚠️ PDF 下载失败：文件异常小。"
                return "⚠️ PDF 下载未触发。"
        except Exception as e:
            return f"Error: PDF 下载失败: {str(e)}"

    def cite(self, detail_url: str) -> str:
        try:
            with managed_browser(self.profile_dir) as (page, _):
                page.goto(detail_url, wait_until="domcontentloaded", timeout=25000)
                time.sleep(random.uniform(3.0, 5.0))
                if "安全验证" in page.title():
                    return "Error: 知网触发了安全验证页面。"
                qb = page.query_selector(".btn-quote, li:has-text('引用')")
                if not qb:
                    qb = page.query_selector("li.btn-quote, li.quote")
                if not qb:
                    return "Error: 未找到知网引用按钮。"
                qb.click()
                time.sleep(2)
                ta = page.query_selector("textarea.text")
                if ta:
                    citation = ta.input_value().strip()
                    if citation:
                        return f"=== CNKI 引用格式 (GB/T 7714) ===\n{citation}"
                return "Error: 未能提取到引用格式。"
        except Exception as e:
            return f"Error: 知网引用提取失败: {str(e)}"


# ==================== PaperTool 主类 (Dispatcher) ====================

class PaperTool(BaseTool):
    name = "paper_tool"
    description = (
        "Multi-source global academic paper search and retrieval. "
        "Searches arXiv, Semantic Scholar, OpenAlex, and CORE.ac.uk simultaneously. "
        "Returns structured paper metadata (title, authors, abstract, PDF link). "
        "Automatically generates one-line insights from abstracts."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "fetch_detail", "download_pdf", "cite"],
                "description": "'search' to search papers, 'fetch_detail' to extract abstract/PDF link, 'download_pdf' to download a CNKI/Wanfang paper PDF, 'cite' to extract GB/T 7714 citation format."
            },
            "query": {"type": "string", "description": "Search query keywords (required for 'search'). Supports both English and Chinese."},
            "max_results": {"type": "integer", "description": "Maximum number of papers to return per source (1-50, default 5)."},
            "sources": {"type": "string", "description": "Comma-separated list of sources: 'arxiv', 'semantic', 'openalex', 'core', 'cnki', 'wanfang', 'all' (default: 'all')."},
            "sort_by": {"type": "string", "enum": ["relevance", "date"], "description": "Sort by relevance (default) or date."},
            "detail_url": {"type": "string", "description": "Paper detail URL. For CNKI download_pdf, MUST use the 'Link' field from search results (kns.cnki.net/kcms2/article/abstract?v=...). Do NOT use pdf_url for CNKI downloads."},
            "pdf_url": {"type": "string", "description": "PDF download URL. Only for Wanfang OSS links (oss.wanfangdata.com.cn). CNKI bar.cnki.net links are NOT supported — use detail_url instead."},
            "save_path": {"type": "string", "description": "Local file path to save the downloaded PDF (optional, defaults to ~/桌面/论文/<title>.pdf)."},
            "page_start": {"type": "integer", "description": "Start page number for search (default 1). Only for 'search' action with browser sources (cnki/wanfang)."}
        },
        "required": ["action"]
    }
    timeout = 60

    def __init__(self):
        super().__init__()
        self._browser_sources = {"cnki": CNKISource(), "wanfang": WanfangSource()}

    def _get_source_from_url(self, url: str) -> BaseAcademicSource:
        if "cnki.net" in url:
            return self._browser_sources["cnki"]
        if "wanfangdata.com.cn" in url:
            return self._browser_sources["wanfang"]
        raise ValueError(f"无法识别来源的 URL: {url}")

    def run(self, action: str = "search", query: str = "", max_results: int = 5,
            sources: str = "all", sort_by: str = "relevance",
            detail_url: str = "", pdf_url: str = "", save_path: str = "",
            page_start: int = 1, **kwargs) -> str:

        # fetch_detail
        if action == "fetch_detail":
            if not detail_url:
                return "Error: 'detail_url' is required."
            try:
                s = self._get_source_from_url(detail_url)
                r = s.fetch_detail(detail_url)
                if r.get("abstract") or r.get("keywords"):
                    return (f"=== {s.source_name} Paper Detail ===\n"
                            f"Abstract: {r.get('abstract', 'N/A')[:800]}\n"
                            f"Keywords: {r.get('keywords', 'N/A')[:300]}\n"
                            f"PDF: {r.get('pdf_url', 'N/A')}")
                return "未能提取到摘要和关键词。"
            except ValueError as e:
                return str(e)

        # cite
        if action == "cite":
            if not detail_url:
                return "Error: 'detail_url' is required."
            try:
                return self._get_source_from_url(detail_url).cite(detail_url)
            except ValueError as e:
                return str(e)

        # download_pdf
        if action == "download_pdf":
            target = pdf_url if pdf_url else detail_url
            if not target:
                return "Error: Either 'detail_url' or 'pdf_url' is required."
            if "bar.cnki.net" in target:
                return "Error: 知网 PDF 直链 (bar.cnki.net) 不支持直接下载，请使用搜索得到的 detail_url。"
            try:
                s = self._get_source_from_url(target)
                if "cnki.net" in target and "kcms2/article/abstract" in target and not detail_url:
                    return s.download_pdf(detail_url=target, save_path=save_path)
                return s.download_pdf(detail_url=detail_url, pdf_url=pdf_url, save_path=save_path)
            except ValueError as e:
                return str(e)

        # search
        if max_results < 1:
            max_results = 5
        if max_results > 50:
            max_results = 50
        if sources == "all":
            source_list = ["arxiv", "semantic", "openalex", "cnki", "wanfang"]
        else:
            source_list = [s.strip().lower() for s in sources.split(",") if s.strip()]

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            for src in source_list:
                if src == "arxiv":
                    futures[executor.submit(self._search_arxiv, query, max_results, sort_by)] = "arXiv"
                elif src in ["semantic", "semanticscholar"]:
                    futures[executor.submit(self._search_semantic_scholar, query, max_results)] = "Semantic Scholar"
                elif src == "openalex":
                    futures[executor.submit(self._search_openalex, query, max_results, sort_by)] = "OpenAlex"
                elif src == "core":
                    futures[executor.submit(self._search_core, query, max_results)] = "CORE.ac.uk"

            for src in source_list:
                if src == "cnki":
                    try:
                        results["CNKI"] = self._browser_sources["cnki"].search(query, max_results, page_start)
                    except Exception:
                        results["CNKI"] = []
                elif src == "wanfang":
                    try:
                        results["万方"] = self._browser_sources["wanfang"].search(query, max_results, page_start)
                    except Exception:
                        results["万方"] = []

            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception:
                    results[name] = []

        # 解包搜索结果为 papers 和元数据
        parsed_results = {}
        for source_name, result in results.items():
            if isinstance(result, dict):
                parsed_results[source_name] = result.get("papers", [])
                if source_name in ["CNKI", "万方"] and result.get("total_pages"):
                    # 记录总页数用于输出
                    pass
            else:
                parsed_results[source_name] = result

        seen = set()
        merged = []
        for src_name, papers in parsed_results.items():
            for paper in papers:
                key = paper.get("id") or paper.get("title", "")
                if key and key not in seen:
                    seen.add(key)
                    paper["source"] = src_name
                    merged.append(paper)

        if not merged:
            return f"No papers found for query '{query}' across all sources."

        lines = [f"=== Global Paper Search: '{query}' ({len(merged)} papers from {len(source_list)} sources) ===\n"]
        # 加总页数信息
        if "万方" in results and isinstance(results["万方"], dict) and results["万方"].get("total_pages"):
            lines.append(f"📊 万方总页数: {results['万方']['total_pages']} | 每页约 20 条\n")
        if "CNKI" in results and isinstance(results["CNKI"], dict) and results["CNKI"].get("total_pages"):
            lines.append(f"📊 知网总页数: {results['CNKI']['total_pages']} | 每页 20 条\n")
        for idx, paper in enumerate(merged, 1):
            title = paper.get("title", "Unknown")
            authors = paper.get("authors", "Unknown")
            pid = paper.get("id", "")
            url = paper.get("url", paper.get("pdf_url", ""))
            year = paper.get("year", "")
            abstract = paper.get("abstract", "")
            src_label = paper.get("source", "Unknown")
            sa = abstract[:300] + "..." if len(abstract) > 300 else abstract
            insight = self._generate_insight(title, abstract, query)
            lines.append(f"## {idx}. {title}")
            lines.append(f"   Authors: {authors}")
            lines.append(f"   Source: {src_label} | Year: {year} | ID: {pid}")
            if url:
                lines.append(f"   Link: {url}")
            lines.append(f"   Abstract: {sa}")
            lines.append(f"   💡 Insight: {insight}")
            lines.append("")

        sc = {}
        for p in merged:
            s = p.get("source", "?")
            sc[s] = sc.get(s, 0) + 1
        lines.append("---")
        lines.append(f"📊 Source breakdown: {', '.join(f'{k}: {v}' for k, v in sc.items())}")
        return "\n".join(lines)

    # ==================== arXiv API ====================
    def _search_arxiv(self, query: str, max_results: int, sort_by: str) -> list:
        eq = urllib.parse.quote(query)
        sm = {"relevance": "relevance", "date": "lastUpdatedDate"}
        api_url = (f"http://export.arxiv.org/api/query?search_query=all:{eq}&start=0&max_results={max_results}&sortBy={sm.get(sort_by, 'relevance')}")
        try:
            import requests
            resp = requests.get(api_url, timeout=15, headers={"User-Agent": "Agent2.0-PaperTool/1.0 (mailto:2634749421@qq.com)"})
            resp.raise_for_status()
        except Exception:
            return []
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        papers = []
        try:
            root = ET.fromstring(resp.text)
            for entry in root.findall("atom:entry", ns):
                title = entry.findtext("atom:title", "Unknown", ns).strip().replace("\n", " ")
                authors = ", ".join(a.findtext("atom:name", "Unknown", ns).strip() for a in entry.findall("atom:author", ns))[:5]
                arxiv_id = entry.findtext("atom:id", "", ns).strip().replace("http://arxiv.org/abs/", "")
                abstract = " ".join(entry.findtext("atom:summary", "", ns).strip().split())
                pdf_link = ""
                for link in entry.findall("atom:link", ns):
                    if link.get("title") == "pdf":
                        pdf_link = link.get("href", "")
                        break
                year = entry.findtext("atom:published", "", ns).strip()[:4]
                papers.append({"title": title, "authors": authors, "id": arxiv_id, "url": f"https://arxiv.org/abs/{arxiv_id}", "pdf_url": pdf_link, "year": year, "abstract": abstract})
        except ET.ParseError:
            pass
        return papers

    # ==================== Semantic Scholar API ====================
    def _search_semantic_scholar(self, query: str, max_results: int) -> list:
        eq = urllib.parse.quote(query)
        api_url = (f"https://api.semanticscholar.org/graph/v1/paper/search?query={eq}&limit={max_results}&fields=title,authors,year,abstract,externalIds,url")
        try:
            import requests
            resp = requests.get(api_url, timeout=15, headers={"User-Agent": "Agent2.0-PaperTool/1.0 (mailto:2634749421@qq.com)"})
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []
        papers = []
        for item in data.get("data", []):
            papers.append({"title": item.get("title", "Unknown"), "authors": ", ".join(a.get("name", "Unknown") for a in item.get("authors", [])[:5]), "id": item.get("paperId", ""), "url": item.get("url") or f"https://www.semanticscholar.org/paper/{item.get('paperId', '')}", "year": str(item.get("year", "") or ""), "abstract": (item.get("abstract") or "")[:500]})
        return papers

    # ==================== OpenAlex API ====================
    def _search_openalex(self, query: str, max_results: int, sort_by: str) -> list:
        eq = urllib.parse.quote(query)
        sp = "relevance_score:desc" if sort_by == "relevance" else "publication_date:desc"
        api_url = (f"https://api.openalex.org/works?search={eq}&per_page={max_results}&sort={sp}")
        try:
            import requests
            resp = requests.get(api_url, timeout=15, headers={"User-Agent": "Agent2.0-PaperTool/1.0 (mailto:2634749421@qq.com)"})
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []
        papers = []
        for item in data.get("results", []):
            authors = ", ".join(a.get("author", {}).get("display_name", "Unknown") for a in item.get("authorships", [])[:5])
            pid = item.get("id", "").split("/")[-1] if item.get("id") else ""
            abstract = ""
            if item.get("abstract_inverted_index"):
                inv = item["abstract_inverted_index"]
                wm = {}
                for w, ps in inv.items():
                    for p in ps:
                        wm[p] = w
                abstract = " ".join(wm[k] for k in sorted(wm))
            year = item.get("publication_year", "")
            doi = item.get("doi", "")
            url = f"https://doi.org/{doi}" if doi else item.get("primary_location", {}).get("landing_page_url", "")
            papers.append({"title": item.get("title", "Unknown"), "authors": authors, "id": pid, "url": url, "year": str(year) if year else "", "abstract": abstract[:500]})
        return papers

    # ==================== CORE.ac.uk API ====================
    def _search_core(self, query: str, max_results: int) -> list:
        eq = urllib.parse.quote(query)
        api_url = (f"https://api.core.ac.uk/v3/search/works?q={eq}&limit={max_results}")
        try:
            import requests
            resp = requests.get(api_url, timeout=15, headers={"User-Agent": "Agent2.0-PaperTool/1.0 (mailto:2634749421@qq.com)"})
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []
        papers = []
        for item in data.get("results", []):
            papers.append({"title": item.get("title", "Unknown"), "authors": ", ".join(a.get("name", "Unknown") for a in item.get("authors", [])[:5]), "id": str(item.get("id", "")), "url": item.get("downloadUrl") or item.get("sourceFulltextUrls", [""])[0], "year": str(item.get("yearPublished", "") or ""), "abstract": (item.get("abstract") or "")[:500]})
        return papers

    # ==================== 洞察生成 ====================
    def _generate_insight(self, title: str, abstract: str, query: str) -> str:
        tl = title.lower(); al = abstract.lower()
        if "survey" in tl or "review" in tl:
            return "综述文章，适合快速了解领域全貌"
        if "benchmark" in tl or "evaluation" in tl:
            return "基准测试/评估文章，可了解当前方法性能对比"
        if "framework" in tl or "architecture" in tl:
            return "提出了新的框架/架构设计"
        if "agent" in tl and ("tool" in al or "act" in al):
            return "涉及 AI Agent 的工具使用或行为研究"
        if "retrieval" in al or "rag" in al:
            return "涉及检索增强生成 (RAG) 技术"
        return "与搜索主题相关，建议阅读摘要后深入评估"