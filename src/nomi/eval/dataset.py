"""Evaluation datasets: closed GitHub issues whose triage labels are the answer key.

Maintainers already mark many closed issues as invalid, "cannot reproduce" or
confirmed. Those labels are an independent, human verdict on whether a report
held up, so Nomi is scored against them. Every project spells them differently
("Status: Can't Reproduce", "🚫 not-a-bug", "R-worksforme"), hence the
normalisation layer, which only ever maps a label when the match is exact after
cleaning: a wrong gold label would corrupt every metric computed from it.

The labels are the answer, so they must never reach the analyser:
:meth:`EvalItem.to_submission` hands over only what the reporter wrote.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

from pydantic import BaseModel, Field, ValidationError, model_validator

from nomi.models import Kind, Submission

Gold = Literal["invalid", "cannot_reproduce", "confirmed"]
GOLDS: tuple[Gold, ...] = get_args(Gold)

# Keys are already in the form produced by `_canonical`, so lookups are exact.
LABEL_ALIASES: dict[str, Gold] = {
    **dict.fromkeys(
        ("invalid", "not a bug", "notabug", "not bug", "user error", "spam"),
        "invalid",
    ),
    **dict.fromkeys(
        (
            "cannot reproduce",
            "can't reproduce",
            "cant reproduce",
            "can not reproduce",
            "could not reproduce",
            "couldn't reproduce",
            "couldnt reproduce",
            "not reproducible",
            "non reproducible",
            "unable to reproduce",
            "unreproducible",
            "irreproducible",
            "cannot repro",
            "can't repro",
            "cant repro",
            "works for me",
            "worksforme",
        ),
        "cannot_reproduce",
    ),
    **dict.fromkeys(
        (
            "confirmed",
            "bug confirmed",
            "confirmed bug",
            "verified",
            "verified bug",
            "bug verified",
            "reproduced",
            "reproducible",
        ),
        "confirmed",
    ),
}

# GitHub caps label names at 50 characters; anything far longer is not a label.
_MAX_LABEL_CHARS = 256
_APOSTROPHES = str.maketrans(dict.fromkeys("‘’ʼ´`", "'"))
_SHORTCODE = re.compile(r":[a-z0-9_+\-]+:")
_KEPT_PUNCTUATION = frozenset("'-_:/?")
_SEPARATORS = re.compile(r"[\s\-_]+")
_SCOPE_PREFIX = re.compile(r"^[^:/]{0,24}[:/]")  # "status:", "resolution/", "s:"
_PREFIX_WORDS = frozenset(
    {"s", "r", "status", "state", "resolution", "resolved", "type", "kind", "triage", "closed"}
)
_EDGE_CHARS = " :/'-_"
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


class IssueSource(Protocol):
    """The two GitHub calls the dataset builder needs (GitHubClient satisfies it)."""

    def list_labels(self, repo: str) -> Sequence[Any]: ...

    def search_issues(self, query: str, *, max_items: int = ...) -> Sequence[Any]: ...


class EvalItem(BaseModel):
    """One closed issue plus the maintainers' verdict on it."""

    id: str
    repo: str
    number: int = Field(ge=1)
    url: str
    kind: Kind = Kind.ISSUE
    title: str
    body: str = ""
    created_at: datetime
    gold: Gold
    source_labels: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _default_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("id") and "repo" in data and "number" in data:
            return {**data, "id": f"{data['repo']}#{data['number']}"}
        return data

    def to_submission(self) -> Submission:
        """The issue as its reporter wrote it; labels and gold are left out on purpose."""
        return Submission(
            kind=self.kind,
            repo=self.repo,
            number=self.number,
            url=self.url,
            title=self.title,
            body=self.body,
            created_at=self.created_at,
        )


# --------------------------------------------------------------------------
# Label normalisation
# --------------------------------------------------------------------------


def _clean_char(ch: str) -> str:
    if ch.isalnum() or ch in _KEPT_PUNCTUATION:
        return ch
    if unicodedata.category(ch) == "Cf":
        return ""  # zero-width joiners, soft hyphens: invisible inside a word
    return " "  # whitespace, emoji, symbols, controls and other punctuation


def _strip_scopes(text: str) -> str:
    while True:
        stripped = _SCOPE_PREFIX.sub("", text, count=1).strip(_EDGE_CHARS)
        if stripped == text or not stripped:
            return text
        text = stripped


def _strip_prefix_words(text: str) -> str:
    words = text.split(" ")
    while len(words) > 1 and words[0] in _PREFIX_WORDS:
        words = words[1:]
    return " ".join(words)


def _canonical(name: str) -> str:
    """Lowercase, emoji-free, prefix-free spelling of a label name."""
    if len(name) > _MAX_LABEL_CHARS:
        return ""
    text = unicodedata.normalize("NFKC", name).translate(_APOSTROPHES).casefold()
    text = _SHORTCODE.sub(" ", text)
    text = "".join(_clean_char(ch) for ch in text)
    text = _SEPARATORS.sub(" ", text).strip(_EDGE_CHARS)
    text = _strip_scopes(text)
    return _strip_prefix_words(text)


def normalize_label(name: str) -> Gold | None:
    """Map a repository label name onto a gold class, or None when it is not one."""
    if not isinstance(name, str):
        return None
    return LABEL_ALIASES.get(_canonical(name))


