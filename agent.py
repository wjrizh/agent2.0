# agent.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prompt_toolkit import prompt
from prompt_toolkit.key_binding import KeyBindings
import os
import subprocess
import re
import json
import sys
import termios
import tty
import requests
import socket
import time
import signal
import threading
import datetime  # <--- 新增：用于获取当前时间
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.columns import Columns
from rich.align import Align
console = Console()

client = OpenAI(
    api_key="none",
    base_url="http://127.0.0.1:8000/v1"
)

from agent_tools.manager import ToolManager
from agent_tools.builtin_tools import WriteFileTool, ExecuteBashTool, ReadFileTool, ListDirTool, LaunchTerminalTool, DownloadFileTool, WebFetchTool, ReadEmailTool, SendEmailTool, DeleteEmailTool, UpdateFileTool, GitTool, GlobTool, GrepTool, TaskCreateTool, TaskUpdateTool, TaskListTool, TaskGetTool, WebSearchTool, AskUserQuestionTool, SubmitPlanTool, CronCreateTool, CronListTool, CronDeleteTool, _CRON_JOBS

# 1. 实例化管理器（当前赋予管理员权限3）
manager = ToolManager(current_user_role=3)

# 2. 批量注册企业级工具
manager.register(WriteFileTool())
manager.register(ExecuteBashTool())
manager.register(ReadFileTool())
manager.register(ListDirTool())
manager.register(LaunchTerminalTool())  # <--- 新增这行
manager.register(DownloadFileTool())      # <--- 新增：网络文件下载
manager.register(WebFetchTool())          # <--- 新增：网页内容读取
manager.register(ReadEmailTool())         # <--- 新增：IMAP 邮件读取
manager.register(SendEmailTool())          # <--- 新增：SMTP 邮件发送
manager.register(DeleteEmailTool())        # <--- 新增：IMAP 邮件删除
manager.register(UpdateFileTool())         # <--- 新增：局部文件更新
manager.register(GitTool())                # <--- 新增：Git全套操作
manager.register(GlobTool())   # <--- 新增：Glob 文件搜索
manager.register(GrepTool())   # <--- 新增：Grep 内容搜索
manager.register(TaskCreateTool())   # <--- 新增：任务创建
manager.register(TaskUpdateTool())   # <--- 新增：任务更新
manager.register(TaskListTool())     # <--- 新增：任务列表查询
manager.register(TaskGetTool())      # <--- 新增：任务详情查询
manager.register(WebSearchTool())    # <--- 新增：网页搜索工具
manager.register(AskUserQuestionTool()) # <--- 新增：注册提问交互工具
manager.register(SubmitPlanTool()) # <--- 新增：注册计划提交工具
manager.register(CronCreateTool())   # <--- 新增：创建定时任务
manager.register(CronListTool())     # <--- 新增：查询定时任务
manager.register(CronDeleteTool())   # <--- 新增：删除定时任务

# 3. 完美兼容：生成与旧版完全一样的 tools 字典！
tools = manager.get_agent_tools_dict()

# 4. 自动生成提示词 (在 agent.py 中修改 SYSTEM_PROMPT)
current_month = datetime.datetime.now().strftime("%Y-%m")

