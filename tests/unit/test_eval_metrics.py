"""Tests for evaluation metrics on small cases computed by hand."""

from __future__ import annotations

import json

import pytest

from nomi.eval.metrics import ABSTAIN, compute_metrics, render_metrics_markdown

LABELS = ("invalid", "cannot_reproduce", "confirmed")


def test_perfect_predictions() -> None:
    gold = ["invalid", "cannot_reproduce", "confirmed", "confirmed"]
    m = compute_metrics(gold, list(gold))
    assert m["n"] == 4
    assert m["coverage"] == m["accuracy"] == m["selective_accuracy"] == 1.0
    assert m["macro_f1"] == 1.0
    assert m["false_invalid_rate"] == 0.0
    assert all(s["precision"] == s["recall"] == s["f1"] == 1.0 for s in m["per_class"].values())
    assert m["confusion"]["confirmed"] == {
        "invalid": 0,
        "cannot_reproduce": 0,
        "confirmed": 2,
        ABSTAIN: 0,
    }


def test_all_abstain() -> None:
    m = compute_metrics(["invalid", "confirmed", "confirmed"], [None, None, None])
    assert m["coverage"] == 0.0 and m["accuracy"] == 0.0
    assert m["selective_accuracy"] is None
    assert m["abstained"] == 3
    assert m["macro_f1"] == 0.0
    assert m["false_invalid_rate"] == 0.0  # abstaining never calls a real bug invalid
    assert m["per_class"]["invalid"] == {"precision": 0.0, "recall": 0.0, "f1": 0.0, "support": 1}
    assert m["confusion"]["confirmed"][ABSTAIN] == 2


def test_hand_computed_mixed_case() -> None:
    gold = ["invalid", "invalid", "cannot_reproduce", "confirmed", "confirmed", "confirmed"]
    pred = ["invalid", "confirmed", None, "confirmed", "invalid", None]
    m = compute_metrics(gold, pred)
    assert (m["n"], m["covered"], m["correct"]) == (6, 4, 2)
    assert m["coverage"] == pytest.approx(4 / 6)
    assert m["accuracy"] == pytest.approx(2 / 6)
    assert m["selective_accuracy"] == pytest.approx(0.5)

    invalid, cannot, confirmed = (m["per_class"][label] for label in LABELS)
    assert (invalid["precision"], invalid["recall"], invalid["f1"]) == (0.5, 0.5, 0.5)
    assert invalid["support"] == 2
    assert cannot == {"precision": 0.0, "recall": 0.0, "f1": 0.0, "support": 1}
    assert confirmed["precision"] == 0.5
    assert confirmed["recall"] == pytest.approx(1 / 3)
    assert confirmed["f1"] == pytest.approx(0.4)
    assert m["macro_f1"] == pytest.approx(0.3)

    assert m["false_invalid"] == 1
    assert m["false_invalid_rate"] == pytest.approx(1 / 3)
    assert m["majority_label"] == "confirmed"
    assert m["majority_baseline_accuracy"] == 0.5
    assert m["confusion"] == {
        "invalid": {"invalid": 1, "cannot_reproduce": 0, "confirmed": 1, ABSTAIN: 0},
        "cannot_reproduce": {"invalid": 0, "cannot_reproduce": 0, "confirmed": 0, ABSTAIN: 1},
        "confirmed": {"invalid": 1, "cannot_reproduce": 0, "confirmed": 1, ABSTAIN: 1},
    }


def test_zero_division_is_zero_for_per_class_scores() -> None:
    m = compute_metrics(["invalid", "invalid"], ["confirmed", "confirmed"])
    assert m["per_class"]["confirmed"] == {
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
        "support": 0,
    }
    assert m["per_class"]["cannot_reproduce"]["precision"] == 0.0
    assert m["false_invalid_rate"] is None  # no confirmed bug to misjudge


def test_false_invalid_rate_counts_only_confirmed_called_invalid() -> None:
    gold = ["confirmed", "confirmed", "confirmed", "confirmed", "invalid"]
    pred = ["invalid", "invalid", "cannot_reproduce", None, "invalid"]
    m = compute_metrics(gold, pred)
    assert m["false_invalid"] == 2
    assert m["false_invalid_rate"] == 0.5


