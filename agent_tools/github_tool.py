"""
github_tool.py — GitHub 仓库爬取与评估工具

基于 GitHub Search API，支持：
- 按关键词搜索仓库
- 获取名称、Star 数、语言、README 摘要、License
- 自动分析项目价值
"""

import urllib.parse
import json
import re
from .core import BaseTool


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
                "description": "Search query keywords (e.g., 'AI agent tool use', 'browser automation python')."
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of repos to return (1-30, default 10)."
            },
            "sort": {
                "type": "string",
                "enum": ["stars", "updated", "forks"],
                "description": "Sort by: 'stars' (default), 'updated', or 'forks'."
            },
            "language": {
                "type": "string",
                "description": "Filter by programming language (e.g., 'python', 'typescript'). Optional."
            }
        },
        "required": ["query"]
    }
    timeout = 30

    def run(self, query: str, max_results: int = 10, sort: str = "stars", language: str = "", **kwargs) -> str:
        """搜索 GitHub 仓库并返回结构化结果"""
        if max_results < 1:
            max_results = 10
        if max_results > 30:
            max_results = 30

        encoded_query = urllib.parse.quote(query)
        api_url = (
            f"https://api.github.com/search/repositories?"
            f"q={encoded_query}"
            f"&sort={sort}"
            f"&per_page={max_results}"
        )
        if language:
            api_url += f"+language:{urllib.parse.quote(language)}"

        try:
            import requests
            headers = {
                "User-Agent": "Agent2.0-GitHubTool/1.0",
                "Accept": "application/vnd.github.v3+json"
            }
            resp = requests.get(api_url, timeout=self.timeout, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return f"Error: Failed to fetch GitHub data: {str(e)}"

        items = data.get("items", [])
        if not items:
            return f"No repositories found for query '{query}'."

        total_count = data.get("total_count", 0)
        lines = [
            f"=== GitHub Search Results: '{query}' ===",
            f"Total matches: {total_count:,} | Showing top {len(items)} (sorted by {sort})\n"
        ]

        for idx, repo in enumerate(items, 1):
            name = repo.get("full_name", "Unknown")
            stars = repo.get("stargazers_count", 0)
            forks = repo.get("forks_count", 0)
            lang = repo.get("language", "N/A")
            description = repo.get("description", "") or "(No description)"
            html_url = repo.get("html_url", "")
            topics = repo.get("topics", [])
            license_info = repo.get("license")
            license_name = license_info.get("spdx_id", "Unknown") if license_info else "Unknown"
            updated_at = repo.get("updated_at", "")[:10]
            created_at = repo.get("created_at", "")[:10]

            # 自动评估
            rating = self._rate_project(stars, lang, description, topics, query)

            lines.append(f"## {idx}. [{name}]({html_url})")
            lines.append(f"   ⭐ {stars:,} stars | 🍴 {forks:,} forks | 💻 {lang} | 📅 Updated: {updated_at}")
            lines.append(f"   License: {license_name} | Created: {created_at}")
            lines.append(f"   Description: {description[:200]}")
            if topics:
                lines.append(f"   Topics: {', '.join(topics[:10])}")
            lines.append(f"   📊 Assessment: {rating}")
            lines.append("")

        return "\n".join(lines)

    def _rate_project(self, stars: int, lang: str, description: str, topics: list, query: str) -> str:
        """自动评估项目与查询的相关性和价值"""
        desc_lower = description.lower()
        query_lower = query.lower()
        topics_lower = [t.lower() for t in topics]

        # 计算相关性得分
        relevance = 0
        for word in query_lower.split():
            if word in desc_lower:
                relevance += 1
            if any(word in t for t in topics_lower):
                relevance += 1

        # 评级
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