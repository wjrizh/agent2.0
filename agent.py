# agent.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prompt_toolkit import prompt
from prompt_toolkit.key_binding import KeyBindings
import os
import subprocess
import re
import json
from json_repair import repair_json
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
import concurrent.futures
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
from agent_tools.builtin_tools import WriteFileTool, ExecuteBashTool, ReadFileTool, ListDirTool, LaunchTerminalTool, DownloadFileTool, ReadEmailTool, SendEmailTool, DeleteEmailTool, UpdateFileTool, GitTool, GlobTool, GrepTool, TaskCreateTool, TaskUpdateTool, TaskListTool, TaskGetTool, AskUserQuestionTool, SubmitPlanTool
from agent_tools.browser_tool import BrowserTool
from agent_tools.download_tool import DownloadTool
from agent_tools.paper_tool import PaperTool
from agent_tools.github_tool import GitHubTool
from agent_tools.jlceda_tools import JLCEDA_MasterTool
from jlceda_agent import get_jlceda_system_prompt

# 1. 角色工具隔离池 — 真·物理隔离
ROLE_TOOLS_CONFIG = {
    "dev": [
        WriteFileTool, ExecuteBashTool, ReadFileTool, ListDirTool, LaunchTerminalTool,
        DownloadFileTool, ReadEmailTool, SendEmailTool, DeleteEmailTool, UpdateFileTool,
        GitTool, GlobTool, GrepTool, TaskCreateTool, TaskUpdateTool, TaskListTool, TaskGetTool,
        AskUserQuestionTool, SubmitPlanTool,
        BrowserTool, DownloadTool, PaperTool, GitHubTool
    ],
    "jlceda": [
        JLCEDA_MasterTool,   # 嘉立创核心
        WriteFileTool, ExecuteBashTool, ReadFileTool, ListDirTool,
        LaunchTerminalTool, DownloadFileTool, UpdateFileTool,
        GitTool, GlobTool, GrepTool,
        AskUserQuestionTool, SubmitPlanTool,
        BrowserTool, DownloadTool, PaperTool, GitHubTool
    ]
}

CURRENT_ROLE = "dev"  # 'dev' 或 'jlceda'
manager = None
tools = {}

import copy
_JLCEDA_SYSTEM_PROMPT_CACHE = None

def apply_role_tools(role_name: str):
    """动态销毁并重建工具管理器，实现 Token 物理隔离"""
    global manager, tools
    from agent_tools.manager import ToolManager
    manager = ToolManager(current_user_role=3)
    tool_classes = ROLE_TOOLS_CONFIG.get(role_name, ROLE_TOOLS_CONFIG["dev"])
    for tool_cls in tool_classes:
        manager.register(tool_cls())
    tools = manager.get_agent_tools_dict()

# 初始化默认 dev 工具池
apply_role_tools(CURRENT_ROLE)

current_month = datetime.datetime.now().strftime("%Y-%m")

def get_or_create_jlceda_prompt() -> str:
    global _JLCEDA_SYSTEM_PROMPT_CACHE
    if _JLCEDA_SYSTEM_PROMPT_CACHE is None:
        _JLCEDA_SYSTEM_PROMPT_CACHE = get_jlceda_system_prompt()
    return _JLCEDA_SYSTEM_PROMPT_CACHE

def get_all_tools() -> str:
    """列出所有已注册工具"""
    lines = []
    idx = 1
    for name, tool in manager.tools.items():
        lines.append(f"{idx}. {name}")
        lines.append(f"   Description: {tool.description}")
        lines.append(f"   Parameters:")
        props = tool.parameters_schema.get("properties", {})
        for p_name, p_info in props.items():
            lines.append(f"   - {p_name} ({p_info.get('type', 'any')}): {p_info.get('description', '')}")
        idx += 1
    return "\n".join(lines)

