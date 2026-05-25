# chapter-mcp

`chapter-mcp` gives coding agents a compact way to search a project by useful
sections instead of opening whole files.

It indexes a workspace locally, splits files into "chapters", and exposes MCP
tools for searching, listing, and reading those chapters. The goal is simple:
help an assistant find the right part of a repo before it burns context on raw
file reads.

It is intentionally boring in a good way: local SQLite FTS5, deterministic
full-text search, no embeddings, no vector database, and no external service.

## What it is good for

Use `chapter-mcp` when an assistant needs project context but does not yet know
which file or section matters. Good fits:

- project README sections
- `AGENTS.md`, `CLAUDE.md`, instructions, and local notes
- ADRs and design docs
- Markdown/TXT documentation
- source files where top-level classes or functions make useful chapters
- focused reads after a search result points to the right line range

Not a good fit:

- exact code matching with punctuation, operators, routes, or config keys
- symbol references, definitions, diagnostics, or safe edits
- fuzzy semantic search across very different wording

For those, keep using the sharper tool: `rg` for exact text, Serena or another
language-aware tool for symbols, raw file reads for known ranges, and
vector/RAG tooling for real semantic retrieval.

## How chapters work

`chapter-mcp` turns files into smaller units: Markdown by headings, Python by
top-level classes/functions, and other text files by paragraphs. Search results
point to chapter names and line ranges. Reads can return a whole chapter or a
limited slice of chapter content.

By default, if no explicit config is provided, `chapter-mcp` discovers visible
top-level project entries and indexes them. In a Git repo it uses tracked files,
skips hidden top-level entries and `.chapter-mcp`, respects `.gitignore`, and
applies `.aiignore` rules.

That means the happy path is: add the MCP server to your assistant, start the
assistant in a repo, and let discovery do the first pass.

## Install

`chapter-mcp` is available on PyPI:

```sh
uv pip install chapter-mcp
```

For local development from this checkout:

```sh
uv sync
```

For MCP clients, point at the installed `chapter-mcp` executable. From this
checkout, that is usually:

```text
/absolute/path/to/chapter-mcp/.venv/bin/chapter-mcp
```

## Use Case 1: Local Development Project

For a normal local project, do not start `chapter-mcp` manually in a terminal.
Add it to the MCP config for the assistant you use in that project.

When the assistant starts the server, `chapter-mcp` indexes the project root
automatically unless you override it. No project config is required for the
first run.

### Codex

Add a project-local Codex MCP entry, for example in `.codex/config.toml`:

```toml
[mcp_servers.chapter-mcp]
command = "/absolute/path/to/chapter-mcp/.venv/bin/chapter-mcp"
args = ["--root", "."]
cwd = "."
startup_timeout_sec = 20
required = false
```

Then add instructions in `AGENTS.md` so Codex actually reaches for the tool:

```md
## Project context

Use `chapter-mcp` before broad file reads when looking for project docs,
instructions, README sections, ADRs, or chapterized source sections.

Prefer this flow:
1. Search with `chapter-mcp` to find the relevant section.
2. Read the matching chapter or a small slice of it.
3. Use Serena for symbol definitions, references, diagnostics, and safe edits.
4. For literal code/config queries, pass `exact_code_matches=true` to
   `search` or `read_search`.
5. Use `rg` when you need exhaustive exact matching, absence checks, or
   verification after edits.

Use `chapter-mcp` for discovery and focused reads. Use `rg` as the final source
of truth for repo-wide exact matching.
```

### Claude Code

For Claude Code, add a project `.mcp.json`:

```json
{
  "mcpServers": {
    "chapter-mcp": {
      "command": "/absolute/path/to/chapter-mcp/.venv/bin/chapter-mcp",
      "args": ["--root", "${CLAUDE_PROJECT_DIR:-.}"]
    }
  }
}
```

Or add it through Claude's MCP command:

```sh
claude mcp add-json chapter-mcp '{"command":"/absolute/path/to/chapter-mcp/.venv/bin/chapter-mcp","args":["--root","${CLAUDE_PROJECT_DIR:-.}"]}'
```

Then add instructions in `CLAUDE.md`:

```md
## Project context

Use the `chapter-mcp` MCP server before reading large files. It is best for
finding relevant README sections, docs, local instructions, ADRs, and
chapterized source sections.

Use `chapter-mcp` for discovery and focused chapter reads. Use normal file
reads only after the relevant file or line range is known.

For literal code/config queries, pass `exact_code_matches=true` to `search` or
`read_search`. Use exact search tools such as `rg` for exhaustive repo-wide
matching, absence checks, and verification after edits. Use language-aware tools
for symbols.
```

### Optional project config

Auto-discovery is the default. Add `.chapter-mcp/config.json` only when you want
to pin categories, exclude noisy top-level folders, or index a specific set of
paths:

```json
{
  "paths": [
    "docs=docs",
    "src=src",
    "tests=tests",
    "readme=README.md"
  ]
}
```

