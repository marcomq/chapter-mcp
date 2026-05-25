# AGENTS.md

## Using chapter-mcp with Serena, rg, and sed

- Use `chapter-mcp` first for indexed, relevance-ranked project context such as docs, instructions, README files, ADRs, Markdown/TXT, and chapterized source sections.
- Use Serena for symbol-aware work: definitions, references, implementations, diagnostics, and safe symbol edits.
- Use `rtk rg` for exact literals, identifiers, routes, config keys, error messages, regex searches, non-indexed files, and raw verification.
- Use `read_chapter(..., content_limit=...)` for indexed section reads; use focused `rtk sed -n '<start>,<end>p' <file>` only when a known raw line range is needed.
- Do not rely on `chapter-mcp` for exact code matching with special characters. FTS5 tokenization can change how code and formal-language strings are represented.
- Avoid broad or repeated `sed` reads. Narrow with `chapter-mcp`, Serena, or `rtk rg` first.

Typical flow:
1. Find the relevant section or function name with `chapter-mcp`.
2. Switch to Serena if symbols or references matter.
3. Read the smallest needed section with `read_chapter(..., content_limit=...)` or focused `rtk sed`.
4. Use `list_chapters_as_columns(fields=['name'])` when only names are needed.
5. Use `rtk rg` for exact verification.