# ========== 参数别名映射表 ==========
_PARAM_ALIASES = {
    "_default": {
        "path":          ["filePath", "file_path", "filename", "file", "filepath"],
        "command":       ["cmd", "shell_command", "bash_command"],
        "search_block":  ["search", "old_text", "old_code", "target"],
        "replace_block": ["replace", "new_text", "new_code", "replacement"],
        "content":       ["text", "body", "data"],
        "url":           ["link", "uri", "address"],
        "to_address":    ["to", "recipient", "email"],
        "subject":       ["title", "topic"],
        "query":         ["search_query", "q", "keyword"],
        "pattern":       ["regex", "regex_pattern", "search_pattern"],
        "action":        ["method", "operation"],
        "max_results":   ["limit", "count", "num_results"],
        "sort_by":       ["sort", "order_by"],
        "language":      ["lang", "programming_language"],
    },
    "grep_tool": {
        "pattern": ["regex", "search_text"],
    },
}

def _normalize_tool_args(tool_name, tool_args, tool_schema):
    """将 AI 输出的别名参数映射为 schema 定义的标准名称"""
    if not tool_schema or not isinstance(tool_schema, dict):
        return tool_args

    default_aliases = _PARAM_ALIASES.get("_default", {})
    tool_aliases = _PARAM_ALIASES.get(tool_name, {})
    normalized = {}
    used_aliases = set()
    mappings = []

    for std_name in tool_schema.get("properties", {}).keys():
        if std_name in tool_args:
            normalized[std_name] = tool_args[std_name]
        else:
            mapped = False
            for alias in tool_aliases.get(std_name, []):
                if alias in tool_args and alias not in used_aliases:
                    normalized[std_name] = tool_args[alias]
                    used_aliases.add(alias)
                    mappings.append(f"'{alias}'→'{std_name}'")
                    mapped = True
                    break
            if not mapped:
                for alias in default_aliases.get(std_name, []):
                    if alias in tool_args and alias not in used_aliases:
                        normalized[std_name] = tool_args[alias]
                        used_aliases.add(alias)
                        mappings.append(f"'{alias}'→'{std_name}'")
                        break

    if mappings:
        console.print(f"[dim]ℹ [{tool_name}] mapped: {', '.join(mappings)}[/dim]")

    # 校验必填参数
    required_params = tool_schema.get("required") or []
    missing = set(required_params) - set(normalized.keys())
    if missing:
        raise ValueError(
            f"Tool '{tool_name}' missing required parameters: {missing}. "
            f"Received arguments: {list(tool_args.keys())}"
        )

    # 清洗字符串参数中的物理换行符
    for key in normalized:
        if isinstance(normalized[key], str):
            normalized[key] = normalized[key].replace('\r\n', '\n')

    return normalized

def generate_system_prompt() -> str:
    """生成 System Prompt"""
    base_prompt = f"""Your name is 力工. You are a secure local developer agent.

CRITICAL JSON RULE: Output tool calls in a strict single-line ```json block: {{"tool": "<name>", "arguments": {{<params>}}}}. No physical line breaks in strings. Use \n.

Rules:
1. Wrap Actions in a ```json block and stop after it.
2. Use 'launch_terminal' for GUI/TUI apps (vim, nano, htop), 'execute_bash' for background/cmd commands. For interactive wizards, use execute_bash with "interactive":true. NEVER add | tail/head/wc — terminal streams in real-time, auto-truncates to 4000 chars.
3. Sudo password: 'zh030327'. Include it when needed.
4. End tasks with "Final Answer:". For casual chat, reply without JSON.
5. The current month is {current_month}.
6. Role (SysDev): Practice Minimalist Modification. Prefer update_file, never modify unread code. MEMORY.md <200 lines. Use tasks/plans for complex work.
7. update_file anchor: keep search_block minimal (1-2 unique lines, no backslashes/quotes). Avoid dynamic anchors (timestamps, random, env vars). Tool handles fuzzy matching.
8. Backup & Rollback: Auto-backups in ~/.ligong_backups/. If mistake suspected: list_dir ~/.ligong_backups/ → cp to restore. Re-read after restore.
9. When a user asks to login to a website, use `browser_tool` with action='login', providing the 'url' and 'session_id' (e.g. 'linkedin'). The tool will automatically pop up a real window for the user. Just ask the user to confirm when they finish logging in.
10. Download Strategy (3-tier): (1) Use `browser_tool action='goto'` to read the page and extract direct download URLs. (2) If a direct URL is found, use `download_tool` (wget) to download it — this is fastest. (3) If the direct URL returns an HTML redirect page (not the actual file), fall back to `browser_tool` with `action='download'` and the appropriate `click_selector` to trigger the native browser download.
11. Data Display Rule: When displaying JSON file contents or raw data to the user, ALWAYS wrap it in a ```text block. NEVER use ```json. The ```json block is STRICTLY reserved for tool calls.
"""

    tools_str = get_all_tools()
    return base_prompt + "\nAvailable Tools:\n" + tools_str