Supported config fields are `root`, `db`, `paths`, `watch`, `watch_interval`,
and `sync_startup`. `paths` is required when using config. CLI flags override
project config when both are present.

Useful server flags:

```sh
chapter-mcp --root .
chapter-mcp --root . --path docs --path src
chapter-mcp --root . --path knowledge=.serena/memories
chapter-mcp --root . --no-watch
chapter-mcp --root . --no-sync-startup
chapter-mcp --root . --watch-interval 0.5
```

## Use Case 2: Assistants and Agents

The agent use case is similar, but the emphasis is different: `chapter-mcp`
becomes a context-routing layer. When a task starts vague, the first move can be
a small search over indexed chapters instead of a broad file read.

Recommended flow:

1. Search local instructions and docs with `chapter-mcp`.
2. Read the best chapter with `read_search` or `read_chapter`.
3. Switch to symbol tools if the task becomes code-aware.
4. Use `rg` for exact verification.
5. Use raw reads only after the path and range are clear.

This is especially useful when pairing `chapter-mcp` with tools such as Serena:

- `chapter-mcp` answers "which section should I inspect?"
- Serena answers "which symbol is this, and who references it?"
- `rg` answers "where does this exact text occur?"
- raw reads answer "what is the exact source in this known range?"

### Minimal agent instruction block

If you only want a small instruction, this is enough:

```md
Use `chapter-mcp` before broad file reads for indexed project context:
README sections, docs, instructions, ADRs, Markdown/TXT files, and chapterized
source sections.

Use `read_chapter` with `content_limit` for focused reads. Use `rg` for exact
literals and Serena/language tools for symbols and references. If you want code
or config hits to contain the raw query exactly, pass `exact_code_matches=true`
to `search` or `read_search`.
```

## Compared with file-read-mcp

`file-read-mcp` is useful when the assistant already knows the file or range it
needs. It is direct and close to the filesystem.

`chapter-mcp` is useful one step earlier. It helps the assistant discover the
right section before choosing what to read.

In practice:

- use `chapter-mcp` to search and shortlist relevant sections
- use `read_chapter` to inspect the matching chapter without flooding context
- use `file-read-mcp`, `sed`, or editor reads for exact raw ranges

The distinction is small but important. `file-read-mcp` is about access.
`chapter-mcp` is about choosing what is worth accessing.

## MCP tools

The server exposes `search`, `search_chapter`, `read_search`, `read_chapter`,
`list_chapters`, `list_chapters_as_columns`, `list_files`, `stats`, and
`reindex`.

`search` and `read_search` also accept `exact_code_matches=false`. When set to
`true`, docs keep normal FTS behavior, but code/config chapters must contain the
raw query as an exact substring.

Example calls:

```text
stats()
list_files()
search("config handling", limit=3, include_snippet=True)
search("extract-codex-session", exact_code_matches=True)
search_chapter("Style guide", category="docs", limit=3)
read_search("config handling", category="docs")
read_chapter("Style guide", file="docs/style-guide.md", content_limit=40)
list_chapters_as_columns(fields=["name"])
```

## Benchmark basics

`chapter-mcp` does not measure savings inside the server. The benchmark helper
summarizes observed tool traffic from outside the server so you can compare a
baseline run with a chapter-first run.

For Codex sessions, extract a neutral JSONL log from the latest session:

```sh
uv run chapter-mcp benchmark extract-codex-session --out baseline.jsonl
```

Or pass a specific session log:

```sh
uv run chapter-mcp benchmark extract-codex-session \
  ~/.codex/sessions/2026/05/25/example.jsonl \
  --out chapter-first.jsonl
```

Summarize one run:

```sh
uv run chapter-mcp benchmark summarize baseline.jsonl
```

Compare two runs:

```sh
uv run chapter-mcp benchmark compare baseline.jsonl chapter-first.jsonl
```

The input format is deliberately simple JSONL:

```jsonl
{"tool":"sed","cmd":"rtk sed -n '1,220p' src/foo.py","chars_out":12340,"lines_out":220}
{"tool":"rg","cmd":"rtk rg column src","chars_out":1840,"lines_out":35}
{"tool":"chapter-mcp","call_name":"search","chars_out":2300,"lines_out":40}
```

Required fields are `tool` and `chars_out`. Optional fields include
`bytes_out`, `lines_out`, `cmd`, `path`, `range`, and `timestamp`.

The report is meant as a rough comparison, not a scientific token meter. It is
most useful for spotting repeated broad reads and checking whether
chapter-first workflows reduce raw file output.

## Limits

`chapter-mcp` is full-text search, not semantic search. Literal wording matters.
If users ask fuzzy conceptual questions and the source uses very different
phrasing, use a vector or RAG tool instead.

It is also not an exact source-code matcher. SQLite FTS tokenization is not the
right tool for punctuation-heavy code, operators, or paths. Use `rg` for that.

The intended tradeoff is narrow and practical: fast local chapter lookup that
keeps an assistant oriented before it reaches for heavier tools.

## Development

```sh
uv sync
uv run pytest
```