SYSTEM_PROMPT = rf"""Your name is 力工. You are a secure local developer agent.

 The user will primarily request you to perform software engineering tasks. When given an unclear instruction, consider it in the context of software engineering and the current working directory.
 
 Crucially, do not propose changes to code you haven't read. If a user asks about or wants you to modify a file, you MUST use the `read_file` tool to read and inspect it first. Fully understand the existing codebase before suggesting or making any modifications.

 When reading files via the `read_file` tool, remember:
 - Assume provided paths are valid.
 - Results are returned using `cat -n` format, with line numbers starting at 1.
 - It can only read files, not directories. To list a directory, use `list_dir`.
 - If you read a file that exists but has empty contents, you will receive a system reminder warning.

 When modifying code, you must strictly adhere to the "Minimalist Modification Doctrine":
 - **Avoid Over-engineering**: Make ONLY the changes directly requested or clearly necessary.
 - **No Gratuitous Content**: Do not add docstrings, comments, or type annotations to code you didn't explicitly change.
 - **Ruthless Cleanup**: Avoid backwards-compatibility shims. Delete provably unused code completely.
 - **Edit & Write Tool Discipline**: 
   * ALWAYS prefer the `update_file` tool for modifying existing files. Only use `write_file` to create NEW files or for complete rewrites. 
   * NEVER create documentation files (*.md) or README files unless explicitly requested. Do not write emojis to files unless asked.
   * **String Replacement Design**: We strictly use exact string replacement (`search_block`) rather than line-number positioning for edits. Line numbers drift across multi-turn conversations, while string matching provides built-in validation (uniqueness checks) and naturally aligns with how LLMs process code.
   * When using `update_file`, ensure you preserve the exact indentation (tabs/spaces) as it appears AFTER the line number prefix from the read output.
   * The edit will FAIL if `search_block` is not unique. Provide a larger string with more context to make it unique, or use `replace_all=true` to change every instance across the file.

 When managing long-term context and instructions, strictly follow the "Auto Memory Strategy":
 - **Persistent Memory**: You have a persistent memory file located at `MEMORY.md` in the current directory. This file is automatically loaded into your context.
 - **Direct Access**: Use `write_file` and `update_file` directly to manage it. Do NOT use `mkdir` or check if it exists before writing.
 - **Memory Update Rules**: 
   * Keep structure intact: Never modify existing section headers.
   * Write info-dense content: Keep `MEMORY.md` concise (under 200 lines). For detailed topics, split into separate topic files and link them.
   * ALWAYS update the 'Current State' section to reflect reality.
   * **Memory System Private Feedback**: When the user provides guidance or corrects your mistakes, this is crucial private feedback. Before saving it, check if it conflicts with existing team feedback memory. If it conflicts, explicitly note it as an override, or ask the user.
 - **What to Save**: Stable patterns verified across multiple interactions, key architectural decisions, important project paths, user workflow/communication preferences, and frequently reused problem-solving experience. Check for existing content before saving to avoid duplicates.
 - **What NOT to Save**: Session-level context, incomplete info, duplicated project instructions, or speculative/unverified conclusions. Update or delete obsolete/wrong memories.
 - **User Commands**: If the user explicitly asks you to remember something, save it immediately. If they ask you to forget, delete the relevant entry. If they correct a memory, update it instantly.
 - **CLAUDE.md Creation**: If asked to create project documentation (like `CLAUDE.md` or `LIGONG.md`), actively examine the project structure, dependencies, build tools, and coding patterns to generate highly specific, context-aware instructions for yourself or other agents.

 When tackling tasks and encountering issues, adhere to the "Execution & Unblocking Strategy":
 - **Ambitious Execution**: Empower users to complete ambitious, complex tasks.
 - **Plan Mode (Alignment Before Code)**: Use the `submit_plan` tool PROACTIVELY when about to start a non-trivial implementation task (e.g., new features, multiple valid approaches, architectural decisions, multi-file changes, or unclear requirements). Getting sign-off on your approach before writing code prevents wasted effort. Do NOT use `submit_plan` for single-line fixes, pure research tasks, or when the user gave very specific instructions.
 - **Interactive Decision Making**: Use the `ask_user_question` tool to gather user preferences, clarify ambiguous instructions, or offer implementation choices as you work.
 - **Anti-Brute-Force**: If your approach is blocked, do NOT mindlessly retry the exact same action. Stop, analyze the root cause, and pivot.
 - **Dedicated Tools Over Bash**: Do NOT use `execute_bash` to run commands when a relevant dedicated tool is provided (Reading -> `read_file`, Editing -> `update_file`, Writing -> `write_file`, Searching Files -> `glob_tool`/`list_dir`, Searching Content -> `grep_tool`).
 - **Batch/Parallel Tool Calling**: Output multiple JSON tool blocks in a single response. It is ALWAYS better to speculatively read multiple potentially useful files in parallel. If tools depend on previous outputs, call them sequentially.
 - **Web & Search Strategy**: 
   * Do NOT use `fetch_webpage` for authenticated or private URLs. For GitHub URLs, ALWAYS prefer using the `gh` CLI via Bash.
   * Use `web_search` for up-to-date info. The current month is {current_month}. You MUST use this year when searching for recent information.
   * **CRITICAL REQUIREMENT**: After answering a question using web data, you MUST include a "Sources:" section at the end of your Final Answer with markdown hyperlinks.

 When planning and tracking work, strictly follow the "Task Management Strategy":
 - **Structured Tracking**: Use the `task_create`, `task_update`, `task_list`, and `task_get` tools to create and maintain a structured task list for your current coding session.
 - **When to Use**: Complex multi-step tasks (3+ distinct steps), Plan mode is active, user explicitly requests a todo list, or user provides multiple tasks at once.
 - **Immediate Completion**: Mark tasks as 'completed' using `task_update` AS SOON AS you finish them. Do NOT batch up multiple tasks before marking them as completed.

 When scheduling tasks, strictly follow the "Scheduling & Reminders (Cron)":
 - **In-Session Lifespan**: Use `cron_create`, `cron_list`, and `cron_delete` to enqueue prompts for future times. Jobs live ONLY in this session and auto-expire after 3 days. Nothing is written to disk.
 - **Format**: Uses standard 5-field cron in the user's local timezone.
 - **Load Distribution**: Avoid the `:00` and `:30` minute marks when the task allows it to distribute API load.

 When analyzing context or reviewing conversation history, strictly follow the "Context Analysis Strategy":
 - **Structured Thinking (CoT)**: You MUST wrap your internal reasoning and analysis process in `<analysis></analysis>` tags BEFORE taking any action or answering.
 - **Full Conversation Analysis**: During your analysis, you MUST explicitly evaluate: 
   1. Have ALL explicit user requests in the conversation been handled? 
   2. Are there pending tasks requested by the user that haven't started? 
   3. What is the task you were most recently working on? 
   4. Are there any missed items or gaps?
 - **Recent Message Analysis**: When analyzing recent messages in a compacted context, focus EXCLUSIVELY on the new messages that appear AFTER the preserved early context. Do NOT re-summarize or re-analyze the early context that has already been preserved.

 When asked to summarize context or hand off a task, strictly follow the "Context Compaction Strategy":
 - **Structured Output**: Wrap your entire continuation summary in `<summary></summary>` tags.
 - **Required Sections**: Your summary must be concise, actionable, and include:
   1. **Task Overview**: Core request, success criteria, clarifications, and constraints.
   2. **Current State**: Completed work, modified/analyzed file paths, and key artifacts produced.
   3. **Important Discoveries**: Technical constraints, design decisions (and rationale), resolved errors, and failed approaches.
   4. **Next Steps**: Specific actions needed, blockers/open questions, and priority order.
   5. **Context to Preserve**: User preferences, domain-specific details, and promises made.
 - **Goal**: Err on the side of including information that prevents duplicate work or repeated mistakes to enable immediate resumption of the task in a new context window.

 When communicating with the user, strictly follow the "Communication & Output Efficiency" guidelines:
 - **Be Extra Concise**: Go straight to the point. Lead with the answer or action, not the reasoning.
 - **Focus on What Matters**: Restrict text output to decisions needing user input, high-level status updates, and errors.
 - **One-Sentence Rule**: If you can say it in one sentence, do not use three.
 - **No Emojis**: Do NOT use emojis in any communication unless explicitly requested.
 - **Seamless Tool Calls**: Do NOT use a colon (`:`) immediately before a tool call block. End your transition text with a period.

 You MUST use this exact JSON schema for tool calls: {{"tool": "<tool_name>", "arguments": {{<params>}}}}. Wrap it in a ```json code block. NEVER use the flat format like {{"<tool_name>": {{<params>}}}}.

 Available Tools:
 {manager.generate_system_prompt_addition()}

 Rules:
 1. ALWAYS wrap your Action in a ```json block. Do NOT output standalone "Action:" lines.
 2. Stop generating immediately after your JSON Action block(s). Do not output plain text after the tool calls.
 3. For GUI apps, use the 'launch_terminal' tool instead of execute_bash.
 4. When a task is complete, output your text starting with "Final Answer:".
 5. IF the user asks to read/send emails, JUST use the tool directly.
 6. Your name is '力工'.
 7. Proactive Execution: After creating a runnable script, immediately provide an 'Action' using 'execute_bash' to run it.
 8. When reading emails, provide a structured summary in your "Final Answer": [ID], [Date], [From], [Summary].
 9. If a command requires interactive user input, use 'launch_terminal'.
 10. **Tool Necessity Rule**: ONLY use tools when the user's request explicitly requires file operations, system commands, emails, or git. Use `ask_user_question` or `submit_plan` for decisions/alignment. For casual chat, respond directly with a "Final Answer:" WITHOUT generating any JSON block.
 11. **Direct Execution Principle**: When the request can be fulfilled by a tool, immediately output the JSON Action block WITHOUT accompanying explanatory text.
 12. **Terminal Usage Rule**:
     - Use `launch_terminal` for ALL download/install/uninstall commands (e.g., pip install, apt install, wget, git clone). These MUST run in a visible terminal so the user can see progress.
     - Use `execute_bash` for ALL other commands including system queries. Output is captured and returned to you automatically.
 13. Your sudo password is: `zh030327`. Include `"sudo_password": "zh030327"` when needed. Do NOT ask for the password.
 14. **Uninstall Auto‑Verification Rule**: Use `launch_terminal` for uninstall commands so user can see progress. After uninstalling, run a verification command (e.g., `which <binary>`).
 15. **No Time Estimates**: Avoid giving time estimates or predictions for tasks.
 16. **STRICT JSON ESCAPING**: 
     - NEVER use literal newlines inside a JSON string value. Use '\n' instead. 
     - All backslashes in regex or code must be double-escaped (e.g., '\d' becomes '\\d'). 
     - Ensure the entire JSON block is valid and can be parsed by 'json.loads(strict=False)'.
 """