def gold_from_labels(
    labels: Iterable[str], *, label_map: Mapping[str, Gold] | None = None
) -> Gold | None:
    """The single gold class the labels agree on; None when none match or they conflict.

    ``label_map`` holds explicit ``{label name: gold}`` choices (matched exactly,
    then case-insensitively) that take precedence over the built-in aliases.
    """
    explicit = dict(label_map or {})
    folded = {name.casefold(): gold for name, gold in explicit.items()}
    golds: set[Gold] = set()
    for name in labels:
        if not isinstance(name, str):
            continue
        gold = explicit.get(name) or folded.get(name.casefold()) or normalize_label(name)
        if gold is not None:
            golds.add(gold)
    return golds.pop() if len(golds) == 1 else None


# --------------------------------------------------------------------------
# Building a dataset from GitHub
# --------------------------------------------------------------------------


def _label_name(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping) and isinstance(raw.get("name"), str):
        return raw["name"]
    return None


def _label_names(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    names = (_label_name(entry) for entry in raw)
    return list(dict.fromkeys(name for name in names if name))


def _gold_order(item: tuple[str, Gold]) -> tuple[int, str, str]:
    name, gold = item
    return GOLDS.index(gold), name.casefold(), name


def discover_labels(client: IssueSource, repo: str) -> dict[str, Gold]:
    """Every label of ``repo`` that maps onto a gold class, grouped by class then name."""
    found: dict[str, Gold] = {}
    for raw in client.list_labels(repo):
        name = _label_name(raw)
        gold = normalize_label(name) if name else None
        if name and gold:
            found[name] = gold
    return dict(sorted(found.items(), key=_gold_order))


def _check_repo(repo: str) -> None:
    if not isinstance(repo, str) or not _REPO.match(repo):
        raise ValueError(f"expected a repository as 'owner/name', got {repo!r}")


def _check_golds(label_map: Mapping[str, Gold]) -> None:
    unknown = sorted({str(gold) for gold in label_map.values()} - set(GOLDS))
    if unknown:
        raise ValueError(f"unknown gold label(s) {unknown}; expected one of {list(GOLDS)}")


def _item_from_search(repo: str, raw: Any, label_map: Mapping[str, Gold]) -> EvalItem | None:
    if not isinstance(raw, Mapping) or "pull_request" in raw:
        return None
    number = raw.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    labels = _label_names(raw.get("labels"))
    gold = gold_from_labels(labels, label_map=label_map)
    if gold is None:
        return None
    url = raw.get("html_url")
    if not isinstance(url, str) or not url:
        url = f"https://github.com/{repo}/issues/{number}"
    body = raw.get("body")
    try:
        return EvalItem(
            repo=repo,
            number=number,
            url=url,
            title=raw.get("title"),
            body=body if isinstance(body, str) else "",
            created_at=raw.get("created_at"),
            gold=gold,
            source_labels=labels,
        )
    except ValidationError:
        return None


def _interleave(items: Iterable[EvalItem]) -> list[EvalItem]:
    """Round-robin over the classes so the first N items stay class-balanced."""
    pool = sorted(items, key=lambda item: (item.repo, item.number))
    groups = [[item for item in pool if item.gold == gold] for gold in GOLDS]
    longest = max((len(group) for group in groups), default=0)
    return [group[i] for i in range(longest) for group in groups if i < len(group)]


def build_dataset(
    client: IssueSource,
    repo: str,
    *,
    per_label: int = 10,
    label_map: Mapping[str, Gold] | None = None,
) -> list[EvalItem]:
    """Fetch up to ``per_label`` closed issues for every mapped label of ``repo``.

    Pull requests and issues whose labels disagree (say "invalid" and "confirmed")
    are dropped. Labels containing a double quote cannot be expressed in GitHub's
    search syntax and are skipped. Without ``label_map`` the labels are discovered
    with :func:`discover_labels`.
    """
    _check_repo(repo)
    mapping = dict(label_map) if label_map is not None else discover_labels(client, repo)
    _check_golds(mapping)
    if per_label <= 0:
        return []
    found: dict[str, EvalItem] = {}
    for label in mapping:
        if '"' in label or not label.strip():
            continue
        query = f'repo:{repo} is:issue is:closed label:"{label}"'
        for raw in list(client.search_issues(query, max_items=per_label))[:per_label]:
            item = _item_from_search(repo, raw, mapping)
            if item is not None:
                found.setdefault(item.id, item)
    return _interleave(found.values())


# --------------------------------------------------------------------------
# JSONL persistence
# --------------------------------------------------------------------------


def save_dataset(items: Iterable[EvalItem], path: Path | str) -> None:
    """Write one JSON object per line (UTF-8), replacing ``path`` atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("".join(f"{item.model_dump_json()}\n" for item in items), encoding="utf-8")
    os.replace(tmp, path)


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    where = ".".join(str(part) for part in error.get("loc", ()))
    message = str(error.get("msg", "invalid value"))[:200]
    return f"{where}: {message}" if where else message


def load_dataset(path: Path | str) -> list[EvalItem]:
    """Read a JSONL dataset; blank lines are skipped.

    Raises ValueError naming the line number for undecodable, malformed or
    duplicate rows, so a hand-edited dataset fails loudly instead of skewing scores.
    """
    path = Path(path)
    items: list[EvalItem] = []
    first_line: dict[str, int] = {}
    for lineno, raw in enumerate(path.read_bytes().splitlines(), start=1):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path}: line {lineno}: not valid UTF-8") from exc
        text = text.removeprefix("﻿") if lineno == 1 else text
        if not text.strip():
            continue
        try:
            item = EvalItem.model_validate_json(text)
        except ValidationError as exc:
            raise ValueError(f"{path}: line {lineno}: invalid item ({_first_error(exc)})") from exc
        if item.id in first_line:
            raise ValueError(
                f"{path}: line {lineno}: duplicate id {item.id!r} "
                f"(first seen on line {first_line[item.id]})"
            )
        first_line[item.id] = lineno
        items.append(item)
    return items
