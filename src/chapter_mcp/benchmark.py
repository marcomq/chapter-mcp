from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any


DEFAULT_CHARS_PER_TOKEN = 4.0
DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
RAW_READ_TOOLS = {"sed", "cat", "head", "tail"}
EXACT_SEARCH_TOOLS = {"rg"}
_SED_CMD_RE = re.compile(r"""(?:^|\s)sed\s+-n\s+['"]?(?P<start>\d+),(?P<end>\d+)p['"]?\s+(?P<path>\S+)""")
_HEAD_CMD_RE = re.compile(r"""(?:^|\s)head(?:\s+-n\s+(?P<count>\d+))?\s+(?P<path>\S+)""")
_TAIL_CMD_RE = re.compile(r"""(?:^|\s)tail(?:\s+-n\s+(?P<count>\d+))?\s+(?P<path>\S+)""")
_CAT_CMD_RE = re.compile(r"""(?:^|\s)cat\s+(?P<path>\S+)""")


def estimate_tokens(chars_out: int, *, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """Estimate output tokens using a simple chars/token heuristic."""
    if chars_out <= 0:
        return 0
    return math.ceil(chars_out / chars_per_token)


def load_records(path: Path) -> list[dict[str, Any]]:
    """Load neutral JSONL records from disk."""
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL in {path} on line {line_number}: {exc}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"invalid JSONL in {path} on line {line_number}: expected an object")
            if "tool" not in raw or "chars_out" not in raw:
                raise ValueError(f"invalid JSONL in {path} on line {line_number}: expected tool and chars_out")
            if raw["chars_out"] is None:
                raise ValueError(f"invalid JSONL in {path} on line {line_number}: expected tool and chars_out")
            raw["tool"] = str(raw["tool"])
            for key in ("chars_out", "bytes_out", "lines_out"):
                if key not in raw or raw[key] is None:
                    continue
                value = int(raw[key])
                if value < 0:
                    raise ValueError(
                        f"invalid JSONL in {path} on line {line_number}: {key} must be non-negative, got {value}"
                    )
                raw[key] = value
            records.append(raw)
    return records


