"""`nomi eval` through Typer's runner (offline, no model)."""

from __future__ import annotations

import hashlib
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


ROW = {
    "id": "acme/widget#1",
    "repo": "acme/widget",
    "number": 1,
    "url": "https://github.com/acme/widget/issues/1",
    "kind": "issue",
    "title": "It does not work",
    "body": "Please fix",
    "created_at": "2026-01-01T00:00:00Z",
    "gold": "cannot_reproduce",
}


def _offline(monkeypatch, workspace, note=None):
    import nomi.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "open_workspace_for", lambda sub, settings, at_head=False: (workspace, note)
    )


def _eval(*args):
    return runner.invoke(app, ["eval", *map(str, args), "--no-llm", "--no-sandbox"])


def test_eval_checkout_failure_is_an_error_that_resume_retries(tmp_path, monkeypatch, sample_repo):
    dataset = tmp_path / "d.jsonl"
    dataset.write_text(json.dumps(ROW) + "\n")
    out = tmp_path / "run"
    _offline(monkeypatch, None, "Repository checkout failed: network down")
    result = _eval(dataset, "--out", out)
    assert result.exit_code == 0, result.output
    [row] = [json.loads(line) for line in (out / "predictions.jsonl").read_text().splitlines()]
    assert row["pred"] is None and "Repository checkout failed" in row["error"]
    assert json.loads((out / "metrics.json").read_text())["errors"] == 1

    _offline(monkeypatch, sample_repo)
    result = _eval(dataset, "--out", out)
    assert result.exit_code == 0, result.output
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["errors"] == 0 and metrics["n"] == 1
    assert (out / "predictions.jsonl").read_text().count("\n") == 2


def test_eval_defaults_to_an_output_directory_per_dataset(tmp_path, monkeypatch, sample_repo):
    _offline(monkeypatch, sample_repo)
    monkeypatch.chdir(tmp_path)
    dataset = tmp_path / "flask-small.jsonl"
    dataset.write_text(json.dumps(ROW) + "\n")
    result = _eval(dataset)
    assert result.exit_code == 0, result.output
    out = tmp_path / "eval-runs" / "flask-small"
    assert (out / "metrics.json").is_file()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["dataset"] == str(dataset.resolve())
    assert manifest["dataset_sha256"] == hashlib.sha256(dataset.read_bytes()).hexdigest()
    assert manifest["model"] is None and manifest["sandbox"] is False


def test_eval_refuses_to_resume_against_a_changed_dataset(tmp_path, monkeypatch, sample_repo):
    _offline(monkeypatch, sample_repo)
    dataset = tmp_path / "d.jsonl"
    dataset.write_text(json.dumps(ROW) + "\n")
    out = tmp_path / "run"
    assert _eval(dataset, "--out", out).exit_code == 0

    dataset.write_text(json.dumps({**ROW, "gold": "invalid"}) + "\n")
    result = _eval(dataset, "--out", out)
    assert result.exit_code == 2
    assert "--fresh" in result.output and "dataset_sha256" in result.output
    assert (out / "predictions.jsonl").read_text().count("\n") == 1

    result = _eval(dataset, "--out", out, "--fresh")
    assert result.exit_code == 0, result.output
    assert json.loads((out / "metrics.json").read_text())["per_class"]["invalid"]["support"] == 1
