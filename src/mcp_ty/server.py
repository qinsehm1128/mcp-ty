"""
MCP Server powered by ty type checker for semantic Python code analysis.
"""

import json
import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .lsp_client import (
    TyLspClient, Location, Diagnostic, WorkspaceEdit, TextEdit, CodeAction
)


def _ok(data: Any = None) -> str:
    """Return success JSON response."""
    return json.dumps({"status": "ok", "data": data}, ensure_ascii=False)


def _error(message: str) -> str:
    """Return error JSON response."""
    return json.dumps({"status": "error", "message": message}, ensure_ascii=False)


def _not_found(message: str) -> str:
    """Return not found JSON response."""
    return json.dumps({"status": "not_found", "message": message}, ensure_ascii=False)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create the MCP server
mcp = FastMCP(
    name="ty-context-engine",
    instructions="Python semantic analysis via ty. Call start_project first."
)

# Global LSP client instance
_lsp_client: TyLspClient | None = None

# LSP SymbolKind mapping
SYMBOL_KINDS = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package",
    5: "Class", 6: "Method", 7: "Property", 8: "Field",
    9: "Constructor", 10: "Enum", 11: "Interface", 12: "Function",
    13: "Variable", 14: "Constant", 15: "String", 16: "Number",
    17: "Boolean", 18: "Array", 19: "Object", 20: "Key",
    21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
    25: "Operator", 26: "TypeParameter"
}

SEVERITY_MAP = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _uri_to_path(uri: str) -> Path:
    """Convert file:// URI to Path."""
    path = uri
    if path.startswith("file://"):
        path = path[7:]
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
    return Path(path)


def _format_location(loc: Location) -> str:
    """Format a Location as file:line:col."""
    path = _uri_to_path(loc.uri)
    return f"{path}:{loc.range.start.line + 1}:{loc.range.start.character + 1}"


def _format_diagnostic(diag: Diagnostic) -> str:
    """Format a Diagnostic as L:C severity: message."""
    severity = SEVERITY_MAP.get(diag.severity or 1, "error")
    return f"L{diag.range.start.line + 1}:{diag.range.start.character + 1} {severity}: {diag.message}"


def _apply_text_edit(content: str, edit: TextEdit) -> str:
    """Apply a single TextEdit to file content."""
    lines = content.splitlines(keepends=True)
    if not lines:
        lines = [""]

    start = edit.range.start
    end = edit.range.end

    before_lines = lines[:start.line]
    before_text = "".join(before_lines)
    if start.line < len(lines):
        before_text += lines[start.line][:start.character]

    after_text = ""
    if end.line < len(lines):
        after_text = lines[end.line][end.character:]
        after_lines = lines[end.line + 1:]
        after_text += "".join(after_lines)

    return before_text + edit.new_text + after_text


def _apply_edits_to_file(file_path: Path, edits: list[TextEdit]) -> str:
    """Apply multiple edits to a file and return the new content."""
    content = file_path.read_text(encoding="utf-8")
    sorted_edits = sorted(
        edits,
        key=lambda e: (e.range.start.line, e.range.start.character),
        reverse=True
    )
    for edit in sorted_edits:
        content = _apply_text_edit(content, edit)
    return content


def _format_workspace_edit(edit: WorkspaceEdit) -> list[str]:
    """Format WorkspaceEdit as structured lines."""
    all_edits = edit.get_all_edits()
    if not all_edits:
        return ["no_changes"]

    result = []
    for uri, edits in all_edits.items():
        path = _uri_to_path(uri)
        for e in edits:
            preview = e.new_text[:80].replace(
                "\n", "\\n") if e.new_text else "(delete)"
            result.append(
                f"{path.name}:L{e.range.start.line + 1}:{e.range.start.character + 1} -> {preview}")
    return result


def _get_client() -> TyLspClient:
    """Get the active LSP client or raise an error."""
    if _lsp_client is None or not _lsp_client.is_initialized:
        raise RuntimeError("Project not initialized. Call start_project first.")
    return _lsp_client


# ----- MCP Tools -----

@mcp.tool()
async def start_project(project_path: str) -> str:
    """Initialize ty for a Python project. Must be called first."""
    global _lsp_client

    path = Path(project_path).resolve()
    if not path.exists():
        return _error(f"Path not found: {project_path}")
    if not path.is_dir():
        return _error(f"Not a directory: {project_path}")

    if _lsp_client is not None:
        try:
            await _lsp_client.stop()
        except Exception:
            pass

    try:
        _lsp_client = TyLspClient()
        await _lsp_client.start(path)
        return _ok({"path": str(path), "initialized": True})
    except Exception as e:
        _lsp_client = None
        logger.exception("Failed to start ty server")
        return _error(str(e))