def summarize_records(
    records: Sequence[dict[str, Any]],
    *,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    top_n: int = 5,
) -> dict[str, Any]:
    """Summarize observed tool-output traffic for one run."""
    by_tool: dict[str, dict[str, int]] = {}
    largest_raw_reads: list[dict[str, Any]] = []

    total_chars = 0
    total_bytes = 0
    total_lines = 0
    total_tokens = 0
    chapter_mcp_candidate_calls = 0
    chapter_mcp_candidate_chars = 0
    chapter_mcp_candidate_bytes = 0
    chapter_mcp_candidate_lines = 0
    chapter_mcp_candidate_tokens = 0
    exact_search_calls = 0
    exact_search_tokens = 0
    repeated_groups: dict[tuple[str, str | None, str | None, str | None], list[dict[str, Any]]] = defaultdict(list)

    for record in records:
        tool = str(record["tool"])
        chars_out = int(record["chars_out"])
        bytes_out = int(record.get("bytes_out", chars_out))
        lines_out = int(record.get("lines_out", 0))
        path, range_label = _path_and_range(record)
        tokens = estimate_tokens(chars_out, chars_per_token=chars_per_token)

        tool_summary = by_tool.setdefault(
            tool,
            {"calls": 0, "chars": 0, "bytes": 0, "lines": 0, "estimated_tokens": 0},
        )
        tool_summary["calls"] += 1
        tool_summary["chars"] += chars_out
        tool_summary["bytes"] += bytes_out
        tool_summary["lines"] += lines_out
        tool_summary["estimated_tokens"] += tokens

        total_chars += chars_out
        total_bytes += bytes_out
        total_lines += lines_out
        total_tokens += tokens
        if tool in RAW_READ_TOOLS:
            chapter_mcp_candidate_calls += 1
            chapter_mcp_candidate_chars += chars_out
            chapter_mcp_candidate_bytes += bytes_out
            chapter_mcp_candidate_lines += lines_out
            chapter_mcp_candidate_tokens += tokens
        if tool in EXACT_SEARCH_TOOLS:
            exact_search_calls += 1
            exact_search_tokens += tokens

        if tool in RAW_READ_TOOLS:
            largest_raw_reads.append(
                {
                    "tool": tool,
                    "chars_out": chars_out,
                    "bytes_out": bytes_out,
                    "lines_out": lines_out,
                    "estimated_tokens": tokens,
                    "path": path,
                    "range": range_label,
                    "cmd": record.get("cmd"),
                    "timestamp": record.get("timestamp"),
                }
            )
            if path is not None or record.get("cmd") is not None:
                repeated_groups[(tool, path, range_label, _string_or_none(record.get("cmd")))].append(record)

    largest_raw_reads.sort(key=lambda item: (-item["chars_out"], -item["lines_out"], item["tool"]))

    repeated_reads: list[dict[str, Any]] = []
    for (tool, path, range_label, cmd), grouped_records in repeated_groups.items():
        if len(grouped_records) < 2:
            continue
        repeated_reads.append(
            {
                "tool": tool,
                "path": path,
                "range": range_label,
                "cmd": cmd,
                "count": len(grouped_records),
                "chars_out": sum(int(record["chars_out"]) for record in grouped_records),
                "bytes_out": sum(int(record.get("bytes_out", record["chars_out"])) for record in grouped_records),
                "lines_out": sum(int(record.get("lines_out", 0)) for record in grouped_records),
                "estimated_tokens": sum(
                    estimate_tokens(int(record["chars_out"]), chars_per_token=chars_per_token)
                    for record in grouped_records
                ),
            }
        )
    repeated_reads.sort(key=lambda item: (-item["count"], -item["chars_out"], item["tool"]))

    chapter_mcp_candidate_percent = 0.0 if total_tokens == 0 else round((chapter_mcp_candidate_tokens / total_tokens) * 100, 2)

    return {
        "summary": {
            "calls": len(records),
            "chars": total_chars,
            "bytes": total_bytes,
            "lines": total_lines,
            "estimated_tokens": total_tokens,
            "chars_per_token": chars_per_token,
            "chapter_mcp_candidate_calls": chapter_mcp_candidate_calls,
            "chapter_mcp_candidate_chars": chapter_mcp_candidate_chars,
            "chapter_mcp_candidate_bytes": chapter_mcp_candidate_bytes,
            "chapter_mcp_candidate_lines": chapter_mcp_candidate_lines,
            "chapter_mcp_candidate_estimated_tokens": chapter_mcp_candidate_tokens,
            "chapter_mcp_candidate_percent": chapter_mcp_candidate_percent,
            "exact_search_calls": exact_search_calls,
            "exact_search_estimated_tokens": exact_search_tokens,
        },
        "by_tool": by_tool,
        "largest_raw_reads": largest_raw_reads[:top_n],
        "repeated_reads": repeated_reads[:top_n],
    }


def summarize_log(path: Path, *, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN, top_n: int = 5) -> dict[str, Any]:
    """Load and summarize a single observed run log."""
    result = summarize_records(load_records(path), chars_per_token=chars_per_token, top_n=top_n)
    result["log_path"] = path.as_posix()
    return result


