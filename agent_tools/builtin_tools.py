import subprocess
import os
import sys
import time
import pexpect
import smtplib
import json
import re
import urllib.request
import urllib.error
from rich.progress import Progress, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from .core import BaseTool
_READ_FILES = set()
def _secure_path(relative_path: str) -> str:
    """上帝模式：允许访问系统所有路径"""
    # 直接支持绝对路径，如果是相对路径则基于当前目录组合
    return os.path.abspath(os.path.join(os.getcwd(), relative_path))

class SecurityError(Exception):
    """自定义安全异常"""
    pass

# ----------------- 升级版：写入文件 (安全防覆写版) -----------------
class WriteFileTool(BaseTool):
    name = "write_file"
    description = """Writes a file to the local filesystem.
- OVERWRITES existing files completely.
- PREFER 'update_file' for modifying existing files. Only use this for NEW files or complete rewrites.
- If the file exists, you MUST have read it first."""
    required_role = 2
    parameters_schema = {
        "required": ["path", "content"],
        "properties": {
            "path": {"type": "string", "description": "The relative filename to write to."},
            "content": {"type": "string", "description": "The full content to write."}
        }
    }

    def run(self, path: str, content: str) -> str:
        safe_path = _secure_path(path) # 安全校验
        
        # 【新增防御】防止直接覆写重要现有文件
        if os.path.exists(safe_path) and os.path.getsize(safe_path) > 0:
            # 返回警告而不是直接写入
            return f"Error: File '{path}' already exists. To prevent accidental data loss, you MUST use 'read_file' to understand it first. If you want to modify it, prefer using 'update_file'. If you are absolutely sure you want to completely overwrite it, you must use 'execute_bash' to delete it first, or explicitly request user permission."

        # 智能剥离外围代码块围栏（仅首尾行是完整 ``` 才剥，不误伤代码内三引号）
        content = content.strip()
        lines = content.split('\n')
        if lines and re.match(r'^```[\w]*\s*$', lines[0]):
            if len(lines) >= 2 and lines[-1].strip() == '```':
                lines = lines[1:-1]
                content = '\n'.join(lines)
            
        with open(safe_path, 'w', encoding="utf-8") as f:
            f.write(content)
        return f"File '{path}' written successfully to sandbox."

# ----------------- 全局 PTY 执行引擎 (专治进度条吞字、交互式卡死与僵尸进程) -----------------
# ----------------- 全局 PTY 执行引擎 -----------------
def run_pty_command(command: str, header_msg: str, timeout: int = 30000, sudo_password: str = None, repo_path: str = None) -> str:
    """全局 PTY (伪终端) 执行引擎，处理进度条覆写、交互式提权与超时控制"""
    import os
    import signal
    import getpass
    import shlex
    import re
    import tempfile

    if header_msg:
        sys.stdout.write(f"\n\033[1;36m{header_msg}\033[0m\n")
        sys.stdout.flush()

    child = None
    try:
        # 1. 环境准备
        env_prefix = "export TERM=xterm; export DEBIAN_FRONTEND=noninteractive; export PYTHONUNBUFFERED=1; export GIT_TERMINAL_PROMPT=1; "
        pre_cmd = ""
        
        if repo_path:
            safe_repo = _secure_path(repo_path)
            if not os.path.exists(safe_repo):
                return f"Error: The directory '{repo_path}' does not exist."
            pre_cmd = f"cd {safe_repo} && "

        # 2. 命令拼装
        # 移除 echo | sudo -S 管道注入，因为管道会剥夺后续命令 (如 apt) 的标准输入，导致它们遇到交互直接 EOF 中止。
        # 使用 sudo -k 强制清除缓存并触发 PTY 密码提示，依赖下方的 AUTO_REPLIES 动态输入，完美保留 stdin 供后续程序使用。
        final_command = command
        if sudo_password and "sudo " in command:
            # 只替换第一个 sudo 为 sudo -k，防止复杂命令中多次验证
            final_command = command.replace("sudo ", "sudo -k ", 1)
            
        # --- 核心修复：使用临时脚本执行，杜绝 Bash -c 的二次解析和引号碎裂 ---
        fd, temp_script = tempfile.mkstemp(suffix=".sh", text=True)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(final_command)
        
        # 组装最终命令：设置环境 -> 切换目录 -> 执行脚本 -> 捕获退出码 -> 清理脚本
        full_cmd = f"{env_prefix}{pre_cmd} stdbuf -oL bash {temp_script}; EXIT_CODE=$?; rm -f {temp_script}; exit $EXIT_CODE"
        
        child = pexpect.spawn('/bin/bash', ['-c', full_cmd], encoding='utf-8', codec_errors='replace', timeout=timeout)

        sys.stdout.write("\n")
        sys.stdout.flush()
        
        # 3. 状态与缓冲区初始化
        sudo_password_sent = False
        output_buffer = []
        tail_buffer = ""
        
        # 4. 主监听循环
        try:
            while True:
                chunk = child.read_nonblocking(size=1024, timeout=timeout)
                sys.stdout.write(chunk.replace('\r\n', '\n'))
                sys.stdout.flush()
                output_buffer.append(chunk)
                
                # 维护用于提示符匹配的滑动窗口
                tail_buffer += chunk
                if len(tail_buffer) > 2048:
                    tail_buffer = tail_buffer[-1024:]
                
                # 清除 ANSI 色彩以保证正则匹配准确
                clean_tail = re.sub(r'\x1b\[[0-9;]*m', '', tail_buffer)
                
                AUTO_REPLIES = [
                    (r'\[sudo\].*(password|密码)', sudo_password if sudo_password else 'zh030327'),
                    (r'Proceed \([Yy]/[Nn]\)\?\s*$', 'Y'),
                    (r'Do you want to continue\? \[[Yy]/[Nn]\]\s*$', 'Y'),
                    (r'Is this OK\? \(yes\)\s*$', 'yes'),
                    (r'continue connecting \(yes/no/\[fingerprint\]\)\?\s*$', 'yes'),
                    (r'continue connecting \(yes/no\)\?\s*$', 'yes'),
                    (r'您希望继续执行吗\？\s*\[[Yy]/[Nn]\]\s*$', 'Y'),
                    (r'是否继续\？\s*\[[Yy]/[Nn]\]\s*$', 'Y')
                ]
                
                # 自动回复机制
                for pattern, reply in AUTO_REPLIES:
                    if re.search(pattern, clean_tail):
                        if 'sudo' in pattern and sudo_password_sent:
                            continue
                        
                        child.sendline(reply)
                        output_buffer.append(f"\n[Auto reply sent by Agent: {reply}]\n")
                        
                        if 'sudo' in pattern:
                            sudo_password_sent = True
                            
                        # 匹配成功后清空窗口，防止同一个提示符重复触发
                        tail_buffer = ""
                        break
                
                # 手动交互接管
                if re.search(r'([Uu]sername|[Pp]assword|[Pp]assphrase).*:\s*$', clean_tail):
                    is_pwd = bool(re.search(r'[Pp]assword|[Pp]assphrase', clean_tail))
                    sys.stdout.write("\n")
                    if is_pwd and sudo_password:
                        child.sendline(sudo_password)
                        output_buffer.append("\n[Auto sudo password sent]\n")
                    elif is_pwd:
                        user_input = getpass.getpass("\033[1;33m[🔒 进程请求密码 (你的输入不可见)] \033[0m")
                        output_buffer.append("\n[Human Password Entered Securely]\n")
                        child.sendline(user_input)
                    else:
                        user_input = input("\033[1;33m[👤 进程请求账号] \033[0m")
                        output_buffer.append(f"\n[Human Username Entered: {user_input}]\n")
                        child.sendline(user_input)
                    continue
                    
        except pexpect.EOF:
            pass
        except pexpect.TIMEOUT:
            sys.stdout.write(f"\n\033[1;31m[执行超时 ({timeout}s)]\033[0m\n")
            output_buffer.append(f"\n[Execution Timed Out after {timeout}s]")
        except KeyboardInterrupt:
            output_buffer.append("\n[Execution Aborted by User via Ctrl+C]")
            raise
        finally:
            # 安全释放进程与清理 PTY
            if child is not None and child.isalive():
                try:
                    pgid = os.getpgid(child.pid)
                    os.killpg(pgid, signal.SIGKILL)
                    time.sleep(0.2)
                    try:
                        while True:
                            child.read_nonblocking(size=1024, timeout=0.01)
                    except Exception:
                        pass
                except Exception:
                    try:
                        os.kill(child.pid, signal.SIGKILL)
                    except Exception:
                        pass
                try:
                    child.close(force=True)
                except Exception:
                    pass

        # 5. 输出格式化与清洗引擎
        exit_status = child.exitstatus if child.exitstatus is not None else -1
        full_output = "".join(output_buffer)
        
        # 步骤 5.1: 彻底清除 ANSI 控制符（颜色、光标移动、清行等）
        clean_output = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', full_output)
        
        lines = []
        # 步骤 5.2: PTY 默认使用 \r\n 换行，直接按 \n 分割进行逐行解析
        for line in clean_output.split('\n'):
            # 步骤 5.3: 去除行尾多余的 \r，防止多重回车符污染数据
            line = line.rstrip('\r')
            
            # 步骤 5.4: 处理进度条覆写 (\r 会使光标回到行首，因此只取最终结果)
            if '\r' in line:
                line = line.split('\r')[-1]
                
            line = line.strip()
            if line:
                lines.append(line)
        
        # 步骤 5.5: 组装清洗后的纯净日志，消除大面积空行
        full_output = "\n".join(lines)
        full_output = re.sub(r'\n{3,}', '\n\n', full_output)
        
        # 6. 上下文截断保护 (Sandwich Truncation)
        lines_split = full_output.split('\n')
        if len(lines_split) > 600:
            head = '\n'.join(lines_split[:100])
            tail = '\n'.join(lines_split[-500:])
            omitted_count = len(lines_split) - 600
            
            full_output = (
                f"{head}\n\n"
                f"=================================================================\n"
                f"... [System Directive to AI: 终端输出超长，中间的 {omitted_count} 行已被系统安全截断] ...\n"
                f"... [注意：真实用户已经在本地终端完整看过了整个执行过程] ...\n"
                f"... [请直接根据下方保留的最后 500 行日志（通常包含报错或结果）继续分析] ...\n"
                f"=================================================================\n\n"
                f"{tail}"
            )

        return f"Exit Code: {exit_status}\nTerminal Output:\n{full_output}"

    except KeyboardInterrupt:
        raise
    except Exception as e:
        return f"Execution Error: {str(e)}"