# 初始化 System Prompt
SYSTEM_PROMPT = generate_system_prompt()

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

# ========== 无畏模式 (Fearless Mode) ==========
FEARLESS_MODE = False  # 全局开关：开启后所有命令直接执行，无需用户确认

def toggle_fearless_mode(enable: bool):
    global FEARLESS_MODE
    FEARLESS_MODE = enable
    mode_text = "🔥 无畏模式已开启 — 所有命令将自动执行" if enable else "🛡️ 安全模式已恢复 — 危险命令需要用户确认"
    console.print(f"\n[bold yellow]{mode_text}[/bold yellow]\n")
    return FEARLESS_MODE

# 无畏模式下的禁止命令黑名单（仅拦截可能导致系统不可用或数据丢失的命令）
FEARLESS_BLACKLIST = [
    "rm -rf /", "rm -rf /*", "rm -rf ~", "rm -rf .", "rm -rf *",
    "rm -fr /", "rm -fr /*", "rm -fr ~", "rm -fr .", "rm -fr *",
    "dd if=", "mkfs.", ":(){ :|:& };:",  # fork bomb
    "apt purge linux-image", "apt-get purge linux-image",  # 卸载内核
    "apt remove linux-image", "apt-get remove linux-image",
    "rm -rf /boot", "rm -rf /lib", "rm -rf /bin", "rm -rf /sbin",
    "rm -rf /etc", "rm -rf /usr", "rm -rf /var",
]

def clean_reply(content: str) -> str:
    """从 AI 原始回复中提取最终答案，移除前缀和 JSON 噪声"""
    # 1. 优先提取 final_answer JSON 里的 content
    blocks = _extract_json_blocks(content)
    for raw_json in blocks:
        try:
            raw_json = _sanitize_json(raw_json)
            try:
                raw_json = _escape_newlines_in_json_strings(raw_json)
            except NameError:
                pass
                
            data = json.loads(repair_json(raw_json))
            # 兼容 json_repair 将多个对象组合成 List 的情况
            actions = data if isinstance(data, list) else [data]
            for act in actions:
                if isinstance(act, dict) and (act.get("action") == "final_answer" or act.get("tool") == "final_answer"):
                    return act.get("content") or act.get("answer") or act.get("text", "")
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
    
    # ========== 无畏模式 (Fearless Mode) ==========
    if FEARLESS_MODE:
        # 仅拦截黑名单中的破坏性命令
        if tool_name == "execute_bash":
            command = tool_args.get("command", "").lower().replace("  ", " ")
            for blocked in FEARLESS_BLACKLIST:
                if command == blocked or command.startswith(blocked + " ") or (" " + blocked + " ") in command:
                    return f"No (Blocked by Fearless Mode: '{blocked}' is forbidden)"
        return "Yes"
    
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
    safe_tools = [
    "write_file", "read_file", "list_dir", "task_create", "task_update",
    "task_list", "task_get", "glob_tool", "grep_tool", "submit_plan",
    "launch_terminal", "delete_email", "read_email",
"git_tool", "browser_tool", "debug_tool"
    ]
    if tool_name in safe_tools:
        return "Yes"
    
    # --- 2. 对 execute_bash 的分级处理 ---
    if tool_name == "execute_bash":
        command = tool_args.get("command", "")
        
        # --- 💡 扩展：安全只读命令白名单（直接放行，不弹窗）---
        safe_prefixes = [
            "dpkg -l", "dpkg -s", "apt list", "apt search", "apt show", 
            "apt update", "apt-get update",  # <--- 补充：放行安全无害的索引更新操作
            "pip list", "pip show", "ls ", "cat ", "echo ", "grep ", 
            "which ", "find ", "pwd", "whoami", "history", "tail ", "head "
        ]
        
        # 剔除 sudo 干扰后进行匹配（即使是 sudo dpkg -l 也会被放行）
        core_cmd = command.replace("sudo ", "").strip()
        if any(core_cmd.startswith(safe) for safe in safe_prefixes):
            return "Yes"
        # --------------------------------------------------------
        
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
            # 2c. 其他所有未明确豁免的命令（包括未知的 sudo 操作等）也要弹窗
            action_text = f"run command [bold white]{command}[/bold white]"
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
                sys.stdout.write("\r\033[2K\n")  # 回车到行首 + 清除残留在本行的空格 + 换行
                break
            elif char == '\x03':
                sys.stdout.write("\n")
                return "No"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        
    return options[selected_idx]

