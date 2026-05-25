# AGENTS.md

This is just a sample on how to use this MCP instead of "rg" or "rtk rg":

## Discovery

- Prefer `chapter-mcp` before `rg` or `rtk rg`.
- Use `list_chapters_as_columns(fields=['name'])` when only names are needed.
- Use `list_chapters()` when grouped file context plus line ranges are useful for the next step.
- Fall back to `rtk rg` or `rg` only for non-indexed targets, raw text matching, or syntax/details not exposed by chapter metadata.

## Follow-up reads

- Read the smallest live range needed.
- Prefer `read_chapter` when chapter boundaries are enough.
- Use focused `sed -n '<start>,<end>p' <file>` only when exact surrounding live lines are needed.