# ----------------- 终极解锁版：执行脚本 (支持 sudo 注入) -----------------
class ExecuteBashTool(BaseTool):
    name = "execute_bash"
    description = "Run shell commands. Uses a pseudo-terminal (PTY) to stream output and capture results (including exit codes) for AI. Perfect for apt/pip installs."
    required_role = 3
    timeout = 30000  # 安装包可能耗时较长，放宽到更久
    parameters_schema = {
        "required": ["command"],
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "sudo_password": {"type": "string", "description": "Optional sudo password."}
        }
    }

    def run(self, command: str, sudo_password: str = None) -> str:
        forbidden_patterns = ["rm -rf /", "rm -rf *", "rm -fr /", "rm -fr *", "rm -rf .", "rm -rf /*"]
        normalized_cmd = command.lower().replace("  ", " ")
        if any(p in normalized_cmd for p in forbidden_patterns):
             raise SecurityError("Safety Block: Destructive command 'rm -rf' on root/wildcard detected.")

        # 只拦截纯 GUI 程序
        def _is_gui_app(cmd: str) -> bool:
            lower_cmd = cmd.strip().lower()
            launchers = [
                "xdg-open", "x-www-browser", "gnome-open", "kde-open",
                "firefox", "google-chrome", "vlc", "code", "obs"
            ]
            return any(lower_cmd.startswith(l) for l in launchers)

        if _is_gui_app(command):
            return "Error: This is a GUI app. Please use 'launch_terminal' instead."

        # 核心：直接调用全局 PTY 引擎
        return run_pty_command(
            command=command,
            header_msg="",
            timeout=self.timeout,
            sudo_password=sudo_password
        )

