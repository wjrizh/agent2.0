import os, json, subprocess, urllib.parse, urllib.request, time
from pathlib import Path
from .core import BaseTool
from json_repair import repair_json

class LspTool(BaseTool):
    name = "lsp_tool"
    description = "LSP client. Actions: 'definition', 'references', 'hover', 'diagnostics'."
    parameters_schema = {
        "type": "object",
        "properties": {
            "server_cmd": {"type": "string", "description": "LSP command (default: 'pylsp')."},
            "action": {"type": "string", "enum": ["definition", "references", "hover", "diagnostics"]},
            "file_path": {"type": "string", "description": "Path to source file."},
            "line": {"type": "integer", "description": "0-indexed line."},
            "character": {"type": "integer", "description": "0-indexed character."}
        },
        "required": ["action", "file_path", "line", "character"]
    }
    timeout = 30

    def run(self, action: str, file_path: str, line: int, character: int, server_cmd: str = "pylsp", **kwargs) -> str:
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            return f"Error: File '{abs_path}' does not exist."

        file_uri = Path(abs_path).as_uri()
        project_root = os.path.dirname(abs_path)

        try:
            proc = subprocess.Popen(
                server_cmd.split(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=project_root
            )
        except Exception as e:
            return f"Error starting LSP server '{server_cmd}': {str(e)}"

        req_id = 1

        def send(method, params, notify=False):
            nonlocal req_id
            req = {"jsonrpc": "2.0", "method": method, "params": params}
            if not notify:
                req["id"] = req_id
                req_id += 1
            body = json.dumps(req).encode()
            proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            proc.stdin.flush()
            return req.get("id")

        def read_msg(timeout=12.0):
            start = time.time()
            while True:
                if time.time() - start > timeout:
                    return {"error": "LSP Read Timeout"}
                h = {}
                while True:
                    if time.time() - start > timeout:
                        return {"error": "LSP Read Timeout"}
                    lb = proc.stdout.readline()
                    if not lb: return None
                    s = lb.decode(errors='replace').strip()
                    if s == "": break
                    if ":" in s:
                        k, v = s.split(":", 1)
                        h[k.strip().lower()] = v.strip()
                cl = int(h.get("content-length", 0))
                if cl == 0: continue
                body = b""
                while len(body) < cl:
                    if time.time() - start > timeout:
                        return {"error": "LSP Read Timeout"}
                    chunk = proc.stdout.read(cl - len(body))
                    if not chunk: break
                    body += chunk
                try:
                    return json.loads(repair_json(body.decode(errors='replace')))
                except:
                    continue

        try:
            # 1. initialize
            init_id = send("initialize", {
                "processId": os.getpid(),
                "rootUri": Path(project_root).as_uri(),
                "capabilities": {"textDocument": {"publishDiagnostics": {"relatedInformation": True}}}
            })
            init_resp = read_msg()
            if not init_resp or "error" in init_resp:
                return f"LSP Init failed: {init_resp}"

            send("initialized", {}, notify=True)

            # 2. open file
            with open(abs_path, 'r') as f:
                text = f.read()
            ext = os.path.splitext(abs_path)[1].lower()
            lang_map = {
                ".py": "python", ".js": "javascript", ".ts": "typescript",
                ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
                ".html": "html", ".css": "css", ".json": "json",
                ".go": "go", ".rs": "rust", ".java": "java"
            }

            send("textDocument/didOpen", {
                "textDocument": {
                    "uri": file_uri,
                    "languageId": lang_map.get(ext, "plaintext"),
                    "version": 1,
                    "text": text
                }
            }, notify=True)

            # 3. query
            if action == "diagnostics":
                resp = read_msg(timeout=15.0)
                # 跳过可能先到达的 logMessage 等通知，直到拿到 publishDiagnostics
                while resp and resp.get("method") != "textDocument/publishDiagnostics":
                    if "error" in resp:
                        break
                    resp = read_msg(timeout=2.0)
            else:
                method_map = {
                    "definition": "textDocument/definition",
                    "references": "textDocument/references",
                    "hover": "textDocument/hover"
                }
                params = {
                    "textDocument": {"uri": file_uri},
                    "position": {"line": line, "character": character}
                }
                if action == "references":
                    params["context"] = {"includeDeclaration": True}
                qid = send(method_map[action], params)
                resp = read_msg()

            # 4. terminate
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except:
                proc.kill()

            if not resp or "error" in resp:
                return f"LSP Error: {resp.get('error', 'No response') if resp else 'No response'}"

            # 5. parse result
            if action == "diagnostics":
                diags = resp.get("params", {}).get("diagnostics", [])
                if not diags:
                    return f"=== LSP DIAGNOSTICS ===\nFile: {file_path}\n0 issues ✅"
                out = []
                sev_map = {1: "🔴 ERROR", 2: "🟡 WARNING", 3: "🔵 INFO", 4: "⚪ HINT"}
                for d in diags:
                    sev = sev_map.get(d.get("severity"), "?")
                    sl = d.get("range", {}).get("start", {}).get("line", 0)
                    msg = d.get("message", "")
                    out.append(f"[{sev}] Line {sl}: {msg}")
                return f"=== LSP DIAGNOSTICS ===\nFile: {file_path}\n{len(out)} issue(s):\n" + "\n".join(out)

            result = resp.get("result")
            if not result:
                return f"No {action} results."

            if action == "hover":
                contents = result.get("contents", "")
                if isinstance(contents, dict):
                    return f"=== HOVER ===\n{contents.get('value', '')}"
                elif isinstance(contents, list):
                    parts = []
                    for c in contents:
                        if isinstance(c, dict):
                            parts.append(c.get("value", str(c)))
                        else:
                            parts.append(str(c))
                    return f"=== HOVER ===\n" + "\n".join(parts)
                return f"=== HOVER ===\n{contents}"

            locations = result if isinstance(result, list) else [result]
            out = []
            for loc in locations:
                uri = loc.get("uri", "")
                path = urllib.request.url2pathname(urllib.parse.urlparse(uri).path)
                sl = loc.get("range", {}).get("start", {}).get("line", 0)
                sc = loc.get("range", {}).get("start", {}).get("character", 0)
                out.append(f"{path}:{sl}:{sc}")
            return f"=== LSP {action.upper()} ===\n" + "\n".join(out)

        except Exception as e:
            try:
                if proc.poll() is None:
                    proc.kill()
            except:
                pass
            return f"LSP Error: {str(e)}"