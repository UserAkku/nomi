"""CLI behaviour through Typer's runner (offline)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from nomi import __version__
from nomi.cli import app

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "fabricated-security-report"
runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_analyze_offline_example_writes_reports(tmp_path):
    result = runner.invoke(
        app,
        [
            "analyze",
            "--input-file",
            str(EXAMPLE / "input.json"),
            "--repo-path",
            str(EXAMPLE / "repo"),
            "--no-llm",
            "--no-sandbox",
            "--format",
            "both",
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["assessment"]["verdict"] == "likely_invalid"
    markdown = (tmp_path / "report.md").read_text()
    assert "<!-- nomi:report -->" in markdown
    assert "Likely invalid" in markdown


def test_analyze_prints_markdown_to_stdout():
    result = runner.invoke(
        app,
        [
            "analyze",
            "--input-file",
            str(EXAMPLE / "input.json"),
            "--repo-path",
            str(EXAMPLE / "repo"),
            "--no-llm",
            "--no-sandbox",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Nomi evidence report" in result.stdout


def test_analyze_requires_a_target():
    result = runner.invoke(app, ["analyze", "--no-llm"])
    assert result.exit_code == 2


def test_analyze_rejects_bad_format_and_bad_input(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert runner.invoke(app, ["analyze", "--input-file", str(bad), "--no-llm"]).exit_code == 2
    result = runner.invoke(
        app, ["analyze", "--input-file", str(EXAMPLE / "input.json"), "--format", "pdf", "--no-llm"]
    )
    assert result.exit_code == 2


def test_analyze_rejects_unparseable_target():
    result = runner.invoke(app, ["analyze", "not-a-github-reference", "--no-llm"])
    assert result.exit_code == 2