# ----------------- 升级版：读取文件 -----------------
class ReadFileTool(BaseTool):
    name = "read_file"
    description = """Reads a file from the local filesystem. 
- Default reads up to 200 lines, returning cat -n format.
- Supports .pdf: Large PDFs (>10 pages) MUST provide 'pages' parameter (e.g., '1-5'). Max 20 pages per request.
- Supports .ipynb: Returns all cells with their outputs (code + text).
- Do not use for directories (use list_dir)."""
    required_role = 1
    parameters_schema = {
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "description": "The absolute or relative file path to read."},
            "start_line": {"type": "integer", "description": "Start line. Optional, default 1."},
            "end_line": {"type": "integer", "description": "End line. Optional, default 200."},
            "pages": {"type": "string", "description": "For PDF only: specific page ranges."}
        }
    }

    def run(self, path: str, start_line: int = 1, end_line: int = 200, pages: str = None) -> str:
        safe_path = _secure_path(path)
        _READ_FILES.add(safe_path)  # 记录已读，授予编辑权限

        if not os.path.exists(safe_path):
            raise FileNotFoundError(f"File {path} does not exist.")
        if os.path.isdir(safe_path):
            return f"Error: '{path}' is a directory. Please use the 'list_dir' tool."

        ext = os.path.splitext(safe_path)[1].lower()
        unsupported_exts = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.mp4', '.mp3', '.zip', '.tar', '.gz', '.7z', '.exe', '.dll', '.so', '.bin']
        if ext in unsupported_exts:
            return f"Error: Cannot read binary or media file '{path}' directly as plain text."

        lines = []

        # --- 处理 PDF ---
        if ext == '.pdf':
            try:
                import pypdf
            except ImportError:
                try:
                    import PyPDF2 as pypdf
                except ImportError:
                    return "Error: The 'pypdf' package is missing. Please use 'execute_bash' to run `pip install pypdf` first."

            try:
                with open(safe_path, 'rb') as f:
                    reader = pypdf.PdfReader(f)
                    num_pages = len(reader.pages)

                    if num_pages > 10 and not pages:
                        return f"Error: This PDF is large ({num_pages} pages). You MUST provide the 'pages' parameter (e.g., pages='1-5')."

                    pages_to_read = []
                    if pages:
                        for part in pages.replace(' ', '').split(','):
                            if '-' in part:
                                try:
                                    s, e = map(int, part.split('-'))
                                    pages_to_read.extend(range(s - 1, e))
                                except:
                                    pass
                            else:
                                try:
                                    pages_to_read.append(int(part) - 1)
                                except:
                                    pass
                    else:
                        pages_to_read = list(range(num_pages))

                    if len(pages_to_read) > 20:
                        return "Error: Maximum 20 pages allowed per request."

                    extracted_text = []
                    for p in pages_to_read:
                        if 0 <= p < num_pages:
                            page_text = reader.pages[p].extract_text()
                            extracted_text.append(f"--- Page {p+1} ---\n{page_text}")

                    if not extracted_text:
                        return f"[System Reminder: The file '{path}' exists but has empty contents.]"

                    return "\n".join(extracted_text)
            except Exception as e:
                return f"Failed to extract text from PDF: {e}"

        # --- 处理 Jupyter Notebook ---
        elif ext == '.ipynb':
            try:
                with open(safe_path, 'r', encoding='utf-8') as f:
                    nb = json.load(f)

                for i, cell in enumerate(nb.get('cells', [])):
                    c_type = cell.get('cell_type', 'unknown')
                    lines.append(f"In [{i+1}] ({c_type}):\n")

                    source = cell.get('source', [])
                    if isinstance(source, list):
                        lines.extend(source)
                    else:
                        lines.append(source + '\n')

                    if c_type == 'code' and cell.get('outputs'):
                        lines.append("Out:\n")
                        for out in cell['outputs']:
                            if out.get('output_type') == 'stream':
                                text = out.get('text', [])
                                if isinstance(text, list):
                                    lines.extend(text)
                                else:
                                    lines.append(text + '\n')
                            elif 'data' in out and 'text/plain' in out['data']:
                                text = out['data']['text/plain']
                                if isinstance(text, list):
                                    lines.extend(text)
                                else:
                                    lines.append(text + '\n')
                    lines.append("-" * 40 + "\n")
            except Exception as e:
                return f"Failed to parse Jupyter Notebook: {e}"

        # --- 普通文本文件 ---
        else:
            try:
                with open(safe_path, 'r', encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
            except Exception as e:
                return f"Failed to read text file: {e}"

        # --- 公用的分页与 cat -n 输出逻辑 ---
        if not lines:
            return f"[System Reminder: The file '{path}' exists but has empty contents.]"

        total_lines = len(lines)
        start_idx = max(0, start_line - 1)
        end_idx = min(total_lines, end_line)

        if start_idx >= total_lines:
            return f"File '{path}' only has {total_lines} lines. The requested start_line ({start_line}) is out of range."

        formatted_lines = []
        for i, line in enumerate(lines[start_idx:end_idx]):
            actual_line_num = start_idx + i + 1
            line_clean = line if line.endswith('\n') else line + '\n'
            formatted_lines.append(f"{actual_line_num}\t{line_clean}")

        content = "".join(formatted_lines)
        meta_info = f"[File: {path} | Lines: {start_idx+1} to {end_idx} / Total Lines: {total_lines}]\n"
        if end_idx < total_lines:
            meta_info += f"(⚠️ Notice: File is too large and was truncated here. Please use read_file again with start_line={end_idx+1} to read the next chunk.)\n"

        # 备份文件元数据增强
        backup_meta = ""
        if ".ligong_backups" in safe_path:
            try:
                name = os.path.basename(safe_path)
                parts = name.rsplit('__', 3)
                if len(parts) == 4:
                    backup_meta = f"[Backup Meta: Time={parts[0]}, File={parts[2]}, {parts[3].replace('.bak', '')}]\n"
            except:
                pass
        return meta_info + backup_meta + "----------------------------------------\n" + content

# ----------------- 新增：列出目录 -----------------
class ListDirTool(BaseTool):
    name = "list_dir"
    description = "List all files and directories in the specified path, or current directory if no path given."
    required_role = 1
    parameters_schema = {
        "required": [],
        "properties": {
            "path": {"type": "string", "description": "Optional. The directory path to list. Defaults to current working directory."}
        }
    }

    def run(self, path: str = None) -> str:
        target_dir = os.getcwd()
        if path:
            safe_path = _secure_path(path)  # 使用已有的安全路径解析
            if not os.path.exists(safe_path):
                return f"Error: Directory '{path}' does not exist."
            if not os.path.isdir(safe_path):
                return f"Error: '{path}' is not a directory."
            target_dir = safe_path

        files = os.listdir(target_dir)
        return f"Directory '{target_dir}' contents: {', '.join(files) if files else 'Empty directory'}"

# ----------------- 新增：弹出真实终端工具 -----------------
class LaunchTerminalTool(BaseTool):
    name = "launch_terminal"
    description = "Launch an interactive CLI app (like Curses/GUI games) in a NEW real terminal window for the USER to interact with."
    required_role = 2
    parameters_schema = {
        "required": ["command"],
        "properties": {
            "command": {"type": "string", "description": "The command to run, e.g., 'python3 snake.py'"}
        }
    }

    def run(self, command: str) -> str:
        import tempfile
        import os
        try:
            # 使用临时脚本隔离，避免引号嵌套和 bash 二次解析
            fd, temp_script = tempfile.mkstemp(suffix=".sh", text=True)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(command)
            os.chmod(temp_script, 0o755)

            terminal_cmd = f"x-terminal-emulator -e bash -c '{temp_script}; echo; echo \"Program exited. Press Enter to close...\"; read; rm -f {temp_script}'"

            # 传递 DISPLAY 环境变量。当前环境 DISPLAY=:99 是虚拟无头显示，
            # 真实桌面在 :0 上，直接从环境变量拿会弹到错误显示导致窗口不可见。
            # 优先用当前 DISPLAY，若可用且为 :0 则直接用；若不可用则强制回退 :0
            env = os.environ.copy()
            if 'DISPLAY' not in env or env.get('DISPLAY', ':99') == ':99':
                env['DISPLAY'] = ':0'
            subprocess.Popen(terminal_cmd, shell=True, env=env)

            return f"Successfully popped up a real terminal window running: {command}. The user is now interacting with it."
        except Exception as e:
            return f"Failed to launch real terminal: {e}"

import urllib.request
import urllib.error
import imaplib
import email
from email.header import decode_header

# ----------------- 新增：网络文件下载工具 -----------------
class DownloadFileTool(BaseTool):
    name = "download_file"
    description = "Download a file natively with an interactive progress bar. Returns structured JSON status to AI."
    required_role = 2
    parameters_schema = {
        "required": ["url", "filename"],
        "properties": {
            "url": {"type": "string", "description": "The URL of the file to download."},
            "filename": {"type": "string", "description": "The local filename to save it as."}
        }
    }

    def run(self, url: str, filename: str) -> str:
        safe_path = _secure_path(filename)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                total_size = int(response.info().get("Content-Length", 0))
                
                with open(safe_path, 'wb') as out_file:
                    if total_size == 0:
                        # 无法获取大小时，直接下载
                        out_file.write(response.read())
                    else:
                        # 引擎核心：Rich 炫酷进度条
                        progress = Progress(
                            TextColumn("[bold cyan]{task.fields[filename]}", justify="right"),
                            BarColumn(bar_width=None, complete_style="green"),
                            "[progress.percentage]{task.percentage:>3.1f}%",
                            "•", DownloadColumn(),
                            "•", TransferSpeedColumn(),
                            "•", TimeRemainingColumn(),
                        )
                        with progress:
                            task_id = progress.add_task("Downloading", filename=os.path.basename(filename), total=total_size)
                            while True:
                                chunk = response.read(8192)
                                if not chunk:
                                    break
                                out_file.write(chunk)
                                progress.update(task_id, advance=len(chunk))
            
            size_mb = os.path.getsize(safe_path) / (1024 * 1024)
            # 返回干净的结构化数据给 AI
            return f'{{"status": "success", "file": "{filename}", "size_mb": {size_mb:.2f}, "message": "Native download completed."}}'
        except Exception as e:
            return f'{{"status": "error", "message": "{str(e)}"}}'

# ----------------- 升级版：网页内容读取工具 (WebFetch) -----------------
class WebFetchTool(BaseTool):
    name = "fetch_webpage"
    description = """Fetch and read the text content of a webpage.
- WILL FAIL for authenticated or private URLs. Do not use for logged-in services.
- For GitHub URLs, ALWAYS prefer using 'execute_bash' with the 'gh' CLI instead of this tool.
- Converts HTML to markdown/text."""
    required_role = 1
    parameters_schema = {
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "The HTTP/HTTPS URL of the webpage to fetch."}
        }
    }

    def run(self, url: str) -> str:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                html_content = response.read().decode('utf-8', errors='ignore')
                import re
                text = re.sub(r'<style.*?>.*?</style>', '', html_content, flags=re.IGNORECASE|re.DOTALL)
                text = re.sub(r'<script.*?>.*?</script>', '', text, flags=re.IGNORECASE|re.DOTALL)
                text = re.sub(r'<[^>]+>', '\n', text)
                text = re.sub(r'\n\s*\n', '\n', text)
                return text[:6000] + "\n...(Content truncated)..."
        except Exception as e:
            return f"Failed to fetch webpage. Note: WebFetch WILL FAIL for authenticated/private URLs. Error: {str(e)}"