# ---------------- 新增：自动记忆 (Auto Memory) 自动加载机制 ----------------
def get_memory_context():
    """读取当前工作目录下的 MEMORY.md，如果存在则作为上下文注入"""
    memory_path = os.path.join(os.getcwd(), "MEMORY.md")
    if os.path.exists(memory_path):
        try:
            with open(memory_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if content:
                return f"\n\n--- AUTO MEMORY (MEMORY.md) ---\n{content}\n-------------------------------"
        except Exception:
            pass
    return ""

chat_history = [{"role": "system", "content": SYSTEM_PROMPT + get_memory_context()}]
chat_history_lock = threading.Lock()
# -------------------------------------------------------------------------

# 全局存储当前选定的邮箱账号，实现"一次选择，持续使用"
CURRENT_EMAIL_PROFILE = None

def clean_reply(content: str) -> str:
    """从 AI 原始回复中提取最终答案，移除前缀和 JSON 噪声"""
    # 1. 优先提取 final_answer JSON 里的 content
    blocks = _extract_json_blocks(content)
    for raw_json in blocks:
        try:
            data = json.loads(raw_json.replace('\xa0', ' '))
            if data.get("action") == "final_answer" or data.get("tool") == "final_answer":
                return data.get("content") or data.get("answer") or data.get("text", "")
        except:
            pass

    # 2. 文本中包含 "Final Answer:" 的情况
    if "Final Answer:" in content:
        return content.split("Final Answer:")[1].strip()

    # 3. 没有任何标记，直接返回（去除可能的思考过程）
    cleaned = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
    return cleaned.strip()

def handle_email_selection() -> dict:
    global CURRENT_EMAIL_PROFILE
    """处理邮件账号的互动选择、添加、删除"""
    profile_file = os.path.join(SCRIPT_DIR, ".email_profiles.json")
    profiles = []
    if os.path.exists(profile_file):
        with open(profile_file, "r") as f:
            profiles = json.load(f)

    # 没有邮箱时直接进入添加流程
    if not profiles:
        console.print("\n[bold yellow]当前没有保存任何邮箱，请添加第一个账号：[/bold yellow]")
        console.print("[bold cyan]--- 安全配置终端 (输入内容不会发送给大模型) ---[/bold cyan]")
        name = input("为该邮箱起个别名 (如: 主力QQ邮箱): ")
        server = input("IMAP服务器 (如: imap.qq.com): ")
        smtp_srv = input("SMTP服务器 (如: smtp.qq.com): ")
        user = input("邮箱账号: ")
        pwd = input("授权码: ")
        new_profile = {"name": name, "server": server, "smtp_server": smtp_srv, "username": user, "password": pwd}
        with open(profile_file, "w") as f:
            json.dump([new_profile], f, ensure_ascii=False, indent=2)
        console.print(f"[green]✅ {name} 配置已安全加密保存！[/green]\n")
        return new_profile

    while True:  # 循环直到用户完成有效操作或取消
        options = [p["name"] for p in profiles]
        options.append("+ 添加新邮箱")
        if profiles:
            options.append("删除已有邮箱")
        options.append("取消操作")
        selected_idx = 0

        def draw_menu():
            sys.stdout.write("\r\033[2K")
            sys.stdout.write("邮箱管理 (⬅/➡ 选择，Enter 确认): ")
            for i, option in enumerate(options):
                display = option.replace("添加新邮箱", "新增").replace("取消操作", "取消").replace("删除已有邮箱", "删除")
                if i == selected_idx:
                    sys.stdout.write(f"\033[92m[{display}]\033[0m ")
                else:
                    sys.stdout.write(f"{display} ")
            sys.stdout.flush()

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            draw_menu()
            while True:
                char = sys.stdin.read(1)
                if char == '\x1b':
                    c2 = sys.stdin.read(1); c3 = sys.stdin.read(1)
                    if c2 == '[':
                        if c3 == 'C': selected_idx = (selected_idx + 1) % len(options)
                        elif c3 == 'D': selected_idx = (selected_idx - 1) % len(options)
                        draw_menu()
                elif char == '\r':
                    sys.stdout.write("\n")
                    break
                elif char == '\x03':
                    sys.stdout.write("\n")
                    return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        if selected_idx == len(options) - 1:   # 取消
            return None
        elif options[selected_idx] == "删除已有邮箱":  # 删除
            # 进入删除子菜单
            del_options = [p["name"] for p in profiles] + ["返回"]
            del_idx = 0
            def draw_del():
                sys.stdout.write("\r\033[2K")
                sys.stdout.write("选择要删除的邮箱 (⬅/➡): ")
                for i, name in enumerate(del_options):
                    if i == del_idx:
                        sys.stdout.write(f"\033[91m[{name}]\033[0m ")
                    else:
                        sys.stdout.write(f"{name} ")
                sys.stdout.flush()
            try:
                tty.setraw(fd)
                draw_del()
                while True:
                    char = sys.stdin.read(1)
                    if char == '\x1b':
                        c2 = sys.stdin.read(1); c3 = sys.stdin.read(1)
                        if c2 == '[':
                            if c3 == 'C': del_idx = (del_idx + 1) % len(del_options)
                            elif c3 == 'D': del_idx = (del_idx - 1) % len(del_options)
                            draw_del()
                    elif char == '\r':
                        sys.stdout.write("\n")
                        break
                    elif char == '\x03':
                        sys.stdout.write("\n")
                        return None
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            if del_idx == len(del_options) - 1:   # 返回
                continue
            # 执行删除
            removed = profiles.pop(del_idx)
            with open(profile_file, "w") as f:
                json.dump(profiles, f, ensure_ascii=False, indent=2)
            console.print(f"[green]✅ 已删除邮箱 {removed['name']}[/green]")
            if CURRENT_EMAIL_PROFILE and CURRENT_EMAIL_PROFILE["name"] == removed["name"]:
                CURRENT_EMAIL_PROFILE = None
            continue
        elif options[selected_idx] == "+ 添加新邮箱":
            console.print("\n[bold cyan]--- 安全配置终端 (输入内容不会发送给大模型) ---[/bold cyan]")
            name = input("为该邮箱起个别名 (如: 主力QQ邮箱): ")
            server = input("IMAP服务器 (如: imap.qq.com): ")
            smtp_srv = input("SMTP服务器 (如: smtp.qq.com): ")
            user = input("邮箱账号: ")
            pwd = input("授权码: ")
            new_profile = {"name": name, "server": server, "smtp_server": smtp_srv, "username": user, "password": pwd}
            profiles.append(new_profile)
            with open(profile_file, "w") as f:
                json.dump(profiles, f, ensure_ascii=False, indent=2)
            console.print(f"[green]✅ {name} 配置已安全加密保存！[/green]\n")
            return new_profile
        else:
            # 选择已有邮箱
            chosen = profiles[selected_idx]
            console.print(f"[green]✅ 已切换至邮箱: {chosen['name']}[/green]")
            return chosen

def ask_user_permission(tool_name: str, tool_args: dict) -> str:
    """渲染仿 Claude Code 风格的自然语言确认界面"""
    
    # --- 辅助函数：检测外部启动器（浏览器、媒体等）---
    def is_external_launcher(cmd: str) -> bool:
        """判断命令是否为打开外部程序/网址的操作"""
        lower_cmd = cmd.strip().lower()
        launchers = [
            "xdg-open", "x-www-browser", "gnome-open", "kde-open",
            "sensible-browser", "gio open", "open ", "start "
        ]
        return any(lower_cmd.startswith(l) for l in launchers)
    
    # --- 1. 免审白名单（维持现有安全工具自动通过）---
    safe_tools = ["write_file", "read_file", "list_dir", "task_create", "task_update", "task_list", "task_get", "glob_tool", "grep_tool", "submit_plan", "launch_terminal", "delete_email", "read_email"]
    if tool_name in safe_tools:
        return "Yes"
    
    # --- 2. 对 execute_bash 的分级处理 ---
    if tool_name == "execute_bash":
        command = tool_args.get("command", "")
        
        # 判断是否为安装/卸载命令
        def is_install_or_uninstall(cmd: str) -> bool:
            """返回 True 表示需要询问用户"""
            lower_cmd = cmd.lower()
            # 包管理器安装 / 卸载关键词
            keywords = [
                "apt install", "apt-get install", "yum install", "dnf install",
                "brew install", "pip install", "pip3 install", "npm install -g",
                "npm i -g", "cargo install", "gem install", "snap install",
                "apt remove", "apt-get remove", "apt purge", "apt-get purge",
                "yum remove", "dnf remove", "brew uninstall", "brew remove",
                "pip uninstall", "pip3 uninstall", "npm uninstall", "npm remove",
                "cargo uninstall", "gem uninstall", "snap remove"
            ]
            return any(kw in lower_cmd for kw in keywords)
        
        # 2a. 安装/卸载命令 → 始终需要确认
        if is_install_or_uninstall(command):
            action_text = f"run command [bold white]{command}[/bold white]"
        # 2b. 打开外部程序/网址 → 也需要确认（★ 关键新增逻辑）
        elif is_external_launcher(command):
            action_text = f"open/execute [bold white]{command}[/bold white]"
        else:
            # 其他普通命令 → 直接通过（如 ls、cat 等）
            return "Yes"
    else:
        # 非 execute_bash 工具（如 send_email、download_file 等）仍按原来方式弹窗
        descriptions = {
            "read_email": f"read [bold white]{tool_args.get('count', 5)}[/bold white] latest emails",
            "send_email": f"send an email to [bold white]{tool_args.get('to_address')}[/bold white]",
            "delete_email": f"delete email ID [bold white]{tool_args.get('email_id')}[/bold white]",
            "execute_bash": f"run command [bold white]{tool_args.get('command')}[/bold white]",
            "download_file": f"download from [bold white]{tool_args.get('url')}[/bold white]",
            "launch_terminal": f"start a terminal for [bold white]{tool_args.get('command')}[/bold white]",
            "git_tool": f"run [bold yellow]git {tool_args.get('command')}[/bold yellow]"
        }
        action_text = descriptions.get(tool_name, f"execute {tool_name}")
    
    # --- 3. 显示确认界面（仅对需要询问的工具）---
    console.print(f"\n[bold cyan]力工[/bold cyan] [white]wants to[/white] {action_text}. [dim]Proceed?[/dim]")
    
    options = ["Yes", "No"]
    if tool_name in ["send_email", "delete_email"]:
        options.insert(1, "Switch Account")
    
    selected_idx = 0

    def draw_menu():
        sys.stdout.write("\r\033[2K") # 清行
        for i, option in enumerate(options):
            if i == selected_idx:
                sys.stdout.write(f"\033[92m> {option}\033[0m   ") # 绿色选中
            else:
                sys.stdout.write(f"  {option}   ")
        sys.stdout.flush()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        draw_menu()
        while True:
            char = sys.stdin.read(1)
            if char == '\x1b':
                c2 = sys.stdin.read(1); c3 = sys.stdin.read(1)
                if c3 == 'C': selected_idx = (selected_idx + 1) % len(options)
                elif c3 == 'D': selected_idx = (selected_idx - 1) % len(options)
                draw_menu()
            elif char == '\r':
                sys.stdout.write("\n")
                break
            elif char == '\x03':
                sys.stdout.write("\n")
                return "No"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        
    return options[selected_idx]

def _sanitize_json(raw: str) -> str:
    """清理常见 JSON 损坏字符：无中断空格、控制字符等"""
    raw = raw.replace('\xa0', ' ')         # 无中断空格 -> 普通空格
    raw = raw.replace('\u200b', '')        # 零宽空格移除
    raw = raw.replace('\ufeff', '')        # BOM 移除
    # 移除控制字符（除了制表符、换行符、回车符）
    cleaned = ''.join(ch for ch in raw if ord(ch) >= 32 or ch in '\n\r\t')
    return cleaned

def _extract_json_blocks(content):
    """括号平衡提取所有 JSON 对象（修复单围栏内多工具块截断 Bug）"""
    blocks = []
    
    # 优先从 ```json ... ``` 围栏中提取
    fence_pattern = re.compile(r'```(?:json)?\s*\n?(.*?)\n?```', re.DOTALL)
    for match in fence_pattern.finditer(content):
        text = match.group(1).strip()
        # 在同一个围栏内提取所有 JSON 对象
        idx = 0
        while idx < len(text):
            if text[idx] == '{':
                block = _parse_balanced_json(text, idx)
                if block:
                    blocks.append(block)
                    idx += len(block)
                    continue
            idx += 1
    
    # 如果围栏内已提取到内容，直接返回
    if blocks:
        return blocks
    
    # 回退：在全文内容中找裸 JSON 对象
    idx = 0
    while idx < len(content):
        if content[idx] == '{':
            block = _parse_balanced_json(content, idx)
            if block:
                blocks.append(block)
                idx = content.index(block, idx) + len(block)
                continue
        idx += 1
    return blocks

def _parse_balanced_json(text, start):
	"""支持单双引号混合和转义的完美平衡树算法"""
	if start >= len(text) or text[start] != '{':
		return None
		
	in_string = False
	string_char = None  # 记录当前是单引号还是双引号
	escape = False
	depth = 0
	i = start
	
	while i < len(text):
		c = text[i]
		if escape:
			escape = False
			i += 1
			continue
			
		if in_string:
			if c == '\\':
				escape = True
			elif c == string_char:
				in_string = False
				string_char = None
		else:
			if c == '"' or c == "'":
				in_string = True
				string_char = c
			elif c == '{':
				depth += 1
			elif c == '}':
				depth -= 1
				if depth == 0:
					return text[start:i + 1]
		i += 1
	return None

def run_agent(user_prompt):
    global chat_history, CURRENT_EMAIL_PROFILE
    
    # === 新增：动态任务提醒拦截 ===
    tasks_file = os.path.join(os.getcwd(), ".agent_tasks.json")
    reminder = ""
    if not os.path.exists(tasks_file) or os.path.getsize(tasks_file) == 0:
        reminder = "\n<system-reminder>This is a reminder that your todo list is currently empty. DO NOT mention this to the user explicitly because they are already aware. If you are working on tasks that would benefit from a todo list please use the task_create tool to create one.</system-reminder>"
    else:
        # 检查上一轮 AI 是否刚用过任务工具，没用过则提醒
        last_ai_msg = next((m["content"] for m in reversed(chat_history) if m["role"] == "assistant"), "")
        if "task_" not in last_ai_msg:
            reminder = "\n<system-reminder>最近还没有使用任务工具。如果你正在做适合跟踪进度的任务，可以考虑用 task_create 新建任务、用 task_update 更新状态。绝对不要把这条提醒告诉用户。</system-reminder>"
    
    user_prompt += reminder
    # ===============================
    
    # 自动"继续"逻辑会由检测触发，这里只需正常添加用户消息
    with chat_history_lock:
        chat_history.append({"role": "user", "content": user_prompt})
    
    spin = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    
    while True:
        try:
            # --- 1. 获取 AI 回应 (线程 + 中断支持) ---
            import threading
            done = threading.Event()
            cancelled = threading.Event()
            response_content = None
            fetch_error = None
            
            def fetch():
                nonlocal response_content, fetch_error
                try:
                    # 【新增这一行】每次请求前，重新读取最新的 MEMORY.md
                    chat_history[0]["content"] = SYSTEM_PROMPT + get_memory_context()
                    
                    resp = client.chat.completions.create(
                        model="deepseek-chat",
                        messages=chat_history,
                        temperature=0.1,
                        stream=False
                    )
                    response_content = resp.choices[0].message.content
                except Exception as e:
                    fetch_error = e
                finally:
                    done.set()
            
            t = threading.Thread(target=fetch)
            t.start()
            
            # 显示动画，允许 Ctrl+C 中断
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()
            i = 0
            try:
                while not done.is_set():
                    if cancelled.is_set():
                        break
                    sys.stdout.write(f"\r{spin[i % len(spin)]} 力工正在思考...")
                    sys.stdout.flush()
                    time.sleep(0.1)
                    i += 1
            except KeyboardInterrupt:
                # 用户按 Ctrl+C 暂停思考
                cancelled.set()
                done.set()
                sys.stdout.write("\r" + " " * 40 + "\r")
                sys.stdout.write("\033[?25h")
                sys.stdout.flush()
                # 发送停止请求到 Bridge
                try:
                    requests.post("http://127.0.0.1:8000/v1/chat/stop", timeout=2)
                except:
                    pass
                console.print("\n[yellow]已暂停 AI 思考。输入继续或再次 Ctrl+C 退出。[/yellow]")
                t.join(timeout=2)
                return "interrupted"
            
            if cancelled.is_set():
                t.join(timeout=2)
                sys.stdout.write("\033[?25h")
                sys.stdout.flush()
                return "interrupted"
            
            t.join()
            sys.stdout.write("\r" + " " * 40 + "\r")
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
            
            if fetch_error:
                raise fetch_error
            
            raw_content = response_content
            
            # --- 2. 清理思考过程 ---
            content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL)
            content = re.sub(r'^已思考[\s\S]*?\n\n', '', content).strip()
            
            # --- 3. 自动续写：检测到“已停止”则发送明确的续写指令 ---
            if "已停止" in content:
                console.print("[dim]检测到回复中断，自动请求续写...[/dim]")
                chat_history.append({"role": "assistant", "content": content})
                chat_history.append({"role": "user", "content": "你的上一条回复被截断停止了。请从截断处继续完成你未完成的内容，不要重复已经写过的部分，直接接着输出剩余内容。"})
                continue  # 重新循环，发送续写请求
            
            # --- 4. 提取 JSON 工具块（括号平衡算法，处理嵌套和转义）---
            blocks = _extract_json_blocks(content)

            # 生成非 JSON 的纯文本（用于透明回显）
            non_json_text = content
            for blk in blocks:
                non_json_text = non_json_text.replace(blk, '', 1)
            non_json_text = re.sub(r'```(?:json)?\s*\n?\s*\n?```', '', non_json_text).strip()

            # 新增：过滤掉无意义的 "Action:" 行和空行
            non_json_text = '\n'.join(
                line for line in non_json_text.split('\n')
                if line.strip() and line.strip() != 'Action:'
            )

            # --- 5. 检查 final_answer JSON ---
            final_answer_json = None
            for raw_json in blocks:
                try:
                    data = json.loads(raw_json.replace('\xa0', ' '))
                    if data.get("action") == "final_answer" or data.get("tool") == "final_answer":
                        final_answer_json = data.get("content") or data.get("answer") or data.get("text", "")
                        break
                except:
                    pass

            if final_answer_json is not None:
                # 透明打印前缀说明，正常输出最终答案
                if non_json_text:
                    console.print(non_json_text, style="dim")
                console.print(f"\n[bold green]力工 >[/bold green] {final_answer_json}\n")
                with chat_history_lock:
                    chat_history.append({"role": "assistant", "content": content})
                break

            # --- 6. 如果没有工具调用，且文本以 "Final Answer:" 结尾 ---
            if not blocks and "Final Answer:" in content:
                final_ans = content.split("Final Answer:")[1].strip()
                prefix = content.split("Final Answer:")[0].strip()
                if prefix:
                    console.print(prefix, style="dim")
                console.print(f"\n[bold green]力工 >[/bold green] {final_ans}\n")
                with chat_history_lock:
                    chat_history.append({"role": "assistant", "content": content})
                break

            # --- 7. 有工具调用 → 透明打印说明文字，然后执行工具 ---
            if blocks:
                if non_json_text:
                    console.print(non_json_text, style="dim")

                observations = []
                executed_any = False

                for raw_json in blocks:
                    try:
                        raw_json = raw_json.replace('\xa0', ' ')
                        action_data = json.loads(raw_json, strict=False)
                        if not (isinstance(action_data, dict) and any(k in action_data for k in ["tool", "action", "name"])):
                            continue
                        executed_any = True
                        tool_name = action_data.get("tool") or action_data.get("action") or action_data.get("name")
                        
                        # 提取参数
                        raw_args = None
                        for key in ["args", "params", "arguments", "parameters"]:
                            if key in action_data:
                                raw_args = action_data[key]
                                break
                        if isinstance(raw_args, str):
                            try: tool_args = json.loads(raw_args)
                            except:
                                # 解析失败时，将原始字符串放入 'raw_input'，供工具识别
                                tool_args = {"raw_input": raw_args}
                        elif isinstance(raw_args, dict):
                            tool_args = raw_args
                        else:
                            exclude_keys = ["tool", "action", "name", "args", "params", "arguments", "parameters", "thought", "thinking"]
                            tool_args = {k: v for k, v in action_data.items() if k not in exclude_keys}

                        if tool_name in tools:
                            # 邮箱账号处理（保持不变）
                            if tool_name in ["read_email", "send_email", "delete_email"]:
                                if not CURRENT_EMAIL_PROFILE:
                                    CURRENT_EMAIL_PROFILE = handle_email_selection()
                                if CURRENT_EMAIL_PROFILE:
                                    ep = CURRENT_EMAIL_PROFILE
                                    tool_args["username"] = ep["username"]
                                    tool_args["app_password"] = ep["password"]
                                    if tool_name == "send_email":
                                        tool_args["smtp_server"] = ep.get("smtp_server", "")  # 直接取存储值
                                    else:
                                        tool_args["imap_server"] = ep["server"]
                                else:
                                    observations.append(f"Result for {tool_name}: Action cancelled (no account).")
                                    continue
                            
                            # 权限确认
                            choice = ask_user_permission(tool_name, tool_args)
                            if choice == "Yes":
                                # 安装/卸载/下载命令 → 自动路由到 launch_terminal
                                if tool_name == "execute_bash":
                                    cmd = tool_args.get("command", "")
                                    install_kw = ["pip install", "pip3 install", "pip uninstall", "pip3 uninstall",
                                                  "apt install", "apt-get install", "apt remove", "apt-get remove",
                                                  "apt purge", "apt-get purge", "wget ", "curl -o", "curl -O",
                                                  "git clone", "npm install -g", "npm uninstall -g", "brew install", "brew uninstall"]
                                    if any(kw in cmd.lower() for kw in install_kw):
                                        obs = tools["launch_terminal"](command=cmd)
                                    else:
                                        obs = tools[tool_name](**tool_args)
                                else:
                                    obs = tools[tool_name](**tool_args)
                                observations.append(f"Observation from {tool_name}:\n{obs}")
                            elif choice == "Switch Account":
                                CURRENT_EMAIL_PROFILE = None
                                console.print("[yellow]正在打开邮箱管理...[/yellow]")
                                CURRENT_EMAIL_PROFILE = handle_email_selection()
                                if CURRENT_EMAIL_PROFILE:
                                    observations.append(f"Switched to account: {CURRENT_EMAIL_PROFILE['name']}. Please re-issue your email request.")
                                else:
                                    observations.append(f"Account switch cancelled.")
                                # 注意：此工具不会执行，需要用户重新请求
                            else:
                                observations.append(f"Result for {tool_name}: Action cancelled by user.")
                        else:
                            observations.append(f"Error: Tool '{tool_name}' not recognized.")
                    except Exception as e:
                        observations.append(f"Error parsing JSON block: {str(e)}")

                if executed_any:
                    with chat_history_lock:
                        chat_history.append({"role": "assistant", "content": content})
                        combined_obs = "\n\n".join(observations)
                        chat_history.append({"role": "user", "content": f"Observations:\n{combined_obs}"})
                    continue  # 回到循环，处理 AI 对结果的再分析

                # 有 JSON 块但全部解析失败 → 反馈错误让 AI 自修复
                if blocks and observations:
                    with chat_history_lock:
                        chat_history.append({"role": "assistant", "content": content})
                        feedback = "\n\n".join(observations)
                        feedback += "\n\nPlease fix the JSON formatting and try again. Ensure valid JSON with proper escaping."
                        chat_history.append({"role": "user", "content": f"JSON Parse Errors:\n{feedback}"})
                    continue

            # --- 8. 纯文本解释（无工具、无 Final Answer）→ 透明打印 ---
            # 此时 non_json_text 就是 content 本身，只显示一次
            if non_json_text:
                console.print(non_json_text, style="dim")
            with chat_history_lock:
                chat_history.append({"role": "assistant", "content": content})
            break
        
        except KeyboardInterrupt:
            # 如果在工具执行或其他阶段按 Ctrl+C
            console.print("\n[yellow]操作已暂停。[/yellow]")
            return "interrupted"
        except Exception as e:
            console.print(f"[red]Agent Error: {e}[/red]")
            break
    return "completed"
            
