import subprocess
import os
import sys
import time
import pexpect
import smtplib
import json
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

        # 原有清洗逻辑
        content = content.strip()
        if content.startswith("```"):
            lines = content.split('\n')
            if len(lines) > 0 and lines[0].startswith("```"): lines = lines[1:]
            if len(lines) > 0 and lines[-1].strip() == "```": lines = lines[:-1]
            content = '\n'.join(lines)
            
        with open(safe_path, 'w', encoding="utf-8") as f:
            f.write(content)
        return f"File '{path}' written successfully to sandbox."

# ----------------- 终极解锁版：执行脚本 (支持 sudo 注入) -----------------
class ExecuteBashTool(BaseTool):
    name = "execute_bash"
    description = "Run any shell command. Destructive root commands are blocked. Supports automatic sudo password injection."
    required_role = 3
    timeout = 120    
    parameters_schema = {
        "required": ["command"],
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "sudo_password": {"type": "string", "description": "Optional. The user password if sudo is required."}
        }
    }

    def run(self, command: str, sudo_password: str = None) -> str:
        forbidden_patterns = ["rm -rf /", "rm -rf *", "rm -fr /", "rm -fr *", "rm -rf .", "rm -rf /*"]
        normalized_cmd = command.lower().replace("  ", " ")
        
        if any(p in normalized_cmd for p in forbidden_patterns):
             raise SecurityError("Safety Block: Destructive command 'rm -rf' on root/wildcard detected.")

        # 拦截必须在新终端执行的命令
        def _must_use_terminal(cmd: str) -> bool:
            lower_cmd = cmd.strip().lower()
            launchers = [
                "xdg-open", "x-www-browser", "gnome-open", "kde-open",
                "sensible-browser", "gio open", "open ", "start ",
                "firefox", "google-chrome", "chromium-browser", "chromium",
                "brave-browser", "vivaldi", "opera", "vlc", "code", "gnome-terminal",
                "xfce4-terminal", "konsole", "terminator", "kazam", "obs", "audacity"
            ]
            if any(lower_cmd.startswith(l) for l in launchers):
                return True
            interactive_patterns = [
                "gh auth", "npm init", "npm login", "ssh ", "ssh-keygen",
                "adduser", "passwd", "nano ", "vim ", "vi ", "emacs "
            ]
            if any(p in lower_cmd for p in interactive_patterns):
                return True
            if any(kw in lower_cmd for kw in ["serve", "daemon", "--watch"]):
                return True
            return False

        if _must_use_terminal(command):
            return (
                "Error: This command is a long‑running GUI app, an installation, or an interactive process. "
                "It MUST be executed using the 'launch_terminal' tool instead of 'execute_bash'. "
                "Please retry with the launch_terminal tool (include sudo_password if needed)."
            )

        try:
            if not sudo_password or "sudo " not in command:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout
                )
                out = result.stdout.strip()
                err = result.stderr.strip()
                if not out and not err:
                    return "Command executed successfully (no output)."
                output = []
                if out: output.append(f"STDOUT:\n{out}")
                if err: output.append(f"STDERR:\n{err}")
                return "\n".join(output)
            
            # pexpect 处理 sudo
            child = pexpect.spawn(f'/bin/bash -c "{command}"', encoding='utf-8', timeout=self.timeout)
            while True:
                index = child.expect([
                    r'\[sudo\] password for',
                    r'Sorry, try again',
                    pexpect.EOF,
                    pexpect.TIMEOUT
                ])
                if index == 0:
                    child.sendline(sudo_password)
                elif index == 1:
                    child.close()
                    return "Error: Sudo password incorrect."
                else:
                    break
            child.timeout = 30
            output = child.read()
            child.close()
            if child.exitstatus != 0:
                return f"Error: Command exited with status {child.exitstatus}.\nOutput:\n{output}"
            return f"Command executed successfully.\nOutput:\n{output}"

        except pexpect.TIMEOUT:
             return f"Error: Command timed out after {self.timeout} seconds."
        except Exception as e:
            return f"Execution Error: {str(e)}"

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

        return meta_info + "----------------------------------------\n" + content

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
        # 移除可能导致安全问题的复杂拼接
        safe_command = command.replace("'", '"')
        
        try:
            # 针对 Debian/Ubuntu 系统，调用 x-terminal-emulator 或 gnome-terminal
            # 命令执行完毕后用 exec bash 保持窗口不自动闪退关闭
            terminal_cmd = f"x-terminal-emulator -e \"bash -c '{safe_command}; echo \\\"\\nProgram exited. Press Enter to close...\\\"; read'\""
            
            # 使用 Popen 异步弹出，不阻塞主 Agent，且绝对不使用 capture_output
            subprocess.Popen(terminal_cmd, shell=True)
            
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
    description = "Download a file from a URL and save it to the local sandbox workspace."
    required_role = 2
    parameters_schema = {
        "required": ["url", "filename"],
        "properties": {
            "url": {"type": "string", "description": "The URL of the file to download."},
            "filename": {"type": "string", "description": "The local filename to save it as (e.g., 'app.zip')."}
        }
    }

    def run(self, url: str, filename: str) -> str:
        safe_path = _secure_path(filename) # 复用安全路径校验
        try:
            # 添加伪装 Header，防止被简单的反爬虫拦截
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response, open(safe_path, 'wb') as out_file:
                out_file.write(response.read())
            return f"Successfully downloaded file from {url} and saved to {filename}"
        except Exception as e:
            return f"Failed to download file: {str(e)}"

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
            from duckduckgo_search import DDGS
        except ImportError:
            return "Error: The 'duckduckgo-search' package is missing. Please use 'execute_bash' to run `pip install duckduckgo-search` first, then try your search again."
        
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
            "imap_server": {"type": "string", "description": "Injected by system"},
            "username": {"type": "string", "description": "Injected by system"},
            "app_password": {"type": "string", "description": "Injected by system"}
        }
    }

    def run(self, imap_server: str = "", username: str = "", app_password: str = "", count: int = 3) -> str:
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
class UpdateFileTool(BaseTool):
    name = "update_file"
    description = "Update a file by searching for a specific block of code and replacing it. Fails if search_block is not unique unless replace_all is true."
    required_role = 2
    parameters_schema = {
        "required": ["path", "search_block", "replace_block"],
        "properties": {
            "path": {"type": "string", "description": "File path."},
            "search_block": {"type": "string", "description": "The exact string currently in the file. Must be unique."},
            "replace_block": {"type": "string", "description": "The new string to replace it with."},
            "replace_all": {"type": "boolean", "description": "Set to true to change every instance of search_block in the file. Default is false."}
        }
    }

    def run(self, path: str, search_block: str, replace_block: str, replace_all: bool = False) -> str:
        safe_path = _secure_path(path)
        
        # 【拦截1】未经阅读直接修改
        if safe_path not in _READ_FILES:
            return f"Error: You MUST use the 'read_file' tool on '{path}' before attempting to edit it."
            
        if not os.path.exists(safe_path):
            return f"Error: File '{path}' not found."
            
        with open(safe_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        occurrences = content.count(search_block)
        
        # 【拦截2】字符串未找到
        if occurrences == 0:
            return "Error: Could not find the exact search_block in the file. Ensure you preserved the exact indentation (tabs/spaces) AFTER the line number prefix from the read output."
            
        # 【拦截3】字符串不唯一且未开启 replace_all
        if occurrences > 1 and not replace_all:
            return f"Error: The search_block is not unique (found {occurrences} times). Please provide a larger string with more surrounding context to make it unique, or set 'replace_all': true."
            
        if replace_all:
            new_content = content.replace(search_block, replace_block)
        else:
            new_content = content.replace(search_block, replace_block, 1)
            
        with open(safe_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
            
        return f"Successfully updated '{path}'. Replaced {occurrences} occurrence(s)."

# ----------------- 新增：Git 核心操作工具 -----------------
class GitTool(BaseTool):
    name = "git_tool"
    description = "Execute git commands (e.g., 'status', 'add .', 'commit -m \"msg\"', 'push', 'pull', 'log', 'branch'). Just provide the arguments after 'git'."
    required_role = 2
    parameters_schema = {
        "required": ["command"],
        "properties": {
            "command": {"type": "string", "description": "The git subcommand and arguments (e.g., 'commit -m \"fix bug\"' or 'status')."}
        }
    }

    def run(self, command: str) -> str:
        # 防挂起保护：拦截没有 -m 的 commit，防止弹出 vim/nano 导致主线程永久卡死
        if command.strip() in ["commit", "commit -a"]:
            return "Error: You must provide a commit message using -m, e.g., 'commit -m \"message\"'. Interactive text editors are not supported in this sandbox."
            
        full_command = f"git {command}"
        try:
            result = subprocess.run(
                full_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout
            )
            
            out = result.stdout.strip()
            err = result.stderr.strip()
            
            if result.returncode != 0:
                return f"Git Command Failed (Code {result.returncode}):\n{err}\n{out}".strip()
            
            output = []
            if out: output.append(f"STDOUT:\n{out}")
            if err: output.append(f"STDERR:\n{err}")
            return "\n".join(output) if output else "Git command executed successfully (no output)."
        except Exception as e:
            return f"Git Execution Error: {str(e)}"

# ----------------- 新增：Glob 搜索工具 -----------------
import glob

class GlobTool(BaseTool):
    name = "glob_tool"
    description = "Fast file pattern matching tool. Supports glob patterns like '**/*.js'. Returns matching file paths sorted by modification time. Use this to find files by name patterns."
    required_role = 1
    parameters_schema = {
        "required": ["pattern"],
        "properties": {
            "pattern": {"type": "string", "description": "The glob pattern to search for (e.g., 'src/**/*.js')."}
        }
    }

    def run(self, pattern: str) -> str:
        try:
            # 兼容绝对路径和相对路径处理
            safe_pattern = _secure_path(pattern) if not pattern.startswith("*") and not os.path.isabs(pattern) else pattern
            
            if pattern.startswith("*"):
                 matches = glob.glob(os.path.join(os.getcwd(), pattern), recursive=True)
            else:
                 matches = glob.glob(safe_pattern, recursive=True)
                 
            if not matches:
                return f"No files found matching pattern: {pattern}"
            
            # 按修改时间倒序排列 (最近修改的文件排在最前面)
            matches.sort(key=lambda x: os.path.getmtime(x) if os.path.exists(x) else 0, reverse=True)
            
            # 转为相对路径，让大模型看着更清爽
            cwd = os.getcwd()
            rel_matches = [os.path.relpath(m, cwd) for m in matches]
            
            if len(rel_matches) > 200:
                return "\n".join(rel_matches[:200]) + f"\n... and {len(rel_matches) - 200} more."
            return "\n".join(rel_matches)
        except Exception as e:
            return f"Glob error: {str(e)}"

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
        # 在终端渲染交互菜单
        print(f"\n\033[1;33m[力工 提问]\033[0m \033[1m{question}\033[0m")
        for i, opt in enumerate(options):
            print(f"  \033[1;36m{i + 1}.\033[0m {opt}")
            # 如果有预览，渲染预览块
            if previews and str(i) in previews:
                preview_text = previews[str(i)]
                print(f"      \033[2m--- Preview ---\n      {preview_text.replace(chr(10), chr(10)+'      ')}\n      ---------------\033[0m")
        
        # 自动增加 Other 选项
        other_idx = len(options) + 1
        print(f"  \033[1;36m{other_idx}.\033[0m Other (custom input)")
        
        prompt_text = "  (Multi-select: commas allowed, or type text) ❯ " if multi_select else "  (Enter number or type text) ❯ "
        
        try:
            sys.stdout.flush()
            user_input = input(f"\n{prompt_text}").strip()
            
            if multi_select:
                parts = [p.strip() for p in user_input.split(",")]
                answers = []
                for p in parts:
                    if p.isdigit():
                        idx = int(p)
                        if 1 <= idx <= len(options):
                            answers.append(options[idx - 1])
                        elif idx == other_idx:
                            custom = input("  Please specify 'Other': ").strip()
                            answers.append(f"Other: {custom}")
                        else:
                            answers.append(p)
                    else:
                        answers.append(p)
                return "User selected: " + ", ".join(answers)
            else:
                if user_input.isdigit():
                    idx = int(user_input)
                    if 1 <= idx <= len(options):
                        return f"User selected: {options[idx - 1]}"
                    elif idx == other_idx:
                        custom = input("  Please specify 'Other': ").strip()
                        return f"User selected: Other: {custom}"
                # 如果用户直接输入了文字，当做自定义文本返回
                return f"User selected/answered: {user_input}"
        except Exception as e:
            return f"Error receiving user input: {e}"

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
            sys.stdout.flush()
            user_input = input("\n  ❯ Do you approve this plan? (Press Enter to approve, or type your modifications) : ").strip()
            
            if not user_input or user_input.lower() in ['y', 'yes', 'ok', 'approve']:
                return "User approved the plan. You may proceed with execution."
            else:
                return f"User provided feedback or modifications: {user_input}"
        except Exception as e:
            return f"Error receiving user input: {e}"