def compare_logs(
    baseline_path: Path,
    chapter_first_path: Path,
    *,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    top_n: int = 5,
) -> dict[str, Any]:
    """Compare two observed runs using the same token estimator."""
    baseline = summarize_log(baseline_path, chars_per_token=chars_per_token, top_n=top_n)
    chapter_first = summarize_log(chapter_first_path, chars_per_token=chars_per_token, top_n=top_n)

    baseline_tokens = baseline["summary"]["estimated_tokens"]
    chapter_tokens = chapter_first["summary"]["estimated_tokens"]
    baseline_chars = baseline["summary"]["chars"]
    chapter_chars = chapter_first["summary"]["chars"]
    baseline_bytes = baseline["summary"]["bytes"]
    chapter_bytes = chapter_first["summary"]["bytes"]
    baseline_lines = baseline["summary"]["lines"]
    chapter_lines = chapter_first["summary"]["lines"]
    baseline_candidate_tokens = baseline["summary"]["chapter_mcp_candidate_estimated_tokens"]
    chapter_candidate_tokens = chapter_first["summary"]["chapter_mcp_candidate_estimated_tokens"]

    token_delta = baseline_tokens - chapter_tokens
    reduction_percent = 0.0 if baseline_tokens == 0 else round((token_delta / baseline_tokens) * 100, 2)

    return {
        "summary": {
            "baseline_log": baseline_path.as_posix(),
            "chapter_first_log": chapter_first_path.as_posix(),
            "baseline_estimated_tokens": baseline_tokens,
            "chapter_first_estimated_tokens": chapter_tokens,
            "estimated_output_token_delta": token_delta,
            "estimated_reduction_percent": reduction_percent,
            "baseline_chars": baseline_chars,
            "chapter_first_chars": chapter_chars,
            "char_delta": baseline_chars - chapter_chars,
            "baseline_bytes": baseline_bytes,
            "chapter_first_bytes": chapter_bytes,
            "byte_delta": baseline_bytes - chapter_bytes,
            "baseline_lines": baseline_lines,
            "chapter_first_lines": chapter_lines,
            "line_delta": baseline_lines - chapter_lines,
            "chars_per_token": chars_per_token,
            "baseline_chapter_mcp_candidate_estimated_tokens": baseline_candidate_tokens,
            "chapter_first_chapter_mcp_candidate_estimated_tokens": chapter_candidate_tokens,
            "chapter_first_additional_room_estimated_tokens": chapter_candidate_tokens,
        },
        "baseline": baseline,
        "chapter_first": chapter_first,
    }


def format_summary_report(result: dict[str, Any]) -> str:
    """Render a compact human-readable summary for one observed run."""
    summary = result["summary"]
    lines = [
        f"log: {result['log_path']}",
        (
            f"totals: calls={summary['calls']} chars={summary['chars']} bytes={summary['bytes']} "
            f"lines={summary['lines']} est_tokens={summary['estimated_tokens']}"
        ),
        (
            f"chapter-mcp candidate raw reads: calls={summary['chapter_mcp_candidate_calls']} "
            f"est_tokens={summary['chapter_mcp_candidate_estimated_tokens']} "
            f"({summary['chapter_mcp_candidate_percent']}% of observed output)"
        ),
    ]
    if summary["exact_search_calls"] > 0:
        lines.append(
            f"exact-search traffic kept separate: calls={summary['exact_search_calls']} "
            f"est_tokens={summary['exact_search_estimated_tokens']}"
        )
    return "\n".join(lines)


def format_compare_report(result: dict[str, Any]) -> str:
    """Render a compact human-readable comparison between two observed runs."""
    summary = result["summary"]
    lines = [
        f"baseline: {summary['baseline_log']}",
        f"chapter-first: {summary['chapter_first_log']}",
        (
            f"estimated output tokens: baseline={summary['baseline_estimated_tokens']} "
            f"chapter_first={summary['chapter_first_estimated_tokens']} "
            f"delta={summary['estimated_output_token_delta']} "
            f"reduction={summary['estimated_reduction_percent']}%"
        ),
        (
            f"chars: baseline={summary['baseline_chars']} chapter_first={summary['chapter_first_chars']} "
            f"delta={summary['char_delta']}"
        ),
        (
            f"chapter-mcp candidate raw-read budget: baseline={summary['baseline_chapter_mcp_candidate_estimated_tokens']} "
            f"chapter_first={summary['chapter_first_chapter_mcp_candidate_estimated_tokens']}"
        ),
        (
            f"additional chapter-first room estimate: "
            f"{summary['chapter_first_additional_room_estimated_tokens']} tokens"
        ),
    ]
    return "\n".join(lines)


