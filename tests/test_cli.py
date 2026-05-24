from __future__ import annotations

from pathlib import Path

import pytest

from chapter_mcp import cli


class FakeApp:
    def __init__(self) -> None:
        self.run_calls: list[dict[str, object]] = []

    def run(self, **kwargs) -> None:
        self.run_calls.append(kwargs)


def test_cli_runs_fastmcp_without_stdout_banner(monkeypatch, tmp_path: Path) -> None:
    app = FakeApp()
    captured: dict[str, object] = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return app

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setenv("CHAPTER_MCP_LOG_LEVEL", "INFO")
    monkeypatch.setattr(
        "sys.argv",
        ["chapter-mcp", "--root", str(tmp_path), "--path", "docs"],
    )

    cli.main()

    assert captured["root"] == tmp_path
    assert captured["paths"] == ["docs"]
    assert app.run_calls == [{"show_banner": False}]


def test_cli_rejects_non_positive_watch_interval(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["chapter-mcp", "--root", str(tmp_path), "--path", "docs", "--watch-interval", "0"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
