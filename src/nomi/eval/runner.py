"""Batch evaluation: run the analyser over a labelled dataset and score it.

A run against a local model can take hours and will be interrupted, so each
prediction is appended to ``predictions.jsonl`` the moment it exists and a
rerun resumes where the last one stopped. One failing issue must not sink the
run either: its exception is recorded as an abstention together with the error.
Metrics are always recomputed from the predictions file rather than from
memory, so the numbers on disk and the numbers returned can never disagree.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterable
from hashlib import sha256
from pathlib import Path
from typing import IO, Any

from nomi.eval.dataset import EvalItem
from nomi.eval.metrics import compute_metrics, render_metrics_markdown
from nomi.models import Report, Verdict

PREDICTIONS_FILE = "predictions.jsonl"
METRICS_JSON = "metrics.json"
METRICS_MD = "metrics.md"
REPORTS_DIR = "reports"

AnalyzeFn = Callable[[EvalItem], Report]
ProgressFn = Callable[[int, int, dict[str, Any]], None]

_MAX_ERROR_CHARS = 500
_MAX_NAME_CHARS = 120
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------
# predictions.jsonl
# --------------------------------------------------------------------------


def _valid_row(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and bool(row["id"])
        and isinstance(row.get("gold"), str)
        and (row.get("pred") is None or isinstance(row.get("pred"), str))
    )


def load_predictions(path: Path) -> list[dict[str, Any]]:
    """Rows of a predictions file, one per id (the latest row wins).

    Lines that are not valid rows, such as a half-written last line left by a
    killed run, are skipped rather than failing the whole evaluation.
    """
    if not path.is_file():
        return []
    rows: dict[str, dict[str, Any]] = {}
    for raw in path.read_bytes().splitlines():
        try:
            row = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if _valid_row(row):
            rows[row["id"]] = row
    return list(rows.values())


def _open_for_append(path: Path) -> IO[str]:
    """Append handle that never glues a new row onto a truncated last line."""
    needs_newline = False
    if path.is_file() and path.stat().st_size:
        with path.open("rb") as existing:
            existing.seek(-1, 2)
            needs_newline = existing.read(1) != b"\n"
    handle = path.open("a", encoding="utf-8", newline="\n")
    if needs_newline:
        handle.write("\n")
    return handle


# --------------------------------------------------------------------------
# One item
# --------------------------------------------------------------------------


def report_filename(item_id: str) -> str:
    """Filesystem-safe stem for an item id: ``owner/name#123`` -> ``owner_name_123``.

    Ids that lose more than their ``/`` and ``#`` (unicode, dots, overlong) get a
    short hash suffix so two different ids never overwrite each other's report.
    """
    name = _UNSAFE_NAME.sub("_", item_id).strip("._-") or "item"
    if len(name) > _MAX_NAME_CHARS or name != item_id.replace("/", "_").replace("#", "_"):
        digest = sha256(item_id.encode("utf-8", "surrogatepass")).hexdigest()[:10]
        name = f"{name[:_MAX_NAME_CHARS]}-{digest}"
    return name


def _describe(exc: BaseException) -> str:
    try:
        message = f"{type(exc).__name__}: {exc}"
    except Exception:
        message = type(exc).__name__
    return message if len(message) <= _MAX_ERROR_CHARS else message[: _MAX_ERROR_CHARS - 3] + "..."


def _finite(value: float) -> float | None:
    return round(value, 4) if isinstance(value, int | float) and math.isfinite(value) else None


def _row(
    item: EvalItem, seconds: float, *, report: Report | None = None, error: str | None = None
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": item.id,
        "gold": item.gold,
        "pred": None,
        "verdict": None,
        "confidence": None,
        "score": None,
        "seconds": round(seconds, 3),
        "error": error,
        "n_claims": None,
        "n_checks": None,
        "n_contradicted": None,
    }
    if report is not None:
        row.update(
            pred=report.predicted_label,
            verdict=report.assessment.verdict.value,
            confidence=report.assessment.confidence.value,
            score=_finite(report.assessment.score),
            n_claims=len(report.claims),
            n_checks=len(report.checks),
            n_contradicted=sum(c.verdict is Verdict.CONTRADICTED for c in report.claims),
        )
    return row


def _report_paths(reports_dir: Path, item_id: str) -> tuple[Path, Path]:
    stem = report_filename(item_id)
    return reports_dir / f"{stem}.json", reports_dir / f"{stem}.md"


def _write_report(report: Report, paths: tuple[Path, Path]) -> None:
    json_path, md_path = paths
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    try:
        from nomi.report import render_markdown

        markdown = render_markdown(report)
    except Exception:
        md_path.unlink(missing_ok=True)
        return  # the JSON report is the record; Markdown is a convenience
    md_path.write_text(markdown + "\n", encoding="utf-8")


def _evaluate(item: EvalItem, analyze_fn: AnalyzeFn, reports_dir: Path) -> dict[str, Any]:
    paths = _report_paths(reports_dir, item.id)
    start = time.perf_counter()
    try:
        report = analyze_fn(item)
        if not isinstance(report, Report):
            raise TypeError(f"analyze_fn returned {type(report).__name__}, expected Report")
    except Exception as exc:
        for path in paths:  # a stale report from an earlier run would contradict the row
            path.unlink(missing_ok=True)
        return _row(item, time.perf_counter() - start, error=_describe(exc))
    seconds = time.perf_counter() - start
    _write_report(report, paths)
    return _row(item, seconds, report=report)


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def _select(items: Iterable[EvalItem], limit: int | None) -> list[EvalItem]:
    unique: dict[str, EvalItem] = {}
    for item in items:
        unique.setdefault(item.id, item)
    selected = list(unique.values())
    return selected if limit is None else selected[: max(limit, 0)]


def _score(predictions: Path) -> dict[str, Any]:
    rows = load_predictions(predictions)
    metrics = compute_metrics([row["gold"] for row in rows], [row["pred"] for row in rows])
    durations = [
        float(row["seconds"])
        for row in rows
        if not row.get("error")
        and isinstance(row.get("seconds"), int | float)
        and not isinstance(row.get("seconds"), bool)
    ]
    metrics["errors"] = sum(1 for row in rows if row.get("error"))
    metrics["mean_seconds"] = round(sum(durations) / len(durations), 3) if durations else None
    return metrics


def run_eval(
    items: Iterable[EvalItem],
    analyze_fn: AnalyzeFn,
    *,
    out_dir: Path,
    resume: bool = True,
    limit: int | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Analyse each item, record predictions and reports in ``out_dir``, return metrics.

    Only the first ``limit`` items (after dropping duplicate ids) are considered.
    With ``resume`` the ids already in ``predictions.jsonl`` are skipped;
    without it the file is started afresh. ``on_progress(done, total, row)`` is
    called after each analysed item, where ``done`` also counts the items a
    resumed run skipped. The returned metrics cover every row in the file and
    are also written to ``metrics.json`` and ``metrics.md``.
    """
    out_dir = Path(out_dir)
    reports_dir = out_dir / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    predictions = out_dir / PREDICTIONS_FILE
    if not resume:
        predictions.write_text("", encoding="utf-8")

    selected = _select(items, limit)
    done_ids = {row["id"] for row in load_predictions(predictions)}
    todo = [item for item in selected if item.id not in done_ids]
    total = len(selected)
    with _open_for_append(predictions) as sink:
        for done, item in enumerate(todo, start=total - len(todo) + 1):
            row = _evaluate(item, analyze_fn, reports_dir)
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()
            if on_progress is not None:
                on_progress(done, total, row)

    metrics = _score(predictions)
    (out_dir / METRICS_JSON).write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / METRICS_MD).write_text(render_metrics_markdown(metrics) + "\n", encoding="utf-8")
    return metrics
