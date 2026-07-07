"""LSP MCP Tools — Semantic code navigation via Language Server Protocol.

Wraps a python language server (e.g. pyright-langserver) via stdio JSON-RPC.
Exposes `get_definition` and `find_references` for precise code navigation.
"""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

class LSPWrapper:
    """Manages a stdio JSON-RPC connection to a language server."""

    def __init__(self, command: list[str]):
        self.command = command
        self.process: Optional[subprocess.Popen] = None
        self.request_id = 0
        self.responses: Dict[int, Any] = {}
        self.cond = threading.Condition()
        self.reader_thread: Optional[threading.Thread] = None
        self.initialized = False

    def start(self, root_uri: str) -> bool:
        if self.process and self.process.poll() is None:
            return True

        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
            )
            self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.reader_thread.start()
            
            # Send initialize
            res = self.send_request("initialize", {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "capabilities": {},
            })
            if res:
                self.send_notification("initialized", {})
                self.initialized = True
                return True
            return False
        except Exception as e:
            log.error(f"Failed to start LSP: {e}")
            return False

    def _read_loop(self):
        if not self.process or not self.process.stdout:
            return
        
        while True:
            line = self.process.stdout.readline()
            if not line:
                break
            line = line.decode('utf-8').strip()
            if not line:
                continue
                
            if line.startswith("Content-Length:"):
                length = int(line.split(":")[1].strip())
                # Read empty line
                self.process.stdout.readline()
                # Read content
                content = self.process.stdout.read(length).decode('utf-8')
                try:
                    msg = json.loads(content)
                    if "id" in msg and "result" in msg:
                        with self.cond:
                            self.responses[msg["id"]] = msg["result"]
                            self.cond.notify_all()
                except json.JSONDecodeError:
                    pass

    def send_request(self, method: str, params: dict, timeout: float = 5.0) -> Any:
        if not self.process or not self.process.stdin:
            return None
            
        with self.cond:
            self.request_id += 1
            req_id = self.request_id

        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params
        }
        content = json.dumps(msg)
        header = f"Content-Length: {len(content)}\r\n\r\n"
        
        try:
            self.process.stdin.write(header.encode('utf-8'))
            self.process.stdin.write(content.encode('utf-8'))
            self.process.stdin.flush()
        except OSError:
            return None

        start = time.time()
        with self.cond:
            while req_id not in self.responses:
                if time.time() - start > timeout:
                    return None
                self.cond.wait(timeout)
            return self.responses.pop(req_id)

    def send_notification(self, method: str, params: dict):
        if not self.process or not self.process.stdin:
            return
            
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        content = json.dumps(msg)
        header = f"Content-Length: {len(content)}\r\n\r\n"
        try:
            self.process.stdin.write(header.encode('utf-8'))
            self.process.stdin.write(content.encode('utf-8'))
            self.process.stdin.flush()
        except OSError:
            pass

_wrapper = LSPWrapper(["pyright-langserver", "--stdio"])
_repo_root = str(Path(__file__).resolve().parents[2].as_uri())

def _ensure_started():
    if not _wrapper.initialized:
        _wrapper.start(_repo_root)

def _uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    # Windows paths like /C:/foo need to become C:/foo
    if os.name == 'nt' and path.startswith('/') and len(path) > 2 and path[2] == ':':
        path = path[1:]
    return path

def _path_to_uri(path: str) -> str:
    return Path(path).absolute().as_uri()

def get_definition(file_path: str, line: int, character: int) -> dict:
    """Find the definition of the symbol at the given line and character (0-indexed)."""
    _ensure_started()
    if not _wrapper.initialized:
        return {"ok": False, "error": "LSP Server not available"}
        
    uri = _path_to_uri(file_path)
    # LSP line/character are 0-indexed
    res = _wrapper.send_request("textDocument/definition", {
        "textDocument": {"uri": uri},
        "position": {"line": line, "character": character}
    })
    
    if not res:
        return {"ok": False, "error": "Not found"}
        
    if isinstance(res, dict):
        res = [res]
        
    definitions = []
    for loc in res:
        definitions.append({
            "file": _uri_to_path(loc["uri"]),
            "line": loc["range"]["start"]["line"],
            "character": loc["range"]["start"]["character"]
        })
        
    return {"ok": True, "definitions": definitions}

def find_references(file_path: str, line: int, character: int) -> dict:
    """Find all usages of the symbol at the given line and character (0-indexed)."""
    _ensure_started()
    if not _wrapper.initialized:
        return {"ok": False, "error": "LSP Server not available"}
        
    uri = _path_to_uri(file_path)
    res = _wrapper.send_request("textDocument/references", {
        "textDocument": {"uri": uri},
        "position": {"line": line, "character": character},
        "context": {"includeDeclaration": True}
    })
    
    if not res:
        return {"ok": False, "error": "Not found"}
        
    references = []
    for loc in res:
        references.append({
            "file": _uri_to_path(loc["uri"]),
            "line": loc["range"]["start"]["line"],
            "character": loc["range"]["start"]["character"]
        })
        
    return {"ok": True, "references": references}