@mcp.tool()
async def search_symbol(query: str) -> str:
    """Search symbols (classes, functions, variables) across the project."""
    client = _get_client()

    try:
        symbols = await client.search_workspace_symbols(query)

        if not symbols:
            return _not_found(f"No symbols matching '{query}'")

        results = []
        for sym in symbols:
            uri = sym.get("location", {}).get("uri", "")
            path = _uri_to_path(uri)
            name = sym.get("name", "?")
            kind = SYMBOL_KINDS.get(sym.get("kind", 0), "Symbol")
            range_info = sym.get("location", {}).get("range", {}).get("start", {})
            line = range_info.get("line", 0) + 1
            col = range_info.get("character", 0) + 1
            container = sym.get("containerName", "")
            results.append({
                "name": name,
                "kind": kind,
                "file": path.name,
                "path": str(path),
                "line": line,
                "column": col,
                "container": container or None
            })

        return _ok({"query": query, "count": len(results), "symbols": results})

    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def list_file_symbols(file_path: str) -> str:
    """List all symbols defined in a file."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        await client.open_document(path)
        symbols = await client.search_document_symbols(path)

        if not symbols:
            return _not_found(f"No symbols in {path.name}")

        def parse_symbol(sym: dict) -> dict:
            name = sym.get("name", "?")
            kind = SYMBOL_KINDS.get(sym.get("kind", 0), "Symbol")

            if "range" in sym:
                range_info = sym.get("range", {}).get("start", {})
            else:
                range_info = sym.get("location", {}).get("range", {}).get("start", {})

            line = range_info.get("line", 0) + 1
            children = [parse_symbol(c) for c in sym.get("children", [])]

            result = {"name": name, "kind": kind, "line": line}
            if children:
                result["children"] = children
            return result

        parsed = [parse_symbol(sym) for sym in symbols]
        return _ok({"file": path.name, "path": str(path), "count": len(symbols), "symbols": parsed})

    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def read_code(
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None
) -> str:
    """Read file content, optionally by line range (1-based)."""
    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        total_lines = len(lines)

        start = (start_line or 1) - 1
        end = end_line or total_lines

        if start < 0:
            start = 0
        if end > total_lines:
            end = total_lines
        if start >= total_lines:
            return _error(f"start_line {start_line} exceeds file length {total_lines}")

        selected = {i + 1: lines[i] for i in range(start, end)}

        return _ok({
            "file": path.name,
            "path": str(path),
            "range": {"start": start + 1, "end": end},
            "total_lines": total_lines,
            "lines": selected
        })

    except UnicodeDecodeError:
        return _error(f"Cannot read file (not UTF-8): {file_path}")
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def read_context(
    file_path: str,
    line: int,
    context: int = 10
) -> str:
    """Read code around a specific line with context."""
    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        total_lines = len(lines)

        if line < 1 or line > total_lines:
            return _error(f"Line {line} out of range (1-{total_lines})")

        start = max(0, line - 1 - context)
        end = min(total_lines, line + context)

        selected = {i + 1: lines[i] for i in range(start, end)}

        return _ok({
            "file": path.name,
            "path": str(path),
            "target_line": line,
            "range": {"start": start + 1, "end": end},
            "total_lines": total_lines,
            "lines": selected
        })

    except UnicodeDecodeError:
        return _error(f"Cannot read file (not UTF-8): {file_path}")
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def stop_project() -> str:
    """Stop ty and release resources."""
    global _lsp_client

    if _lsp_client is None:
        return _ok({"stopped": True, "message": "No active project"})

    try:
        await _lsp_client.stop()
        _lsp_client = None
        return _ok({"stopped": True})
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def get_definition(file_path: str, line: int, column: int) -> str:
    """Go to definition of symbol at position (1-based)."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        await client.open_document(path)
        locations = await client.get_definition(path, line - 1, column - 1)

        if not locations:
            return _not_found(f"No definition at {path.name}:{line}:{column}")

        defs = []
        for loc in locations:
            def_path = _uri_to_path(loc.uri)
            defs.append({
                "file": def_path.name,
                "path": str(def_path),
                "line": loc.range.start.line + 1,
                "column": loc.range.start.character + 1
            })

        return _ok({"definitions": defs})
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def find_usages(file_path: str, line: int, column: int) -> str:
    """Find all references to symbol at position (1-based)."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        await client.open_document(path)
        locations = await client.find_references(path, line - 1, column - 1)

        if not locations:
            return _not_found(f"No references at {path.name}:{line}:{column}")

        refs = []
        for loc in locations:
            ref_path = _uri_to_path(loc.uri)
            refs.append({
                "file": ref_path.name,
                "path": str(ref_path),
                "line": loc.range.start.line + 1,
                "column": loc.range.start.character + 1
            })

        files = list(set(r["file"] for r in refs))
        return _ok({"count": len(refs), "files_count": len(files), "references": refs})
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def get_type_info(file_path: str, line: int, column: int) -> str:
    """Get type information for symbol at position (1-based)."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        await client.open_document(path)
        hover_info = await client.get_hover(path, line - 1, column - 1)

        if not hover_info:
            return _not_found(f"No type info at {path.name}:{line}:{column}")

        return _ok({
            "file": path.name,
            "line": line,
            "column": column,
            "type_info": hover_info
        })
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def get_diagnostics(file_path: str) -> str:
    """Get type errors and warnings for a file."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        await client.open_document(path)

        import asyncio
        await asyncio.sleep(0.5)

        diagnostics = client.get_diagnostics(path)

        if not diagnostics:
            return _ok({"file": path.name, "count": 0, "diagnostics": []})

        diag_list = []
        for diag in diagnostics:
            severity = SEVERITY_MAP.get(diag.severity or 1, "error")
            diag_list.append({
                "line": diag.range.start.line + 1,
                "column": diag.range.start.character + 1,
                "severity": severity,
                "message": diag.message
            })

        return _ok({"file": path.name, "count": len(diag_list), "diagnostics": diag_list})
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def get_completions(file_path: str, line: int, column: int) -> str:
    """Get code completion suggestions at position (1-based)."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        await client.open_document(path)
        completions = await client.get_completions(path, line - 1, column - 1)

        if not completions:
            return _not_found(f"No completions at {path.name}:{line}:{column}")

        kind_names = {
            1: "text", 2: "method", 3: "function", 4: "constructor",
            5: "field", 6: "variable", 7: "class", 8: "interface",
            9: "module", 10: "property", 11: "unit", 12: "value",
            13: "enum", 14: "keyword", 15: "snippet", 16: "color",
            17: "file", 18: "reference", 19: "folder", 20: "enum_member",
            21: "constant", 22: "struct", 23: "event", 24: "operator",
            25: "type_parameter"
        }

        items = []
        for item in completions[:30]:
            label = item.get("label", "?")
            kind = kind_names.get(item.get("kind", 0), "unknown")
            detail = item.get("detail", "")
            items.append({
                "label": label,
                "kind": kind,
                "detail": detail or None
            })

        return _ok({
            "count": len(completions),
            "shown": len(items),
            "completions": items
        })
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def analyze_file(file_path: str) -> str:
    """Analyze a Python file: get structure and diagnostics summary."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        content = path.read_text(encoding="utf-8")
        line_count = len(content.splitlines())

        await client.open_document(path)

        import asyncio
        await asyncio.sleep(0.5)

        diagnostics = client.get_diagnostics(path)

        errors = sum(1 for d in diagnostics if d.severity == 1)
        warnings = sum(1 for d in diagnostics if d.severity == 2)
        hints = len(diagnostics) - errors - warnings

        issues = []
        for diag in diagnostics[:15]:
            severity = SEVERITY_MAP.get(diag.severity or 1, "error")
            issues.append({
                "line": diag.range.start.line + 1,
                "column": diag.range.start.character + 1,
                "severity": severity,
                "message": diag.message
            })

        return _ok({
            "file": path.name,
            "path": str(path),
            "lines": line_count,
            "errors": errors,
            "warnings": warnings,
            "hints": hints,
            "issues": issues,
            "total_issues": len(diagnostics)
        })
    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def safe_rename(
    file_path: str, line: int, column: int, new_name: str, apply: bool = False
) -> str:
    """Rename symbol across project. Set apply=True to execute."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        await client.open_document(path)

        workspace_edit = await client.rename_symbol(
            path, line - 1, column - 1, new_name
        )

        if not workspace_edit:
            return _not_found(f"Cannot rename at {path.name}:{line}:{column}")

        all_edits = workspace_edit.get_all_edits()
        total_edits = sum(len(e) for e in all_edits.values())

        if not apply:
            preview = []
            for uri, edits in all_edits.items():
                edit_path = _uri_to_path(uri)
                for e in edits:
                    text_preview = e.new_text[:80].replace("\n", "\\n") if e.new_text else "(delete)"
                    preview.append({
                        "file": edit_path.name,
                        "line": e.range.start.line + 1,
                        "column": e.range.start.character + 1,
                        "new_text": text_preview
                    })
            return _ok({
                "preview": True,
                "new_name": new_name,
                "edits_count": total_edits,
                "files_count": len(all_edits),
                "edits": preview
            })

        applied_files = []
        for uri, edits in all_edits.items():
            file_to_edit = _uri_to_path(uri)
            if file_to_edit.exists():
                new_content = _apply_edits_to_file(file_to_edit, edits)
                file_to_edit.write_text(new_content, encoding="utf-8")
                applied_files.append(file_to_edit.name)

        return _ok({
            "applied": True,
            "new_name": new_name,
            "modified_files": applied_files
        })

    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def get_code_actions(file_path: str, line: int, column: int) -> str:
    """Get available quick fixes and refactorings at position (1-based)."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        await client.open_document(path)

        import asyncio
        await asyncio.sleep(0.3)

        diagnostics = client.get_diagnostics(path)
        relevant_diags = [
            d for d in diagnostics
            if d.range.start.line <= line - 1 <= d.range.end.line
        ]

        actions = await client.get_code_actions(
            path, line - 1, column - 1, line - 1, column, relevant_diags
        )

        if not actions:
            return _not_found(f"No actions at {path.name}:{line}:{column}")

        action_list = []
        for i, action in enumerate(actions, 1):
            action_list.append({
                "index": i,
                "title": action.title,
                "kind": action.kind or None,
                "has_edit": action.edit is not None
            })

        return _ok({"count": len(actions), "actions": action_list})

    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def apply_code_action(
    file_path: str, line: int, column: int, action_index: int
) -> str:
    """Apply a code action by index (from get_code_actions)."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        await client.open_document(path)

        import asyncio
        await asyncio.sleep(0.3)

        diagnostics = client.get_diagnostics(path)
        relevant_diags = [
            d for d in diagnostics
            if d.range.start.line <= line - 1 <= d.range.end.line
        ]

        actions = await client.get_code_actions(
            path, line - 1, column - 1, line - 1, column, relevant_diags
        )

        if not actions:
            return _not_found(f"No actions at {path.name}:{line}:{column}")

        if action_index < 1 or action_index > len(actions):
            return _error(f"Invalid index. Choose 1-{len(actions)}")

        action = actions[action_index - 1]

        if not action.edit:
            return _error(f"Action '{action.title}' has no edits")

        all_edits = action.edit.get_all_edits()
        applied_files = []

        for uri, edits in all_edits.items():
            file_to_edit = _uri_to_path(uri)
            if file_to_edit.exists():
                new_content = _apply_edits_to_file(file_to_edit, edits)
                file_to_edit.write_text(new_content, encoding="utf-8")
                applied_files.append(file_to_edit.name)

        return _ok({
            "applied": True,
            "action": action.title,
            "modified_files": applied_files
        })

    except Exception as e:
        return _error(str(e))