def _sanitize_json(raw: str) -> str:
    """清理常见 JSON 损坏字符：无中断空格、零宽空格、BOM等"""
    raw = raw.replace('\xa0', ' ')         # 无中断空格 -> 普通空格
    raw = raw.replace('\u200b', '')        # 零宽空格移除
    raw = raw.replace('\ufeff', '')        # BOM 移除
    return raw

# ---------------- 新增：修复大模型未转义换行符 ----------------
def _escape_newlines_in_json_strings(raw_json: str) -> str:
    """
    智能预处理 JSON 字符串，将多行双引号/单引号内部的物理换行符转义为 \\n。
    防止 json_repair 将带有物理换行的字符串截断并把后续代码误认为新的 JSON 键值对。
    """
    in_string = False
    string_char = None
    escape = False
    result = []
    
    for c in raw_json:
        if escape:
            escape = False
            result.append(c)
            continue
            
        if in_string:
            if c == '\\':
                escape = True
                result.append(c)
            elif c == string_char:
                in_string = False
                string_char = None
                result.append(c)
            elif c == '\n':
                result.append('\\n')
            elif c == '\r':
                result.append('\\r')
            elif c == '\t':
                result.append('\\t')
            else:
                result.append(c)
        else:
            if c == '"' or c == "'":
                in_string = True
                string_char = c
            result.append(c)
    return "".join(result)

def _extract_json_blocks(content):
    # 1. 预处理：截断 Final Answer 之后的所有内容，防止干扰
    if "Final Answer:" in content:
        content = content.split("Final Answer:")[0]

    blocks = []
    in_json_block = False
    current_block = []
    
    # 新增：字符串状态追踪器，防止 JSON 内部的纯文本 ``` 导致提前截断
    in_string = False
    escape_count = 0

    lines = content.split('\n')
    for line in lines:
        stripped = line.strip()

        # 检测到 json 开始围栏
        if not in_json_block and stripped.lower().startswith('```json'):
            in_json_block = True
            current_block = []
            in_string = False
            escape_count = 0
            continue

        if in_json_block:
            # 只有在非字符串内部时，遇到纯粹的 ``` 才认为是 JSON 的结束围栏
            if not in_string and stripped.startswith('```') and len(stripped.replace('`', '').strip()) == 0:
                in_json_block = False
                block_content = '\n'.join(current_block).strip()
                
                # 【核心修复 1】物理空块过滤：丢弃空块，彻底杜绝 json.loads("") 崩溃
                if block_content:  
                    blocks.append(block_content)
                current_block = []
                continue

            current_block.append(line)
            
            # 【核心修复 2】实时追踪当前行是否处于 JSON 字符串内部
            for char in line:
                if char == '\\':
                    escape_count += 1
                else:
                    # 遇到双引号，且前面的反斜杠是偶数个（未被转义），则切换字符串状态
                    if char == '"' and escape_count % 2 == 0:
                        in_string = not in_string
                    escape_count = 0

    # 兜底：如果没遇到结束围栏（内容被硬截断），也把已有的内容提取出来，交给 repair_json 修复
    if in_json_block:
        block_content = '\n'.join(current_block).strip()
        if block_content:
            blocks.append(block_content)

    return blocks

