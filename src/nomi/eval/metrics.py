"""Scores for Nomi's triage predictions against the maintainers' labels.

Two decisions shape every number here. First, abstaining (an ``inconclusive``
verdict, predicted label None) is a legitimate answer for a verification tool,
so it is reported three ways: plain accuracy counts it as wrong, selective
accuracy ignores it, and coverage says how often Nomi answered at all. Second,
not all mistakes cost the same: calling a confirmed bug "invalid" can get a
real report closed, so that error has its own headline rate.

Pure Python and plain dicts, so results serialise straight to JSON.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

DEFAULT_LABELS: tuple[str, ...] = ("invalid", "cannot_reproduce", "confirmed")
ABSTAIN = "abstain"


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _f1(precision: float, recall: float) -> float:
    total = precision + recall
    return 2 * precision * recall / total if total else 0.0


def _class_scores(label: str, gold: Sequence[str], pred: Sequence[str | None]) -> dict[str, Any]:
    pairs = list(zip(gold, pred, strict=True))
    true_pos = sum(g == label and p == label for g, p in pairs)
    predicted = sum(p == label for p in pred)
    support = sum(g == label for g in gold)
    precision = _rate(true_pos, predicted) or 0.0
    recall = _rate(true_pos, support) or 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "support": support,
    }


def _majority(gold: Sequence[str], labels: Sequence[str]) -> tuple[str | None, int]:
    """Most frequent gold label; ties go to the earlier label in ``labels``."""
    counts = Counter(gold)
    if not counts:
        return None, 0

    def rank(label: str) -> tuple[int, int, str]:
        position = labels.index(label) if label in labels else len(labels)
        return -counts[label], position, label

    best = min(counts, key=rank)
    return best, counts[best]


def _confusion(
    gold: Sequence[str], pred: Sequence[str | None], labels: Sequence[str]
) -> dict[str, dict[str, int]]:
    known = set(labels)
    rows = [*labels, *sorted(set(gold) - known)]
    extra_cols = sorted({p for p in pred if p is not None} - known - {ABSTAIN})
    cols = [*labels, *extra_cols, ABSTAIN]
    matrix = {row: dict.fromkeys(cols, 0) for row in rows}
    for g, p in zip(gold, pred, strict=True):
        matrix[g][ABSTAIN if p is None else p] += 1
    return matrix


def compute_metrics(
    gold: Sequence[str],
    pred: Sequence[str | None],
    labels: Sequence[str] = DEFAULT_LABELS,
) -> dict[str, Any]:
    """Accuracy, coverage, per-class and harm metrics for paired gold/predicted labels.

    Ratios with an empty denominator are 0.0 for the per-class scores (the usual
    zero-division convention) and None for ``selective_accuracy`` (nothing was
    answered) and ``false_invalid_rate`` (no confirmed bug to misjudge).
    """
    if len(gold) != len(pred):
        raise ValueError(f"gold and pred differ in length ({len(gold)} != {len(pred)})")
    labels = tuple(dict.fromkeys(labels))
    pairs = list(zip(gold, pred, strict=True))
    n = len(pairs)
    covered = sum(p is not None for p in pred)
    correct = sum(p is not None and p == g for g, p in pairs)
    confirmed = sum(g == "confirmed" for g in gold)
    false_invalid = sum(g == "confirmed" and p == "invalid" for g, p in pairs)
    per_class = {label: _class_scores(label, gold, pred) for label in labels}
    majority_label, majority_count = _majority(gold, labels)
    return {
        "n": n,
        "coverage": _rate(covered, n) or 0.0,
        "accuracy": _rate(correct, n) or 0.0,
        "selective_accuracy": _rate(correct, covered),
        "macro_f1": sum(s["f1"] for s in per_class.values()) / len(labels) if labels else 0.0,
        "false_invalid_rate": _rate(false_invalid, confirmed),
        "majority_baseline_accuracy": _rate(majority_count, n) or 0.0,
        "majority_label": majority_label,
        "covered": covered,
        "correct": correct,
        "abstained": n - covered,
        "false_invalid": false_invalid,
        "labels": list(labels),
        "per_class": per_class,
        "confusion": _confusion(gold, pred, labels),
    }


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _pct(value: Any) -> str:
    number = _number(value)
    return "n/a" if number is None else f"{number:.1%}"


def _dec(value: Any) -> str:
    number = _number(value)
    return "n/a" if number is None else f"{number:.2f}"


def _cell(value: Any) -> str:
    text = " ".join(str(value).split())
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "'") or " "


def _count(metrics: Mapping[str, Any], key: str) -> int | None:
    value = metrics.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _of(part: int | None, whole: int | None) -> str:
    return f" ({part} of {whole})" if part is not None and whole is not None else ""


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    align = ["---", *["---:"] * (len(header) - 1)]
    lines = [f"| {' | '.join(header)} |", f"| {' | '.join(align)} |"]
    lines.extend(f"| {' | '.join(row)} |" for row in rows)
    return lines


def _confirmed_support(metrics: Mapping[str, Any]) -> int | None:
    per_class = metrics.get("per_class")
    scores = per_class.get("confirmed") if isinstance(per_class, Mapping) else None
    support = scores.get("support") if isinstance(scores, Mapping) else None
    return support if isinstance(support, int) and not isinstance(support, bool) else None


def _summary(metrics: Mapping[str, Any]) -> list[str]:
    n = _count(metrics, "n")
    majority = metrics.get("majority_label")
    baseline = _pct(metrics.get("majority_baseline_accuracy"))
    rows = [
        ["Items scored", str(n if n is not None else "n/a")],
        [
            "Coverage (answered instead of abstaining)",
            _pct(metrics.get("coverage")) + _of(_count(metrics, "covered"), n),
        ],
        [
            "Accuracy (abstentions count as wrong)",
            _pct(metrics.get("accuracy")) + _of(_count(metrics, "correct"), n),
        ],
        ["Selective accuracy (answered items only)", _pct(metrics.get("selective_accuracy"))],
        ["Macro F1", _dec(metrics.get("macro_f1"))],
        [
            "Majority-class baseline accuracy",
            baseline + (f" (always {_cell(majority)})" if majority is not None else ""),
        ],
        [
            "False-invalid rate (confirmed bugs called invalid)",
            _pct(metrics.get("false_invalid_rate"))
            + _of(_count(metrics, "false_invalid"), _confirmed_support(metrics)),
        ],
    ]
    if _count(metrics, "errors") is not None:
        rows.append(["Errors (scored as abstentions)", str(metrics["errors"])])
    if _number(metrics.get("mean_seconds")) is not None:
        rows.append(["Mean seconds per analysed item", f"{metrics['mean_seconds']:.1f}"])
    return _table(["Metric", "Value"], rows)


def _per_class(metrics: Mapping[str, Any]) -> list[str]:
    per_class = metrics.get("per_class")
    if not isinstance(per_class, Mapping) or not per_class:
        return ["_No classes to report._"]
    rows = []
    for label, scores in per_class.items():
        scores = scores if isinstance(scores, Mapping) else {}
        support = scores.get("support")
        rows.append(
            [
                _cell(label),
                _dec(scores.get("precision")),
                _dec(scores.get("recall")),
                _dec(scores.get("f1")),
                str(support) if isinstance(support, int) else "n/a",
            ]
        )
    return _table(["Class", "Precision", "Recall", "F1", "Support"], rows)


def _confusion_table(metrics: Mapping[str, Any]) -> list[str]:
    confusion = metrics.get("confusion")
    if not isinstance(confusion, Mapping) or not confusion:
        return ["_Nothing to tabulate._"]
    rows_in = {g: r for g, r in confusion.items() if isinstance(r, Mapping)}
    cols = list(dict.fromkeys(col for row in rows_in.values() for col in row))
    rows = [[_cell(gold), *(str(row.get(col, 0)) for col in cols)] for gold, row in rows_in.items()]
    intro = "Rows are the maintainers' labels, columns are Nomi's predictions."
    return [intro, "", *_table(["gold \\ predicted", *(_cell(c) for c in cols)], rows)]


_NOTES = [
    "- **Abstention.** When the evidence does not settle a report Nomi answers "
    "`inconclusive`, shown here as `abstain`. Accuracy counts every abstention as wrong; "
    "selective accuracy scores only the answered items, so always read it next to coverage.",
    "- **False-invalid rate** is the share of maintainer-confirmed bugs that Nomi called "
    "invalid. Dismissing a real bug is the most harmful mistake a triage assistant can make, "
    "so this should stay near zero even at the cost of coverage.",
    "- **Majority baseline** is the accuracy of always predicting the most common label; "
    "a useful system has to beat it, not just score high.",
]


def render_metrics_markdown(metrics: Mapping[str, Any], *, title: str = "Nomi evaluation") -> str:
    """Summary, per-class and confusion tables plus reading notes, as Markdown."""
    heading = " ".join(str(title).split()) or "Nomi evaluation"
    sections = [
        [f"# {heading}"],
        _summary(metrics),
        ["## Per class"],
        _per_class(metrics),
        ["## Confusion matrix"],
        _confusion_table(metrics),
        ["## How to read this"],
        _NOTES,
    ]
    return "\n\n".join("\n".join(section) for section in sections)