def extract_codex_session_to_jsonl(
    session_log_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Extract observed tool-call outputs from a Codex session log into neutral JSONL records."""
    records = extract_codex_session_records(session_log_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + ("\n" if records else ""),
        encoding="utf-8",
    )
    return {
        "session_log": session_log_path.as_posix(),
        "output_path": output_path.as_posix(),
        "records_written": len(records),
    }


def extract_codex_session_records(session_log_path: Path) -> list[dict[str, Any]]:
    """Extract neutral benchmark records from a Codex session JSONL file."""
    call_metadata: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []

    with session_log_path.open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid Codex session JSONL in {session_log_path} on line {line_number}: {exc}") from exc
            if not isinstance(entry, dict):
                raise ValueError(f"invalid Codex session JSONL in {session_log_path} on line {line_number}: expected an object")

            entry_type = entry.get("type")
            payload = entry.get("payload")
            if entry_type != "response_item" or not isinstance(payload, dict):
                continue

            payload_type = payload.get("type")
            if payload_type == "function_call":
                call_id = _string_or_none(payload.get("call_id"))
                if call_id is None:
                    continue
                call_metadata[call_id] = {
                    "timestamp": entry.get("timestamp"),
                    "name": payload.get("name"),
                    "namespace": payload.get("namespace"),
                    "arguments": _load_json_object(payload.get("arguments")),
                }
                continue

            if payload_type != "function_call_output":
                continue
            call_id = _string_or_none(payload.get("call_id"))
            output = payload.get("output")
            if call_id is None or not isinstance(output, str):
                continue
            records.append(_codex_output_record(entry.get("timestamp"), output, call_metadata.get(call_id)))

    return records


def latest_codex_session_log(sessions_dir: Path = DEFAULT_CODEX_SESSIONS_DIR) -> Path:
    """Return the most recently modified Codex session JSONL file."""
    candidates = sorted(sessions_dir.glob("**/*.jsonl"), key=lambda path: path.stat().st_mtime_ns)
    if not candidates:
        raise ValueError(f"no Codex session logs found under {sessions_dir}")
    return candidates[-1]


def _path_and_range(record: dict[str, Any]) -> tuple[str | None, str | None]:
    path = _string_or_none(record.get("path"))
    range_label = _string_or_none(record.get("range"))
    if path is not None or range_label is not None:
        return path, range_label
    cmd = _string_or_none(record.get("cmd"))
    if cmd is None:
        return None, None
    match = _SED_CMD_RE.search(cmd)
    if match:
        return match.group("path"), f"{match.group('start')}-{match.group('end')}"
    match = _HEAD_CMD_RE.search(cmd)
    if match:
        count = int(match.group("count") or "10")
        return match.group("path"), f"1-{count}"
    match = _TAIL_CMD_RE.search(cmd)
    if match:
        count = int(match.group("count") or "10")
        return match.group("path"), f"tail:{count}"
    match = _CAT_CMD_RE.search(cmd)
    if match:
        return match.group("path"), "full"
    return None, None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _load_json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _codex_output_record(
    timestamp: Any,
    output: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata = metadata or {}
    name = _string_or_none(metadata.get("name")) or "unknown"
    namespace = _string_or_none(metadata.get("namespace"))
    arguments = metadata.get("arguments") if isinstance(metadata.get("arguments"), dict) else {}
    cmd = _string_or_none(arguments.get("cmd"))
    normalized_tool = _normalize_codex_tool(name=name, namespace=namespace, cmd=cmd)

    record: dict[str, Any] = {
        "tool": normalized_tool,
        "chars_out": len(output),
        "bytes_out": len(output.encode("utf-8")),
        "lines_out": len(output.splitlines()),
    }
    if timestamp is not None:
        record["timestamp"] = str(timestamp)
    if name != "unknown":
        record["call_name"] = name
    if namespace is not None:
        record["namespace"] = namespace
    if cmd is not None:
        record["cmd"] = cmd
    path, range_label = _path_and_range({"cmd": cmd} if cmd is not None else {})
    if path is not None:
        record["path"] = path
    if range_label is not None:
        record["range"] = range_label
    return record


def _normalize_codex_tool(*, name: str, namespace: str | None, cmd: str | None) -> str:
    if name == "exec_command":
        return _shell_tool_from_command(cmd) or "exec_command"
    if name == "write_stdin":
        return "write_stdin"
    if namespace is None:
        return name
    if namespace.startswith("mcp__"):
        server = namespace.removeprefix("mcp__").strip("_").replace("__", "-")
        if server:
            return server
    return f"{namespace}.{name}"


def _shell_tool_from_command(cmd: str | None) -> str | None:
    if not cmd:
        return None
    tokens = cmd.strip().split()
    if not tokens:
        return None
    if tokens[0] == "rtk" and len(tokens) > 1:
        return tokens[1]
    return tokens[0]