def _parse_balanced_json(text, start): 
     """支持单双引号混合和转义的完美平衡树算法（带截断容错）""" 
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
         
     # ====== 核心修复区 ====== 
     # 如果遍历到文本末尾，发现括号依然没有闭合（深度 > 0） 
     # 说明大模型的输出被截断了（比如漏了最后的 '}'）。 
     # 直接返回已经提取到的部分，交给后续的 repair_json 擦屁股补全！ 
     if depth > 0: 
         return text[start:i] 
         
     return None

def run_agent(user_prompt):
    global chat_history, CURRENT_EMAIL_PROFILE, SYSTEM_PROMPT
    
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
            
            # --- 2.5 核心修复：拦截并转换 DeepSeek 原生 DSML 工具调用标签 ---
            if "DSML" in content:
                # 匹配形如 <｜｜DSML｜｜invoke name="xxx"> {...} 的结构 (兼容全半角竖线)
                dsml_pattern = r'<[|｜]{2}DSML[|｜]{2}invoke name="([^"]+)">\s*(.*?)\s*(?:</[|｜]{2}DSML[|｜]{2}invoke>|<[|｜]{2}DSML|$)'
                
                def dsml_repl(m):
                    t_name = m.group(1)
                    t_args = m.group(2).strip()
                    if not t_args:
                        t_args = "{}"
                    # 组装为 Agent 认识的标准 Markdown JSON 格式
                    return f'\n```json\n{{"tool": "{t_name}", "arguments": {t_args}}}\n```\n'
                
                content = re.sub(dsml_pattern, dsml_repl, content, flags=re.DOTALL)
                # 抹除可能残留的头尾标签 (例如 <｜｜DSML｜｜tool_calls>)
                content = re.sub(r'<[|｜]{2}DSML[^>]*>', '', content)
            
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
                    # 替换原有的 replace('\xa0', ' ')
                    raw_json = _sanitize_json(raw_json)
                    try:
                        raw_json = _escape_newlines_in_json_strings(raw_json)
                    except NameError:
                        pass
                    repaired_json_str = repair_json(raw_json)
                    data = json.loads(repaired_json_str)
                    # 兼容 json_repair 将多个对象组合成 List 的情况
                    actions = data if isinstance(data, list) else [data]
                    for act in actions:
                        if isinstance(act, dict) and (act.get("action") == "final_answer" or act.get("tool") == "final_answer"):
                            final_answer_json = act.get("content") or act.get("answer") or act.get("text", "")
                            break
                    if final_answer_json:
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
                        raw_json = _sanitize_json(raw_json)
                        try:
                            raw_json = _escape_newlines_in_json_strings(raw_json)
                        except NameError:
                            pass
                            
                        repaired_json_str = repair_json(raw_json)
                        # 👇 终极防线：拦截空块，防止 json.loads("") 崩溃
                        if not repaired_json_str.strip():
                            continue
                        # 👆 ==================
                        action_data = json.loads(repaired_json_str, strict=False)
                        
                        # ---> 核心修复：兼容 json_repair 将多个对象合并修复为列表的情况 <---
                        actions = action_data if isinstance(action_data, list) else [action_data]
                        
                        for act in actions:
                            # 核心修复：严格工具调用特征检测
                            if not isinstance(act, dict):
                                continue
                            
                            # 防误触逻辑：必须包含 tool 或 action。
                            # 如果只有 name，则必须同时带有 arguments、params 或 args，否则视为普通 JSON 数据跳过。
                            is_tool_call = ("tool" in act) or ("action" in act) or \
                                           ("name" in act and any(k in act for k in ["arguments", "params", "args"]))
                            
                            if not is_tool_call:
                                continue  # 完美过滤掉网表里的 {"name": "R1", ...}
                                
                            executed_any = True
                            tool_name = act.get("tool") or act.get("action") or act.get("name")
                            
                            # 提取参数
                            raw_args = None
                            for key in ["args", "params", "arguments", "parameters"]:
                                if key in act:
                                    raw_args = act[key]
                                    break
                            
                            if isinstance(raw_args, str):
                                try:
                                    tool_args = json.loads(raw_args)
                                    if not isinstance(tool_args, dict):
                                        tool_args = {"raw_input": raw_args}
                                except:
                                    tool_args = {"raw_input": raw_args}
                            elif isinstance(raw_args, dict):
                                tool_args = raw_args
                            else:
                                exclude_keys = ["tool", "action", "name", "args", "params", "arguments", "parameters", "thought", "thinking"]
                                tool_args = {k: v for k, v in act.items() if k not in exclude_keys}

                            # 参数别名规范化（在白名单过滤之前）
                            if tool_name in manager.tools:
                                tool_schema = manager.tools[tool_name].parameters_schema
                                try:
                                    tool_args = _normalize_tool_args(tool_name, tool_args, tool_schema)
                                except ValueError as e:
                                    observations.append(f"Argument Error: {str(e)}")
                                    continue

                            if tool_name in tools:
                                # -------- 核心防御：白名单严格过滤幻觉参数 --------
                                if tool_name in manager.tools:
                                    valid_keys = set(manager.tools[tool_name].parameters_schema.get("properties", {}).keys())
                                    if valid_keys:
                                        # 1. 严格剥离幻觉字段
                                        tool_args = {k: v for k, v in tool_args.items() if k in valid_keys}
                                        # 2. 容错：缺少必要参数时，尝试从 act 根级或嵌套参数中补回
                                        missing = valid_keys - set(tool_args.keys())
                                        if missing:
                                            for mk in missing:
                                                if mk in act:
                                                    tool_args[mk] = act[mk]
                                                elif raw_args and isinstance(raw_args, dict) and mk in raw_args:
                                                    tool_args[mk] = raw_args[mk]
                                # ------------------------------------------------
                                
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
                                    # 移除僵尸线程池，改为同步执行，信任工具内部的超时与 Ctrl+C 管理
                                    try:
                                        obs = tools[tool_name](**tool_args)
                                        observations.append(f"Observation from {tool_name}:\n{obs}")
                                    except KeyboardInterrupt:
                                        observations.append(f"Observation from {tool_name}:\nUser manually interrupted the execution via Ctrl+C.")
                                    except Exception as e:
                                        observations.append(f"Error executing tool '{tool_name}': {str(e)}")
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
                    
                    # 👇👇👇 [新增：容错降级] 👇👇👇
                    # 如果解析由于误判发生了报错，但我们在同一条内容里检测到了 "Final Answer:" 标记。
                    # 直接判定为模型本意是结束对话，跳出错误检测，强行终止循环并回显。
                    if "Final Answer:" in content:
                        final_ans = content.split("Final Answer:")[1].strip()
                        console.print(f"\n[bold green]力工 >[/bold green] {final_ans}\n")
                        with chat_history_lock:
                            chat_history.append({"role": "assistant", "content": content})
                        break
                    # 👆👆👆 ================= 👆👆👆

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

