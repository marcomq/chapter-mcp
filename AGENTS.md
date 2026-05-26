# AGENTS.md

## Using chapter-mcp with Serena, rg, and sed

- Use `chapter-mcp` first for indexed, relevance-ranked project context such as docs, instructions, README files, ADRs, Markdown/TXT, and chapterized source sections.
- Use Serena for symbol-aware work: definitions, references, implementations, diagnostics, and safe symbol edits.
- Use `rtk rg` for exact literals, identifiers, routes, config keys, error messages, regex searches, non-indexed files, and raw verification.
- Use `read_chapter(..., content_limit=...)` for indexed section reads. Start with a small block and abort early if the returned chapter or line range is not the one you need.
- Use `read_search(..., content_limit=...)` to open a best search match with bounded content.
- Use `read_chapter_at(file=..., line=..., content_limit=...)` when you know an indexed file and approximate line. This is the indexed alternative to a raw line-range read.
- Use focused `rtk sed -n '<start>,<end>p' <file>` only when a known raw line range is needed. Start with the smallest useful range and expand only if that smaller read is insufficient.
- Do not rely on `chapter-mcp` for exact code matching with special characters. FTS5 tokenization can change how code and formal-language strings are represented.
- Avoid broad or repeated `sed` reads. Narrow with `chapter-mcp`, Serena, or `rtk rg` first.

Tool order for this repo:
1. Use `chapter-mcp` first for README, docs, ADRs, instructions, and chapterized content discovery.
2. Use Serena first for symbol-aware code lookup, references, and edits.
3. Use `rtk rg` for exact literals, regex, absence checks, and verification.
4. Use `rtk sed` only after the file and approximate line range are already known.
5. Do not start README/docs discovery with raw `sed` or broad `rg` when `chapter-mcp` can answer it.

Typical flow:
1. Find the relevant section or function name with `chapter-mcp`.
2. Switch to Serena if symbols or references matter.
3. Read the smallest needed section with `read_search`, `read_chapter`, or `read_chapter_at` plus `content_limit`; verify the chapter/range before reading more.
4. Use `list_chapters_as_columns(fields=['name'])` when only names are needed.
5. Use `rtk rg` for exact verification.