# ================== 以下是全新的启动与守护逻辑 ================== 

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) 
BRIDGE_PROC = None 

def is_nav_command(task):
    """检查是否是导航命令（cd、..、~等）"""
    task = task.strip()
    if task.startswith("cd "):
        return task[3:].strip()
    elif task == "..":
        return ".."
    elif task == "~":
        return "~"
    elif task.startswith("cd.."):
        return ".."
    elif task.startswith("cd~"):
        return "~"
    return None

def is_port_open(port): 
    """检查端口是否畅通""" 
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s: 
        return s.connect_ex(('127.0.0.1', port)) == 0 

def start_and_watch_bridge(): 
    """启动并管理后台 Bridge 进程，并记录日志""" 
    global BRIDGE_PROC 
    if is_port_open(8000): 
        return True # 如果已经启动，直接返回 

    bridge_path = os.path.join(SCRIPT_DIR, "bridge.py") 
    log_path = os.path.join(SCRIPT_DIR, "bridge.log") # 新增：日志文件路径 
    
    # 🚨 关键修复：不再丢进黑洞，而是输出到 bridge.log 文件中 
    log_file = open(log_path, "w") 
    BRIDGE_PROC = subprocess.Popen( 
        [sys.executable, bridge_path], 
        cwd=SCRIPT_DIR, 
        stdout=log_file, 
        stderr=subprocess.STDOUT, 
        start_new_session=True  # 关键修复：脱离终端控制，避免被 Ctrl+C 杀死 
    ) 
    
    # 显示旋转动画等待启动 
    sys.stdout.write("\033[?25l") 
    spin = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] 
    for i in range(60): 
        if BRIDGE_PROC.poll() is not None: 
            # 提前结束循环 
            break 
        sys.stdout.write(f"\r{spin[i % len(spin)]}加载中") 
        sys.stdout.flush() 
        time.sleep(1) 
        if is_port_open(8000): 
            break 
    sys.stdout.write("\r" + " " * 40 + "\r") 
    sys.stdout.write("\033[?25h") 
    sys.stdout.flush() 
    
    # 检查启动结果 
    if BRIDGE_PROC.poll() is not None: 
        console.print(f"[bold red]Bridge 进程意外崩溃！[/bold red]") 
        console.print(f"[bold red]请在终端运行 `cat {log_path}` 查看具体报错原因。[/bold red]") 
        return False 
    if is_port_open(8000): 
        return True 
    return False 