@mcp.tool()
async def get_edit_preview(
    file_path: str, line: int, column: int, action_index: int
) -> str:
    """Preview changes a code action would make."""
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return _error(f"File not found: {file_path}")

    try:
        await client.open_document(path)

        import asyncio
        await asyncio.sleep(0.3)

        diagnostics = client.get_diagnostics(path)
        relevant_diags = [
            d for d in diagnostics
            if d.range.start.line <= line - 1 <= d.range.end.line
        ]

        actions = await client.get_code_actions(
            path, line - 1, column - 1, line - 1, column, relevant_diags
        )

        if not actions:
            return _not_found(f"No actions at {path.name}:{line}:{column}")

        if action_index < 1 or action_index > len(actions):
            return _error(f"Invalid index. Choose 1-{len(actions)}")

        action = actions[action_index - 1]

        if not action.edit:
            return _error(f"Action '{action.title}' has no edits")

        all_edits = action.edit.get_all_edits()
        total_edits = sum(len(e) for e in all_edits.values())

        preview = []
        for uri, edits in all_edits.items():
            edit_path = _uri_to_path(uri)
            for e in edits:
                text_preview = e.new_text[:80].replace("\n", "\\n") if e.new_text else "(delete)"
                preview.append({
                    "file": edit_path.name,
                    "line": e.range.start.line + 1,
                    "column": e.range.start.character + 1,
                    "new_text": text_preview
                })

        return _ok({
            "action": action.title,
            "edits_count": total_edits,
            "files_count": len(all_edits),
            "edits": preview
        })

    except Exception as e:
        return _error(str(e))


def run_server():
    """Run the MCP server (stdio mode)."""
    mcp.run()


if __name__ == "__main__":
    run_server()
