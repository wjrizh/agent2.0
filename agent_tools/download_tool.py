import os
import base64
from .core import BaseTool
from .builtin_tools import run_pty_command


class DownloadTool(BaseTool):
    name = "download_tool"
    description = "Download a file from URL to local disk with terminal real-time progress. Auto-saves to ~/下载 if no absolute path given. Incomplete files are automatically deleted on Ctrl+C or download failure."
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL of the file to download."
            },
            "filename": {
                "type": "string",
                "description": "Optional. Local filename to save as. If omitted, auto-generates from URL and saves to ~/下载."
            }
        },
        "required": ["url"]
    }
    timeout = 600

    def run(self, url: str, filename: str = "", **kwargs) -> str:
        if not filename:
            filename = url.split('/')[-1].split('?')[0] or "downloaded_file"
        if not os.path.isabs(filename):
            dl_dir = os.path.expanduser("~/下载")
            os.makedirs(dl_dir, exist_ok=True)
            filename = os.path.join(dl_dir, filename)
        filename = os.path.abspath(filename)

        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

        # 脱离进程组的死神守护进程：setsid 创建独立会话，免疫 Python 的 SIGKILL
        script = f"""
TARGET="{filename}"
URL="{url}"
FLAG="${{TARGET}}.success"
rm -f "$FLAG"

setsid bash -c "
    while kill -0 $$ 2>/dev/null; do sleep 0.5; done
    if [ ! -f '{filename}.success' ]; then
        rm -f '{filename}'
    else
        rm -f '{filename}.success'
    fi
" >/dev/null 2>&1 &

wget -U "{ua}" -O "{filename}" "{url}"
EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    touch "{filename}.success"
fi
exit $EXIT_CODE
"""
        b64_script = base64.b64encode(script.encode('utf-8')).decode('utf-8')
        cmd = f'bash -c "$(echo {b64_script} | base64 -d)"'

        return run_pty_command(
            command=cmd,
            header_msg="",
            timeout=self.timeout
        )