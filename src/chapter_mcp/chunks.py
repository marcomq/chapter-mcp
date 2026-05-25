from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Chunk:
    chunk_type: str
    name: str
    content: str
    start_line: int
    end_line: int


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
TEXT_EXTENSIONS = {
    ".js",
    ".jsx",
    ".md",
    ".py",
    ".rs",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
PYTHON_NODE_LABELS: dict[type[ast.AST], str] = {
    ast.AsyncFunctionDef: "async function",
    ast.ClassDef: "class",
    ast.FunctionDef: "function",
    ast.Module: "module",
}
JS_DECLARATION_RE = re.compile(
    r"^(?:export\s+)?(?:(default)\s+)?(?:(async)\s+)?"
    r"(function|class|interface|type)\s+([A-Za-z_$][\w$]*)\b"
)
JS_CONST_RE = re.compile(r"^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=")
RUST_DECLARATION_RE = re.compile(
    r"^(?:pub(?:\([^)]*\))?\s+)?(?:(async)\s+)?(fn|struct|enum|trait)\s+([A-Za-z_][\w]*)\b"
)
RUST_IMPL_RE = re.compile(r"^(?:pub\s+)?impl(?:\s*<[^>]+>)?\s+([A-Za-z_][\w:]*)")
YAML_TOP_LEVEL_KEY_RE = re.compile(r"^[^ \t#][^:]*:")
TOML_SECTION_RE = re.compile(r"^\[\[?([^\]]+)\]?\]$")


def parse_file(path: Path, text: str) -> list[Chunk]:
    """Parse a file into chunks using a structure-aware strategy for known suffixes."""
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return parse_markdown(text)
    if suffix == ".py":
        return parse_python(text)
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return parse_javascript_like(text)
    if suffix == ".rs":
        return parse_rust(text)
    if suffix in {".yaml", ".yml"}:
        return parse_yaml(text)
    if suffix == ".toml":
        return parse_toml(text)
    return parse_paragraphs(text)


def parse_markdown(text: str) -> list[Chunk]:
    """Split Markdown into heading-based sections with hierarchical section names."""
    lines = text.splitlines()
    headings: list[tuple[int, int, str, str]] = []
    hierarchy: list[tuple[int, str]] = []

    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        hierarchy = [(old_level, old_title) for old_level, old_title in hierarchy if old_level < level]
        hierarchy.append((level, title))
        name = " > ".join(item_title for _, item_title in hierarchy)
        headings.append((index, level, title, name))

    if not headings:
        return parse_paragraphs(text)

    chunks: list[Chunk] = []
    if lines[: headings[0][0]]:
        preamble = "\n".join(lines[: headings[0][0]]).strip()
        if preamble:
            chunks.append(
                Chunk(
                    chunk_type="paragraph",
                    name="preamble",
                    content=preamble,
                    start_line=1,
                    end_line=headings[0][0],
                )
            )

    for heading_index, (start, _, title, name) in enumerate(headings):
        end = headings[heading_index + 1][0] if heading_index + 1 < len(headings) else len(lines)
        content = "\n".join(lines[start:end]).strip()
        if content:
            chunks.append(
                Chunk(
                    chunk_type="markdown_section",
                    name=name or title,
                    content=content,
                    start_line=start + 1,
                    end_line=end,
                )
            )
    return chunks


def parse_python(text: str) -> list[Chunk]:
    """Split Python into top-level classes and functions, including their source text."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return parse_paragraphs(text)

    lines = text.splitlines()
    chunks: list[Chunk] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.end_lineno is None:
            continue
        start_line = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        source = "\n".join(lines[start_line - 1 : node.end_lineno]).strip()
        docstring = ast.get_docstring(node)
        label = PYTHON_NODE_LABELS.get(type(node), type(node).__name__.lower())
        name = getattr(node, "name", "<anon>")
        content_parts = [f"{label} {name}"]
        if docstring:
            content_parts.append(docstring)
        content_parts.append(source)
        chunks.append(
            Chunk(
                chunk_type="python_class" if isinstance(node, ast.ClassDef) else "python_function",
                name=node.name,
                content="\n\n".join(content_parts),
                start_line=start_line,
                end_line=node.end_lineno,
            )
        )

    return chunks or parse_paragraphs(text)


def parse_javascript_like(text: str) -> list[Chunk]:
    """Split JS and TS source into top-level declarations such as functions and classes."""
    return _parse_brace_language(text, _match_javascript_declaration)


def parse_rust(text: str) -> list[Chunk]:
    """Split Rust source into top-level functions, types, and impl blocks."""
    return _parse_brace_language(text, _match_rust_declaration)


def parse_yaml(text: str) -> list[Chunk]:
    """Split YAML into top-level key sections."""
    return _parse_top_level_sections(text, YAML_TOP_LEVEL_KEY_RE, chunk_type="yaml_section")


def parse_toml(text: str) -> list[Chunk]:
    """Split TOML into section-based chunks and keep leading assignments as a preamble."""
    lines = text.splitlines()
    headers: list[tuple[int, str]] = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = TOML_SECTION_RE.match(stripped)
        if match:
            headers.append((index, match.group(1).strip()))

    if not headers:
        return parse_paragraphs(text)

    chunks: list[Chunk] = []
    if lines[: headers[0][0]]:
        preamble = "\n".join(lines[: headers[0][0]]).strip()
        if preamble:
            chunks.append(
                Chunk(
                    chunk_type="toml_section",
                    name="preamble",
                    content=preamble,
                    start_line=1,
                    end_line=headers[0][0],
                )
            )

    for header_index, (start, name) in enumerate(headers):
        end = headers[header_index + 1][0] if header_index + 1 < len(headers) else len(lines)
        content = "\n".join(lines[start:end]).strip()
        if content:
            chunks.append(
                Chunk(
                    chunk_type="toml_section",
                    name=name,
                    content=content,
                    start_line=start + 1,
                    end_line=end,
                )
            )
    return chunks


def parse_paragraphs(text: str) -> list[Chunk]:
    """Group consecutive non-blank lines into paragraph chunks."""
    lines = text.splitlines()
    chunks: list[Chunk] = []
    start: int | None = None
    buffer: list[str] = []

    def flush(end_line: int) -> None:
        nonlocal start, buffer
        if start is None:
            return
        content = "\n".join(buffer).rstrip()
        if content:
            number = len(chunks) + 1
            chunks.append(
                Chunk(
                    chunk_type="paragraph",
                    name=f"paragraph-{number}",
                    content=content,
                    start_line=start,
                    end_line=end_line,
                )
            )
        start = None
        buffer = []

    for index, line in enumerate(lines, start=1):
        if line.strip():
            if start is None:
                start = index
            buffer.append(line)
        else:
            flush(index - 1)
    flush(len(lines))
    return chunks

def _parse_brace_language(
    text: str,
    match_declaration: callable,
) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    start_line: int | None = None
    name = ""
    chunk_type = ""
    depth = 0
    opened_scope = False
    is_type_decl = False

    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if start_line is None and depth == 0:
            matched = match_declaration(stripped)
            is_type_decl = False
            js_decl_match = JS_DECLARATION_RE.match(stripped)
            if js_decl_match and js_decl_match.group(3) == "type":
                sanitized_for_decl = _sanitize_brace_line(line)
                if "=" in stripped or ("{" not in sanitized_for_decl and not stripped.endswith(";")):
                    is_type_decl = True

            if matched is not None:
                chunk_type, name = matched
                start_line = index
                opened_scope = False

        sanitized = _sanitize_brace_line(line)
        if start_line is not None:
            if "{" in sanitized:
                opened_scope = True
            depth += sanitized.count("{")
            depth -= sanitized.count("}")

            if (opened_scope and depth == 0) or (
                not opened_scope
                and (stripped.endswith(";") or (is_type_decl and ";" in stripped))
            ):
                source = "\n".join(lines[start_line - 1 : index]).strip()
                chunks.append(
                    Chunk(
                        chunk_type=chunk_type,
                        name=name,
                        content=f"{chunk_type.replace('_', ' ')} {name}\n\n{source}",
                        start_line=start_line,
                        end_line=index,
                    )
                )
                start_line = None
                name = ""
                chunk_type = ""
                is_type_decl = False
        else:
            depth += sanitized.count("{")
            depth -= sanitized.count("}")

    return chunks or parse_paragraphs(text)


def _parse_top_level_sections(text: str, pattern: re.Pattern[str], *, chunk_type: str) -> list[Chunk]:
    lines = text.splitlines()
    headers: list[tuple[int, str]] = []

    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace():
            continue
        if not pattern.match(line):
            continue
        name = line.split(":", 1)[0].strip().strip("'\"")
        headers.append((index, name or f"section-{len(headers) + 1}"))

    if not headers:
        return parse_paragraphs(text)

    chunks: list[Chunk] = []
    for header_index, (start, name) in enumerate(headers):
        end = headers[header_index + 1][0] if header_index + 1 < len(headers) else len(lines)
        content = "\n".join(lines[start:end]).strip()
        if content:
            chunks.append(
                Chunk(
                    chunk_type=chunk_type,
                    name=name,
                    content=content,
                    start_line=start + 1,
                    end_line=end,
                )
            )
    return chunks


def _match_javascript_declaration(line: str) -> tuple[str, str] | None:
    match = JS_DECLARATION_RE.match(line)
    if match:
        kind = match.group(3)
        name = match.group(4)
        if kind == "class":
            return ("javascript_class", name)
        return ("javascript_function", name)
    # sanitize before checking for arrow functions so we don't match arrows inside
    # strings, comments or template literals
    sanitized = _sanitize_brace_line(line)
    const_match = JS_CONST_RE.match(sanitized)
    if const_match and "=>" in sanitized:
        return ("javascript_function", const_match.group(1))
    return None


def _match_rust_declaration(line: str) -> tuple[str, str] | None:
    match = RUST_DECLARATION_RE.match(line)
    if match:
        kind = match.group(2)
        name = match.group(3)
        if kind == "fn":
            return ("rust_function", name)
        return ("rust_type", name)
    impl_match = RUST_IMPL_RE.match(line)
    if impl_match:
        return ("rust_impl", impl_match.group(1))
    return None


def _sanitize_brace_line(line: str) -> str:
    # remove single-line block comments
    line = re.sub(r"/\*.*?\*/", "", line)
    # remove double-quoted and single-quoted string contents
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
    # remove template literal contents on a single line (``)
    line = re.sub(r'`(?:\\.|[^`\\])*`', '``', line)
    # remove interpolation expressions like ${...} (best-effort)
    line = re.sub(r"\$\{(?:\\.|[^}\\])*\}", "", line)
    # strip line comments
    return line.split("//", 1)[0]


def is_probably_text(path: Path, sample: bytes) -> bool:
    """Heuristically decide whether a file sample should be treated as UTF-8 text."""
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
