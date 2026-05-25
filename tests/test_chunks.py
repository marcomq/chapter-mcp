from pathlib import Path

from chapter_mcp.chunks import parse_file, parse_markdown, parse_paragraphs, parse_python


def test_parse_markdown_sections_preserve_hierarchy() -> None:
    text = "\n".join(
        [
            "# Guide",
            "Intro",
            "## Install",
            "Use uv.",
            "### Verify",
            "Run tests.",
            "## Search",
            "Find chunks.",
        ]
    )

    chunks = parse_markdown(text)

    assert [chunk.name for chunk in chunks] == [
        "Guide",
        "Guide > Install",
        "Guide > Install > Verify",
        "Guide > Search",
    ]
    assert chunks[0].content == "# Guide\nIntro"
    assert chunks[1].content == "## Install\nUse uv."
    assert chunks[1].start_line == 3
    assert chunks[1].end_line == 4


def test_parse_python_functions_classes_and_docstrings() -> None:
    text = "\n".join(
        [
            "import os",
            "",
            "@decorator",
            "def build():",
            '    """Build docs."""',
            "    return os.getcwd()",
            "",
            "async def fetch():",
            "    return 1",
            "",
            "class Runner:",
            '    """Runs tasks."""',
            "    def run(self):",
            "        return None",
        ]
    )

    chunks = parse_python(text)

    assert [chunk.name for chunk in chunks] == ["build", "fetch", "Runner"]
    assert chunks[0].chunk_type == "python_function"
    assert chunks[0].content.startswith("function build")
    assert "Build docs." in chunks[0].content
    assert "@decorator" in chunks[0].content
    assert chunks[1].content.startswith("async function fetch")
    assert chunks[2].chunk_type == "python_class"
    assert chunks[2].content.startswith("class Runner")
    assert chunks[2].start_line == 11
    assert chunks[2].end_line == 14


def test_parse_python_syntax_error_falls_back_to_paragraphs() -> None:
    chunks = parse_python("def broken(:\n    pass\n\nvalid text\n")

    assert [chunk.chunk_type for chunk in chunks] == ["paragraph", "paragraph"]
    assert chunks[0].name == "paragraph-1"


def test_parse_paragraphs_line_ranges() -> None:
    chunks = parse_paragraphs("one\nstill one\n\n two\n\nthree")

    assert [(chunk.content, chunk.start_line, chunk.end_line) for chunk in chunks] == [
        ("one\nstill one", 1, 2),
        (" two", 4, 4),
        ("three", 6, 6),
    ]


def test_parse_javascript_like_uses_top_level_declarations() -> None:
    text = "\n".join(
        [
            "export function outer() {",
            "  function inner() {",
            "    return 1",
            "  }",
            "  return inner()",
            "}",
            "",
            "const helper = () => {",
            "  return 2",
            "}",
            "",
            "class Runner {",
            "  run() {",
            "    return helper()",
            "  }",
            "}",
        ]
    )

    chunks = parse_file(Path("example.ts"), text)

    assert [chunk.name for chunk in chunks] == ["outer", "helper", "Runner"]
    assert [chunk.chunk_type for chunk in chunks] == [
        "javascript_function",
        "javascript_function",
        "javascript_class",
    ]
    assert "function inner()" in chunks[0].content


def test_parse_rust_uses_top_level_declarations() -> None:
    text = "\n".join(
        [
            "pub fn outer() {",
            "    fn inner() {",
            "    }",
            "    inner();",
            "}",
            "",
            "struct Runner {",
            "    ready: bool,",
            "}",
            "",
            "impl Runner {",
            "    fn run(&self) {}",
            "}",
        ]
    )

    chunks = parse_file(Path("lib.rs"), text)

    assert [chunk.name for chunk in chunks] == ["outer", "Runner", "Runner"]
    assert [chunk.chunk_type for chunk in chunks] == [
        "rust_function",
        "rust_type",
        "rust_impl",
    ]
    assert "fn inner()" in chunks[0].content


def test_parse_yaml_uses_top_level_keys() -> None:
    text = "\n".join(
        [
            "root:",
            "  child:",
            "    name: value",
            "second:",
            "  enabled: true",
        ]
    )

    chunks = parse_file(Path("config.yaml"), text)

    assert [chunk.name for chunk in chunks] == ["root", "second"]
    assert [chunk.chunk_type for chunk in chunks] == ["yaml_section", "yaml_section"]
    assert "child:" in chunks[0].content


def test_parse_toml_uses_sections() -> None:
    text = "\n".join(
        [
            'name = "chapter-mcp"',
            "",
            "[project]",
            'version = "0.1.0"',
            "",
            "[[tool.example]]",
            'mode = "demo"',
        ]
    )

    chunks = parse_file(Path("pyproject.toml"), text)

    assert [chunk.name for chunk in chunks] == ["preamble", "project", "tool.example"]
    assert [chunk.chunk_type for chunk in chunks] == ["toml_section", "toml_section", "toml_section"]
