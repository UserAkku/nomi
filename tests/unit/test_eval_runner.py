"""Tests for the resumable evaluation runner, using a fake analyser."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nomi.eval.dataset import EvalItem
from nomi.eval.runner import load_predictions, report_filename, run_eval
from nomi.models import (
    Assessment,
    Check,
    CheckStatus,
    Claim,
    ClaimAssessment,
    Confidence,
    Kind,
    OverallVerdict,
    Report,
    Verdict,
)

VERDICT_FOR_GOLD = {
    "invalid": OverallVerdict.LIKELY_INVALID,
    "cannot_reproduce": OverallVerdict.NEEDS_INFO,
    "confirmed": OverallVerdict.LIKELY_VALID,
}
GOLDS = ("invalid", "cannot_reproduce", "confirmed")
ROW_KEYS = {
    "id",
    "gold",
    "pred",
    "verdict",
    "confidence",
    "score",
    "seconds",
    "error",
    "n_claims",
    "n_checks",
    "n_contradicted",
}


def make_item(number: int, gold: str | None = None, **extra: Any) -> EvalItem:
    return EvalItem(
        repo="acme/widget",
        number=number,
        url=f"https://github.com/acme/widget/issues/{number}",
        title=f"Issue {number} ✨",
        body="parse_url crashes",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        gold=gold or GOLDS[number % 3],
        **extra,
    )


def make_report(item: EvalItem, verdict: OverallVerdict) -> Report:
    claims = [
        ClaimAssessment(
            claim=Claim(id=f"C{i}", text=f"claim {i}"),
            verdict=claim_verdict,
            confidence=Confidence.MEDIUM,
            rationale="checked",
        )
        for i, claim_verdict in enumerate([Verdict.CONTRADICTED, Verdict.SUPPORTED], start=1)
    ]
    return Report(
        nomi_version="test",
        kind=Kind.ISSUE,
        repo=item.repo,
        number=item.number,
        url=item.url,
        title=item.title,
        analyzed_at=datetime(2024, 6, 1, tzinfo=UTC),
        summary="Fake analysis.",
        claims=claims,
        checks=[Check(id="grounding.symbol", title="Symbol exists", status=CheckStatus.PASS)],
        assessment=Assessment(verdict=verdict, confidence=Confidence.HIGH, score=0.25),
    )


class FakeAnalyzer:
    """Predicts the gold label except for ids listed in `wrong`, `abstain` or `fail`."""

    def __init__(self, *, wrong=(), abstain=(), fail=()) -> None:
        self.calls: list[str] = []
        self.wrong, self.abstain, self.fail = set(wrong), set(abstain), set(fail)

    def __call__(self, item: EvalItem) -> Report:
        self.calls.append(item.id)
        if item.number in self.fail:
            raise RuntimeError(f"boom on {item.number}\nsecond line")
        if item.number in self.abstain:
            verdict = OverallVerdict.INCONCLUSIVE
        elif item.number in self.wrong:
            verdict = OverallVerdict.LIKELY_INVALID
        else:
            verdict = VERDICT_FOR_GOLD[item.gold]
        return make_report(item, verdict)


def read_rows(out: Path) -> list[dict[str, Any]]:
    lines = (out / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_run_writes_predictions_reports_and_metrics(tmp_path: Path) -> None:
    items = [make_item(n) for n in range(1, 7)]
    analyzer = FakeAnalyzer(wrong={5}, abstain={6})
    progress: list[tuple[int, int, str]] = []
    metrics = run_eval(
        items,
        analyzer,
        out_dir=tmp_path / "run",
        on_progress=lambda done, total, row: progress.append((done, total, row["id"])),
    )
    out = tmp_path / "run"
    rows = read_rows(out)
    assert [row["id"] for row in rows] == [item.id for item in items]
    assert all(set(row) == ROW_KEYS for row in rows)
    first = rows[0]
    assert first["gold"] == "cannot_reproduce" and first["pred"] == "cannot_reproduce"
    assert first["verdict"] == "needs_info" and first["confidence"] == "high"
    assert (first["score"], first["error"]) == (0.25, None)
    assert (first["n_claims"], first["n_checks"], first["n_contradicted"]) == (2, 1, 1)
    assert first["seconds"] >= 0
    assert rows[5]["pred"] is None and rows[5]["verdict"] == "inconclusive"

    assert progress == [(i, 6, f"acme/widget#{i}") for i in range(1, 7)]
    assert metrics["n"] == 6 and metrics["correct"] == 4 and metrics["covered"] == 5
    assert metrics["errors"] == 0 and metrics["mean_seconds"] is not None
    assert metrics["false_invalid_rate"] == pytest.approx(1 / 2)  # #5 is confirmed
    assert json.loads((out / "metrics.json").read_text(encoding="utf-8")) == metrics
    assert (out / "metrics.md").read_text(encoding="utf-8").startswith("# Nomi evaluation")

    report_json = out / "reports" / "acme_widget_1.json"
    assert Report.model_validate_json(report_json.read_text(encoding="utf-8")).number == 1
    assert (out / "reports" / "acme_widget_1.md").read_text(encoding="utf-8").strip()


def test_resume_skips_items_already_predicted(tmp_path: Path) -> None:
    items = [make_item(n) for n in range(1, 6)]
    first = FakeAnalyzer()
    run_eval(items, first, out_dir=tmp_path, limit=2)
    assert first.calls == ["acme/widget#1", "acme/widget#2"]

    second = FakeAnalyzer()
    progress: list[tuple[int, int]] = []
    metrics = run_eval(
        items,
        second,
        out_dir=tmp_path,
        on_progress=lambda done, total, row: progress.append((done, total)),
    )
    assert second.calls == ["acme/widget#3", "acme/widget#4", "acme/widget#5"]
    assert progress == [(3, 5), (4, 5), (5, 5)]
    assert len(read_rows(tmp_path)) == 5
    assert metrics["n"] == 5 and metrics["accuracy"] == 1.0

    third = FakeAnalyzer()
    assert run_eval(items, third, out_dir=tmp_path)["n"] == 5
    assert third.calls == []


def test_fresh_run_starts_over(tmp_path: Path) -> None:
    items = [make_item(n) for n in range(1, 4)]
    run_eval(items, FakeAnalyzer(), out_dir=tmp_path)
    again = FakeAnalyzer(abstain={1, 2, 3})
    metrics = run_eval(items, again, out_dir=tmp_path, resume=False)
    assert len(again.calls) == 3
    assert len(read_rows(tmp_path)) == 3
    assert metrics["coverage"] == 0.0


def test_exceptions_are_recorded_and_the_run_continues(tmp_path: Path) -> None:
    items = [make_item(n) for n in range(1, 5)]
    stale = tmp_path / "reports" / "acme_widget_2.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old report")
    metrics = run_eval(items, FakeAnalyzer(fail={2}), out_dir=tmp_path)
    rows = {row["id"]: row for row in read_rows(tmp_path)}
    failed = rows["acme/widget#2"]
    assert failed["pred"] is None and failed["verdict"] is None
    assert failed["error"].startswith("RuntimeError: boom on 2")
    assert failed["n_claims"] is None
    assert rows["acme/widget#3"]["error"] is None
    assert metrics["n"] == 4 and metrics["covered"] == 3 and metrics["errors"] == 1
    assert not stale.exists()
    assert not (tmp_path / "reports" / "acme_widget_2.json").exists()


def test_wrong_return_type_and_huge_error_messages(tmp_path: Path) -> None:
    def analyze(item: EvalItem) -> Report:
        if item.number == 1:
            return "not a report"  # type: ignore[return-value]
        raise ValueError("x" * 10_000)

    run_eval([make_item(1), make_item(2)], analyze, out_dir=tmp_path)
    rows = read_rows(tmp_path)
    assert rows[0]["error"] == "TypeError: analyze_fn returned str, expected Report"
    assert len(rows[1]["error"]) <= 500 and rows[1]["error"].endswith("...")


def test_limit_is_honoured(tmp_path: Path) -> None:
    items = [make_item(n) for n in range(1, 6)]
    analyzer = FakeAnalyzer()
    metrics = run_eval(items, analyzer, out_dir=tmp_path / "a", limit=3)
    assert len(analyzer.calls) == 3 and metrics["n"] == 3

    nothing = FakeAnalyzer()
    metrics = run_eval(items, nothing, out_dir=tmp_path / "b", limit=0)
    assert nothing.calls == [] and metrics["n"] == 0
    assert (tmp_path / "b" / "metrics.json").is_file()
    assert run_eval(items, FakeAnalyzer(), out_dir=tmp_path / "c", limit=-1)["n"] == 0


def test_duplicate_items_are_analysed_once(tmp_path: Path) -> None:
    analyzer = FakeAnalyzer()
    run_eval([make_item(1), make_item(2), make_item(1, "invalid")], analyzer, out_dir=tmp_path)
    assert analyzer.calls == ["acme/widget#1", "acme/widget#2"]
    assert read_rows(tmp_path)[0]["gold"] == "cannot_reproduce"


def test_metrics_cover_every_row_in_the_file(tmp_path: Path) -> None:
    run_eval([make_item(1), make_item(2)], FakeAnalyzer(), out_dir=tmp_path)
    metrics = run_eval([make_item(10), make_item(11)], FakeAnalyzer(), out_dir=tmp_path)
    assert metrics["n"] == 4


def test_markdown_is_skipped_when_renderer_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "nomi.report", None)
    run_eval([make_item(1)], FakeAnalyzer(), out_dir=tmp_path)
    assert (tmp_path / "reports" / "acme_widget_1.json").is_file()
    assert not (tmp_path / "reports" / "acme_widget_1.md").exists()
    assert len(read_rows(tmp_path)) == 1


def test_truncated_and_garbage_prediction_lines_are_tolerated(tmp_path: Path) -> None:
    good = {"id": "acme/widget#1", "gold": "invalid", "pred": "invalid"}
    (tmp_path / "predictions.jsonl").write_bytes(
        json.dumps(good).encode()
        + b"\r\n\n[1, 2]\n\xff\xfe\n"
        + b'{"id": 5, "gold": "invalid"}\n'
        + b'{"id": "acme/widget#2", "gold": "conf'
    )
    analyzer = FakeAnalyzer()
    metrics = run_eval([make_item(1), make_item(2)], analyzer, out_dir=tmp_path)
    assert analyzer.calls == ["acme/widget#2"]
    assert metrics["n"] == 2
    ids = [row["id"] for row in load_predictions(tmp_path / "predictions.jsonl")]
    assert ids == ["acme/widget#1", "acme/widget#2"]


def test_load_predictions_keeps_the_latest_row_per_id(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    assert load_predictions(path) == []
    rows = [
        {"id": "a/b#1", "gold": "invalid", "pred": None},
        {"id": "a/b#2", "gold": "confirmed", "pred": "confirmed"},
        {"id": "a/b#1", "gold": "invalid", "pred": "invalid"},
        {"id": "a/b#3", "gold": "invalid", "pred": 3},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert load_predictions(path) == [rows[2], rows[1]]


@pytest.mark.parametrize(
    ("item_id", "expected"),
    [
        ("acme/widget#7", "acme_widget_7"),
        ("acme/widget.js#7", "acme_widget.js_7"),
    ],
)
def test_report_filename_is_readable_for_normal_ids(item_id: str, expected: str) -> None:
    assert report_filename(item_id) == expected


@pytest.mark.parametrize("item_id", ["../../etc/passwd#1", "..", "ü/ñ#3", "a" * 500, "/", "a b#1"])
def test_report_filename_is_safe_and_unique(item_id: str) -> None:
    name = report_filename(item_id)
    assert name and "/" not in name and not name.startswith(".") and len(name) <= 140
    assert name != report_filename(item_id + "x")


def test_reports_stay_inside_the_output_directory(tmp_path: Path) -> None:
    item = make_item(1, id="../../escape#1")
    run_eval([item], FakeAnalyzer(), out_dir=tmp_path / "out")
    written = sorted(p.name for p in (tmp_path / "out" / "reports").iterdir())
    assert len(written) == 2 and all(name.startswith("escape_1-") for name in written)
    assert not (tmp_path / "escape_1.json").exists()


def test_progress_callback_receives_rows(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    callback: Callable[[int, int, dict[str, Any]], None] = lambda d, t, row: seen.append(row)  # noqa: E731
    run_eval([make_item(1)], FakeAnalyzer(fail={1}), out_dir=tmp_path, on_progress=callback)
    assert seen[0]["error"].startswith("RuntimeError") and seen[0]["gold"] == "cannot_reproduce"