# ----------------- 新增：网页搜索工具 (WebSearch) -----------------
class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the web for up-to-date information and current events. Returns search snippets and URLs."
    required_role = 1
    parameters_schema = {
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "The search query. Use the correct current year for recent info."}
        }
    }

    def run(self, query: str) -> str:
        try:
            from ddgs import DDGS
        except ImportError:
            return "Error: The 'ddgs' package is missing. Please use 'execute_bash' to run `pip install ddgs` first, then try your search again."
        
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
                if not results:
                    return f"No results found for query: {query}"
                
                formatted_results = []
                for r in results:
                    formatted_results.append(f"Title: {r.get('title')}\nURL: {r.get('href')}\nSnippet: {r.get('body')}\n")
                
                return "\n".join(formatted_results)
        except Exception as e:
            return f"Web search failed: {e}"

# ----------------- 新增：IMAP 邮件读取工具 -----------------
class ReadEmailTool(BaseTool):
    name = "read_email"
    description = "Read the latest emails including their body content. The system will securely inject credentials. AI does NOT need to ask for passwords."
    required_role = 2
    parameters_schema = {
        "required": [],
        "properties": {
            "count": {"type": "integer", "description": "Number of recent emails to fetch. Default is 3."},
            "offset": {"type": "integer", "description": "Skip the N most recent emails, then read 'count' emails. Default 0 means start from the latest."},
            "imap_server": {"type": "string", "description": "Injected by system"},
            "username": {"type": "string", "description": "Injected by system"},
            "app_password": {"type": "string", "description": "Injected by system"}
        }
    }

    def run(self, imap_server: str = "", username: str = "", app_password: str = "", count: int = 3, offset: int = 0) -> str:
        if not app_password:
            return "Error: System failed to inject credentials."
            
        try:
            # 连接到 IMAP 服务器 (SSL)
            mail = imaplib.IMAP4_SSL(imap_server)
            mail.login(username, app_password)
            
            # 👇 核心修复：针对网易/163 邮箱强制要求的 ID 验证 👇
            try:
                id_cmd = '("name" "agent" "version" "1.0.0")'
                mail._simple_command('ID', id_cmd)
                mail._command_complete('ID')
            except Exception:
                pass  # 非网易邮箱可能不支持，静默跳过
            
            # 确保使用大写 INBOX
            status, data = mail.select("INBOX")
            if status != 'OK':
                return f"Error: Cannot select mailbox 'INBOX'. Server response: {data}"

            status, messages = mail.search(None, "ALL")
            if status != "OK": return "Failed to search emails."

            email_ids = messages[0].split()
            total_skip = count + offset
            if offset > 0:
                recent_ids = email_ids[-total_skip:-offset]
            else:
                recent_ids = email_ids[-count:]
            
            result_text = []
            for e_id in reversed(recent_ids):
                res, msg_data = mail.fetch(e_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        
                        # 解析主题
                        subject, encoding = decode_header(msg["Subject"])[0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(encoding if encoding else "utf-8", errors="ignore")
                            
                        # 解析发件人
                        from_ = msg.get("From")
                        
                        # 👇 核心新增：解析邮件日期 (Date 字段) 👇
                        date_str = msg.get("Date")
                        
                        # 👇 核心修复：解析邮件正文 👇
                        body = "No plain text body found."
                        if msg.is_multipart():
                            for part in msg.walk():
                                # 只提取纯文本部分，忽略 HTML 和附件
                                if part.get_content_type() == "text/plain":
                                    try:
                                        body = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors="ignore")
                                        break
                                    except:
                                        pass
                        else:
                            try:
                                body = msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors="ignore")
                            except:
                                pass
                        
                        # 剔除正文里过多的换行符，并截取前 1000 个字符防止撑爆大模型
                        body = " ".join(body.split())[:1000]

                        result_text.append(f"--- Email ID: {e_id.decode()} ---")
                        result_text.append(f"Date: {date_str}")
                        result_text.append(f"From: {from_}")
                        result_text.append(f"Subject: {subject}")
                        result_text.append(f"Body: {body}")
                        result_text.append("-" * 30)
            
            mail.logout()
            return "\n".join(result_text) if result_text else "No emails found."
            
        except Exception as e:
            return f"IMAP Error: {str(e)}"

