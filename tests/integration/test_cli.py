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


def _flat(output: str) -> str:
    """Rich wraps stderr at the terminal width; undo it for phrase checks."""
    return " ".join(output.split())


def _offline_args(*extra: str) -> list[str]:
    return [
        "analyze",
        "--input-file",
        str(EXAMPLE / "input.json"),
        "--repo-path",
        str(EXAMPLE / "repo"),
        "--no-llm",
        "--no-sandbox",
        *extra,
    ]


def test_analyze_out_is_an_existing_file_fails_cleanly(tmp_path):
    target = tmp_path / "report.md"
    target.write_text("keep me")
    result = runner.invoke(app, _offline_args("--out", str(target)))
    assert result.exit_code == 2
    assert "cannot write report to" in _flat(result.output)
    assert "is not a directory" in _flat(result.output)
    assert not isinstance(result.exception, OSError)
    assert target.read_text() == "keep me"


def test_analyze_unwritable_out_fails_cleanly(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    result = runner.invoke(app, _offline_args("--out", str(blocker / "sub")))
    assert result.exit_code == 2
    assert "cannot write report to" in _flat(result.output)
    assert not isinstance(result.exception, OSError)


def test_analyze_repo_path_that_is_a_file(tmp_path):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("")
    result = runner.invoke(
        app,
        [
            "analyze",
            "--input-file",
            str(EXAMPLE / "input.json"),
            "--repo-path",
            str(not_a_dir),
            "--no-llm",
            "--no-sandbox",
        ],
    )
    assert result.exit_code == 2
    assert "is not a directory" in _flat(result.output)
    assert "does not exist" not in _flat(result.output)


def test_serve_rejects_out_of_range_port():
    for port in ("0", "70000"):
        result = runner.invoke(app, ["serve", "--port", port])
        assert result.exit_code == 2
        assert not isinstance(result.exception, OverflowError)


def test_make_injection_set_script_has_help():
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "make-injection-set.py"
    shown = subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True, check=False
    )
    assert shown.returncode == 0, shown.stderr
    assert "src" in shown.stdout and "dest" in shown.stdout
    missing = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert missing.returncode == 2
    assert "IndexError" not in missing.stderr