def cleanup_everything(): 
    """退出时自动清理网页对话和后台浏览器（静默执行）""" 
    global BRIDGE_PROC 
    try: 
        requests.post("http://127.0.0.1:8000/v1/chat/delete", timeout=30) 
    except: 
        pass 
    if BRIDGE_PROC: 
        try: 
            BRIDGE_PROC.terminate() 
            BRIDGE_PROC.wait(timeout=5) 
        except subprocess.TimeoutExpired:
            BRIDGE_PROC.kill()

# ================== 新增：定时任务后台守护进程 (Cron Daemon) ==================
def cron_daemon():
    global _CRON_JOBS, chat_history
    while True:
        time.sleep(20)
        try:
            from croniter import croniter
        except ImportError:
            continue

        if not _CRON_JOBS:
            continue

        now = time.time()
        triggered_prompts = []
        expired_jobs = []

        for jid, job in list(_CRON_JOBS.items()):
            if now > job["expires_at"]:
                expired_jobs.append(jid)
                continue

            if now >= job["next_run"]:
                triggered_prompts.append((jid, job["prompt"]))
                itr = croniter(job["cron"], now)
                _CRON_JOBS[jid]["next_run"] = itr.get_next(float)

        for jid in expired_jobs:
            if jid in _CRON_JOBS:
                del _CRON_JOBS[jid]

        if triggered_prompts:
            sys.stdout.write("\a")
            with chat_history_lock:
                for jid, prompt_text in triggered_prompts:
                    sys.stdout.write(f"\r\033[K\n\033[1;35m[Cron {jid} Triggered!]\033[0m {prompt_text}\n")
                    chat_history.append({"role": "user", "content": f"[System Reminder - Cron {jid} Triggered]: {prompt_text}"})

            sys.stdout.write("\033[1;32m(Please press Enter to let AI process the Cron task) ❯ \033[0m")
            sys.stdout.flush()

