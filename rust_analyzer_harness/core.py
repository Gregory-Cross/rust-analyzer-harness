#!/usr/bin/env python3
"""
Rust Analyzer Tool for Claude Code
A persistent, efficient rust-analyzer integration that provides:
- Fast responses via persistent server connection
- Caching of analysis results
- Integration with Claude Code's tool system
- Support for diagnostics and code actions
"""

import subprocess
import json
import sys
import os
import threading
import queue
import time
import tempfile
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Configuration
RUST_ANALYZER_CMD = "rust-analyzer"
CACHE_DURATION = timedelta(minutes=5)
RESPONSE_TIMEOUT = 60  # Increased for large projects
MAX_DIAGNOSTICS_PER_FILE = 50

@dataclass
class CacheEntry:
    """Represents a cached analysis result"""
    result: Any
    timestamp: datetime
    file_hash: str
    
    def is_valid(self, current_hash: str) -> bool:
        """Check if cache entry is still valid"""
        age = datetime.now() - self.timestamp
        return age < CACHE_DURATION and self.file_hash == current_hash

class PersistentRustAnalyzer:
    """A persistent rust-analyzer server that maintains state across requests"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, project_root: Path):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self, project_root: Path):
        if self._initialized:
            return
            
        self.project_root = project_root
        self.process = None
        self.reader_thread = None
        self.message_queue = queue.Queue()
        self.request_id_counter = 1
        self.cache: Dict[str, CacheEntry] = {}
        self.open_files: Dict[str, str] = {}  # uri -> content hash
        self.diagnostics: Dict[str, List[Dict]] = {}  # uri -> diagnostics
        self._server_ready = threading.Event()
        self._shutdown_requested = False
        self._initialized = True
        self._indexing_complete = threading.Event()
        self._active_progress_tokens = set()  # Track active progress operations
        
    def start(self):
        """Start the rust-analyzer server"""
        if self.process and self.process.poll() is None:
            return  # Already running
        
        self.process = subprocess.Popen(
            [RUST_ANALYZER_CMD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.project_root,
            bufsize=0
        )
        
        self.reader_thread = threading.Thread(target=self._read_messages, daemon=True)
        self.reader_thread.start()
        
        # Also start stderr reader for debugging
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_thread.start()
        
        # Initialize LSP
        self._send_initialize()
        if not self._server_ready.wait(timeout=30):  # Increased timeout
            raise RuntimeError("rust-analyzer failed to initialize within 30 seconds")
        
        # Wait for initial indexing to complete
        self._wait_for_indexing()
        
        # Give rust-analyzer extra time to analyze test code
        import time
        time.sleep(2)
        
    def _read_messages(self):
        """Read and process LSP messages"""
        buffer = b""
        content_length = None
        # Message reader thread started
        
        while not self._shutdown_requested:
            try:
                chunk = self.process.stdout.read(1024)
                if not chunk:
                    print("No more data from rust-analyzer stdout", file=sys.stderr)
                    break
                    
                buffer += chunk
                # Read chunk
                
                while True:
                    if content_length is None:
                        if b"\r\n\r\n" in buffer:
                            header, rest = buffer.split(b"\r\n\r\n", 1)
                            for line in header.split(b"\r\n"):
                                if line.startswith(b"Content-Length: "):
                                    content_length = int(line[16:])
                            buffer = rest
                    
                    if content_length is not None and len(buffer) >= content_length:
                        message_bytes = buffer[:content_length]
                        buffer = buffer[content_length:]
                        content_length = None
                        
                        try:
                            message = json.loads(message_bytes.decode("utf-8"))
                            self._handle_message(message)
                        except json.JSONDecodeError:
                            pass
                    else:
                        break
                        
            except Exception:
                if not self._shutdown_requested:
                    break
                    
    def _handle_message(self, message: Dict[str, Any]):
        """Process incoming LSP messages"""
        # Received message
        
        if "id" in message:
            # Response to our request
            self.message_queue.put(message)
            # Check if this is the initialize response
            if not self._server_ready.is_set():
                self._server_ready.set()
        elif "method" in message:
            # Server notification
            method = message["method"]
            params = message.get("params", {})
            
            if method == "textDocument/publishDiagnostics":
                uri = params.get("uri", "")
                diagnostics = params.get("diagnostics", [])
                
                # Only clear diagnostics if we're explicitly told to (empty list)
                # Don't overwrite with empty if we haven't seen this file before
                if diagnostics or uri in self.diagnostics:
                    self.diagnostics[uri] = diagnostics[:MAX_DIAGNOSTICS_PER_FILE]
                
                # Debug logging disabled - uncomment for debugging
                # if diagnostics and "constant_evaluator" in uri:
                #     print(f"DEBUG: Got {len(diagnostics)} diagnostics for constant_evaluator.rs", file=sys.stderr)
                #     for d in diagnostics[:3]:  # Show first 3
                #         print(f"  - Line {d['range']['start']['line'] + 1}: {d['message'][:100]}", file=sys.stderr)
            elif method == "$/progress":
                # Track progress for readiness detection
                token = params.get("token")
                value = params.get("value", {})
                kind = value.get("kind")
                title = value.get("title", "")
                
                if kind == "begin":
                    self._active_progress_tokens.add(token)
                elif kind == "end":
                    self._active_progress_tokens.discard(token)
                    # Check if all progress is complete
                    if not self._active_progress_tokens:
                        self._indexing_complete.set()
    
    def _wait_for_indexing(self, timeout: float = 120.0):
        """Wait for rust-analyzer to complete initial indexing"""
        print("Waiting for rust-analyzer to complete indexing...", file=sys.stderr)
        
        # First, give it a moment to start progress notifications
        import time
        time.sleep(1)
        
        # If no progress has started yet, wait a bit more
        if not self._active_progress_tokens:
            print("No progress tokens yet, waiting for indexing to start...", file=sys.stderr)
            time.sleep(2)
        
        # Now wait for all progress to complete
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self._indexing_complete.is_set():
                print(f"Indexing completed in {time.time() - start_time:.1f} seconds", file=sys.stderr)
                return
            
            # Also check if there are no active tokens (might have missed the notification)
            if not self._active_progress_tokens and time.time() - start_time > 3:
                print("No active progress tokens - assuming indexing complete", file=sys.stderr)
                return
                
            time.sleep(0.1)
        
        print(f"Warning: Indexing timeout after {timeout} seconds", file=sys.stderr)
    
    def _wait_for_diagnostics_stable(self, stable_time: float = 3.0, timeout: float = 60.0):
        """Wait for diagnostics to stop changing"""
        print("Waiting for diagnostics to stabilize...", file=sys.stderr)
        
        import time
        import copy
        
        last_diagnostic_state = {}
        last_change_time = time.time()
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            # Create a snapshot of current diagnostics count per file
            current_state = {uri: len(diags) for uri, diags in self.diagnostics.items()}
            
            # Check if state changed
            if current_state != last_diagnostic_state:
                last_diagnostic_state = copy.deepcopy(current_state)
                last_change_time = time.time()
                print(f"Diagnostics changed - {sum(current_state.values())} total errors across {len(current_state)} files", file=sys.stderr)
            elif time.time() - last_change_time >= stable_time:
                print(f"Diagnostics stable for {stable_time}s - done waiting", file=sys.stderr)
                return
                
            time.sleep(0.1)
        
        print(f"Warning: Diagnostics didn't stabilize within {timeout}s", file=sys.stderr)
    
    def _read_stderr(self):
        """Read stderr from rust-analyzer for debugging"""
        while not self._shutdown_requested:
            try:
                line = self.process.stderr.readline()
                if not line:
                    break
                # Capture stderr but don't print unless error
            except Exception as e:
                print(f"Error reading stderr: {e}", file=sys.stderr)
                break
                    
    def _send_message(self, message: Dict[str, Any]):
        """Send a message to rust-analyzer"""
        json_str = json.dumps(message)
        content = json_str.encode("utf-8")
        header = f"Content-Length: {len(content)}\r\n\r\n".encode("utf-8")
        
        self.process.stdin.write(header + content)
        self.process.stdin.flush()
        
    def _send_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a request and wait for response"""
        request_id = self.request_id_counter
        self.request_id_counter += 1
        
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params
        }
        
        # Sending request
        self._send_message(request)
        
        # Wait for response
        start_time = time.time()
        while time.time() - start_time < RESPONSE_TIMEOUT:
            try:
                message = self.message_queue.get(timeout=0.1)
                if message.get("id") == request_id:
                    return message
            except queue.Empty:
                # Still waiting...
                continue
                
        raise TimeoutError(f"Timeout waiting for response to {method}")
        
    def _send_notification(self, method: str, params: Dict[str, Any]):
        """Send a notification (no response expected)"""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        self._send_message(notification)
        
    def _send_initialize(self):
        """Send the initialize request"""
        init_params = {
            "processId": os.getpid(),
            "rootUri": self.project_root.as_uri(),
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {},
                    "references": {},
                    "implementation": {},
                    "typeDefinition": {},
                    "codeAction": {},
                    "rename": {},
                    "publishDiagnostics": {"relatedInformation": True}
                },
                "workspace": {
                    "symbol": {},
                    "executeCommand": {}
                }
            },
            "initializationOptions": {
                "cargo": {
                    "allFeatures": True,
                    "allTargets": True,
                    "buildScripts": {
                        "enable": True
                    },
                    "features": []
                },
                "cfg": {
                    "setTest": True
                },
                "check": {
                    "allTargets": True,
                    "command": "check",
                    "extraArgs": ["--tests", "--lib"]
                },
                "checkOnSave": {
                    "enable": True,
                    "command": "check",
                    "allTargets": True,
                    "extraArgs": ["--tests", "--lib"]
                },
                "diagnostics": {
                    "enable": True,
                    "experimental": {
                        "enable": True
                    },
                    "disabled": []
                },
                "procMacro": {
                    "enable": True
                }
            }
        }
        
        response = self._send_request("initialize", init_params)
        if "error" not in response:
            self._send_notification("initialized", {})
            
            # Request workspace reload to ensure test code is analyzed
            import time
            time.sleep(1)
            try:
                self._send_request("rust-analyzer/reloadWorkspace", {})
            except:
                pass  # Some versions might not support this
            
    def _get_file_hash(self, file_path: Path) -> str:
        """Get hash of file contents"""
        try:
            content = file_path.read_text(encoding="utf-8")
            return hashlib.md5(content.encode()).hexdigest()
        except Exception:
            return ""
            
    def _ensure_file_open(self, file_path: Path):
        """Ensure file is open in rust-analyzer"""
        # Ensure absolute path
        file_path = file_path.resolve()
        uri = file_path.as_uri()
        file_hash = self._get_file_hash(file_path)
        
        if uri not in self.open_files or self.open_files[uri] != file_hash:
            # Open or update the file
            try:
                content = file_path.read_text(encoding="utf-8")
                # If file was already open, close it first
                if uri in self.open_files:
                    self._send_notification("textDocument/didClose", {
                        "textDocument": {"uri": uri}
                    })
                    time.sleep(0.1)
                
                self._send_notification("textDocument/didOpen", {
                    "textDocument": {
                        "uri": uri,
                        "languageId": "rust",
                        "version": 1,
                        "text": content
                    }
                })
                self.open_files[uri] = file_hash
                
                # Force a change notification to trigger full analysis including tests
                self._send_notification("textDocument/didChange", {
                    "textDocument": {
                        "uri": uri,
                        "version": 2
                    },
                    "contentChanges": [{
                        "text": content
                    }]
                })
                
                time.sleep(1.0)  # Give analyzer more time to process including test analysis
            except Exception:
                pass
                
    def _get_cache_key(self, method: str, file_path: Path, **kwargs) -> str:
        """Generate cache key for a request"""
        parts = [method, str(file_path)]
        parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return "|".join(parts)
        
    def _check_cache(self, cache_key: str, file_path: Path) -> Optional[Any]:
        """Check if we have a valid cached result"""
        if cache_key in self.cache:
            entry = self.cache[cache_key]
            current_hash = self._get_file_hash(file_path)
            if entry.is_valid(current_hash):
                return entry.result
        return None
        
    def _update_cache(self, cache_key: str, result: Any, file_path: Path):
        """Update the cache with a new result"""
        self.cache[cache_key] = CacheEntry(
            result=result,
            timestamp=datetime.now(),
            file_hash=self._get_file_hash(file_path)
        )
        
    # Public API methods
    
    def get_hover(self, file_path: Path, line: int, column: int) -> Optional[Dict[str, Any]]:
        """Get hover information at a position"""
        file_path = file_path.resolve()  # Ensure absolute
        cache_key = self._get_cache_key("hover", file_path, line=line, column=column)
        cached = self._check_cache(cache_key, file_path)
        if cached is not None:
            return cached
            
        self._ensure_file_open(file_path)
        
        params = {
            "textDocument": {"uri": file_path.as_uri()},
            "position": {"line": line - 1, "character": column - 1}
        }
        
        response = self._send_request("textDocument/hover", params)
        result = response.get("result")
        
        if result and result.get("contents"):
            formatted = self._format_hover(result)
            self._update_cache(cache_key, formatted, file_path)
            return formatted
            
        return None
        
    def get_definition(self, file_path: Path, line: int, column: int) -> List[Dict[str, Any]]:
        """Get definition locations"""
        file_path = file_path.resolve()  # Ensure absolute
        cache_key = self._get_cache_key("definition", file_path, line=line, column=column)
        cached = self._check_cache(cache_key, file_path)
        if cached is not None:
            return cached
            
        self._ensure_file_open(file_path)
        
        params = {
            "textDocument": {"uri": file_path.as_uri()},
            "position": {"line": line - 1, "character": column - 1}
        }
        
        response = self._send_request("textDocument/definition", params)
        locations = self._format_locations(response.get("result", []))
        
        self._update_cache(cache_key, locations, file_path)
        return locations
        
    def get_references(self, file_path: Path, line: int, column: int) -> List[Dict[str, Any]]:
        """Find all references"""
        file_path = file_path.resolve()  # Ensure absolute
        cache_key = self._get_cache_key("references", file_path, line=line, column=column)
        cached = self._check_cache(cache_key, file_path)
        if cached is not None:
            return cached
            
        self._ensure_file_open(file_path)
        
        params = {
            "textDocument": {"uri": file_path.as_uri()},
            "position": {"line": line - 1, "character": column - 1},
            "context": {"includeDeclaration": True}
        }
        
        response = self._send_request("textDocument/references", params)
        locations = self._format_locations(response.get("result", []))
        
        self._update_cache(cache_key, locations, file_path)
        return locations
        
    def get_diagnostics(self, file_path: Optional[Path] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Get current diagnostics"""
        if file_path:
            file_path = file_path.resolve()  # Ensure absolute
            self._ensure_file_open(file_path)
            # Give diagnostics a moment to arrive after opening file
            import time
            time.sleep(0.5)
            uri = file_path.as_uri()
            
            # Windows case fix: try lowercase drive letter
            if uri.startswith('file:///C:'):
                uri_lowercase = uri.replace('file:///C:', 'file:///c:')
                result = self.diagnostics.get(uri_lowercase, self.diagnostics.get(uri, []))
            else:
                result = self.diagnostics.get(uri, [])
            
            return {uri: result}
        else:
            # Wait for diagnostics to stabilize when getting all
            self._wait_for_diagnostics_stable()
            # Return all diagnostics
            print(f"Total diagnostics stored: {sum(len(d) for d in self.diagnostics.values())}", file=sys.stderr)
            print(f"Files with diagnostics: {len(self.diagnostics)}", file=sys.stderr)
            return dict(self.diagnostics)
    
    def get_code_actions(self, file_path: Path, line: int, column: int, diagnostics: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Get code actions (quick fixes) for a position"""
        file_path = file_path.resolve()
        self._ensure_file_open(file_path)
        
        # Build context with diagnostics if provided
        context = {"diagnostics": diagnostics or []}
        
        params = {
            "textDocument": {"uri": file_path.as_uri()},
            "range": {
                "start": {"line": line - 1, "character": column - 1},
                "end": {"line": line - 1, "character": column - 1}
            },
            "context": context
        }
        
        response = self._send_request("textDocument/codeAction", params)
        return response.get("result", [])
            
    def search_symbols(self, query: str) -> List[Dict[str, Any]]:
        """Search workspace symbols"""
        response = self._send_request("workspace/symbol", {"query": query})
        return self._format_symbols(response.get("result", []))
        
    def get_implementations(self, file_path: Path, line: int, column: int) -> List[Dict[str, Any]]:
        """Find implementations"""
        file_path = file_path.resolve()  # Ensure absolute
        cache_key = self._get_cache_key("implementations", file_path, line=line, column=column)
        cached = self._check_cache(cache_key, file_path)
        if cached is not None:
            return cached
            
        self._ensure_file_open(file_path)
        
        params = {
            "textDocument": {"uri": file_path.as_uri()},
            "position": {"line": line - 1, "character": column - 1}
        }
        
        response = self._send_request("textDocument/implementation", params)
        locations = self._format_locations(response.get("result", []))
        
        self._update_cache(cache_key, locations, file_path)
        return locations
        
    def shutdown(self):
        """Shutdown the server"""
        self._shutdown_requested = True
        try:
            self._send_request("shutdown", {})
            self._send_notification("exit", {})
        except:
            pass
        
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=5)
            
    # Formatting helpers
    
    def _format_hover(self, hover_result: Dict[str, Any]) -> Dict[str, Any]:
        """Format hover results"""
        contents = hover_result["contents"]
        if isinstance(contents, dict):
            return {
                "kind": contents.get("kind", "plaintext"),
                "value": contents.get("value", "")
            }
        elif isinstance(contents, str):
            return {"kind": "plaintext", "value": contents}
        elif isinstance(contents, list):
            # Combine multiple parts
            parts = []
            for item in contents:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get("value", ""))
            return {"kind": "markdown", "value": "\n---\n".join(parts)}
        return {"kind": "plaintext", "value": ""}
        
    def _format_locations(self, locations: Any) -> List[Dict[str, Any]]:
        """Format location results"""
        if not locations:
            return []
        if isinstance(locations, dict):
            locations = [locations]
        
        formatted = []
        for loc in locations:
            # Handle both Location and LocationLink
            uri = loc.get("targetUri") or loc.get("uri")
            range_data = loc.get("targetRange") or loc.get("range")
            
            if uri and range_data:
                path = Path(uri.replace("file://", ""))
                formatted.append({
                    "path": str(path),
                    "line": range_data["start"]["line"] + 1,
                    "column": range_data["start"]["character"] + 1,
                    "snippet": self._get_line_snippet(path, range_data["start"]["line"])
                })
                
        return formatted
        
    def _format_symbols(self, symbols: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Format symbol search results"""
        formatted = []
        
        for sym in symbols:
            location = sym.get("location", {})
            uri = location.get("uri", "")
            range_data = location.get("range", {})
            
            if uri:
                path = Path(uri.replace("file://", ""))
                formatted.append({
                    "name": sym.get("name", ""),
                    "kind": self._symbol_kind_name(sym.get("kind", 0)),
                    "container": sym.get("containerName", ""),
                    "path": str(path),
                    "line": range_data.get("start", {}).get("line", 0) + 1,
                    "column": range_data.get("start", {}).get("character", 0) + 1
                })
                
        return formatted
        
    def _get_line_snippet(self, path: Path, line_idx: int) -> str:
        """Get a snippet of the line content"""
        try:
            lines = path.read_text().splitlines()
            if 0 <= line_idx < len(lines):
                return lines[line_idx].strip()
        except:
            pass
        return ""
        
    def _symbol_kind_name(self, kind: int) -> str:
        """Convert symbol kind number to name"""
        kinds = {
            1: "File", 2: "Module", 3: "Namespace", 4: "Package",
            5: "Class", 6: "Method", 7: "Property", 8: "Field",
            9: "Constructor", 10: "Enum", 11: "Interface", 12: "Function",
            13: "Variable", 14: "Constant", 15: "String", 16: "Number",
            17: "Boolean", 18: "Array", 19: "Object", 20: "Key",
            21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
            25: "Operator", 26: "TypeParameter"
        }
        return kinds.get(kind, f"Unknown({kind})")


# CLI Interface
def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Fast rust-analyzer interface for Claude Code"
    )
    
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Common arguments for position-based commands
    pos_parent = argparse.ArgumentParser(add_help=False)
    pos_parent.add_argument("file", type=Path)
    pos_parent.add_argument("line", type=int)
    pos_parent.add_argument("column", type=int)
    
    # Commands
    subparsers.add_parser("hover", parents=[pos_parent])
    subparsers.add_parser("definition", parents=[pos_parent])
    subparsers.add_parser("references", parents=[pos_parent])
    subparsers.add_parser("implementations", parents=[pos_parent])
    
    diag_parser = subparsers.add_parser("diagnostics")
    diag_parser.add_argument("--file", type=Path, required=False)
    
    symbol_parser = subparsers.add_parser("symbols")
    symbol_parser.add_argument("query")
    
    args = parser.parse_args()
    
    # Get or create the persistent server
    server = PersistentRustAnalyzer(Path.cwd())
    server.start()
    
    try:
        # Execute command
        if args.command == "hover":
            result = server.get_hover(args.file, args.line, args.column)
        elif args.command == "definition":
            result = server.get_definition(args.file, args.line, args.column)
        elif args.command == "references":
            result = server.get_references(args.file, args.line, args.column)
        elif args.command == "implementations":
            result = server.get_implementations(args.file, args.line, args.column)
        elif args.command == "diagnostics":
            result = server.get_diagnostics(args.file if hasattr(args, 'file') else None)
        elif args.command == "symbols":
            result = server.search_symbols(args.query)
            
        # Format output
        if args.format == "json":
            print(json.dumps({"status": "success", "result": result}, indent=2))
        else:
            # Markdown formatting
            if args.command == "hover" and result:
                print(f"## Hover Information\n\n```rust\n{result['value']}\n```")
            elif args.command in ["definition", "references", "implementations"] and result:
                print(f"## {args.command.title()}\n")
                for loc in result:
                    print(f"- `{loc['path']}:{loc['line']}:{loc['column']}` - {loc.get('snippet', '')}")
            elif args.command == "diagnostics":
                for uri, diags in result.items():
                    if diags:
                        print(f"## Diagnostics for {uri}\n")
                        for d in diags:
                            severity = ["Error", "Warning", "Info", "Hint"][d.get("severity", 1) - 1]
                            print(f"- **{severity}** at line {d['range']['start']['line'] + 1}: {d['message']}")
            elif args.command == "symbols":
                print("## Symbol Search Results\n")
                for sym in result:
                    print(f"- **{sym['name']}** ({sym['kind']}) in `{sym['path']}:{sym['line']}`")
                    
    except Exception as e:
        output = {"status": "error", "message": str(e)}
        print(json.dumps(output, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()