def test_empty_input() -> None:
    m = compute_metrics([], [])
    assert m["n"] == 0
    assert m["coverage"] == m["accuracy"] == m["majority_baseline_accuracy"] == 0.0
    assert m["selective_accuracy"] is None and m["false_invalid_rate"] is None
    assert m["majority_label"] is None
    assert m["confusion"]["invalid"] == dict.fromkeys([*LABELS, ABSTAIN], 0)
    json.dumps(m)


def test_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="length"):
        compute_metrics(["invalid"], [])


def test_unknown_labels_get_their_own_rows_and_columns() -> None:
    m = compute_metrics(["invalid", "duplicate"], ["needs_info", "invalid"])
    assert m["accuracy"] == 0.0 and m["coverage"] == 1.0
    assert list(m["confusion"]) == [*LABELS, "duplicate"]
    assert list(m["confusion"]["invalid"]) == [*LABELS, "needs_info", ABSTAIN]
    assert m["confusion"]["invalid"]["needs_info"] == 1
    assert m["confusion"]["duplicate"]["invalid"] == 1
    assert set(m["per_class"]) == set(LABELS)


def test_majority_tie_breaks_by_label_order_and_custom_labels() -> None:
    m = compute_metrics(["confirmed", "invalid"], ["invalid", "invalid"])
    assert m["majority_label"] == "invalid"
    custom = compute_metrics(["yes", "no", "no"], ["yes", "no", None], labels=("yes", "no", "yes"))
    assert custom["labels"] == ["yes", "no"]
    assert custom["macro_f1"] == pytest.approx((1.0 + 2 / 3) / 2)
    assert custom["false_invalid_rate"] is None


def test_render_markdown_has_every_section() -> None:
    gold = ["invalid", "invalid", "cannot_reproduce", "confirmed", "confirmed", "confirmed"]
    pred = ["invalid", "confirmed", None, "confirmed", "invalid", None]
    metrics = {**compute_metrics(gold, pred), "errors": 1, "mean_seconds": 12.345}
    md = render_metrics_markdown(metrics, title="Run on acme/widget")
    assert md.startswith("# Run on acme/widget\n")
    assert "| Items scored | 6 |" in md
    assert "| Coverage (answered instead of abstaining) | 66.7% (4 of 6) |" in md
    assert "| Accuracy (abstentions count as wrong) | 33.3% (2 of 6) |" in md
    assert "| Selective accuracy (answered items only) | 50.0% |" in md
    assert "| Macro F1 | 0.30 |" in md
    assert "always confirmed" in md
    assert "| False-invalid rate (confirmed bugs called invalid) | 33.3% (1 of 3) |" in md
    assert "| Errors (scored as abstentions) | 1 |" in md
    assert "| Mean seconds per analysed item | 12.3 |" in md
    assert "| confirmed | 0.50 | 0.33 | 0.40 | 3 |" in md
    assert "| gold \\ predicted | invalid | cannot_reproduce | confirmed | abstain |" in md
    assert "| cannot_reproduce | 0 | 0 | 0 | 1 |" in md
    assert "## How to read this" in md and "False-invalid rate" in md and "abstain" in md
    assert not md.endswith("\n")


def test_render_markdown_handles_none_and_missing_values() -> None:
    md = render_metrics_markdown(compute_metrics([], []))
    assert "| Selective accuracy (answered items only) | n/a |" in md
    assert "| False-invalid rate (confirmed bugs called invalid) | n/a (0 of 0) |" in md
    bare = render_metrics_markdown({})
    assert bare.startswith("# Nomi evaluation")
    assert "_No classes to report._" in bare and "_Nothing to tabulate._" in bare
    weird = render_metrics_markdown(
        {"n": "six", "per_class": {"x": "oops"}, "confusion": {"a": {"b": 1}, "c": None}}
    )
    assert "| x | n/a | n/a | n/a | n/a |" in weird
    assert "| a | 1 |" in weird


def test_render_markdown_escapes_table_breaking_text() -> None:
    m = compute_metrics(["a|b"], ["a|b"], labels=("a|b",))
    md = render_metrics_markdown(m, title="multi\nline   title")
    assert md.startswith("# multi line title\n")
    assert "| a\\|b | 1.00 | 1.00 | 1.00 | 1 |" in md
