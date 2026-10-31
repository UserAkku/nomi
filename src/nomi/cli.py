"""Command-line interface: `nomi analyze`, `nomi eval`, `nomi dataset`, `nomi serve`, `nomi doctor`."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from nomi import __version__
from nomi.config import Settings, get_settings
from nomi.models import Report, Submission

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Nomi: evidence-based triage for GitHub issues and pull requests.",
)
dataset_app = typer.Typer(
    no_args_is_help=True, help="Build evaluation datasets from closed GitHub issues."
)
app.add_typer(dataset_app, name="dataset")

err = Console(stderr=True)


def _fail(message: str, code: int = 2) -> None:
    err.print(f"[bold red]error:[/] {message}")
    raise typer.Exit(code)


def _load_submission(target: str | None, input_file: Path | None, settings: Settings) -> Submission:
    if input_file is not None:
        try:
            return Submission.model_validate_json(input_file.read_text())
        except (OSError, ValueError) as exc:
            _fail(f"cannot read submission from {input_file}: {exc}")
    if not target:
        _fail("give an issue/PR URL (or owner/repo#number), or --input-file")
    from nomi.github.client import GitHubClient, GitHubError, parse_target

    try:
        ref = parse_target(target)
        with GitHubClient(settings.token) as client:
            return client.get_submission(ref.repo, ref.number)
    except ValueError as exc:
        _fail(str(exc))
    except GitHubError as exc:
        _fail(str(exc))
    raise AssertionError("unreachable")


def _write_outputs(report: Report, fmt: str, out: Path | None) -> None:
    from nomi.report import render_json, render_markdown

    markdown = render_markdown(report)
    payload = render_json(report)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        if fmt in ("md", "both"):
            (out / "report.md").write_text(markdown)
        if fmt in ("json", "both"):
            (out / "report.json").write_text(payload)
        err.print(f"[green]wrote[/] report to {out}/")
    else:
        sys.stdout.write((payload if fmt == "json" else markdown) + "\n")


@app.command()
def analyze(
    target: str | None = typer.Argument(None, help="Issue/PR URL, or owner/repo#number."),
    input_file: Path | None = typer.Option(
        None, "--input-file", help="Read the submission from a JSON file."
    ),
    repo_path: Path | None = typer.Option(
        None, "--repo-path", help="Verify against a local checkout instead of cloning."
    ),
    at_head: bool = typer.Option(
        False,
        "--at-head",
        help="Use the latest commit instead of the commit when the issue was filed.",
    ),
    no_llm: bool = typer.Option(
        False, "--no-llm", help="Deterministic checks only (no language model)."
    ),
    no_sandbox: bool = typer.Option(
        False, "--no-sandbox", help="Never execute reproduction snippets."
    ),
    fmt: str = typer.Option("md", "--format", help="md, json or both."),
    out: Path | None = typer.Option(
        None, "--out", help="Write report.md / report.json into this directory."
    ),
    model: str | None = typer.Option(None, "--model", help="Override the Ollama model tag."),
) -> None:
    """Verify the technical claims in one issue or pull request."""
    from nomi.pipeline import analyze as run_analysis
    from nomi.pipeline import build_llm, build_sandbox, open_workspace_for
    from nomi.repo.workspace import Workspace

    if fmt not in ("md", "json", "both"):
        _fail("--format must be md, json or both")
    settings = get_settings()
    if model:
        settings = settings.model_copy(update={"llm_model": model})
    submission = _load_submission(target, input_file, settings)

    notes: list[str] = []
    if repo_path is not None:
        try:
            workspace = Workspace(repo_path, repo=submission.repo)
        except FileNotFoundError as exc:
            _fail(str(exc))
    else:
        with err.status(f"Checking out {submission.repo}..."):
            workspace, note = open_workspace_for(submission, settings, at_head=at_head)
        if note:
            notes.append(note)
            err.print(f"[yellow]warning:[/] {note}")

    llm, llm_note = build_llm(settings, enabled=not no_llm)
    if llm_note and not no_llm:
        err.print(f"[yellow]warning:[/] {llm_note}")
    if llm_note:
        notes.append(llm_note)
    sandbox, sb_note = build_sandbox(settings, enabled=not no_sandbox)
    if sb_note:
        notes.append(sb_note)

    with err.status("Analysing claims (a local model can take a minute)..."):
        report = run_analysis(submission, workspace, llm=llm, sandbox=sandbox, notes=notes)
    _write_outputs(report, fmt, out)
    verdict = report.assessment
    err.print(f"[bold]verdict:[/] {verdict.verdict.value} (confidence {verdict.confidence.value})")


@app.command("eval")
def evaluate(
    dataset: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="JSONL dataset from `nomi dataset build`."
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Output directory (resumable). Default: eval-runs/<dataset name>."
    ),
    limit: int | None = typer.Option(None, "--limit", help="Only the first N items."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Deterministic checks only."),
    no_sandbox: bool = typer.Option(
        False, "--no-sandbox", help="Never execute reproduction snippets."
    ),
    fresh: bool = typer.Option(False, "--fresh", help="Ignore earlier predictions in --out."),
    max_temp: float | None = typer.Option(
        None,
        "--max-temp",
        help="Pause before an issue while the chip is at or above this °C (macOS).",
    ),
    cooldown: float = typer.Option(0.0, "--cooldown", help="Seconds to rest between issues."),
) -> None:
    """Score Nomi against closed issues whose labels are the reference answers."""
    from nomi.eval.dataset import EvalItem, load_dataset
    from nomi.eval.metrics import render_metrics_markdown
    from nomi.eval.runner import ManifestMismatchError, dataset_fingerprint, run_eval
    from nomi.pipeline import analyze as run_analysis
    from nomi.pipeline import build_llm, build_sandbox, open_workspace_for

    settings = get_settings()
    try:
        items = load_dataset(dataset)
    except ValueError as exc:
        _fail(str(exc))
    llm, llm_note = build_llm(settings, enabled=not no_llm)
    if llm_note and not no_llm:
        err.print(f"[yellow]warning:[/] {llm_note}")
    sandbox, _ = build_sandbox(settings, enabled=not no_sandbox)

    from nomi.thermal import wait_until_cool

    def analyze_item(item: EvalItem) -> Report:
        # Sustained local inference heats a laptop; pace the run instead of cooking it.
        if max_temp is not None:
            wait_until_cool(max_temp, notify=lambda msg: err.print(f"[yellow]thermal:[/] {msg}"))
        if cooldown > 0:
            time.sleep(cooldown)
        submission = item.to_submission()
        workspace, note = open_workspace_for(submission, settings)
        if workspace is None:
            # Scoring a text-only analysis as Nomi's answer would skew the metrics;
            # an error row is retried when the run is resumed.
            raise RuntimeError(note or "Repository checkout failed.")
        notes = [n for n in (note, llm_note) if n]
        return run_analysis(submission, workspace, llm=llm, sandbox=sandbox, notes=notes)

    def progress(done: int, total: int, row: dict) -> None:
        mark = (
            "✓" if row.get("pred") == row.get("gold") else ("·" if row.get("pred") is None else "✗")
        )
        err.print(
            f"[{done}/{total}] {mark} {row['id']}: gold={row['gold']} pred={row.get('pred')} ({row.get('seconds', 0):.1f}s)"
        )

    config = {
        **dataset_fingerprint(dataset),
        "model": llm.name if llm is not None else None,
        "sandbox": sandbox is not None,
    }
    try:
        metrics = run_eval(
            items,
            analyze_item,
            out_dir=out if out is not None else Path("eval-runs") / dataset.stem,
            resume=not fresh,
            limit=limit,
            on_progress=progress,
            config=config,
        )
    except ManifestMismatchError as exc:
        _fail(str(exc))
    sys.stdout.write(render_metrics_markdown(metrics) + "\n")


@dataset_app.command("build")
def dataset_build(
    repo: str = typer.Option(..., "--repo", help="owner/name of a public repository."),
    out: Path = typer.Option(..., "--out", help="JSONL file to write."),
    per_label: int = typer.Option(10, "--per-label", help="Closed issues to fetch per label."),
    label: list[str] = typer.Option(
        [],
        "--label",
        help='Explicit mapping "Label name=gold" (gold: invalid, cannot_reproduce, confirmed). Repeatable.',
    ),
) -> None:
    """Fetch closed issues whose labels say invalid / cannot reproduce / confirmed."""
    from nomi.eval.dataset import build_dataset, discover_labels, save_dataset
    from nomi.github.client import GitHubClient, GitHubError

    settings = get_settings()
    label_map: dict[str, str] = {}
    for spec in label:
        name, sep, gold = spec.rpartition("=")
        if not sep or gold not in ("invalid", "cannot_reproduce", "confirmed"):
            _fail(f"bad --label {spec!r}; expected 'Label name=invalid|cannot_reproduce|confirmed'")
        label_map[name] = gold
    try:
        with GitHubClient(settings.token) as client:
            if not label_map:
                label_map = discover_labels(client, repo)
                if not label_map:
                    _fail(
                        f"{repo} has no labels that map to invalid / cannot reproduce / confirmed"
                    )
                err.print(f"labels: {json.dumps(label_map, ensure_ascii=False)}")
            items = build_dataset(client, repo, per_label=per_label, label_map=label_map)  # type: ignore[arg-type]
    except GitHubError as exc:
        _fail(str(exc))
    save_dataset(items, out)
    err.print(f"[green]wrote[/] {len(items)} issues to {out}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Run the GitHub App webhook server."""
    import uvicorn

    from nomi.github.app import create_app

    settings = get_settings()
    if settings.webhook_secret is None:
        err.print(
            "[yellow]warning:[/] NOMI_WEBHOOK_SECRET is not set; the server will refuse webhooks."
        )
    if settings.dry_run:
        err.print(
            f"[cyan]dry run:[/] reports are written to {settings.output_dir}/, nothing is posted to GitHub."
        )
    uvicorn.run(create_app(settings), host=host, port=port)