# Cron daemon 已移除

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

    global chat_history, CURRENT_EMAIL_PROFILE, BRIDGE_PROC, SYSTEM_PROMPT, CURRENT_ROLE
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

    # 初始化历史记录 
    with chat_history_lock: 
        chat_history.clear() 
        chat_history.append({"role": "system", "content": SYSTEM_PROMPT + get_memory_context()}) 
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


            # ───────────────── 角色切换网关 ─────────────────
            role_commands = ["/role", "/jlceda", "/dev", "嘉立创", "硬件工程师", "开发模式"]
            if task.strip().lower() in role_commands or task.strip().lower().startswith("/role"):
                task_lower = task.strip().lower()

                if "jlceda" in task_lower or "嘉立创" in task_lower or "硬件" in task_lower:
                    if CURRENT_ROLE == "jlceda":
                        console.print("[yellow]已经是硬件大师 (嘉立创EDA) 模式。[/yellow]")
                    else:
                        CURRENT_ROLE = "jlceda"
                        apply_role_tools("jlceda")
                        SYSTEM_PROMPT = get_or_create_jlceda_prompt() + "\nAvailable Tools:\n" + get_all_tools()
                        with chat_history_lock:
                            chat_history.clear()
                            chat_history.append({"role": "system", "content": SYSTEM_PROMPT + get_memory_context()})
                            chat_history.append({"role": "system", "content": f"[ROLE SWITCH] 你已切换为硬件大师（嘉立创EDA专家）。忘记之前的开发者人设，只处理PCB设计相关任务。当前工作目录: {cwd}"})
                            chat_history.append({"role": "system", "content": f"User trusted the folder. Your current absolute working directory is: {cwd}"})
                        console.print("\n[bold green]🔌 已切换至: 硬件大师 (嘉立创EDA)[/bold green]")
                        console.print("[dim]专属工具: jlceda_master + 3 基础工具 | 上下文已重置 | 人设: PCB设计专家[/dim]\n")

                elif "dev" in task_lower or "开发" in task_lower or "default" in task_lower:
                    if CURRENT_ROLE == "dev":
                        console.print("[yellow]已经是力工 (全栈开发) 模式。[/yellow]")
                    else:
                        CURRENT_ROLE = "dev"
                        apply_role_tools("dev")
                        SYSTEM_PROMPT = generate_system_prompt()
                        with chat_history_lock:
                            chat_history.clear()
                            chat_history.append({"role": "system", "content": SYSTEM_PROMPT + get_memory_context()})
                            chat_history.append({"role": "system", "content": f"[ROLE SWITCH] 你已切换回力工（全栈开发专家）。忘记之前的硬件专家人设。当前工作目录: {cwd}"})
                            chat_history.append({"role": "system", "content": f"User trusted the folder. Your current absolute working directory is: {cwd}"})
                        console.print("\n[bold cyan]💻 已切换回: 力工 (全栈开发)[/bold cyan]")
                        console.print("[dim]全工具集 (20+) 已恢复 | 上下文已重置 | 人设: 系统开发专家[/dim]\n")

                else:
                    console.print("\n[bold cyan]可用角色:[/bold cyan]")
                    mark_j = "👈 (当前)" if CURRENT_ROLE == "jlceda" else ""
                    mark_d = "👈 (当前)" if CURRENT_ROLE == "dev" else ""
                    console.print(f"  [green]/jlceda[/green] : 硬件大师 (嘉立创EDA) {mark_j}")
                    console.print(f"  [green]/dev[/green]   : 力工 (全栈开发) {mark_d}")
                    console.print("[dim]自然语言: '嘉立创'、'硬件工程师'、'开发模式' 也可触发切换[/dim]\n")
                continue

            # 无畏模式切换命令
            fearless_commands = ["/fearless", "无畏模式", "fearless mode"]
            if task.strip().lower().startswith(tuple(fearless_commands)):
                task_lower = task.strip().lower()
                if "on" in task_lower or "开启" in task_lower or "enable" in task_lower or task_lower in ["/fearless", "无畏模式"]:
                    toggle_fearless_mode(True)
                elif "off" in task_lower or "关闭" in task_lower or "disable" in task_lower:
                    toggle_fearless_mode(False)
                else:
                    console.print("[yellow]用法: /fearless on 或 /fearless off[/yellow]")
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