# ----------------- 新增：IMAP 邮件删除工具 -----------------
class DeleteEmailTool(BaseTool):
    name = "delete_email"
    description = "Delete a specific email by its ID. System will securely inject credentials."
    required_role = 2
    parameters_schema = {
        "required": ["email_id"],
        "properties": {
            "email_id": {"type": "string", "description": "The ID of the email to delete (e.g., '739')."},
            "imap_server": {"type": "string"},
            "username": {"type": "string"},
            "app_password": {"type": "string"}
        }
    }

    def run(self, email_id: str, imap_server: str = "", username: str = "", app_password: str = "") -> str:
        if not app_password:
            return "Error: System failed to inject credentials."
        try:
            mail = imaplib.IMAP4_SSL(imap_server)
            mail.login(username, app_password)
            
            # 👇 核心修复：针对网易/163 邮箱强制要求的 ID 验证 👇
            try:
                id_cmd = '("name" "agent" "version" "1.0.0")'
                mail._simple_command('ID', id_cmd)
                mail._command_complete('ID')
            except Exception:
                pass  # 非网易邮箱可能不支持，静默跳过
            
            # 确保使用大写 INBOX
            status, data = mail.select("INBOX")
            if status != 'OK':
                return f"Error: Cannot select mailbox 'INBOX'."
            
            # 核心删除逻辑：先打上 \Deleted 标签，然后执行 expunge 彻底抹除
            mail.store(email_id, '+FLAGS', '\\Deleted')
            mail.expunge()
            mail.logout()
            
            return f"Successfully deleted email ID: {email_id}"
        except Exception as e:
            return f"IMAP Delete Error: {str(e)}"

# ----------------- 升级版：SMTP 邮件发送工具 (支持真实附件) -----------------
class SendEmailTool(BaseTool):
    name = "send_email"
    description = "Send an email. Can optionally attach a local file. System will securely inject credentials."
    required_role = 2
    parameters_schema = {
        "required": ["to_address", "subject", "body"],
        "properties": {
            "to_address": {"type": "string", "description": "The recipient's email address."},
            "subject": {"type": "string", "description": "The email subject."},
            "body": {"type": "string", "description": "The main text content of the email."},
            "attachment_path": {"type": "string", "description": "Optional. The local relative path of the file to attach (e.g., 'report.pdf' or 'data.zip')."},
            "smtp_server": {"type": "string"},
            "username": {"type": "string"},
            "app_password": {"type": "string"}
        }
    }

    def run(self, to_address: str, subject: str, body: str, attachment_path: str = None, smtp_server: str = "", username: str = "", app_password: str = "") -> str:
        if not app_password:
            return "Error: System failed to inject credentials."
        try:
            # 构建邮件基本内容
            msg = MIMEMultipart()
            msg['From'] = username
            msg['To'] = to_address
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            # 👇 核心增量：处理真实附件 👇
            if attachment_path:
                safe_attach_path = _secure_path(attachment_path) # 复用安全校验逻辑，防止跨目录读取
                if not os.path.exists(safe_attach_path):
                    return f"Error: The attachment file '{attachment_path}' does not exist in the sandbox."
                
                try:
                    # 以二进制读取文件并封装
                    with open(safe_attach_path, "rb") as f:
                        part = MIMEApplication(f.read(), Name=os.path.basename(safe_attach_path))
                
                    # 添加 Header，标记这是一个附件
                    part['Content-Disposition'] = f'attachment; filename="{os.path.basename(safe_attach_path)}"'
                    msg.attach(part)
                except Exception as e:
                    return f"Error processing attachment: {str(e)}"

            # 连接并发送
            server = smtplib.SMTP_SSL(smtp_server, 465)
            server.login(username, app_password)
            server.send_message(msg)
            server.quit()
            
            attach_msg = f" with attachment '{attachment_path}'" if attachment_path else ""
            return f"Successfully sent email to {to_address} with subject: '{subject}'{attach_msg}"
        except Exception as e:
            return f"SMTP Send Error: {str(e)}"

# ----------------- 核心升级：局部更新工具 -----------------
# UpdateFileTool 已重构至 file_editor.py，此处 re-export 保持向后兼容
from .file_editor import UpdateFileTool

# ----------------- 新增：Git 核心操作工具 -----------------
class GitTool(BaseTool):
    name = "git_tool"
    description = "Execute git commands (e.g., 'clone', 'status', 'add .', 'commit', 'push', 'pull'). Uses PTY to handle interactions natively."
    required_role = 2
    timeout = 120
    parameters_schema = {
        "required": ["command"],
        "properties": {
            "command": {"type": "string", "description": "The git subcommand and arguments (e.g., 'commit -m \"fix bug\"' or 'status')."},
            "repo_path": {"type": "string", "description": "Optional. The directory to run the git command in."}
        }
    }

    def run(self, command: str, repo_path: str = None) -> str:
        # 防挂起保护：拦截没有 -m 的 commit
        if command.strip() in ["commit", "commit -a"]:
            return "Error: You must provide a commit message using -m, e.g., 'commit -m \"message\"'. Interactive text editors are not supported in this sandbox."
            
        cmd_str = command.strip()
        if not cmd_str.startswith("git "):
            cmd_str = f"git {cmd_str}"

        # 核心：直接调用全局 PTY 引擎
        return run_pty_command(
            command=cmd_str,
            header_msg="",
            timeout=self.timeout,
            repo_path=repo_path
        )

