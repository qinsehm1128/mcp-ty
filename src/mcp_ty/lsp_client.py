"""
LSP Client for ty type checker.

Handles JSON-RPC communication with the ty language server process.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _find_ty_executable() -> str:
    """
    Find the ty executable path.
    
    Search order:
    1. System PATH
    2. User's .local/bin (uv tool install location)
    3. pipx bin directory
    """
    # 1. Check if ty is in PATH
    ty_path = shutil.which("ty")
    if ty_path:
        return ty_path
    
    # 2. Check common installation locations
    home = Path.home()
    
    # Windows: uv installs to ~/.local/bin
    if sys.platform == "win32":
        candidates = [
            home / ".local" / "bin" / "ty.exe",
            home / "AppData" / "Local" / "Programs" / "ty" / "ty.exe",
            home / ".cargo" / "bin" / "ty.exe",
        ]
    else:
        # Linux/macOS
        candidates = [
            home / ".local" / "bin" / "ty",
            home / ".cargo" / "bin" / "ty",
            Path("/usr/local/bin/ty"),
            Path("/usr/bin/ty"),
        ]
    
    for candidate in candidates:
        if candidate.exists():
            logger.info(f"Found ty at: {candidate}")
            return str(candidate)
    
    # 3. Last resort: return "ty" and hope it works
    logger.warning(
        "Could not find ty executable. Please ensure ty is installed:\n"
        "  uv tool install ty\n"
        "Or add it to your PATH."
    )
    return "ty"


@dataclass
class Position:
    """LSP Position (0-based line and character)."""
    line: int
    character: int

    def to_dict(self) -> dict[str, int]:
        return {"line": self.line, "character": self.character}


@dataclass 
class Range:
    """LSP Range."""
    start: Position
    end: Position

    def to_dict(self) -> dict[str, dict[str, int]]:
        return {"start": self.start.to_dict(), "end": self.end.to_dict()}


@dataclass
class Location:
    """LSP Location."""
    uri: str
    range: Range

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Location":
        return cls(
            uri=data["uri"],
            range=Range(
                start=Position(
                    line=data["range"]["start"]["line"],
                    character=data["range"]["start"]["character"]
                ),
                end=Position(
                    line=data["range"]["end"]["line"],
                    character=data["range"]["end"]["character"]
                )
            )
        )


@dataclass
class Diagnostic:
    """LSP Diagnostic."""
    range: Range
    message: str
    severity: int | None = None
    source: str | None = None
    code: str | int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Diagnostic":
        return cls(
            range=Range(
                start=Position(
                    line=data["range"]["start"]["line"],
                    character=data["range"]["start"]["character"]
                ),
                end=Position(
                    line=data["range"]["end"]["line"],
                    character=data["range"]["end"]["character"]
                )
            ),
            message=data["message"],
            severity=data.get("severity"),
            source=data.get("source"),
            code=data.get("code")
        )


@dataclass
class TextEdit:
    """LSP TextEdit - a change to be applied to a document."""
    range: Range
    new_text: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TextEdit":
        return cls(
            range=Range(
                start=Position(
                    line=data["range"]["start"]["line"],
                    character=data["range"]["start"]["character"]
                ),
                end=Position(
                    line=data["range"]["end"]["line"],
                    character=data["range"]["end"]["character"]
                )
            ),
            new_text=data.get("newText", "")
        )


@dataclass
class TextDocumentEdit:
    """Edit to a specific document."""
    uri: str
    edits: list[TextEdit]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TextDocumentEdit":
        uri = data.get("textDocument", {}).get("uri", "")
        edits = [TextEdit.from_dict(e) for e in data.get("edits", [])]
        return cls(uri=uri, edits=edits)


@dataclass
class WorkspaceEdit:
    """LSP WorkspaceEdit - changes across multiple files."""
    changes: dict[str, list[TextEdit]]  # uri -> edits
    document_changes: list[TextDocumentEdit]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkspaceEdit":
        changes: dict[str, list[TextEdit]] = {}
        for uri, edits in data.get("changes", {}).items():
            changes[uri] = [TextEdit.from_dict(e) for e in edits]
        
        document_changes = [
            TextDocumentEdit.from_dict(dc) 
            for dc in data.get("documentChanges", [])
            if "textDocument" in dc  # Filter out create/rename/delete operations
        ]
        
        return cls(changes=changes, document_changes=document_changes)

    def get_all_edits(self) -> dict[str, list[TextEdit]]:
        """Get all edits organized by URI."""
        result = dict(self.changes)
        for doc_edit in self.document_changes:
            if doc_edit.uri in result:
                result[doc_edit.uri].extend(doc_edit.edits)
            else:
                result[doc_edit.uri] = list(doc_edit.edits)
        return result


@dataclass
class CodeAction:
    """LSP CodeAction - a potential fix or refactoring."""
    title: str
    kind: str | None
    diagnostics: list[Diagnostic]
    edit: WorkspaceEdit | None
    is_preferred: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CodeAction":
        edit = None
        if "edit" in data:
            edit = WorkspaceEdit.from_dict(data["edit"])
        
        diagnostics = [
            Diagnostic.from_dict(d) for d in data.get("diagnostics", [])
        ]
        
        return cls(
            title=data.get("title", ""),
            kind=data.get("kind"),
            diagnostics=diagnostics,
            edit=edit,
            is_preferred=data.get("isPreferred", False)
        )


class TyLspClient:
    """
    Async LSP client for communicating with ty language server.
    
    Handles process lifecycle and JSON-RPC protocol communication.
    """

    def __init__(self, ty_command: str | None = None):
        self.ty_command = ty_command or _find_ty_executable()
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future[Any]] = {}
        self._initialized = False
        self._root_uri: str | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._buffer = b""
        # Store diagnostics published by the server
        self._diagnostics: dict[str, list[Diagnostic]] = {}
        self._lock = asyncio.Lock()

    async def start(self, root_path: str | Path) -> None:
        """Start the ty language server and initialize it."""
        root_path = Path(root_path).resolve()
        self._root_uri = root_path.as_uri()

        logger.info(f"Starting ty server for project: {root_path}")
        logger.info(f"Using ty command: {self.ty_command}")

        # Verify ty executable exists
        ty_path = Path(self.ty_command)
        if not ty_path.is_absolute():
            # If not absolute, check if it's findable
            found = shutil.which(self.ty_command)
            if not found:
                raise FileNotFoundError(
                    f"ty executable not found: {self.ty_command}\n"
                    f"Please install ty: uv tool install ty\n"
                    f"Or ensure it's in your PATH."
                )
            logger.info(f"Found ty in PATH: {found}")

        # Start ty server process
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.ty_command, "server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(root_path)
            )
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"Failed to start ty server: {e}\n"
                f"Command: {self.ty_command} server\n"
                f"Please verify ty is installed correctly."
            ) from e

        # Start background reader task
        self._reader_task = asyncio.create_task(self._read_responses())

        # Initialize LSP connection
        await self._initialize()

    async def stop(self) -> None:
        """Stop the ty language server."""
        if self._initialized:
            try:
                await self._send_request("shutdown", None)
                await self._send_notification("exit", None)
            except Exception as e:
                logger.warning(f"Error during LSP shutdown: {e}")

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

        self._initialized = False
        self._process = None
        logger.info("ty server stopped")

    async def _initialize(self) -> None:
        """Send LSP initialize and initialized notifications."""
        init_params = {
            "processId": None,
            "rootUri": self._root_uri,
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {"linkSupport": True},
                    "references": {},
                    "publishDiagnostics": {"relatedInformation": True},
                    "completion": {
                        "completionItem": {"snippetSupport": False}
                    }
                },
                "workspace": {
                    "workspaceFolders": True
                }
            },
            "workspaceFolders": [
                {"uri": self._root_uri, "name": Path(self._root_uri).name}
            ] if self._root_uri else None
        }

        result = await self._send_request("initialize", init_params)
        logger.info(f"Server capabilities: {result.get('capabilities', {}).keys() if result else 'None'}")
        
        await self._send_notification("initialized", {})
        self._initialized = True
        logger.info("LSP connection initialized")

    def _next_id(self) -> int:
        """Get next request ID."""
        self._request_id += 1
        return self._request_id

    async def _send_request(self, method: str, params: Any) -> Any:
        """Send a JSON-RPC request and wait for response."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("LSP server not started")

        request_id = self._next_id()
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params
        }

        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future

        await self._write_message(request)
        logger.debug(f"Sent request {request_id}: {method}")

        try:
            result = await asyncio.wait_for(future, timeout=30.0)
            return result
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise TimeoutError(f"Request {method} timed out")

    async def _send_notification(self, method: str, params: Any) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("LSP server not started")

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }

        await self._write_message(notification)
        logger.debug(f"Sent notification: {method}")

    async def _write_message(self, message: dict[str, Any]) -> None:
        """Write a JSON-RPC message to the server."""
        if not self._process or not self._process.stdin:
            return

        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        
        async with self._lock:
            self._process.stdin.write(header + body)
            await self._process.stdin.drain()

    async def _read_responses(self) -> None:
        """Background task to read and dispatch server responses."""
        if not self._process or not self._process.stdout:
            return

        while True:
            try:
                message = await self._read_message()
                if message is None:
                    break
                await self._handle_message(message)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error reading LSP response: {e}")
                break

    async def _read_message(self) -> dict[str, Any] | None:
        """Read a single JSON-RPC message from the server."""
        if not self._process or not self._process.stdout:
            return None

        # Read headers
        content_length = 0
        while True:
            line = await self._process.stdout.readline()
            if not line:
                return None
            
            line_str = line.decode("ascii").strip()
            if not line_str:
                break
            
            if line_str.lower().startswith("content-length:"):
                content_length = int(line_str.split(":")[1].strip())

        if content_length == 0:
            return None

        # Read body
        body = await self._process.stdout.readexactly(content_length)
        return json.loads(body.decode("utf-8"))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle an incoming JSON-RPC message."""
        if "id" in message and "method" not in message:
            # Response to a request
            request_id = message["id"]
            future = self._pending_requests.pop(request_id, None)
            if future and not future.done():
                if "error" in message:
                    future.set_exception(
                        RuntimeError(f"LSP error: {message['error']}")
                    )
                else:
                    future.set_result(message.get("result"))
        elif "method" in message:
            # Server notification or request
            await self._handle_server_message(message)

    async def _handle_server_message(self, message: dict[str, Any]) -> None:
        """Handle server-initiated notifications and requests."""
        method = message.get("method", "")
        params = message.get("params", {})

        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            diagnostics = [
                Diagnostic.from_dict(d) for d in params.get("diagnostics", [])
            ]
            self._diagnostics[uri] = diagnostics
            logger.debug(f"Received {len(diagnostics)} diagnostics for {uri}")
        elif method == "window/logMessage":
            level = params.get("type", 3)
            msg = params.get("message", "")
            if level <= 1:
                logger.error(f"[ty] {msg}")
            elif level == 2:
                logger.warning(f"[ty] {msg}")
            else:
                logger.info(f"[ty] {msg}")

    # ----- Public LSP API Methods -----

    async def open_document(self, file_path: str | Path) -> None:
        """Notify server that a document has been opened."""
        file_path = Path(file_path).resolve()
        uri = file_path.as_uri()
        
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            raise RuntimeError(f"Cannot read file {file_path}: {e}")

        await self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "python",
                "version": 1,
                "text": content
            }
        })
        logger.debug(f"Opened document: {file_path}")

    async def close_document(self, file_path: str | Path) -> None:
        """Notify server that a document has been closed."""
        file_path = Path(file_path).resolve()
        uri = file_path.as_uri()

        await self._send_notification("textDocument/didClose", {
            "textDocument": {"uri": uri}
        })
        logger.debug(f"Closed document: {file_path}")

    async def get_definition(
        self, file_path: str | Path, line: int, character: int
    ) -> list[Location]:
        """Get definition location for symbol at position."""
        file_path = Path(file_path).resolve()
        uri = file_path.as_uri()

        result = await self._send_request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character}
        })

        if not result:
            return []

        # Result can be Location | Location[] | LocationLink[]
        if isinstance(result, dict):
            return [Location.from_dict(result)]
        elif isinstance(result, list):
            locations = []
            for item in result:
                if "targetUri" in item:
                    # LocationLink format
                    locations.append(Location(
                        uri=item["targetUri"],
                        range=Range(
                            start=Position(
                                line=item["targetSelectionRange"]["start"]["line"],
                                character=item["targetSelectionRange"]["start"]["character"]
                            ),
                            end=Position(
                                line=item["targetSelectionRange"]["end"]["line"],
                                character=item["targetSelectionRange"]["end"]["character"]
                            )
                        )
                    ))
                else:
                    locations.append(Location.from_dict(item))
            return locations
        
        return []

    async def find_references(
        self, file_path: str | Path, line: int, character: int,
        include_declaration: bool = True
    ) -> list[Location]:
        """Find all references to symbol at position."""
        file_path = Path(file_path).resolve()
        uri = file_path.as_uri()

        result = await self._send_request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": include_declaration}
        })

        if not result:
            return []

        return [Location.from_dict(loc) for loc in result]

    async def get_hover(
        self, file_path: str | Path, line: int, character: int
    ) -> str | None:
        """Get hover information (type info) at position."""
        file_path = Path(file_path).resolve()
        uri = file_path.as_uri()

        result = await self._send_request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character}
        })

        if not result or "contents" not in result:
            return None

        contents = result["contents"]
        
        # Contents can be MarkedString | MarkedString[] | MarkupContent
        if isinstance(contents, str):
            return contents
        elif isinstance(contents, dict):
            if "value" in contents:
                return contents["value"]
            elif "kind" in contents:
                return contents.get("value", "")
        elif isinstance(contents, list):
            parts = []
            for item in contents:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and "value" in item:
                    parts.append(item["value"])
            return "\n\n".join(parts)

        return None

    def get_diagnostics(self, file_path: str | Path) -> list[Diagnostic]:
        """Get cached diagnostics for a file."""
        file_path = Path(file_path).resolve()
        uri = file_path.as_uri()
        return self._diagnostics.get(uri, [])

    async def get_completions(
        self, file_path: str | Path, line: int, character: int
    ) -> list[dict[str, Any]]:
        """Get completion items at position."""
        file_path = Path(file_path).resolve()
        uri = file_path.as_uri()

        result = await self._send_request("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character}
        })

        if not result:
            return []

        # Result can be CompletionItem[] | CompletionList
        if isinstance(result, list):
            return result
        elif isinstance(result, dict) and "items" in result:
            return result["items"]

        return []

    async def search_workspace_symbols(self, query: str) -> list[dict[str, Any]]:
        """
        Search for symbols across the entire workspace.
        
        This is semantic search - only finds actual code symbols
        (classes, functions, variables), not text in comments/strings.
        """
        result = await self._send_request("workspace/symbol", {
            "query": query
        })

        if not result:
            return []

        return result

    async def search_document_symbols(
        self, file_path: str | Path
    ) -> list[dict[str, Any]]:
        """Get all symbols defined in a specific document."""
        file_path = Path(file_path).resolve()
        uri = file_path.as_uri()

        result = await self._send_request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri}
        })

        if not result:
            return []

        return result

    async def rename_symbol(
        self, file_path: str | Path, line: int, character: int, new_name: str
    ) -> WorkspaceEdit | None:
        """Rename a symbol across the entire project."""
        file_path = Path(file_path).resolve()
        uri = file_path.as_uri()

        # First check if rename is valid at this position
        prepare_result = await self._send_request("textDocument/prepareRename", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character}
        })

        if not prepare_result:
            return None

        # Perform the actual rename
        result = await self._send_request("textDocument/rename", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "newName": new_name
        })

        if not result:
            return None

        return WorkspaceEdit.from_dict(result)

    async def get_code_actions(
        self, file_path: str | Path, start_line: int, start_char: int,
        end_line: int, end_char: int, diagnostics: list[Diagnostic] | None = None
    ) -> list[CodeAction]:
        """Get available code actions (quick fixes, refactorings) for a range."""
        file_path = Path(file_path).resolve()
        uri = file_path.as_uri()

        context: dict[str, Any] = {"diagnostics": []}
        if diagnostics:
            for diag in diagnostics:
                context["diagnostics"].append({
                    "range": diag.range.to_dict(),
                    "message": diag.message,
                    "severity": diag.severity,
                    "source": diag.source,
                    "code": diag.code
                })

        result = await self._send_request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": {
                "start": {"line": start_line, "character": start_char},
                "end": {"line": end_line, "character": end_char}
            },
            "context": context
        })

        if not result:
            return []

        actions = []
        for item in result:
            # Skip command-only actions (no edit)
            if isinstance(item, dict) and "title" in item:
                actions.append(CodeAction.from_dict(item))
        
        return actions

    @property
    def is_initialized(self) -> bool:
        """Check if the LSP connection is initialized."""
        return self._initialized

    @property
    def root_uri(self) -> str | None:
        """Get the current project root URI."""
        return self._root_uri