@app.command()
def doctor() -> None:
    """Check the local setup: git, Ollama + model, Docker sandbox, GitHub access."""
    from nomi.pipeline import build_llm, build_sandbox

    settings = get_settings()
    table = Table(title=f"Nomi {__version__} environment")
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Detail")

    from nomi.thermal import hottest_celsius

    table.add_row("python", "✅", sys.version.split()[0])
    temp = hottest_celsius()
    if temp is not None:
        table.add_row(
            "chip temperature", "✅" if temp < 85 else "⚠️", f"{temp:.0f} °C (hottest SoC sensor)"
        )
    table.add_row(
        "git", "✅" if shutil.which("git") else "❌", shutil.which("git") or "git is required"
    )
    llm, llm_note = build_llm(settings)
    table.add_row("model", "✅" if llm else "⚠️", settings.llm_model if llm else (llm_note or ""))
    sandbox, sb_note = build_sandbox(settings)
    table.add_row(
        "sandbox",
        "✅" if sandbox else "⚠️",
        settings.sandbox_image if sandbox else (sb_note or "disabled"),
    )
    table.add_row(
        "github token",
        "✅" if settings.token else "⚠️",
        "set" if settings.token else "unset: public API limit is 60 requests/hour",
    )
    app_ready = bool(settings.app_id and settings.app_private_key_path and settings.webhook_secret)
    table.add_row(
        "github app",
        "✅" if app_ready else "ℹ️",
        "configured" if app_ready else "not configured (CLI works without it)",
    )
    Console().print(table)


@app.command()
def version() -> None:
    """Print the Nomi version."""
    sys.stdout.write(f"nomi {__version__}\n")


if __name__ == "__main__":  # pragma: no cover
    app()
