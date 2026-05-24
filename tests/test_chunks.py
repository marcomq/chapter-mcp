from chapter_mcp.chunks import parse_markdown, parse_paragraphs, parse_python


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
