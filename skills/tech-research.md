# 技能：技术调研 (Tech Research)
# 触发词：调研、论文搜索、GitHub 搜索、找项目、搜索论文、爬论文、爬 GitHub
# 当用户输入匹配触发词时，力工应自动执行以下流程

name: tech-research
description: 通用技术调研流程 — 搜索论文、GitHub 项目、官网文档，自动整理摘要和洞察
triggers:
  - 调研
  - 论文搜索
  - GitHub 搜索
  - 找项目
  - 搜索论文
  - 爬论文
  - 爬 GitHub
  - 技术调研
  - 调研报告

steps:
  # ==================== 第一步：多路搜索 ====================
  - step: 1
    action: 并行搜索多个信息源
    description: "同时从 arXiv 论文库、GitHub 仓库、Bing 网页三个渠道搜索用户指定的关键词"
    tools:
      - tool: arxiv_tool
        params:
          query: "{从用户输入中提取的关键词，英文}"
          max_results: 5
          sort_by: relevance
        说明: "搜索 arXiv 论文库，获取学术研究"
      
      - tool: github_tool
        params:
          query: "{从用户输入中提取的关键词，英文}"
          max_results: 10
          sort: stars
        说明: "搜索 GitHub 仓库，按 Star 数排序"
      
      - tool: browser_tool
        params:
          action: search
          query: "{从用户输入中提取的关键词} 官网"
        说明: "用 Bing 搜索项目/技术的官方网站"

  # ==================== 第二步：访问官网获取详细信息 ====================
  - step: 2
    action: 访问官方网站获取详细信息
    description: "从 Bing 搜索结果中找到官方网站链接，用 goto 访问并提取内容"
    tools:
      - tool: browser_tool
        params:
          action: goto
          url: "{从 Bing 搜索结果中提取的官网 URL}"
        说明: "访问官网获取详细的产品/技术信息"

  # ==================== 第三步：在官网内搜索更多内容 ====================
  - step: 3
    action: 在官方网站内进行站内搜索
    description: "如果官网提供了搜索功能，在官网内搜索更多相关内容"
    tools:
      - tool: browser_tool
        params:
          action: search
          query: "site:{官网域名} {关键词}"
        说明: "用 site: 语法在官网内搜索更多细节"

  # ==================== 第四步：整理分析结果 ====================
  - step: 4
    action: 整理调研结果
    description: "将前面步骤收集到的论文、GitHub 项目、官网信息整理成结构化的调研报告"
    output_format: |
      ## 技术调研报告：{原始搜索关键词}
      
      ### 一、学术论文 (来自 arXiv)
      | 序号 | 标题 | 年份 | 核心洞察 |
      |------|------|------|----------|
      | 1 | {论文标题} | {年份} | {一句话洞察} |
      ...
      
      ### 二、开源项目 (来自 GitHub)
      | 序号 | 项目名 | Stars | 语言 | 核心价值 |
      |------|--------|-------|------|----------|
      | 1 | {项目名} | {Star数} | {语言} | {核心价值评估} |
      ...
      
      ### 三、官方网站资源
      - 官网地址: {URL}
      - 最新动态: {从官网提取的关键信息}
      
      ### 四、综合建议
      {基于所有信息的综合分析，给用户的建议}

    tool: write_file
    params:
      path: "~/space/research/{关键词}_调研报告_{日期}.md"
      content: "{整理好的报告内容}"
    说明: "将调研报告保存到本地文件"