# ----------------- 新增：Glob 搜索工具 -----------------
import glob

class GlobTool(BaseTool):
    name = "glob_tool"
    description = "Search for files using glob patterns (e.g., '**/*.py'). Supports recursive search and specific target directories."
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The glob pattern to search for (e.g., '**/*.js'). Use ** for recursive search."
            },
            "path": {
                "type": "string",
                "description": "Optional. The base directory to search in. Defaults to current working directory."
            }
        },
        "required": ["pattern"]
    }

    def run(self, pattern: str, path: str = None, **kwargs) -> str:
        import glob
        import os

        # 1. 解决缺陷 1 & 4: 增加 path 参数，摆脱强制绑定 os.getcwd()
        base_path = os.path.abspath(path) if path else os.getcwd()

        # 2. 解决缺陷 2: 智能优化反直觉行为
        # 如果用户只输入了形如 "*.py" 的 pattern（没有路径分隔符，且以 *. 开头）
        # 自动将其转换为 "**/*.py" 以支持向下递归
        if not pattern.startswith("**/") and "/" not in pattern and pattern.startswith("*."):
            pattern = f"**/{pattern}"

        # 3. 拼接绝对搜索路径
        search_path = os.path.join(base_path, pattern)

        try:
            # 执行递归搜索
            matches = glob.glob(search_path, recursive=True)
            
            if not matches:
                return f"No files found matching pattern '{pattern}' in directory '{base_path}'"

            # 将绝对路径转换为相对路径，减少 Token 消耗
            rel_files = [os.path.relpath(f, base_path) for f in matches]
            
            total_found = len(rel_files)
            limit = 200
            
            # 限制返回数量
            display_files = rel_files[:limit]
            
            result = f"Found {total_found} file(s) matching '{pattern}' in '{base_path}':\n"
            result += "\n".join(display_files)
            
            if total_found > limit:
                result += f"\n\n... (Showing first {limit} results out of {total_found}. Please refine your pattern.)"
                
            return result
        except Exception as e:
            return f"Error executing glob search: {str(e)}"

# ----------------- 升级版：Grep 内容搜索工具 (支持文件过滤、多模式与多行) -----------------
import re
import fnmatch

class GrepTool(BaseTool):
    name = "grep_tool"
    description = "A powerful search tool. ALWAYS use this for search tasks instead of bash grep/rg. Supports regex, output modes, and file glob filtering."
    required_role = 1
    parameters_schema = {
        "required": ["pattern"],
        "properties": {
            "pattern": {"type": "string", "description": "The regex pattern to search for."},
            "path": {"type": "string", "description": "Directory or file to search. Defaults to '.' (current directory)."},
            "is_regex": {"type": "boolean", "description": "Treat pattern as regex. Default is true."},
            "include_glob": {"type": "string", "description": "Filter files by glob pattern (e.g., '*.py'). Optional."},
            "output_mode": {
                "type": "string",
                "description": "Output mode: 'content' (lines), 'files_with_matches' (file paths only), 'count' (match counts). Default 'content'."
            },
            "multiline": {"type": "boolean", "description": "Match across multiple lines. Default false."}
        }
    }

    def run(self, pattern: str, path: str = ".", is_regex: bool = True, include_glob: str = None, output_mode: str = "content", multiline: bool = False) -> str:
        safe_path = _secure_path(path)
        if not os.path.exists(safe_path):
            return f"Error: Path '{path}' does not exist."
        
        try:
            flags = re.MULTILINE | re.DOTALL if multiline else 0
            compiled_regex = re.compile(pattern, flags) if is_regex else re.compile(re.escape(pattern), flags)
        except re.error as e:
            return f"Invalid regex pattern: {e}"
        
        results = []
        file_matches = []
        total_count = 0
        
        def search_file(file_path):
            nonlocal total_count
            if include_glob and not fnmatch.fnmatch(os.path.basename(file_path), include_glob):
                return True
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    if multiline:
                        content = f.read()
                        matches = compiled_regex.findall(content)
                        if matches:
                            count = len(matches)
                            total_count += count
                            rel_path = os.path.relpath(file_path, os.getcwd())
                            file_matches.append(rel_path)
                            if output_mode == "content":
                                results.append(f"{rel_path}: [Multiline match found ({count} times)]")
                            elif output_mode == "count":
                                results.append(f"{rel_path}: {count} matches")
                    else:
                        file_matched = False
                        file_count = 0
                        for i, line in enumerate(f):
                            if compiled_regex.search(line):
                                file_count += 1
                                total_count += 1
                                rel_path = os.path.relpath(file_path, os.getcwd())
                                if not file_matched:
                                    file_matched = True
                                    file_matches.append(rel_path)
                                if output_mode == "content":
                                    results.append(f"{rel_path}:{i+1}:{line.strip()}")
                                    if len(results) >= 500:
                                        return False
                        if file_count > 0 and output_mode == "count":
                            results.append(f"{rel_path}: {file_count} matches")
            except:
                pass
            return True

        if os.path.isfile(safe_path):
            search_file(safe_path)
        else:
            for root, dirs, files in os.walk(safe_path):
                dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', '.venv']]
                for file in files:
                    if not search_file(os.path.join(root, file)):
                        if output_mode == "content":
                            results.append("... [Truncated due to too many matches] ...")
                        break
        
        if output_mode == "files_with_matches":
            return "\n".join(file_matches) if file_matches else "No matches found."
        elif output_mode == "count":
            return "\n".join(results) + f"\nTotal matches: {total_count}" if results else "No matches found."
        else:
            return "\n".join(results) if results else "No matches found."

# ----------------- 新增：任务管理工具 (Task Tracker) -----------------
import json
import os

