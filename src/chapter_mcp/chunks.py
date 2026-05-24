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
    ".css",
    ".csv",
    ".html",
    ".js",
    ".json",
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


def parse_file(path: Path, text: str) -> list[Chunk]:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return parse_markdown(text)
    if suffix == ".py":
        return parse_python(text)
    return parse_paragraphs(text)


def parse_markdown(text: str) -> list[Chunk]:
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


def parse_paragraphs(text: str) -> list[Chunk]:
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


def is_probably_text(path: Path, sample: bytes) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
