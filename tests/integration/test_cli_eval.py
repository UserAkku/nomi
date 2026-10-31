"""`nomi eval` through Typer's runner (offline, no model)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from nomi.cli import app

runner = CliRunner()


def test_eval_runs_offline(tmp_path, monkeypatch, sample_repo):
    import nomi.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "open_workspace_for", lambda sub, settings, at_head=False: (sample_repo, None)
    )
    row = {
        "id": "acme/widget#1",
        "repo": "acme/widget",
        "number": 1,
        "url": "https://github.com/acme/widget/issues/1",
        "kind": "issue",
        "title": "It does not work",
        "body": "Please fix",
        "created_at": "2026-01-01T00:00:00Z",
        "gold": "cannot_reproduce",
        "source_labels": ["can't reproduce"],
    }
    dataset = tmp_path / "d.jsonl"
    dataset.write_text(json.dumps(row) + "\n")
    out = tmp_path / "run"
    result = runner.invoke(
        app, ["eval", str(dataset), "--out", str(out), "--no-llm", "--no-sandbox"]
    )
    assert result.exit_code == 0, result.output
    assert (out / "metrics.json").exists()
    assert (out / "predictions.jsonl").read_text().count("\n") == 1