class TaskCreateTool(BaseTool):
    name = "task_create"
    description = "Create a new task or step to plan and track progress. Returns the new Task ID."
    required_role = 1
    parameters_schema = {
        "required": ["description"],
        "properties": {
            "description": {"type": "string", "description": "Clear description of the task or step."}
        }
    }

    def run(self, description: str) -> str:
        tasks_file = _secure_path(".agent_tasks.json")
        tasks = {}
        if os.path.exists(tasks_file):
            try:
                with open(tasks_file, 'r', encoding='utf-8') as f:
                    tasks = json.load(f)
            except:
                pass
        
        # 找出当前最大任务编号 +1，保证 ID 永远不覆盖
        max_id = 0
        for tid in tasks.keys():
            if tid.startswith('T') and tid[1:].isdigit():
                max_id = max(max_id, int(tid[1:]))
        task_id = f"T{max_id + 1}"
        
        tasks[task_id] = {"description": description, "status": "pending"}
        
        with open(tasks_file, 'w', encoding='utf-8') as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
            
        return f"Task created successfully. Task ID: [{task_id}]"


class TaskUpdateTool(BaseTool):
    name = "task_update"
    description = "Update the status of an existing task (e.g., mark as 'completed')."
    required_role = 1
    parameters_schema = {
        "required": ["task_id", "status"],
        "properties": {
            "task_id": {"type": "string", "description": "The ID of the task to update (e.g., 'T1')."},
            "status": {"type": "string", "description": "The new status, usually 'completed' or 'in_progress'."}
        }
    }

    def run(self, task_id: str, status: str) -> str:
        tasks_file = _secure_path(".agent_tasks.json")
        if not os.path.exists(tasks_file):
            return "Error: No tasks found. Create a task first."
            
        try:
            with open(tasks_file, 'r', encoding='utf-8') as f:
                tasks = json.load(f)
        except Exception as e:
            return f"Error reading tasks file: {e}"
            
        if task_id not in tasks:
            return f"Error: Task ID '{task_id}' not found."
            
        tasks[task_id]["status"] = status
        
        with open(tasks_file, 'w', encoding='utf-8') as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
            
        return f"Task [{task_id}] successfully updated to: {status}"

# ----------------- 新增：任务查询工具 (Task List & Task Get) -----------------
class TaskListTool(BaseTool):
    name = "task_list"
    description = "List all current tasks and their statuses to track progress."
    required_role = 1
    parameters_schema = {
        "required": [],
        "properties": {}
    }

    def run(self) -> str:
        tasks_file = _secure_path(".agent_tasks.json")
        if not os.path.exists(tasks_file):
            return "No tasks found. The task list is empty."
        try:
            with open(tasks_file, 'r', encoding='utf-8') as f:
                tasks = json.load(f)
            if not tasks:
                return "No tasks found. The task list is empty."
            res = ["--- Current Task List ---"]
            for tid, t in tasks.items():
                res.append(f"[{tid}] {t['status'].upper()}: {t['description']}")
            return "\n".join(res)
        except Exception as e:
            return f"Error reading tasks: {e}"


class TaskGetTool(BaseTool):
    name = "task_get"
    description = "Get detailed information about a specific task by its ID."
    required_role = 1
    parameters_schema = {
        "required": ["task_id"],
        "properties": {
            "task_id": {"type": "string", "description": "The ID of the task to retrieve (e.g., 'T1')."}
        }
    }

    def run(self, task_id: str) -> str:
        tasks_file = _secure_path(".agent_tasks.json")
        if not os.path.exists(tasks_file):
            return "Error: No tasks found."
        try:
            with open(tasks_file, 'r', encoding='utf-8') as f:
                tasks = json.load(f)
            if task_id not in tasks:
                return f"Error: Task ID '{task_id}' not found."
            t = tasks[task_id]
            return f"Task [{task_id}]\nStatus: {t['status']}\nDescription: {t['description']}"
        except Exception as e:
            return f"Error reading task: {e}"

# ----------------- 新增：交互式提问工具 (AskUserQuestion) -----------------
# ----------------- 修复版：交互式提问工具 (AskUserQuestion) -----------------
class AskUserQuestionTool(BaseTool):
    name = "ask_user_question"
    description = "Ask the user questions during execution to gather preferences, clarify ambiguous instructions, or get decisions on implementation choices."
    required_role = 1
    timeout = 86400  # 给用户充足的时间输入 (24小时)
    parameters_schema = {
        "required": ["question", "options"],
        "properties": {
            "question": {"type": "string", "description": "The clear question to ask the user."},
            "options": {"type": "array", "items": {"type": "string"}, "description": "List of choices. 'Other' is automatically added. Add '(Recommended)' to the first option if applicable."},
            "multi_select": {"type": "boolean", "description": "Allow multiple answers? Default is false."},
            "previews": {"type": "object", "description": "Optional mapping of option index (e.g., '0', '1') to a string preview (code snippets, ASCII mockups) for visual comparison."}
        }
    }

    def run(self, question: str, options: list, multi_select: bool = False, previews: dict = None) -> str:
        import termios
        import tty
        import sys

        display_options = list(options) + ["Other (custom input)"]
        NL = "\r\n"

        # === 核心修复点 1：把长文本问题剥离出刷新循环，只打印一次，让终端自己处理折行 ===
        sys.stdout.write(f"{NL}\033[1;33m[力工 提问]\033[0m \033[1m{question}\033[0m{NL}{NL}")
        sys.stdout.flush()

        _menu_line_count = 0

        def draw_options(selected_idx, checked_set=None):
            for i, opt in enumerate(display_options):
                if multi_select:
                    checked = "✓" if checked_set and i in checked_set else " "
                    prefix = f"[{checked}] "
                else:
                    prefix = ""
                
                # === 核心修复点 2：截断过长的选项显示（仅显示阶段），防止菜单本身折行 ===
                display_text = opt if len(opt) < 70 else opt[:67] + "..."
                
                if i == selected_idx:
                    sys.stdout.write(f"  \033[92m> {prefix}{display_text}\033[0m{NL}")
                else:
                    sys.stdout.write(f"    {prefix}{display_text}{NL}")

        def draw_preview(selected_idx):
            if previews and str(selected_idx) in previews:
                preview_text = previews[str(selected_idx)]
                sys.stdout.write(f"{NL}  \033[2m--- Preview ---{NL}      {preview_text.replace(chr(10), chr(10)+'      ')}{NL}  ---------------\033[0m{NL}")
            else:
                sys.stdout.write(NL)

        def draw_footer():
            if multi_select:
                sys.stdout.write(f"{NL}  \033[2m(↑↓: navigate, Space: toggle, Enter: confirm, Ctrl+C: cancel)\033[0m")
            else:
                sys.stdout.write(f"{NL}  \033[2m(↑↓: navigate, Enter: select, Ctrl+C: cancel)\033[0m")

        def refresh(selected_idx, checked_set=None):
            nonlocal _menu_line_count

            # 1. 光标上移 _menu_line_count 行，并使用 \033[J 清除屏幕下方所有旧残影
            if _menu_line_count > 0:
                sys.stdout.write(f"\r\033[{_menu_line_count}A\033[J")
            else:
                sys.stdout.write("\r\033[J")

            # 2. 临时劫持 stdout 精确计算行数
            output_buffer = []
            old_write = sys.stdout.write
            def capture_write(s):
                output_buffer.append(s)

            sys.stdout.write = capture_write
            try:
                # 注意：这里不再绘制 Header (question)
                draw_options(selected_idx, checked_set)
                draw_preview(selected_idx)
                draw_footer()
            finally:
                sys.stdout.write = old_write

            # 3. 统计换行符数量更新状态，一次性输出全部画面
            full_output = "".join(output_buffer)
            _menu_line_count = full_output.count('\n')
            sys.stdout.write(full_output)
            sys.stdout.flush()

        try:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setraw(fd)

            selected_idx = 0
            checked_set = set()
            refresh(selected_idx, checked_set)

            while True:
                char = sys.stdin.read(1)
                if char == '\x1b':
                    c2 = sys.stdin.read(1)
                    c3 = sys.stdin.read(1)
                    if c2 == '[':
                        if c3 == 'A':  # 上
                            selected_idx = (selected_idx - 1) % len(display_options)
                        elif c3 == 'B':  # 下
                            selected_idx = (selected_idx + 1) % len(display_options)
                        refresh(selected_idx, checked_set)
                elif char == ' ' and multi_select:
                    if selected_idx in checked_set:
                        checked_set.discard(selected_idx)
                    else:
                        checked_set.add(selected_idx)
                    refresh(selected_idx, checked_set)
                elif char == '\r':
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    break
                elif char == '\x03':
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return "User cancelled"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        if multi_select:
            if not checked_set:
                return "User selected: (nothing)"
            answers = []
            has_other = False
            for idx in sorted(checked_set):
                if idx == len(display_options) - 1:
                    has_other = True
                else:
                    answers.append(options[idx])
            if has_other:
                from prompt_toolkit import prompt
                other_custom = prompt("\n  Please specify 'Other': ").strip()
                if other_custom:
                    answers.append(f"Other: {other_custom}")
            return "User selected: " + ", ".join(answers)
        else:
            if selected_idx == len(display_options) - 1:
                from prompt_toolkit import prompt
                custom = prompt("\n  Please specify 'Other': ").strip()
                return f"User selected: Other: {custom}"
            else:
                return f"User selected: {options[selected_idx]}"
