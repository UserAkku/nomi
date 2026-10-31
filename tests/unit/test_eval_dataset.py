"""Tests for evaluation datasets: label normalisation, GitHub harvesting and JSONL I/O."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nomi.eval.dataset import (
    GOLDS,
    LABEL_ALIASES,
    EvalItem,
    _canonical,
    build_dataset,
    discover_labels,
    gold_from_labels,
    load_dataset,
    normalize_label,
    save_dataset,
)
from nomi.models import Kind, Submission

# --------------------------------------------------------------------------
# Label normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("invalid", "invalid"),
        ("  INVALID  ", "invalid"),
        ("ＩＮＶＡＬＩＤ", "invalid"),
        ("Status: Invalid", "invalid"),
        ("status:\tinvalid", "invalid"),
        ("invalid\r\n", "invalid"),
        ("❌ invalid", "invalid"),
        (":x: invalid", "invalid"),
        ("[invalid]", "invalid"),
        ("status:triage:invalid", "invalid"),
        ("in­valid", "invalid"),
        ("🚫 Not a bug", "invalid"),
        ("type: not-a-bug", "invalid"),
        ("not_a_bug", "invalid"),
        ("NotABug", "invalid"),
        ("resolution: user error", "invalid"),
        ("Spam", "invalid"),
        ("cannot reproduce", "cannot_reproduce"),
        ("Cannot-Reproduce", "cannot_reproduce"),
        ("Resolution: Can't Reproduce", "cannot_reproduce"),
        ("can’t reproduce", "cannot_reproduce"),
        ("cant reproduce", "cannot_reproduce"),
        ("Can Not Reproduce", "cannot_reproduce"),
        ("could not reproduce", "cannot_reproduce"),
        ("not reproducible", "cannot_reproduce"),
        ("unable to reproduce", "cannot_reproduce"),
        ("unreproducible", "cannot_reproduce"),
        ("irreproducible", "cannot_reproduce"),
        ("(cannot reproduce)", "cannot_reproduce"),
        ("Works For Me", "cannot_reproduce"),
        ("works-for-me", "cannot_reproduce"),
        ("r-worksforme", "cannot_reproduce"),
        ("closed: works for me", "cannot_reproduce"),
        ("confirmed", "confirmed"),
        ("S: confirmed", "confirmed"),
        ("S-confirmed", "confirmed"),
        ("bug: confirmed", "confirmed"),
        ("Bug 🐛 Confirmed", "confirmed"),
        ("Confirmed Bug", "confirmed"),
        ("👍 confirmed 👍", "confirmed"),
        ("verified ✅", "confirmed"),
        ("✔️ reproduced", "confirmed"),
        ("status/reproducible", "confirmed"),
    ],
)
def test_normalize_label_maps_real_world_names(name: str, expected: str) -> None:
    assert normalize_label(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "🐛",
        "bug",
        "type: bug",
        "duplicate",
        "wontfix",
        "question",
        "needs repro",
        "status:",
        "confirmed?",
        "not confirmed",
        "reproducible?",
        "invalid " + "x" * 1000,
        "x" * 10_000,
    ],
)
def test_normalize_label_refuses_everything_else(name: str) -> None:
    assert normalize_label(name) is None


def test_normalize_label_tolerates_non_strings() -> None:
    assert normalize_label(None) is None  # type: ignore[arg-type]
    assert normalize_label(42) is None  # type: ignore[arg-type]


def test_aliases_are_canonical_and_cover_every_gold() -> None:
    assert all(_canonical(alias) == alias for alias in LABEL_ALIASES)
    assert set(LABEL_ALIASES.values()) == set(GOLDS)


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["bug", "Status: Invalid"], "invalid"),
        (["invalid", "not a bug", "spam"], "invalid"),
        (["works for me", "cannot reproduce"], "cannot_reproduce"),
        (["invalid", "confirmed"], None),
        (["cannot reproduce", "reproduced"], None),
        (["bug", "enhancement"], None),
        ([], None),
    ],
)
def test_gold_from_labels(labels: list[str], expected: str | None) -> None:
    assert gold_from_labels(labels) == expected


def test_gold_from_labels_prefers_explicit_map_case_insensitively() -> None:
    label_map = {"Needs More Info Closed": "cannot_reproduce", "verified": "invalid"}
    assert gold_from_labels(["needs more info closed"], label_map=label_map) == "cannot_reproduce"
    assert gold_from_labels(["verified"], label_map=label_map) == "invalid"
    assert gold_from_labels(["verified", "confirmed"], label_map=label_map) is None
    assert gold_from_labels([None, 3, "invalid"]) == "invalid"  # type: ignore[list-item]


# --------------------------------------------------------------------------
# EvalItem
# --------------------------------------------------------------------------


def _item(number: int = 7, gold: str = "invalid", **extra: Any) -> EvalItem:
    return EvalItem(
        repo=extra.pop("repo", "acme/widget"),
        number=number,
        url=f"https://github.com/acme/widget/issues/{number}",
        title=extra.pop("title", f"Issue {number}"),
        body=extra.pop("body", "It breaks."),
        created_at=datetime(2024, 5, 1, 12, 0, tzinfo=UTC),
        gold=gold,
        source_labels=extra.pop("source_labels", ["Status: Invalid"]),
        **extra,
    )


def test_eval_item_derives_id_and_kind() -> None:
    item = _item()
    assert item.id == "acme/widget#7"
    assert item.kind is Kind.ISSUE
    assert _item(id="custom").id == "custom"


def test_to_submission_never_leaks_labels() -> None:
    item = _item(body="Crash in `parse_url` 💥", source_labels=["invalid", "spam"])
    submission = item.to_submission()
    assert isinstance(submission, Submission)
    assert submission.repo == "acme/widget" and submission.number == 7
    assert submission.body == "Crash in `parse_url` 💥"
    assert submission.created_at == item.created_at
    dumped = submission.model_dump_json()
    assert "spam" not in dumped and "invalid" not in dumped
    assert "labels" not in submission.model_dump() and "gold" not in submission.model_dump()


# --------------------------------------------------------------------------
# JSONL persistence
# --------------------------------------------------------------------------


def test_jsonl_round_trip_preserves_unicode(tmp_path: Path) -> None:
    items = [
        _item(1, "invalid", body="naïve 🙃   line\r\nbreak"),
        _item(2, "confirmed", title="日本語のタイトル"),
    ]
    path = tmp_path / "nested" / "data.jsonl"
    save_dataset(items, path)
    assert load_dataset(path) == items
    assert path.read_text(encoding="utf-8").count("\n") == 2
    assert not list(path.parent.glob(".*.tmp"))


def test_load_dataset_skips_blank_lines_crlf_and_bom(tmp_path: Path) -> None:
    rows = [_item(1).model_dump_json(), _item(2, "confirmed").model_dump_json()]
    path = tmp_path / "data.jsonl"
    path.write_bytes(("﻿" + rows[0] + "\r\n\r\n   \r\n" + rows[1] + "\r\n").encode())
    assert [item.number for item in load_dataset(path)] == [1, 2]


def test_load_dataset_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert load_dataset(path) == []


@pytest.mark.parametrize(
    ("bad_line", "message"),
    [
        ("{not json", "line 3"),
        ('{"repo": "acme/widget", "number": 3}', "line 3"),
        ("[1, 2, 3]", "line 3"),
        (
            '{"repo": "a/b", "number": 3, "url": "u", "title": "t", '
            '"created_at": "2024-01-01T00:00:00Z", "gold": "wontfix"}',
            "gold",
        ),
    ],
)
def test_load_dataset_names_the_bad_line(tmp_path: Path, bad_line: str, message: str) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text(f"{_item(1).model_dump_json()}\n\n{bad_line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_dataset(path)


def test_load_dataset_rejects_duplicates_and_bad_bytes(tmp_path: Path) -> None:
    path = tmp_path / "dup.jsonl"
    row = _item(1).model_dump_json()
    path.write_text(f"{row}\n{row}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"line 2: duplicate id 'acme/widget#1'"):
        load_dataset(path)
    path.write_bytes(row.encode() + b"\n\xff\xfe\n")
    with pytest.raises(ValueError, match="line 2: not valid UTF-8"):
        load_dataset(path)


# --------------------------------------------------------------------------
# Building from GitHub
# --------------------------------------------------------------------------


def _raw(number: int, *labels: str, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/widget/issues/{number}",
        "title": f"Issue {number}",
        "body": f"Body {number}",
        "created_at": "2024-03-01T10:00:00Z",
        "labels": [{"name": label} for label in labels],
        **extra,
    }


class FakeClient:
    def __init__(self, labels: list[Any], results: dict[str, list[Any]]) -> None:
        self.labels = labels
        self.results = results
        self.queries: list[tuple[str, int]] = []

    def list_labels(self, repo: str) -> list[Any]:
        return self.labels

    def search_issues(self, query: str, *, max_items: int = 100) -> list[Any]:
        self.queries.append((query, max_items))
        label = query.split('label:"', 1)[1].rstrip('"')
        return self.results.get(label, [])


def test_discover_labels_maps_and_orders() -> None:
    client = FakeClient(
        [
            {"name": "confirmed"},
            {"name": "bug"},
            {"name": "Status: Invalid"},
            {"name": "🙅 works for me"},
            {"color": "fff"},
            "not a bug",
            None,
        ],
        {},
    )
    assert discover_labels(client, "acme/widget") == {
        "not a bug": "invalid",
        "Status: Invalid": "invalid",
        "🙅 works for me": "cannot_reproduce",
        "confirmed": "confirmed",
    }


def test_build_dataset_skips_prs_drops_conflicts_and_dedupes() -> None:
    client = FakeClient(
        [{"name": "invalid"}, {"name": "not a bug"}, {"name": "confirmed"}, {"name": "wontfix"}],
        {
            "invalid": [
                _raw(1, "invalid"),
                _raw(2, "invalid", pull_request={"url": "x"}),
                _raw(3, "invalid", "confirmed"),
                _raw(4, "invalid", "not a bug", body=None),
            ],
            "not a bug": [_raw(4, "invalid", "not a bug", body=None)],
            "confirmed": [_raw(5, "bug", "confirmed"), _raw(3, "invalid", "confirmed")],
        },
    )
    items = build_dataset(client, "acme/widget", per_label=10)
    assert [(item.number, item.gold) for item in items] == [
        (1, "invalid"),
        (5, "confirmed"),
        (4, "invalid"),
    ]
    by_number = {item.number: item for item in items}
    assert by_number[4].body == ""
    assert by_number[4].source_labels == ["invalid", "not a bug"]
    assert by_number[5].created_at == datetime(2024, 3, 1, 10, tzinfo=UTC)
    assert ('repo:acme/widget is:issue is:closed label:"invalid"', 10) in client.queries
    assert all("wontfix" not in query for query, _ in client.queries)


def test_build_dataset_respects_per_label_even_if_client_overdelivers() -> None:
    client = FakeClient([], {"invalid": [_raw(n, "invalid") for n in range(1, 9)]})
    items = build_dataset(client, "acme/widget", per_label=3, label_map={"invalid": "invalid"})
    assert [item.number for item in items] == [1, 2, 3]
    assert client.queries == [('repo:acme/widget is:issue is:closed label:"invalid"', 3)]
    assert build_dataset(client, "acme/widget", per_label=0, label_map={"invalid": "invalid"}) == []


def test_build_dataset_interleaves_classes_for_balanced_limits() -> None:
    client = FakeClient(
        [],
        {
            "invalid": [_raw(n, "invalid") for n in (1, 2, 3)],
            "worksforme": [_raw(10, "worksforme")],
            "confirmed": [_raw(n, "confirmed") for n in (20, 21)],
        },
    )
    label_map = {"confirmed": "confirmed", "invalid": "invalid", "worksforme": "cannot_reproduce"}
    items = build_dataset(client, "acme/widget", label_map=label_map)
    assert [item.gold for item in items[:3]] == ["invalid", "cannot_reproduce", "confirmed"]
    assert [item.number for item in items] == [1, 10, 20, 2, 21, 3]


def test_build_dataset_with_explicit_custom_label() -> None:
    client = FakeClient(
        [],
        {
            "Closed: no repro": [
                _raw(1, "Closed: no repro"),
                _raw(2, "Closed: no repro", "confirmed"),
            ]
        },
    )
    items = build_dataset(client, "acme/widget", label_map={"Closed: no repro": "cannot_reproduce"})
    assert [(item.id, item.gold) for item in items] == [("acme/widget#1", "cannot_reproduce")]


def test_build_dataset_skips_malformed_search_results() -> None:
    results = [
        "not a dict",
        {**_raw(1, "invalid"), "number": "1"},
        {**_raw(2, "invalid"), "number": True},
        _raw(3, "invalid", created_at="yesterday"),
        _raw(4, "invalid", title=None),
        _raw(5, "invalid", labels="invalid"),
        _raw(0, "invalid"),
        _raw(6, "invalid", html_url=None),
    ]
    client = FakeClient([], {"invalid": results})
    items = build_dataset(client, "acme/widget", label_map={"invalid": "invalid"})
    assert [item.number for item in items] == [6]
    assert items[0].url == "https://github.com/acme/widget/issues/6"


def test_build_dataset_skips_unsearchable_labels_and_validates_inputs() -> None:
    client = FakeClient([], {})
    build_dataset(client, "acme/widget", label_map={'say "invalid"': "invalid", " ": "invalid"})
    assert client.queries == []
    with pytest.raises(ValueError, match="owner/name"):
        build_dataset(client, "not a repo", label_map={})
    with pytest.raises(ValueError, match="owner/name"):
        build_dataset(client, 'acme/widget" label:x', label_map={})
    with pytest.raises(ValueError, match="unknown gold"):
        build_dataset(client, "acme/widget", label_map={"wontfix": "wontfix"})  # type: ignore[dict-item]


def test_build_dataset_discovers_labels_when_no_map_given(tmp_path: Path) -> None:
    client = FakeClient(
        [{"name": "Resolution: Invalid"}, {"name": "enhancement"}],
        {"Resolution: Invalid": [_raw(9, "Resolution: Invalid")]},
    )
    items = build_dataset(client, "acme/widget")
    assert [(item.number, item.gold) for item in items] == [(9, "invalid")]
    path = tmp_path / "out.jsonl"
    save_dataset(items, path)
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["id"] == "acme/widget#9" and row["source_labels"] == ["Resolution: Invalid"]