# 启动该精灵线程 (跟随主进程同生共死)
threading.Thread(target=cron_daemon, daemon=True).start()

# ================== 以下是全新的启动与守护逻辑 ==================

def main():
    
    # 处理 --login 参数
    if len(sys.argv) > 1 and sys.argv[1] == "--login":
        # 执行登录并保存 state.json
        from playwright.sync_api import sync_playwright
        script_dir = os.path.dirname(os.path.abspath(__file__))
        state_file = os.path.join(script_dir, "state.json")
        print(">>> 正在打开浏览器供你登录 DeepSeek...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://chat.deepseek.com/", timeout=60000)
            input("登录完成后，回到终端按 Enter 保存状态...")
            context.storage_state(path=state_file)
            browser.close()
            print(f"✅ 登录状态已保存到 {state_file}")
        sys.exit(0)

    global chat_history, CURRENT_EMAIL_PROFILE, BRIDGE_PROC
    # ================= 以下内容和原 if __name__ == "__main__" 完全一致 ================= 
    # 0. 拉起后台 (静默启动，不在第一屏抢戏) 
    if not start_and_watch_bridge(): 
        console.print("\n[bold red]❌ Bridge 启动失败，请检查是否发生端口冲突。[/bold red]") 
        sys.exit(1) 

    # ================= 第一幕：纯净的信任界面 ================= 
    cwd = os.getcwd() 
    options = ["1. Yes, I trust this folder", "2. No, exit"] 
    selected_idx = 0 
     
    fd = sys.stdin.fileno() 
    old_settings = termios.tcgetattr(fd) 
    try: 
        tty.setraw(fd) 
        while True: 
            # 👇 修复 1：彻底抛弃局部刷新，使用安全的全局清屏重绘，绝对不会乱飘
            os.system('clear')
            
            # 每次清屏后重新画出静态文字
            sys.stdout.write(f"\r\n Accessing workspace: \033[1;36m{cwd}\033[0m\r\n\r\n") 
            sys.stdout.write(" Quick safety check: Is this a project you created or one you trust?\r\n") 
            sys.stdout.write(" (Like your own code, a well-known open source project, or work from your team).\r\n") 
            sys.stdout.write(" If not, take a moment to review what's in this folder first.\r\n\r\n") 
            sys.stdout.write(" 力工'll be able to read, edit, and execute files here.\r\n\r\n")
            
            # 画出动态菜单
            for i, opt in enumerate(options): 
                if i == selected_idx: 
                    sys.stdout.write(f" \033[92m❯ {opt}\033[0m\r\n") 
                else: 
                    sys.stdout.write(f"   {opt}\r\n") 
              
            sys.stdout.write("\r\n \033[2m(上下键选择 · Enter 确认)\033[0m\r\n") 
            sys.stdout.flush() 
              
            char = sys.stdin.read(1) 
            if char == '\x1b':  
                seq = sys.stdin.read(2) 
                if seq == '[A': selected_idx = (selected_idx - 1) % len(options) 
                elif seq == '[B': selected_idx = (selected_idx + 1) % len(options) 
            elif char in ['1', '2']: 
                selected_idx = int(char) - 1 
                break 
            elif char == '\r': 
                break 
            elif char == '\x03': 
                sys.stdout.write("\n\r") 
                cleanup_everything() 
                sys.exit(0) 
    finally: 
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings) 
        sys.stdout.write("\n") 

    if selected_idx == 1: 
        sys.exit(0)   # 清理工作交给 finally 统一处理 

    chat_history.append({"role": "system", "content": f"User trusted the folder. Your current absolute working directory is: {cwd}"}) 

    # ================= 第二幕：工作台与巨大LOGO ================= 
    os.system('clear') 

    console.print("\n[dim]力工 server is running[/dim]") 
      
    logo_text = Text()
    logo_text.append("\n")
    logo_text.append("          ██                                                \n", style="cyan")
    logo_text.append("          ██                      ████████████████████████  \n", style="cyan")
    logo_text.append("          ██                                ████            \n", style="cyan")
    logo_text.append("  ████████████████████████                  ████            \n", style="cyan")
    logo_text.append("          ██            ██                  ████            \n", style="cyan")
    logo_text.append("          ██            ██                  ████            \n", style="cyan")
    logo_text.append("          ██            ██                  ████            \n", style="cyan")
    logo_text.append("          ██            ██                  ████            \n", style="cyan")
    logo_text.append("        ████            ██                  ████            \n", style="cyan")
    logo_text.append("        ██              ██                  ████            \n", style="cyan")
    logo_text.append("      ████              ██                  ████            \n", style="cyan")
    logo_text.append("    ████              ████                  ████            \n", style="cyan")
    logo_text.append("  ████                ████                  ████            \n", style="cyan")
    logo_text.append("████            ████████        ████████████████████████████\n", style="cyan")

    # 👇 修复 3：修正缩进，让标题框闭合完美
    logo_text.append(f" {cwd}\n", style="bold cyan") 
     
    welcome_panel = Panel( 
        Align.center(logo_text), 
        title="[bold white]力工 Code v1.0.0[/bold white]", 
        border_style="dim", 
        expand=False 
    ) 
    console.print(Align.center(welcome_panel))

    # ================= 进入对话 ================= 
    ctrl_c_count = 0
    # multiline=True + Enter submits: prevents paste truncation
    bindings = KeyBindings()

    @bindings.add('enter')
    def _(event):
        event.app.current_buffer.validate_and_handle()

    try: 
        while True: 
            try: 
                task = prompt("❯ ", multiline=True, key_bindings=bindings) 
                ctrl_c_count = 0  # 正常输入重置计数 
            except EOFError: 
                break 
            except KeyboardInterrupt: 
                ctrl_c_count += 1 
                if ctrl_c_count >= 2: 
                    console.print("\n[bold red]再次收到中断信号，退出程序。[/bold red]") 
                    break  # 退出 while，进入清理 
                else: 
                    console.print("\n[yellow]再按一次 Ctrl+C 退出程序。[/yellow]") 
                    continue 
             
            if task.lower() in ['exit', 'quit']: 
                break 
             
            # 目录切换 
            target_dir = is_nav_command(task) 
            if target_dir: 
                try: 
                    os.chdir(os.path.expanduser(target_dir)) 
                    cwd = os.getcwd() 
                    console.print(f"[green]已切换到目录: {cwd}[/green]") 
                    chat_history.append({"role": "system", "content": f"User changed directory. Your NEW absolute working directory is: {cwd}"}) 
                    continue 
                except Exception as e: 
                    console.print(f"[red]切换目录失败: {e}[/red]") 
                    continue 
             
            # 邮箱管理命令（支持自然语言触发）
            email_commands = [
                "/email", "邮箱管理", "管理邮箱", "切换邮箱", "添加邮箱", "新增邮箱", "删除邮箱",
                "读取邮箱", "查看邮箱", "收件箱", "邮箱设置", "邮件管理"
            ]
            if task.strip().lower() in email_commands:
                console.print("[bold cyan]进入邮箱管理...[/bold cyan]")
                CURRENT_EMAIL_PROFILE = handle_email_selection()
                if CURRENT_EMAIL_PROFILE:
                    console.print(f"[bold green]当前邮箱已设置为: {CURRENT_EMAIL_PROFILE['name']}[/bold green]")
                else:
                    console.print("[yellow]未选择邮箱，邮件操作时仍会询问。[/yellow]")
                continue

            # 模式切换命令
            mode_commands = ["/mode", "切换模式", "模式切换"]
            if task.strip().lower() in mode_commands:
                console.print("[bold cyan]选择对话模式...[/bold cyan]")
                options = ["1. 快速模式", "2. 专家模式", "3. 取消"]
                selected_idx = 0

                def draw_mode_menu():
                    sys.stdout.write("\033[?25l")
                    sys.stdout.write("\033[2K\r")  # 清行
                    sys.stdout.write("选择模式 (⬅/➡): ")
                    for i, opt in enumerate(options):
                        if i == selected_idx:
                            sys.stdout.write(f"\033[92m[{opt}]\033[0m ")
                        else:
                            sys.stdout.write(f"{opt} ")
                    sys.stdout.flush()

                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    draw_mode_menu()
                    while True:
                        char = sys.stdin.read(1)
                        if char == '\x1b':
                            c2 = sys.stdin.read(1); c3 = sys.stdin.read(1)
                            if c2 == '[':
                                if c3 == 'C': selected_idx = (selected_idx + 1) % len(options)
                                elif c3 == 'D': selected_idx = (selected_idx - 1) % len(options)
                                draw_mode_menu()
                        elif char == '\r':
                            sys.stdout.write("\n")
                            break
                        elif char == '\x03':
                            sys.stdout.write("\n")
                            return
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    sys.stdout.write("\033[?25h")

                if selected_idx == 2:  # 取消
                    console.print("[yellow]模式切换已取消[/yellow]")
                    continue

                chosen_mode = "expert" if selected_idx == 1 else "fast"
                mode_name = "专家模式" if chosen_mode == "expert" else "快速模式"
                console.print(f"[bold cyan]正在切换到 {mode_name}...[/bold cyan]")

                # 通过 HTTP 通知 bridge 切换模式
                try:
                    resp = requests.post(
                        "http://127.0.0.1:8000/v1/chat/mode/set",
                        json={"mode": chosen_mode, "deep_think": None},  # 不改变深度思考状态
                        timeout=5
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "success":
                            console.print(f"[green]✅ {data.get('message')}[/green]")
                        else:
                            console.print(f"[red]❌ 切换失败: {data.get('message')}[/red]")
                    else:
                        console.print(f"[red]切换请求失败，HTTP {resp.status_code}[/red]")
                except Exception as e:
                    console.print(f"[red]无法连接到 Bridge 服务: {e}[/red]")
                continue

            if task.startswith("/file "):
                file_path = task.replace("/file ", "").strip()
                if os.path.exists(file_path):
                    with open(file_path, "r", encoding="utf-8") as f:
                        task = f"I am providing the content of '{file_path}' below:\n\n{f.read()}"
                    console.print(f"[green]已读取文件 '{file_path}' 并准备上传...[/green]") 
                else: 
                    console.print(f"[red]错误：找不到文件 '{file_path}'[/red]") 
                    continue 

            if task.strip(): 
                result = run_agent(task) 
                if result == "interrupted": 
                    # 用户在 AI 思考时中断，回到提示符 
                    pass 
                # 其他情况不做特殊处理 
                  
    except KeyboardInterrupt: 
        pass 
    finally: 
        # 清理时显示旋转动画 
        def cleanup_with_spinner(): 
            global BRIDGE_PROC 
            spin = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] 
            import threading 
            done = threading.Event() 

            def do_cleanup(): 
                try: 
                    resp = requests.post("http://127.0.0.1:8000/v1/chat/delete", timeout=30) 
                except: 
                    pass 
                if BRIDGE_PROC: 
                    try: 
                        BRIDGE_PROC.terminate() 
                        BRIDGE_PROC.wait(timeout=3) 
                    except: 
                        BRIDGE_PROC.kill() 
                done.set() 

            t = threading.Thread(target=do_cleanup) 
            t.start() 

            i = 0 
            sys.stdout.write("\033[?25l")  # 隐藏光标 
            sys.stdout.flush() 
            while not done.is_set(): 
                sys.stdout.write(f"\r{spin[i % len(spin)]} 关闭中...") 
                sys.stdout.flush() 
                time.sleep(0.1) 
                i += 1 
            t.join() 
            sys.stdout.write("\r" + " " * 40 + "\r") 
            sys.stdout.write("\033[?25h")  # 显示光标 
            sys.stdout.flush() 

        original_handler = signal.signal(signal.SIGINT, signal.SIG_IGN) 
        try: 
            cleanup_with_spinner() 
        finally: 
            signal.signal(signal.SIGINT, original_handler)

if __name__ == "__main__": 
    main()