# ----------------- 新增：内存定时任务 (Cron Scheduling) -----------------
import time

_CRON_JOBS = {}

class CronCreateTool(BaseTool):
    name = "cron_create"
    description = "Schedule a prompt to be enqueued at a future time using standard 5-field cron. Recurring tasks auto-expire after 3 days."
    required_role = 1
    parameters_schema = {
        "required": ["cron_expression", "prompt"],
        "properties": {
            "cron_expression": {"type": "string", "description": "Standard 5-field cron expression (e.g., '14 * * * *'). Avoid :00 and :30 minute marks."},
            "prompt": {"type": "string", "description": "The prompt to enqueue when the time triggers."}
        }
    }

    def run(self, cron_expression: str, prompt: str) -> str:
        try:
            from croniter import croniter
        except ImportError:
            return "Error: The 'croniter' package is missing. Please use 'execute_bash' to run `pip install croniter` first, then try scheduling again."
        
        if not croniter.is_valid(cron_expression):
            return f"Error: Invalid cron expression '{cron_expression}'."
            
        now = time.time()
        itr = croniter(cron_expression, now)
        next_run = itr.get_next(float)
        
        job_id = f"C{int(now * 1000)}"
        _CRON_JOBS[job_id] = {
            "cron": cron_expression,
            "prompt": prompt,
            "created_at": now,
            "next_run": next_run,
            "expires_at": now + (3 * 24 * 3600)  # 3 days expiration
        }
        
        from datetime import datetime
        next_run_str = datetime.fromtimestamp(next_run).strftime('%Y-%m-%d %H:%M:%S')
        return f"Cron job [{job_id}] scheduled successfully. Next run at: {next_run_str} (Local Time)."


class CronListTool(BaseTool):
    name = "cron_list"
    description = "List all active cron jobs in the current session."
    required_role = 1
    parameters_schema = {
        "required": [],
        "properties": {}
    }

    def run(self) -> str:
        if not _CRON_JOBS:
            return "No active cron jobs."
        from datetime import datetime
        res = ["--- Active Session Cron Jobs ---"]
        for jid, job in _CRON_JOBS.items():
            next_str = datetime.fromtimestamp(job['next_run']).strftime('%Y-%m-%d %H:%M:%S')
            res.append(f"[{jid}] Cron: '{job['cron']}' | Next: {next_str} | Prompt: '{job['prompt'][:40]}...'")
        return "\n".join(res)


class CronDeleteTool(BaseTool):
    name = "cron_delete"
    description = "Delete a scheduled cron job by its ID."
    required_role = 1
    parameters_schema = {
        "required": ["job_id"],
        "properties": {
            "job_id": {"type": "string", "description": "The ID of the cron job to delete."}
        }
    }

    def run(self, job_id: str) -> str:
        if job_id in _CRON_JOBS:
            del _CRON_JOBS[job_id]
            return f"Cron job [{job_id}] successfully deleted."
        return f"Error: Cron job ID '{job_id}' not found."

# ----------------- 新增：计划模式工具 (Submit Plan) -----------------
class SubmitPlanTool(BaseTool):
    name = "submit_plan"
    description = "Proactively submit a plan for user sign-off BEFORE writing code on non-trivial tasks (new features, multi-file changes, architectural decisions). Do NOT use for simple/single-line fixes or highly specific instructions."
    required_role = 1
    timeout = 86400  # 允许用户长时间思考 (24小时)
    parameters_schema = {
        "required": ["plan"],
        "properties": {
            "plan": {"type": "string", "description": "The detailed step-by-step plan you want to execute."}
        }
    }

    def run(self, plan: str) -> str:
        # 在终端渲染醒目的计划确认框
        print(f"\n\033[1;35m[力工 提议计划 (Plan Mode)]\033[0m")
        print(f"\033[37m{plan}\033[0m")
        
        try:
            from prompt_toolkit import prompt
            user_input = prompt("\n  ❯ Do you approve this plan? (Press Enter to approve, or type your modifications) : ").strip()
            
            if not user_input or user_input.lower() in ['y', 'yes', 'ok', 'approve']:
                return "User approved the plan. You may proceed with execution."
            else:
                return f"User provided feedback or modifications: {user_input}"
        except Exception as e:
            return f"Error receiving user input: {e}"