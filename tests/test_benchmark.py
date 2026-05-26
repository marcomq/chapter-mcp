from __future__ import annotations

import json
from pathlib import Path

import pytest

from chapter_mcp import cli
from chapter_mcp.benchmark import compare_logs, estimate_tokens, extract_codex_session_to_jsonl, load_records, summarize_log


def write_jsonl(path: Path, lines: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")


def test_summarize_log_aggregates_sparse_records_and_detects_repeated_reads(tmp_path: Path) -> None:
    log_path = tmp_path / "baseline.jsonl"
    write_jsonl(
        log_path,
        [
            {"tool": "sed", "cmd": "sed -n '1,20p' src/foo.py", "chars_out": 120, "lines_out": 20},
            {"tool": "sed", "cmd": "sed -n '1,20p' src/foo.py", "chars_out": 80, "lines_out": 20},
            {"tool": "rg", "chars_out": 40},
        ],
    )

    result = summarize_log(log_path)

    assert result["summary"]["calls"] == 3
    assert result["summary"]["chars"] == 240
    assert result["summary"]["estimated_tokens"] == estimate_tokens(120) + estimate_tokens(80) + estimate_tokens(40)
    assert result["by_tool"]["sed"]["calls"] == 2
    assert result["summary"]["chapter_mcp_candidate_estimated_tokens"] == estimate_tokens(120) + estimate_tokens(80)
    assert result["summary"]["exact_search_estimated_tokens"] == estimate_tokens(40)
    assert result["largest_raw_reads"][0]["path"] == "src/foo.py"
    assert result["repeated_reads"][0]["range"] == "1-20"
    assert result["repeated_reads"][0]["count"] == 2


def test_compare_logs_reports_observed_deltas(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.jsonl"
    chapter_first_path = tmp_path / "chapter-first.jsonl"
    write_jsonl(
        baseline_path,
        [
            {"tool": "sed", "chars_out": 400, "lines_out": 40},
            {"tool": "rg", "chars_out": 100, "lines_out": 5},
        ],
    )
    write_jsonl(
        chapter_first_path,
        [
            {"tool": "chapter-mcp", "chars_out": 180, "lines_out": 12},
            {"tool": "sed", "chars_out": 20, "lines_out": 2},
        ],
    )

    result = compare_logs(baseline_path, chapter_first_path)

    assert result["summary"]["baseline_estimated_tokens"] == estimate_tokens(400) + estimate_tokens(100)
    assert result["summary"]["chapter_first_estimated_tokens"] == estimate_tokens(180) + estimate_tokens(20)
    assert result["summary"]["estimated_output_token_delta"] > 0
    assert result["summary"]["chapter_first_additional_room_estimated_tokens"] == estimate_tokens(20)
    assert result["baseline"]["by_tool"]["sed"]["calls"] == 1
    assert result["chapter_first"]["by_tool"]["chapter-mcp"]["calls"] == 1


def test_benchmark_cli_summarize_prints_compact_summary_by_default(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "baseline.jsonl"
    write_jsonl(log_path, [{"tool": "rg", "chars_out": 80, "lines_out": 4}, {"tool": "sed", "chars_out": 40}])

    monkeypatch.setattr("sys.argv", ["chapter-mcp", "benchmark", "summarize", str(log_path)])
    cli.main()

    output = capsys.readouterr().out
    assert "totals: calls=2" in output
    assert "chapter-mcp candidate raw reads" in output
    assert "exact-search traffic kept separate" in output


def test_benchmark_cli_summarize_json_flag_prints_json(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    log_path = tmp_path / "baseline.jsonl"
    write_jsonl(log_path, [{"tool": "rg", "chars_out": 80, "lines_out": 4}])

    monkeypatch.setattr("sys.argv", ["chapter-mcp", "benchmark", "summarize", str(log_path), "--json"])
    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["summary"]["calls"] == 1
    assert output["by_tool"]["rg"]["estimated_tokens"] == estimate_tokens(80)


def test_benchmark_cli_compare_prints_compact_summary_by_default(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "baseline.jsonl"
    chapter_first_path = tmp_path / "chapter-first.jsonl"
    write_jsonl(baseline_path, [{"tool": "sed", "chars_out": 200}, {"tool": "rg", "chars_out": 40}])
    write_jsonl(chapter_first_path, [{"tool": "chapter-mcp", "chars_out": 100}, {"tool": "sed", "chars_out": 20}])

    monkeypatch.setattr(
        "sys.argv",
        ["chapter-mcp", "benchmark", "compare", str(baseline_path), str(chapter_first_path)],
    )
    cli.main()

    output = capsys.readouterr().out
    assert "estimated output tokens:" in output
    assert "chapter-mcp candidate raw-read budget:" in output
    assert "additional chapter-first room estimate:" in output


def test_benchmark_cli_compare_json_flag_prints_json(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    baseline_path = tmp_path / "baseline.jsonl"
    chapter_first_path = tmp_path / "chapter-first.jsonl"
    write_jsonl(baseline_path, [{"tool": "sed", "chars_out": 200}])
    write_jsonl(chapter_first_path, [{"tool": "chapter-mcp", "chars_out": 100}])

    monkeypatch.setattr(
        "sys.argv",
        ["chapter-mcp", "benchmark", "compare", str(baseline_path), str(chapter_first_path), "--json"],
    )
    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["summary"]["estimated_output_token_delta"] == estimate_tokens(200) - estimate_tokens(100)


def test_benchmark_cli_rejects_invalid_jsonl(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "invalid.jsonl"
    log_path.write_text("{bad json}\n", encoding="utf-8")

    monkeypatch.setattr("sys.argv", ["chapter-mcp", "benchmark", "summarize", str(log_path)])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2


@pytest.mark.parametrize("field", ["chars_out", "bytes_out", "lines_out"])
def test_load_records_rejects_negative_output_counters(tmp_path: Path, field: str) -> None:
    log_path = tmp_path / "invalid.jsonl"
    payload: dict[str, object] = {"tool": "rg", "chars_out": 10}
    payload[field] = -1
    write_jsonl(log_path, [payload])

    with pytest.raises(ValueError, match=rf"{field} must be non-negative, got -1"):
        load_records(log_path)


def test_extract_codex_session_to_jsonl_normalizes_exec_and_mcp_tools(tmp_path: Path) -> None:
    session_log = tmp_path / "session.jsonl"
    session_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-05-25T10:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call_shell",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "rtk sed -n '1,20p' src/foo.py"}),
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-05-25T10:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_shell",
                            "output": "Chunk ID: 1\nOutput:\nhello\nworld\n",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-05-25T10:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call_mcp",
                            "name": "search",
                            "namespace": "mcp__chapter-mcp__",
                            "arguments": json.dumps({"query": "alpha"}),
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-05-25T10:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_mcp",
                            "output": '{"count":1,"results":[]}',
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "extracted.jsonl"

    result = extract_codex_session_to_jsonl(session_log, output_path)
    extracted_lines = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]

    assert result["records_written"] == 2
    assert extracted_lines[0]["tool"] == "sed"
    assert extracted_lines[0]["cmd"] == "rtk sed -n '1,20p' src/foo.py"
    assert extracted_lines[0]["path"] == "src/foo.py"
    assert extracted_lines[1]["tool"] == "chapter-mcp"
    assert extracted_lines[1]["call_name"] == "search"


def test_benchmark_cli_extract_codex_session_writes_jsonl(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_log = tmp_path / "session.jsonl"
    session_log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-05-25T10:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call_shell",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "rtk rg alpha src"}),
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-05-25T10:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_shell",
                            "output": "match\n",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "log.jsonl"

    monkeypatch.setattr(
        "sys.argv",
        ["chapter-mcp", "benchmark", "extract-codex-session", str(session_log), "--out", str(output_path)],
    )
    cli.main()

    cli_output = json.loads(capsys.readouterr().out)
    extracted_lines = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]

    assert cli_output["records_written"] == 1
    assert extracted_lines[0]["tool"] == "rg"
