# 力工 Agent 2.0 记忆

## 架构
- 前端: `agent.py` (1300行) — 终端UI、对话循环、JSON解析、权限确认
- 桥接: `bridge.py` — Flask + Playwright 操控 DeepSeek 网页
- 工具层: `agent_tools/` — 26个工具 (BaseTool 架构)

## 关键修复 (2026-06-13)
- Ctrl+C 终止: 移除 manager.py 线程池代理 → 信号直通
- 非json块过滤: `_extract_json_blocks` 先清洗所有围栏再提取
- write_file 围栏剥离: 仅剥离首尾独立