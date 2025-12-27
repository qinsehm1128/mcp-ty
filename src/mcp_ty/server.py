"""
MCP Server implementation powered by ty type checker.

Provides semantic code analysis tools through the Model Context Protocol.
"""

import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .lsp_client import (
    TyLspClient, Location, Diagnostic, WorkspaceEdit, TextEdit, CodeAction
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create the MCP server
mcp = FastMCP(
    name="ty-context-engine",
    instructions="Semantic Python code analysis powered by ty type checker. "
    "Call start_project first to initialize a project, then use other tools for code analysis."
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
        raise RuntimeError("NOT_INITIALIZED: Call start_project(path) first")
    return _lsp_client


# ----- MCP Tools -----

@mcp.tool()
async def start_project(project_path: str) -> str:
    """
    Initialize ty type checker for a Python project. Must be called first.

    Args:
        project_path: Absolute path to project root directory.

    Returns:
        Status and available tools.
    """
    global _lsp_client

    path = Path(project_path).resolve()
    if not path.exists():
        return f"ERROR: Path not found: {project_path}"
    if not path.is_dir():
        return f"ERROR: Not a directory: {project_path}"

    if _lsp_client is not None:
        try:
            await _lsp_client.stop()
        except Exception:
            pass

    try:
        _lsp_client = TyLspClient()
        await _lsp_client.start(path)
        return f"""OK: Project initialized
path: {path}

Available tools:
- search_symbol(query) : Search symbols by keyword
- list_file_symbols(file) : List symbols in file
- read_code(file, start, end) : Read file content by line range
- read_context(file, line, context) : Read code around a line
- get_definition(file, line, col) : Go to definition
- find_usages(file, line, col) : Find all references
- get_type_info(file, line, col) : Get type information
- analyze_file(file) : Analyze file for errors
- get_diagnostics(file) : Get type errors
- safe_rename(file, line, col, new_name) : Rename symbol
- get_code_actions(file, line, col) : Get quick fixes"""
    except Exception as e:
        _lsp_client = None
        logger.exception("Failed to start ty server")
        return f"ERROR: {e}"


@mcp.tool()
async def search_symbol(query: str) -> str:
    """
    Search symbols (classes, functions, variables) across the project.
    Semantic search - only matches code symbols, not comments/strings.

    Args:
        query: Symbol name or partial name (e.g., "User", "cache", "get_")

    Returns:
        Matching symbols with file:line:col and type.
    """
    client = _get_client()

    try:
        symbols = await client.search_workspace_symbols(query)

        if not symbols:
            return f"NO_RESULTS: '{query}'"

        results = [f"FOUND: {len(symbols)} symbols matching '{query}'", ""]

        by_file: dict[str, list[dict]] = {}
        for sym in symbols:
            uri = sym.get("location", {}).get("uri", "")
            by_file.setdefault(uri, []).append(sym)

        for uri, syms in by_file.items():
            path = _uri_to_path(uri)
            for sym in syms:
                name = sym.get("name", "?")
                kind = SYMBOL_KINDS.get(sym.get("kind", 0), "Symbol")
                range_info = sym.get("location", {}).get(
                    "range", {}).get("start", {})
                line = range_info.get("line", 0) + 1
                col = range_info.get("character", 0) + 1
                container = sym.get("containerName", "")
                container_str = f" in {container}" if container else ""
                results.append(
                    f"{path.name}:{line}:{col} {kind} {name}{container_str}")

        return "\n".join(results)

    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def list_file_symbols(file_path: str) -> str:
    """
    List all symbols defined in a file (classes, functions, variables).

    Args:
        file_path: Absolute path to Python file.

    Returns:
        Symbol structure with line numbers.
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        await client.open_document(path)
        symbols = await client.search_document_symbols(path)

        if not symbols:
            return f"NO_SYMBOLS: {path.name}"

        def format_symbol(sym: dict, indent: int = 0) -> list[str]:
            lines = []
            prefix = "  " * indent
            name = sym.get("name", "?")
            kind = SYMBOL_KINDS.get(sym.get("kind", 0), "Symbol")

            if "range" in sym:
                range_info = sym.get("range", {}).get("start", {})
            else:
                range_info = sym.get("location", {}).get(
                    "range", {}).get("start", {})

            line = range_info.get("line", 0) + 1
            lines.append(f"{prefix}L{line} {kind} {name}")

            for child in sym.get("children", []):
                lines.extend(format_symbol(child, indent + 1))
            return lines

        results = [f"FILE: {path.name}", f"SYMBOLS: {len(symbols)}", ""]
        for sym in symbols:
            results.extend(format_symbol(sym))

        return "\n".join(results)

    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def read_code(
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None
) -> str:
    """
    Read file content, optionally by line range.

    Args:
        file_path: Absolute path to file.
        start_line: Start line (1-based, inclusive). None = from beginning.
        end_line: End line (1-based, inclusive). None = to end.

    Returns:
        File content with line numbers.

    Examples:
        read_code("file.py") - Read entire file
        read_code("file.py", 10, 20) - Read lines 10-20
        read_code("file.py", 50) - Read from line 50 to end
    """
    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        total_lines = len(lines)

        # Default range
        start = (start_line or 1) - 1  # Convert to 0-based
        end = end_line or total_lines

        # Validate range
        if start < 0:
            start = 0
        if end > total_lines:
            end = total_lines
        if start >= total_lines:
            return f"ERROR: start_line {start_line} exceeds file length {total_lines}"

        selected_lines = lines[start:end]

        # Format with line numbers
        result = [f"FILE: {path.name}"]
        result.append(f"LINES: {start + 1}-{end} of {total_lines}")
        result.append("")

        for i, line in enumerate(selected_lines, start=start + 1):
            result.append(f"{i:4d}| {line}")

        return "\n".join(result)

    except UnicodeDecodeError:
        return f"ERROR: Cannot read file (not UTF-8): {file_path}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def read_context(
    file_path: str,
    line: int,
    context: int = 10
) -> str:
    """
    Read code around a specific line (useful after search_symbol or get_definition).

    Args:
        file_path: Absolute path to file.
        line: Target line number (1-based).
        context: Number of lines before and after (default 10).

    Returns:
        Code snippet centered on the target line.
    """
    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        total_lines = len(lines)

        if line < 1 or line > total_lines:
            return f"ERROR: Line {line} out of range (1-{total_lines})"

        start = max(0, line - 1 - context)
        end = min(total_lines, line + context)

        result = [f"FILE: {path.name}"]
        result.append(f"TARGET: L{line}")
        result.append(f"CONTEXT: L{start + 1}-{end} of {total_lines}")
        result.append("")

        for i in range(start, end):
            marker = ">>>" if i == line - 1 else "   "
            result.append(f"{marker} {i + 1:4d}| {lines[i]}")

        return "\n".join(result)

    except UnicodeDecodeError:
        return f"ERROR: Cannot read file (not UTF-8): {file_path}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def stop_project() -> str:
    """Stop ty type checker and release resources."""
    global _lsp_client

    if _lsp_client is None:
        return "OK: No active project"

    try:
        await _lsp_client.stop()
        _lsp_client = None
        return "OK: Project stopped"
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def get_definition(file_path: str, line: int, column: int) -> str:
    """
    Go to definition of symbol at position. Semantic lookup, not text search.

    Args:
        file_path: Absolute path to Python file.
        line: Line number (1-based).
        column: Column number (1-based).

    Returns:
        Definition location(s).
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        await client.open_document(path)
        locations = await client.get_definition(path, line - 1, column - 1)

        if not locations:
            return f"NO_DEFINITION: {path.name}:{line}:{column}"

        results = ["DEFINITION:"]
        for loc in locations:
            results.append(_format_location(loc))

        return "\n".join(results)
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def find_usages(file_path: str, line: int, column: int) -> str:
    """
    Find all references to symbol at position across the project.

    Args:
        file_path: Absolute path to Python file.
        line: Line number (1-based).
        column: Column number (1-based).

    Returns:
        All reference locations.
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        await client.open_document(path)
        locations = await client.find_references(path, line - 1, column - 1)

        if not locations:
            return f"NO_REFERENCES: {path.name}:{line}:{column}"

        by_file: dict[str, list[Location]] = {}
        for loc in locations:
            by_file.setdefault(loc.uri, []).append(loc)

        results = [f"REFERENCES: {len(locations)} in {len(by_file)} files", ""]
        for uri, locs in by_file.items():
            file_name = _uri_to_path(uri).name
            for loc in locs:
                results.append(
                    f"{file_name}:{loc.range.start.line + 1}:{loc.range.start.character + 1}")

        return "\n".join(results)
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def get_type_info(file_path: str, line: int, column: int) -> str:
    """
    Get type information for symbol at position.

    Args:
        file_path: Absolute path to Python file.
        line: Line number (1-based).
        column: Column number (1-based).

    Returns:
        Type information.
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        await client.open_document(path)
        hover_info = await client.get_hover(path, line - 1, column - 1)

        if not hover_info:
            return f"NO_TYPE_INFO: {path.name}:{line}:{column}"

        return f"TYPE_INFO: {path.name}:{line}:{column}\n{hover_info}"
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def get_diagnostics(file_path: str) -> str:
    """
    Get type errors and warnings for a file.

    Args:
        file_path: Absolute path to Python file.

    Returns:
        List of diagnostics.
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        await client.open_document(path)

        import asyncio
        await asyncio.sleep(0.5)

        diagnostics = client.get_diagnostics(path)

        if not diagnostics:
            return f"OK: No errors in {path.name}"

        results = [f"DIAGNOSTICS: {len(diagnostics)} in {path.name}", ""]
        for diag in diagnostics:
            results.append(_format_diagnostic(diag))

        return "\n".join(results)
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def get_completions(file_path: str, line: int, column: int) -> str:
    """
    Get code completion suggestions at position.

    Args:
        file_path: Absolute path to Python file.
        line: Line number (1-based).
        column: Column number (1-based).

    Returns:
        Completion items.
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        await client.open_document(path)
        completions = await client.get_completions(path, line - 1, column - 1)

        if not completions:
            return f"NO_COMPLETIONS: {path.name}:{line}:{column}"

        kind_names = {
            1: "text", 2: "method", 3: "function", 4: "constructor",
            5: "field", 6: "variable", 7: "class", 8: "interface",
            9: "module", 10: "property", 11: "unit", 12: "value",
            13: "enum", 14: "keyword", 15: "snippet", 16: "color",
            17: "file", 18: "reference", 19: "folder", 20: "enum_member",
            21: "constant", 22: "struct", 23: "event", 24: "operator",
            25: "type_parameter"
        }

        results = [f"COMPLETIONS: {len(completions)}", ""]
        for item in completions[:30]:
            label = item.get("label", "?")
            kind = kind_names.get(item.get("kind", 0), "?")
            detail = item.get("detail", "")
            detail_str = f" : {detail}" if detail else ""
            results.append(f"{label} ({kind}){detail_str}")

        if len(completions) > 30:
            results.append(f"... +{len(completions) - 30} more")

        return "\n".join(results)
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def analyze_file(file_path: str) -> str:
    """
    Analyze a Python file: get structure and type errors.

    Args:
        file_path: Absolute path to Python file.

    Returns:
        File info and diagnostics summary.
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

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

        results = [
            f"FILE: {path.name}",
            f"PATH: {path}",
            f"LINES: {line_count}",
            f"ERRORS: {errors}",
            f"WARNINGS: {warnings}",
            f"HINTS: {hints}",
            ""
        ]

        if diagnostics:
            results.append("ISSUES:")
            for diag in diagnostics[:15]:
                results.append(_format_diagnostic(diag))
            if len(diagnostics) > 15:
                results.append(f"... +{len(diagnostics) - 15} more")

        return "\n".join(results)
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def safe_rename(
    file_path: str, line: int, column: int, new_name: str, apply: bool = False
) -> str:
    """
    Rename symbol across project. Semantic rename using type analysis.

    Args:
        file_path: File containing the symbol.
        line: Line number (1-based).
        column: Column number (1-based).
        new_name: New symbol name.
        apply: If True, apply changes. If False, preview only.

    Returns:
        Preview or confirmation of changes.
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        await client.open_document(path)

        workspace_edit = await client.rename_symbol(
            path, line - 1, column - 1, new_name
        )

        if not workspace_edit:
            return f"CANNOT_RENAME: {path.name}:{line}:{column}"

        all_edits = workspace_edit.get_all_edits()
        total_edits = sum(len(e) for e in all_edits.values())

        if not apply:
            preview_lines = _format_workspace_edit(workspace_edit)
            return f"PREVIEW: {total_edits} edits in {len(all_edits)} files\napply=True to confirm\n\n" + "\n".join(preview_lines)

        applied_files = []
        for uri, edits in all_edits.items():
            file_to_edit = _uri_to_path(uri)
            if file_to_edit.exists():
                new_content = _apply_edits_to_file(file_to_edit, edits)
                file_to_edit.write_text(new_content, encoding="utf-8")
                applied_files.append(file_to_edit.name)

        return f"RENAMED: '{new_name}'\nMODIFIED: {', '.join(applied_files)}"

    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def get_code_actions(file_path: str, line: int, column: int) -> str:
    """
    Get available quick fixes and refactorings at position.

    Args:
        file_path: Absolute path to Python file.
        line: Line number (1-based).
        column: Column number (1-based).

    Returns:
        List of available actions with indices.
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

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
            return f"NO_ACTIONS: {path.name}:{line}:{column}"

        results = [f"ACTIONS: {len(actions)}", ""]
        for i, action in enumerate(actions, 1):
            kind = f" [{action.kind}]" if action.kind else ""
            has_edit = " (editable)" if action.edit else ""
            results.append(f"{i}. {action.title}{kind}{has_edit}")

        results.append("")
        results.append(
            "Use apply_code_action(file, line, col, index) to apply")

        return "\n".join(results)

    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def apply_code_action(
    file_path: str, line: int, column: int, action_index: int
) -> str:
    """
    Apply a code action by index (from get_code_actions output).

    Args:
        file_path: Absolute path to Python file.
        line: Line number (1-based).
        column: Column number (1-based).
        action_index: 1-based index of action to apply.

    Returns:
        Confirmation of applied changes.
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

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
            return f"NO_ACTIONS: {path.name}:{line}:{column}"

        if action_index < 1 or action_index > len(actions):
            return f"INVALID_INDEX: Choose 1-{len(actions)}"

        action = actions[action_index - 1]

        if not action.edit:
            return f"NO_EDIT: Action '{action.title}' has no edits"

        all_edits = action.edit.get_all_edits()
        applied_files = []

        for uri, edits in all_edits.items():
            file_to_edit = _uri_to_path(uri)
            if file_to_edit.exists():
                new_content = _apply_edits_to_file(file_to_edit, edits)
                file_to_edit.write_text(new_content, encoding="utf-8")
                applied_files.append(file_to_edit.name)

        return f"APPLIED: {action.title}\nMODIFIED: {', '.join(applied_files) if applied_files else 'none'}"

    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
async def get_edit_preview(
    file_path: str, line: int, column: int, action_index: int
) -> str:
    """
    Preview changes a code action would make.

    Args:
        file_path: Absolute path to Python file.
        line: Line number (1-based).
        column: Column number (1-based).
        action_index: 1-based index of action to preview.

    Returns:
        Preview of changes.
    """
    client = _get_client()

    path = Path(file_path).resolve()
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

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
            return f"NO_ACTIONS: {path.name}:{line}:{column}"

        if action_index < 1 or action_index > len(actions):
            return f"INVALID_INDEX: Choose 1-{len(actions)}"

        action = actions[action_index - 1]

        if not action.edit:
            return f"NO_PREVIEW: Action '{action.title}' has no edits"

        preview_lines = _format_workspace_edit(action.edit)
        all_edits = action.edit.get_all_edits()
        total_edits = sum(len(e) for e in all_edits.values())

        return f"PREVIEW: {action.title}\nEDITS: {total_edits} in {len(all_edits)} files\n\n" + "\n".join(preview_lines)

    except Exception as e:
        return f"ERROR: {e}"


def run_server():
    """Run the MCP server (stdio mode)."""
    mcp.run()


if __name__ == "__main__":
    run_server()
