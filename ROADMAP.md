# Agent2.0 功能进化路线图

> 基于对国内外前沿项目、论文（arXiv）及 GitHub 高星项目的深度分析，为 力工 (agent2.0) 量身打造的进化蓝图。

## 1. 现有能力总结

我们已经拥有了非常强大的本地开发Agent基础：
-   **核心引擎**：PTY流控、sudo自动注入、无畏模式。
-   **代码智能**：LSP静态分析、RAG语义搜索。
-   **网络交互**：浏览器自动化（搜索、嗅探、磁力下载）。
-   **任务管理**：任务创建、定时任务。
-   **通信**：邮件收发。

## 2. 进化方向一：MCP协议集成 —— 成为生态的一部分

### 灵感来源
-   **Model Context Protocol (MCP)** 正迅速成为AI Agent连接外部世界的标准化协议，如同HTTP之于网络。
-   **Cline CLI** 和 **Copilot** 等工具已支持MCP，允许第三方开发者创建“插件”，Agent可自由调用。

### 为什么我们需要？
-   **打破能力边界**：目前我们的工具是固定的。集成MCP后，agent2.0将能调用整个生态圈的工具，例如直接读取Notion、Jira、Slack的数据。
-   **降低开发成本**：不需要我们为每个新功能写代码，很多现成的MCP Server可以直接用。

### 实施路径
-   **作为MCP Client**：在agent2.0内部实现一个轻量级的MCP客户端，能发现并连接用户配置的MCP Server。
-   **作为MCP Server**：将agent2.0最独特的能力（如PTY执行、浏览器嗅探）包装成MCP Server，供其他AI Agent（如Claude Code）调用。这将极大提升我们的项目影响力！

## 3. 进化方向二：技能与工作流 (Skill & Workflow) —— 记忆最佳实践

### 灵感来源
-   **Claude Code 的 Slash Commands**: 用户可以定义复杂、可分发的命令。
-   **Cline 的 Skills**: 一个独立的技能系统，允许AI在执行特定任务时加载预定义的“知识包”和“最佳实践”。
-   **OpenHands / All Hands AI**: 工作流自动化与任务分解。

### 为什么我们需要？
-   **知识固化**：你教给力工的一套特定流程（如“发布新版本”），可以保存为一个技能文件，以后随时复用，避免遗忘或错误。
-   **社区共享**：可以创建一个“技能市场”，让其他力工用户分享他们的工作流（例如：“一键部署WordPress到AWS”、“STM32开发环境搭建”）。

### 实施路径
-   创建一个`skills/`目录，支持`.md`或`.yaml`格式的技能文件。
-   技能文件定义：`name`, `description`, `triggers` (何时触发), `steps` (执行步骤, 每一步可以是工具调用或用户输入)。
-   在主循环中增加一个“技能匹配”层，根据用户输入自动推荐或执行相关技能。

## 4. 进化方向三：高级记忆与多Agent协作

### 灵感来源
-   **MemGPT / Letta**: 核心思想是“操作系统式记忆管理”，让LLM自己管理自己的上下文（类似虚拟内存）。
-   **LangGraph / CrewAI / Microsoft AutoGen**: 多Agent协作框架，将复杂任务拆解给不同的“角色”Agent并行处理。

### 为什么我们需要？
-   **突破记忆限制**：目前我们的历史记录会因Token窗口而丢失。通过“记忆分层”（短期、中期、长期），力工能记住更多、更久远的事情。
-   **处理复杂任务**：对于一个大型项目，力工可以启动一个“侦察Agent”去分析代码结构，一个“文档Agent”去生成文档，自己则作为“主控Agent”进行决策和代码编写。

### 实施路径
-   **短期**：实现一个简单的长期记忆系统，将重要对话摘要存入向量数据库，下次对话时检索。
-   **中期**：引入任务分解，允许力工在内部创建“子任务”并分配给自己的子线程处理（类似于CrewAI的思路，但更轻量）。

## 5. 进化方向四：高级自动化与环境交互

### 灵感来源
-   **Browser-Use / Stagehand**: AI驱动的浏览器自动化，不再是简单的“请求-响应”，而是能理解页面结构并自主规划操作路径。
-   **Anthropic Computer Use**: AI直接操作桌面环境，识别屏幕内容，模拟键盘鼠标。
-   **Self-Correction**: 当工具调用失败时，AI能根据报错信息自动调整参数并重试，形成自我修复循环。

### 为什么我们需要？
-   **处理动态网页**：我们的`sniff`和`goto`很强，但面对需要复杂交互（如登录后操作）的动态网站仍然困难。
-   **操作GUI应用**：如果能模拟键鼠，力工就能替你操作那些没有API的桌面软件。
-   **更智能的错误处理**：让力工不再“一次调用失败就放弃”，而是自己尝试修复问题，直到成功。

### 实施路径
-   **短期**：引入“自我修正”逻辑，当工具调用失败（如`update_file`匹配失败），AI自动分析原因并重试。
-   **中期**：集成`pyautogui`或`xdotool`，为力工增加一个`gui_click`和`gui_type`工具。
-   **长期**：探索Anthropic Computer Use，实现完整的屏幕理解与操作。

## 6. 持续调研日志 (Research Log)

> 此区域会持续更新，记录我们发现的有价值的论文、GitHub项目和最新趋势。

### 论文跟踪
| 日期 | 来源 | 标题/方向 | 关键洞察 | 对agent2.0的启示 |
|------|------|-----------|----------|-------------------|
| 2026-06 | arXiv | Agent AI: Surveying the Horizons of Multimodal Interaction | 综述了多模态Agent的四大方向：感知、规划、记忆、工具使用 | 我们的工具使用已经很强，但缺乏感知（视觉）和高级规划能力 |
| 2026-06 | arXiv | SWE-Agent: Agent-Computer Interfaces for Automated Software Engineering | 提出了ACI（Agent-Computer Interface）概念，让Agent像人一样操作IDE | 我们可以为力工设计专门的“计算机接口”，让它操作VSCode或终端 |
| 2026-06 | arXiv | ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs | 提出了指令增强和负例采样的方法，让模型能理解任何API | 对我们集成MCP和设计工具Schema有指导意义 |

### GitHub 项目跟踪
| 日期 | 项目 | Stars | 核心能力 | 对agent2.0的启示 |
|------|------|-------|----------|-------------------|
| 2026-06 | Cline (formerly Claude Code) | 60k+ | Skill系统、MCP支持、插件市场 | 我们可以学习其Skill系统和Plugin架构 |
| 2026-06 | OpenHands (formerly OpenDevin) | 50k+ | 多Agent协作、容器化沙箱、CodeAct | 沙箱执行环境是其核心竞争力，我们可以用Docker封装力工 |
| 2026-06 | Browser-Use | 40k+ | AI驱动的浏览器自动化，自主理解DOM | 我们的`sniff`和`goto`可以升级为真正的“AI浏览器操作” |
| 2026-06 | CrewAI | 30k+ | 多Agent角色扮演、任务委派 | 极其适合我们实现“多Agent协作” |
| 2026-06 | Letta (MemGPT) | 18k+ | 操作系统式记忆管理、虚拟上下文 | 可以作为我们长期记忆模块的底层引擎 |
| 2026-06 | SWE-agent | 15k+ | ACI (Agent-Computer Interface) | 专为软件工程设计的Agent接口，理念高度契合 |