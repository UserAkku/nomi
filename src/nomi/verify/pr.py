"""Pull-request checks: does the diff back up what the description says?

Why: a pull request carries its own ground truth. Whatever the description
claims ("added tests", "updated the docs", "fixes a crash", "no breaking
changes") can be compared with the files and lines the PR actually changes,
without any model. Those mismatches are the cheapest strong signal against
low-effort or fabricated PRs: the Hacktoberfest-2020 spam wave was "fix bug"
titles on whitespace-only README edits.

Precision beats recall here. A claim only counts when it is stated as done
(not negated, planned, hedged or asked), and a contradiction is only raised
from what we positively saw in the diff. Whatever we cannot see (no diff,
patches GitHub omitted for large files, no repository checkout) is reported
as such instead of being guessed. Every finding cites the diff lines it rests
on, linked to the PR's "Files changed" view.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from itertools import islice
from pathlib import PurePosixPath
from typing import Literal

from nomi.models import (
    ChangedFile,
    Check,
    CheckStatus,
    Claim,
    ClaimType,
    Evidence,
    EvidenceKind,
    Kind,
    Signal,
    Submission,
)
from nomi.repo.workspace import Workspace, classify_path, normalize_path

MIN_DESCRIPTION_CHARS = 30
LARGE_PR_LINES = 1000
LARGE_PR_FILES = 50
MAX_EXCERPT_LINES = 12
MAX_TEXT_CHARS = 100_000
MAX_MENTIONS = 25

_MAX_LISTED = 8  # names spelled out in a title or detail
_MAX_EVIDENCE = 5  # evidence items per check
_MAX_LINE_CHARS = 200
_MAX_TOKEN_CHARS = 255
_WINDOW = 160  # characters scanned around a claim for negations
_MAX_GREP_HITS = 20

# --------------------------------------------------------------------------
# Unified diff parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffLine:
    """One line inside a diff hunk: '+' added, '-' removed, ' ' context."""

    kind: Literal["+", "-", " "]
    text: str
    old_line: int | None = None
    new_line: int | None = None


@dataclass(frozen=True)
class Hunk:
    """One "@@ -a,b +c,d @@ heading" block of a unified diff."""

    old_start: int | None
    new_start: int | None
    heading: str
    lines: tuple[DiffLine, ...]


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$")


def parse_patch(patch: str | None) -> list[Hunk]:
    """Hunks of a unified diff such as GitHub's per-file `patch` field.

    Lenient on purpose: file headers and anything before the first "@@" are
    skipped, "\\ No newline at end of file" markers are ignored, CRLF line
    endings are accepted and wrong hunk lengths do not drop lines.
    """
    if not patch:
        return []
    raw_lines = patch.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    hunks: list[Hunk] = []
    header: tuple[int | None, int | None, str] | None = None
    body: list[DiffLine] = []
    old = new = None
    for raw in raw_lines:
        line = raw[:-1] if raw.endswith("\r") else raw
        if line.startswith("@@"):
            if header is not None:
                hunks.append(Hunk(*header, tuple(body)))
            m = _HUNK_HEADER.match(line)
            old, new = (int(m.group(1)), int(m.group(2))) if m else (None, None)
            header, body = (old, new, m.group(3).strip() if m else ""), []
        elif line.startswith("diff --git "):
            if header is not None:
                hunks.append(Hunk(*header, tuple(body)))
            header, body = None, []
        elif header is not None and (not line or line[0] in "+- "):
            kind = line[0] if line else " "
            text = line[1:]
            if kind == "+":
                body.append(DiffLine("+", text, None, new))
                new = new + 1 if new is not None else None
            elif kind == "-":
                body.append(DiffLine("-", text, old, None))
                old = old + 1 if old is not None else None
            else:
                body.append(DiffLine(" ", text, old, new))
                old = old + 1 if old is not None else None
                new = new + 1 if new is not None else None
    if header is not None:
        hunks.append(Hunk(*header, tuple(body)))
    return hunks


_CHANGELOG_PATH = re.compile(
    r"(^|/)(changelog\.d|changes|newsfragments?|news|\.changeset|release[-_]?notes)(/|$)", re.I
)
# Test conventions classify_path does not know: Django's tests.py, conftest.py,
# *_spec.rb, FooTest.java / BarTests.cs, fixture and e2e directories.
_EXTRA_TEST_PATH = re.compile(
    r"(^|/)(tests?|conftest)\.\w+$|_(tests?|spec)\.\w+$|[a-z0-9]Tests?\.\w+$"
    r"|(^|/)(testdata|test-data|fixtures|e2e|cypress|playwright)(/|$)"
)


def _category(path: str) -> str:
    """'test', 'doc' or 'code', a little broader than classify_path for PR diffs."""
    kind = classify_path(path)
    if kind == "code" and _EXTRA_TEST_PATH.search(path):
        return "test"
    if kind == "code" and _CHANGELOG_PATH.search(path):
        return "doc"
    return kind


@dataclass(frozen=True)
class _FileDiff:
    """One changed file with its parsed hunks."""

    path: str
    previous_path: str | None
    status: str
    category: str
    additions: int
    deletions: int
    hunks: tuple[Hunk, ...]

    @cached_property
    def lines(self) -> tuple[DiffLine, ...]:
        return tuple(line for hunk in self.hunks for line in hunk.lines)

    @cached_property
    def added(self) -> tuple[DiffLine, ...]:
        return tuple(line for line in self.lines if line.kind == "+")

    @cached_property
    def removed(self) -> tuple[DiffLine, ...]:
        return tuple(line for line in self.lines if line.kind == "-")

    @cached_property
    def blob(self) -> str:
        """All hunk lines joined by newlines, for one fast scan per file."""
        return "\n".join(line.text for line in self.lines)

    @property
    def changed_lines(self) -> int:
        return max(self.additions + self.deletions, len(self.added) + len(self.removed))

    @property
    def patch_missing(self) -> bool:
        """Lines changed but no usable patch (GitHub omits it for large files)."""
        return not self.hunks and self.additions + self.deletions > 0

    @property
    def is_doc(self) -> bool:
        return self.category == "doc"


def _file_diff(cf: ChangedFile) -> _FileDiff | None:
    path = normalize_path(cf.path or "")
    if not path:
        return None
    previous = normalize_path(cf.previous_path) if cf.previous_path else None
    return _FileDiff(
        path=path,
        previous_path=previous or None,
        status=(cf.status or "modified").lower(),
        category=_category(path),
        additions=max(0, cf.additions),
        deletions=max(0, cf.deletions),
        hunks=tuple(parse_patch(cf.patch)),
    )


# --------------------------------------------------------------------------
# The description
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
_QUOTE = re.compile(r"^ {0,3}>")
_UNCHECKED = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[ \]")
_CHECKED = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[[xX]\]\s*")
_LABEL_ONLY = re.compile(
    r"^\s*(?:(?:\*\*|__)[^*_\n]{1,60}(?:\*\*|__)\s*:?|[^\W\d][\w /&()-]{0,40}:)\s*$"
)


_COMMENT_OPEN = re.compile(r"<!--")
_COMMENT_CLOSE = re.compile(r"-->")
_BLOCKQUOTE_OPEN = re.compile(r"<blockquote\b[^>]{0,200}>", re.I)
_BLOCKQUOTE_CLOSE = re.compile(r"</blockquote\s*>", re.I)


def _strip_between(text: str, opening: re.Pattern[str], closing: re.Pattern[str]) -> str:
    """Remove opening...closing spans in linear time; an unclosed one hides the rest."""
    parts: list[str] = []
    pos = 0
    while (start := opening.search(text, pos)) is not None:
        parts.append(text[pos : start.start()])
        end = closing.search(text, start.end())
        if end is None:
            return "".join(parts)
        pos = end.end()
    parts.append(text[pos:])
    return "".join(parts)


def _author_text(body: str) -> str:
    """The body without HTML comments (template hints) and HTML quotes (e.g. release notes)."""
    uncommented = _strip_between(body, _COMMENT_OPEN, _COMMENT_CLOSE)
    return _strip_between(uncommented, _BLOCKQUOTE_OPEN, _BLOCKQUOTE_CLOSE)


@dataclass(frozen=True)
class _Text:
    """The parts of a PR description each check reads."""

    prose: str  # title + author-written lines: no fences, headings, quotes, unchecked boxes
    mentions: str  # prose plus headings: where paths and symbols get named
    links: str  # title + body without comments or fences: closing keywords
    filled_chars: int  # characters of real description in the body


def _parse_text(submission: Submission) -> _Text:
    title = " ".join((submission.title or "")[:MAX_TEXT_CHARS].split())
    body = (submission.body or "")[:MAX_TEXT_CHARS].replace("\r\n", "\n").replace("\r", "\n")
    prose, mentions, links, filled = [title], [title], [title], []
    in_fence = False
    for line in _author_text(body).split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            filled.append(line)
            continue
        links.append(line)
        if _HEADING.match(line):
            mentions.append(line)
        elif _QUOTE.match(line):
            filled.append(line)
        elif not _UNCHECKED.match(line):
            line = _CHECKED.sub(r"\1", line)
            prose.append(line)
            mentions.append(line)
            if not _LABEL_ONLY.match(line):
                filled.append(line)
    return _Text(
        prose="\n".join(prose),
        mentions="\n".join(mentions),
        links="\n".join(links),
        filled_chars=len(" ".join(" ".join(filled).split())),
    )


# --------------------------------------------------------------------------
# Claim detection: stated as done, not negated, planned, hedged or asked
# --------------------------------------------------------------------------

_CLAUSE_BREAK = re.compile(r"[.!?;,:](?=\s|$)|\n")
_SENTENCE_BREAK = re.compile(r"[.!?](?=\s|$)|\n")
_NEGATION = re.compile(
    r"\b(?:no|not|non|never|without|none|nor|cannot|unable|todo|tbd|will|would|should|could|"
    r"may|might|must|need|needs|needed|please|plan|planned|planning|going|if|once|whether|"
    r"already|pending|wip|later)\b|n['’]t\b",
    re.I,
)
_BREAKING_DOUBT = re.compile(
    r"\b(?:not|unsure|unclear|whether|if|maybe|might|possibly|probably|hopefully)\b|n['’]t\b",
    re.I,
)
_AFTERTHOUGHT = re.compile(
    r"\b(?:later|yet|soon|follow[- ]?up|separately|tbd|todo|pending)\b"
    r"|\b(?:in|at)\s+(?:(?:[\w.-]+/[\w.-]+)?#\d|https?://)",
    re.I,
)


def _span(text: str, start: int, end: int, breaks: re.Pattern[str]) -> tuple[int, int]:
    """Bounds of the clause or sentence around text[start:end]."""
    left = max(0, start - _WINDOW)
    for m in breaks.finditer(text, left, start):
        left = m.end()
    stop = min(len(text), end + _WINDOW)
    m = breaks.search(text, end, stop)
    return left, (m.start() if m else stop)


def _first_affirmed(
    text: str,
    patterns: Sequence[re.Pattern[str]],
    doubt: re.Pattern[str] = _NEGATION,
    veto: re.Pattern[str] | None = None,
) -> str | None:
    """First pattern match stated as a fact, as a squashed phrase.

    A match is dropped when its clause is negated or hedged before it ("did
    not add tests"), deferred or asked after it ("tests later", "...?"), or
    when `veto` matches anywhere in its sentence.
    """
    for rx in patterns:
        for m in rx.finditer(text):
            left, right = _span(text, m.start(), m.end(), _CLAUSE_BREAK)
            if doubt.search(text, left, m.start()):
                continue
            if _AFTERTHOUGHT.search(text, m.end(), right) or text.startswith("?", right):
                continue
            if veto is not None:
                s_left, s_right = _span(text, m.start(), m.end(), _SENTENCE_BREAK)
                if veto.search(text, s_left, s_right):
                    continue
            return " ".join(m.group(0).split())
    return None


_TEST_NOUN = (
    r"(?:unit\s+|integration\s+|regression\s+|e2e\s+|end-to-end\s+|snapshot\s+)?"
    r"(?:test\s+cases?|tests?)\b"
    r"(?![-\s]*(?:workflows?|jobs?|matrix|ci\b|pipelines?|runners?|config\w*|dependenc\w*|"
    r"requirements?|commands?|scripts?|env\w*|setup|infra\w*|timeouts?))"
)
_DETERMINERS = (
    r"(?:(?:a|an|some|new|more|the|extra|additional|missing|corresponding|relevant|"
    r"appropriate|matching|existing|its|their|our|few|couple|of|several|multiple|two|three|"
    r"comprehensive|thorough|extensive|basic|simple|proper|necessary|required|dedicated|"
    r"separate|small|minimal|failing)\s+){0,3}"
)
_TEST_CLAIMS = (
    re.compile(
        r"\b(?:add(?:ed|s|ing)?|wr(?:ote|itten|ite|ites|iting)|creat(?:e|ed|es|ing)|"
        r"includ(?:e|ed|es|ing)|introduc(?:e|ed|es|ing)|extend(?:ed|s|ing)?|"
        r"updat(?:e|ed|es|ing)|adjust(?:ed|s|ing)?)\s+" + _DETERMINERS + _TEST_NOUN,
        re.I,
    ),
    re.compile(
        r"\btests?\s+(?:(?:have|has)\s+(?:also\s+)?been\s+|(?:were|was|are|is)\s+(?:also\s+)?)?"
        r"(?:added|updated|included|written|extended|adjusted|provided)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:along\s+with|together\s+with|with\s+(?:new|added|accompanying|corresponding|"
        r"unit|integration|regression))\s+(?:unit\s+|integration\s+|regression\s+)?tests?\b",
        re.I,
    ),
    re.compile(
        r"\b(?:now\s+covered\s+by|covered\s+by\s+(?:the\s+)?(?:new|added|newly\s+added))\s+"
        r"(?:unit\s+|integration\s+|regression\s+)?tests?\b",
        re.I,
    ),
    re.compile(
        r"\b(?:new|additional|extra)\s+(?:unit\s+|integration\s+|regression\s+)?"
        r"(?:test\s+cases?|tests?)\b",
        re.I,
    ),
)

# Tests that live in a source file: Rust #[test] modules, Python doctests,
# Go Test funcs and JS it()/test() blocks added next to the code.
_INLINE_TEST = re.compile(
    r"#\[(?:cfg\()?test\b|^\s*(?:pub\s+)?mod\s+tests?\b|^\s*(?:async\s+)?def\s+test_"
    r"|^\s*func\s+(?:Test|Benchmark|Fuzz)\w*\(|^\s*(?:it|test|describe)\s*\(\s*['\"`]"
    r"|@Test\b|^\s*>>>\s"
)

_DOC_NOUN = (
    r"(?:docs?|documentation|readme|changelog|change\s?log|release\s+notes|user\s+guide|"
    r"man\s+page)\b"
    r"(?!\s*(?:build|site|workflow|ci\b|config\w*|deploy\w*|theme|generat\w*|dependenc\w*))"
)
_DOC_CLAIMS = (
    re.compile(
        r"\b(?:updat(?:e|ed|es|ing)|add(?:ed|s|ing)?|extend(?:ed|s|ing)?|improv(?:e|ed|es|ing)|"
        r"revis(?:e|ed|es|ing)|edit(?:ed|s|ing)?|amend(?:ed|s|ing)?|adjust(?:ed|s|ing)?|"
        r"wr(?:ote|itten|ite|ites|iting)|expand(?:ed|s|ing)?|clarif(?:y|ied|ies|ying))\s+"
        r"(?:(?:the|a|an|some|relevant|corresponding|related|project|api|user|its|our|their|"
        r"accompanying|necessary)\s+){0,2}" + _DOC_NOUN,
        re.I,
    ),
    re.compile(
        r"\b" + _DOC_NOUN + r"\s+(?:(?:has|have)\s+(?:also\s+)?been\s+|(?:was|were|is|are)\s+"
        r"(?:also\s+)?)?(?:updated|added|extended|amended|adjusted|revised|changed|improved|"
        r"written|expanded)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:made|make|making)\s+(?:\w+\s+)?changes?\s+to\s+(?:the\s+)?" + _DOC_NOUN, re.I
    ),
)

# A generic "updated the docs" may mean docstrings or comments; a named
# document (README, changelog) may not.
_NAMED_DOC = re.compile(r"readme|change\s?log|release\s+notes|man\s+page|user\s+guide", re.I)
_COMMENT_LINE = re.compile(r"^\s*(?:#|//|/\*|\*|--|;)|\"\"\"|'''")

_FUNCTIONAL_CLAIMS = (
    re.compile(
        r"\b(?:fix(?:es|ed|ing)?|bug(?:s|fix(?:es)?)?|hotfix|crash(?:es|ed|ing)?|segfault|"
        r"security|vulnerab(?:le|ility|ilities)|perf(?:ormance)?|faster|speed(?:s|ed)?\s+up|"
        r"memory\s+leaks?|leak(?:s|ed|ing)?)\b"
        r"(?!\s*:?\s*(?:[\w.-]+/[\w.-]+)?#\d)(?!\s+https?://)",
        re.I,
    ),
)
_NON_FUNCTIONAL = re.compile(
    r"\b(?:typos?|spelling|grammar|grammatical|docs?|documentation|readme|changelog|wording|"
    r"phrasing|punctuation|comments?|docstrings?|formatting|format|whitespace|indentation|"
    r"style|lint(?:ing|er)?|markdown|links?|translations?|i18n|examples?|badges?|"
    r"license|copyright|contributing|policy|wiki)\b|\.(?:md|rst|txt|adoc)\b",
    re.I,
)

_NON_BREAKING_CLAIMS = (
    re.compile(r"\bno\s+(?:api\s+|public\s+)?breaking(?:\s+(?:api\s+)?changes?)?\b", re.I),
    re.compile(r"\bnot\s+(?:a\s+)?breaking(?:\s+changes?)?\b", re.I),
    re.compile(r"\bnon[- ]?breaking\b", re.I),
    re.compile(r"\b(?:fully\s+)?backwards?[- ]compatible\b", re.I),
    re.compile(r"\bbreaking\s+changes?\s*[:?]\s*(?:no|none|n/?a)\b", re.I),
)

_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s+"
    r"((?:[\w.-]+/[\w.-]+)?#\d+|https://github\.com/[\w.-]+/[\w.-]+/issues/\d+)\b",
    re.I,
)
_ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")


# --------------------------------------------------------------------------
# Mentions of files and symbols
# --------------------------------------------------------------------------

_EXTENSIONS = (
    "py|pyi|pyx|ipynb|js|jsx|mjs|cjs|ts|tsx|mts|cts|vue|svelte|go|rs|java|kt|kts|scala|groovy|"
    "gradle|c|h|cc|cpp|cxx|hpp|hh|cs|rb|php|swift|dart|lua|jl|ex|exs|erl|hrl|hs|clj|cljs|elm|zig|"
    "nim|sh|bash|zsh|fish|ps1|bat|cmd|md|mdx|markdown|rst|txt|adoc|json|jsonc|json5|yaml|yml|"
    "toml|ini|cfg|conf|xml|html|htm|css|scss|sass|less|sql|graphql|gql|proto|tf|hcl|lock|csv|"
    "tsv|cmake|png|jpe?g|gif|svg|ico|webp"
)
_PATH_TOKEN = re.compile(r"[^\s`'\"()<>\[\]{},;|*]+")
_PATH = re.compile(rf"(?:[\w.+~-]+/)*\w[\w.+-]*\.(?:{_EXTENSIONS})", re.I)
_LOCATION_SUFFIX = re.compile(r"(?::\d+(?::\d+)?|#L\d+(?:-L\d+)?)$")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_EXTERNAL_DIRS = frozenset(
    {"site-packages", "dist-packages", "node_modules", ".venv", "venv", "__pycache__", ".tox"}
)
_PRODUCT_NAMES = frozenset({
    "node.js", "vue.js", "next.js", "nuxt.js", "express.js", "d3.js", "chart.js", "three.js",
    "react.js", "angular.js", "ember.js", "p5.js", "nest.js", "moment.js", "day.js",
})  # fmt: skip

_CODE_SPAN = re.compile(r"`([^`\n]{1,120})`")
_SYMBOL = re.compile(r"[A-Za-z_$][\w$]*(?:(?:\.|::|->|#)[A-Za-z_$][\w$]*)*")
_CALL_SUFFIX = re.compile(r"\([^()]{0,80}\)$")
_SEPARATORS = re.compile(r"\.|::|->|#")
_LOWER_CAMEL = re.compile(r"[a-z]{2,}[A-Z][a-z]")
_HUMP = re.compile(r"[A-Z][a-z]+")
_ACRONYM = re.compile(r"[A-Z0-9]{1,5}")
_KEYWORDS = frozenset({
    "true", "false", "null", "none", "nil", "undefined", "nan", "self", "this", "cls", "super",
    "return", "import", "from", "def", "class", "function", "const", "let", "var", "async",
    "await", "yield", "if", "else", "elif", "for", "while", "try", "except", "catch", "finally",
    "raise", "throw", "new", "delete", "in", "is", "not", "and", "or", "pass", "break",
    "continue", "lambda", "with", "static", "public", "private", "void", "int", "str", "string",
    "bool", "float", "dict", "list", "fn", "pub", "mut", "impl", "struct", "enum", "type",
    "interface", "export", "default",
})  # fmt: skip
_TLDS = frozenset({"com", "org", "net", "io", "dev", "ai", "app", "co", "edu", "gov", "info"})


@dataclass(frozen=True)
class _Symbol:
    name: str  # as written, without call parentheses or a leading "@"
    last: str  # the part searched for: `Client.fetch` -> `fetch`
    code_shaped: bool  # unmistakably an identifier, so absence can count against the PR


def _path_mentions(text: str) -> list[str]:
    """Repository-relative file paths named in the text, in order of appearance."""
    found: dict[str, None] = {}
    for m in _PATH_TOKEN.finditer(text):
        token = m.group(0).rstrip(".:!?")
        if len(token) > _MAX_TOKEN_CHARS or "://" in token or "@" in token:
            continue
        if token.startswith(("/", "~", "www.")) or _DRIVE.match(token):
            continue
        token = _LOCATION_SUFFIX.sub("", token)
        if not _PATH.fullmatch(token) or token.lower() in _PRODUCT_NAMES:
            continue
        path = normalize_path(token)
        while path.startswith("../"):
            path = path[3:]
        if path and not _EXTERNAL_DIRS.intersection(path.split("/")):
            found.setdefault(path, None)
            if len(found) >= MAX_MENTIONS:
                break
    return list(found)


def _code_shaped(name: str, called: bool) -> bool:
    core = name.strip("_$")
    return (
        called
        or _SEPARATORS.search(name) is not None
        or "_" in core
        or _LOWER_CAMEL.search(name) is not None
        or len(_HUMP.findall(name)) >= 3
    )


def _symbol_mentions(text: str) -> list[_Symbol]:
    """Backticked identifiers in the text, in order of appearance."""
    found: dict[str, _Symbol] = {}
    for m in _CODE_SPAN.finditer(text):
        raw = m.group(1).strip().lstrip("@").rstrip(":;,")
        name = _CALL_SUFFIX.sub("", raw)
        if not _SYMBOL.fullmatch(name) or name in found or _PATH.fullmatch(name):
            continue
        last = _SEPARATORS.split(name)[-1]
        if len(name) < 2 or name.lower() in _KEYWORDS or last.lower() in _TLDS:
            continue
        if _ACRONYM.fullmatch(name):
            continue
        found[name] = _Symbol(name, last, _code_shaped(name, called=name != raw))
        if len(found) >= MAX_MENTIONS:
            break
    return list(found.values())


# --------------------------------------------------------------------------
# Public definitions removed by the diff
# --------------------------------------------------------------------------

_PY_DEF = re.compile(r"^([ \t]*)(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)")
_PY_CLASS = re.compile(r"^[ \t]*class\s+([A-Za-z_]\w*)")
_JS_EXPORT = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:abstract\s+)?"
    r"(?:function\s*\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)"
)
_GO_DEF = re.compile(r"^(?:func\s+(?:\([^)]*\)\s*)?|type\s+)([A-Z]\w*)")
_RUST_PUB = re.compile(
    r"^\s*pub\s+(?:(?:async|const|unsafe)\s+)*(?:extern\s+\"[^\"]*\"\s+)?"
    r"(?:fn|struct|enum|trait|type)\s+([A-Za-z_]\w*)"
)
_ALIAS = re.compile(r"^([A-Za-z_$][\w$]*)\s*(?::[^=]*)?=(?!=)")
_REEXPORT = re.compile(r"^\s*(?:from\s+\S+\s+import|import|export|pub\s+use|use)\b")
_WORD = re.compile(r"[A-Za-z_$][\w$]*")
_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".ts": "js",
    ".tsx": "js",
    ".mts": "js",
    ".cts": "js",
    ".go": "go",
    ".rs": "rust",
}
_NON_API_DIRS = frozenset(
    {"examples", "example", "scripts", "script", "benchmarks", "bench", "tools", "internal"}
)
_DEF_PATTERNS = {"js": _JS_EXPORT, "go": _GO_DEF, "rust": _RUST_PUB}


def _language(path: str) -> str | None:
    return _LANGUAGES.get(PurePosixPath(path).suffix.lower())


def _is_api_file(fd: _FileDiff) -> bool:
    if fd.category != "code" or _language(fd.path) is None:
        return False
    parts = fd.path.split("/")
    if _NON_API_DIRS.intersection(parts[:-1]) or parts[0].startswith("."):
        return False
    return not any(p.startswith("_") and p != "__init__.py" for p in parts)


def _width(text: str) -> int:
    stripped = text.lstrip(" \t")
    return len(text[: len(text) - len(stripped)].expandtabs(4))


def _python_owner(hunk: Hunk, index: int, width: int) -> str | None:
    """Class enclosing the removed definition at hunk.lines[index], if any.

    Walks back over the old side of the hunk; falls back to the hunk heading
    (git's enclosing top-level line) for a definition indented one level.
    Returns None for nested functions and anything it cannot place.
    """
    for line in reversed(hunk.lines[:index]):
        if line.kind == "+" or not line.text.strip():
            continue
        if _width(line.text) < width:
            cls = _PY_CLASS.match(line.text)
            return cls.group(1) if cls else None
    cls = _PY_CLASS.match(hunk.heading)
    return cls.group(1) if cls and width <= 4 else None


def _removed_python_names(fd: _FileDiff) -> list[tuple[str, str, DiffLine]]:
    """(qualified name, bare name, line) of public Python defs/classes removed."""
    names: list[tuple[str, str, DiffLine]] = []
    for hunk in fd.hunks:
        for i, line in enumerate(hunk.lines):
            m = _PY_DEF.match(line.text) if line.kind == "-" else None
            if not m:
                continue
            width = _width(line.text)
            if width == 0:
                names.append((m.group(2), m.group(2), line))
                continue
            owner = _python_owner(hunk, i, width)
            if owner and not owner.startswith("_"):
                names.append((f"{owner}.{m.group(2)}", m.group(2), line))
    return names


def _removed_public_names(fd: _FileDiff) -> list[tuple[str, str, DiffLine]]:
    language = _language(fd.path)
    if language == "python":
        return _removed_python_names(fd)
    rx = _DEF_PATTERNS.get(language or "")
    if rx is None:
        return []
    found: list[tuple[str, str, DiffLine]] = []
    for line in fd.removed:
        m = rx.match(line.text)
        if m:
            found.append((m.group(1), m.group(1), line))
    return found


def _names_still_provided(files: Sequence[_FileDiff]) -> set[str]:
    """Names the added lines define, alias, import or re-export anywhere in the PR."""
    names: set[str] = set()
    for fd in files:
        for line in fd.added:
            text = line.text
            for rx in (_PY_DEF, _JS_EXPORT, _GO_DEF, _RUST_PUB):
                m = rx.match(text)
                if m:
                    names.add(m.group(m.lastindex or 1))
            alias = _ALIAS.match(text)
            if alias:
                names.add(alias.group(1))
            if _REEXPORT.match(text):
                names.update(_WORD.findall(text))
    return names


# --------------------------------------------------------------------------
# Whitespace-only changes
# --------------------------------------------------------------------------

_INDENT_SENSITIVE = frozenset(
    {".py", ".pyi", ".pyx", ".yaml", ".yml", ".coffee", ".sass", ".styl", ".pug", ".haml"}
    | {".slim", ".nim", ".hs", ".lhs", ".elm", ".fs", ".fsx", ".mk"}
)
_MAKEFILES = frozenset({"makefile", "gnumakefile"})
_CODE_TOKEN = re.compile(
    r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\s+|[^\s\"'`]+|[\"'`]"
)


def _indent_sensitive(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix.lower() in _INDENT_SENSITIVE or p.name.lower() in _MAKEFILES


def _canonical(text: str, keep_indent: bool) -> str:
    """The line without whitespace, except in string literals and meaningful indentation."""
    body = "".join(tok for tok in _CODE_TOKEN.findall(text.strip()) if not tok.isspace())
    if not body:
        return ""
    return text[: len(text) - len(text.lstrip())] + body if keep_indent else body


def _whitespace_only(fd: _FileDiff) -> bool:
    """True only when we positively see a modification that changes nothing but whitespace."""
    if fd.status not in ("modified", "changed") or not (fd.added or fd.removed):
        return False
    keep = _indent_sensitive(fd.path)
    before = Counter(c for line in fd.removed if (c := _canonical(line.text, keep)))
    after = Counter(c for line in fd.added if (c := _canonical(line.text, keep)))
    return before == after


# --------------------------------------------------------------------------
# Evidence and claim links
# --------------------------------------------------------------------------


def _pr_url(submission: Submission) -> str | None:
    url = (submission.url or "").rstrip("/")
    if re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", url):
        return url
    if submission.number and re.fullmatch(r"[\w.-]+/[\w.-]+", submission.repo or ""):
        return f"https://github.com/{submission.repo}/pull/{submission.number}"
    return None


def _diff_url(pr_url: str | None, path: str, side: str = "", line: int | None = None) -> str | None:
    """Link into the PR's "Files changed" tab (GitHub anchors files by SHA-256 of the path)."""
    if pr_url is None:
        return None
    anchor = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return f"{pr_url}/files#diff-{anchor}{side}{line}" if line else f"{pr_url}/files#diff-{anchor}"


def _clip(text: str, limit: int = _MAX_LINE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _quote(names: Sequence[str]) -> str:
    shown = ", ".join(f"`{n}`" for n in names[:_MAX_LISTED])
    extra = len(names) - _MAX_LISTED
    return f"{shown} and {extra} more" if extra > 0 else shown


def _diff_evidence(
    fd: _FileDiff, lines: Sequence[DiffLine], description: str, pr_url: str | None
) -> Evidence:
    shown = list(lines[:MAX_EXCERPT_LINES])
    numbers = [line.new_line for line in shown if line.new_line is not None]
    side = "R"
    if not numbers:
        numbers = [line.old_line for line in shown if line.old_line is not None]
        side = "L"
    start, end = (min(numbers), max(numbers)) if numbers else (None, None)
    return Evidence(
        kind=EvidenceKind.DIFF,
        description=description,
        path=fd.path,
        line_start=start,
        line_end=end,
        excerpt="\n".join(_clip(f"{line.kind}{line.text}") for line in shown) or None,
        url=_diff_url(pr_url, fd.path, side, start),
    )


def _changes_excerpt(fd: _FileDiff) -> list[DiffLine]:
    return [line for line in fd.lines if line.kind != " "][:MAX_EXCERPT_LINES]


def _added_matching(
    files: Iterable[_FileDiff], rx: re.Pattern[str]
) -> list[tuple[_FileDiff, list[DiffLine]]]:
    """Per file, the added lines matching `rx` (files without a match are left out)."""
    found = []
    for fd in files:
        lines = [line for line in fd.added if rx.search(line.text)]
        if lines:
            found.append((fd, lines))
    return found


def _file_list_evidence(files: Sequence[_FileDiff], description: str) -> Evidence:
    rows = [
        f"{(fd.status[:1] or 'm').upper()} {fd.path} ({fd.category}, +{fd.additions} -{fd.deletions})"
        for fd in files[:MAX_EXCERPT_LINES]
    ]
    if len(files) > MAX_EXCERPT_LINES:
        rows.append(f"… and {len(files) - MAX_EXCERPT_LINES} more")
    return Evidence(kind=EvidenceKind.DIFF, description=description, excerpt="\n".join(rows))


_EVIDENCE_KIND = {"code": EvidenceKind.CODE, "doc": EvidenceKind.DOC, "test": EvidenceKind.TEST}


def _ws_evidence(ws: Workspace, path: str, line: int, description: str) -> list[Evidence]:
    """Workspace lines as evidence; nothing for binary, empty or out-of-range citations."""
    lines = ws.lines(path)
    if not lines or not 1 <= line <= len(lines):
        return []
    return [
        Evidence(
            kind=_EVIDENCE_KIND[classify_path(path)],
            description=description,
            path=path,
            line_start=line,
            line_end=line,
            excerpt=ws.excerpt(path, line),
            url=ws.permalink(path, line),
        )
    ]


def _term_pattern(terms: Iterable[str]) -> re.Pattern[str] | None:
    unique = sorted({t for t in terms if t}, key=lambda t: (-len(t), t))
    if not unique:
        return None
    return re.compile(
        r"(?<![\w$])(?:" + "|".join(re.escape(t) for t in unique) + r")(?![\w$])", re.I
    )


def _claims_about(claims: Sequence[Claim], terms: Iterable[str]) -> list[str]:
    """Ids of CHANGE claims whose text, quote or entities name one of `terms`."""
    rx = _term_pattern(terms)
    if rx is None:
        return []
    return [
        c.id
        for c in claims
        if c.type is ClaimType.CHANGE
        and rx.search("\n".join([c.text, c.quote, *c.entities])) is not None
    ]


def _claims_naming(claims: Sequence[Claim], terms: Iterable[str]) -> list[str]:
    """Ids of claims of any type whose text, quote or entities name one of `terms`."""
    rx = _term_pattern(terms)
    if rx is None:
        return []
    return [c.id for c in claims if rx.search("\n".join([c.text, c.quote, *c.entities]))]


_TEST_TERMS = ("test", "tests", "testing", "test case", "test cases", "unit test", "unit tests")
_DOC_TERMS = ("doc", "docs", "documentation", "readme", "changelog", "release notes")
_FIX_TERMS = (
    "fix", "fixes", "fixed", "bug", "crash", "security", "vulnerability",
    "performance", "faster", "memory leak", "leak",
)  # fmt: skip
_BREAKING_TERMS = ("breaking", "non-breaking", "backwards compatible", "backward compatible")


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


@dataclass
class _Context:
    submission: Submission
    text: _Text
    files: list[_FileDiff]
    claims: list[Claim]
    ws: Workspace | None
    pr_url: str | None
    _grep_cache: dict[str, tuple[str, int] | None] = field(default_factory=dict)

    @cached_property
    def patches_incomplete(self) -> bool:
        return any(fd.patch_missing for fd in self.files)

    @cached_property
    def functional_claim(self) -> str | None:
        return _first_affirmed(self.text.prose, _FUNCTIONAL_CLAIMS, veto=_NON_FUNCTIONAL)

    @cached_property
    def ws_basenames(self) -> frozenset[str]:
        if self.ws is None:
            return frozenset()
        return frozenset(f.rsplit("/", 1)[-1].casefold() for f in self.ws.files)

    def grep_word(self, word: str) -> tuple[str, int] | None:
        """A (path, line) in the workspace containing `word` as a whole word, code first."""
        if self.ws is None:
            return None
        if word not in self._grep_cache:
            hits = self.ws.grep(word, fixed=True, word=True, max_hits=_MAX_GREP_HITS)
            best = next((h for h in hits if classify_path(h.path) == "code"), None)
            best = best or (hits[0] if hits else None)
            self._grep_cache[word] = (best.path, best.line) if best else None
        return self._grep_cache[word]


def _check(
    check_id: str,
    title: str,
    status: CheckStatus,
    signal: Signal = Signal.NEUTRAL,
    strength: float = 0.0,
    detail: str = "",
    claim_ids: Sequence[str] = (),
    evidence: Sequence[Evidence] = (),
) -> Check:
    return Check(
        id=check_id,
        title=title,
        status=status,
        signal=signal,
        strength=strength,
        detail=detail,
        claim_ids=list(claim_ids),
        evidence=list(evidence[:_MAX_EVIDENCE]),
    )


def _check_diff(ctx: _Context) -> list[Check]:
    if not ctx.files:
        return [
            _check(
                "pr.diff",
                "The diff of this pull request is unavailable, so only its description was checked.",
                CheckStatus.SKIPPED,
                detail="diff unavailable: no changed files were provided",
            )
        ]
    hidden = [fd for fd in ctx.files if fd.patch_missing]
    if not hidden:
        return []
    return [
        _check(
            "pr.diff",
            f"GitHub returned no patch for {len(hidden)} of {len(ctx.files)} changed files; "
            "line-level checks cover only the rest.",
            CheckStatus.INFO,
            detail="Patches are omitted for very large or binary files: "
            + _quote([fd.path for fd in hidden]),
            evidence=[_file_list_evidence(hidden, "changed files without a patch")],
        )
    ]


def _check_description(ctx: _Context) -> list[Check]:
    chars = ctx.text.filled_chars
    if chars >= MIN_DESCRIPTION_CHARS:
        return []
    return [
        _check(
            "pr.description",
            "The pull request has no real description.",
            CheckStatus.WARN,
            Signal.MISSING_INFO,
            0.4,
            detail=f"Only {chars} characters remain once HTML comments, unfilled template "
            f"headings and unchecked boxes are removed (minimum {MIN_DESCRIPTION_CHARS}).",
        )
    ]


def _check_size(ctx: _Context) -> list[Check]:
    lines = sum(fd.changed_lines for fd in ctx.files)
    if lines <= LARGE_PR_LINES and len(ctx.files) <= LARGE_PR_FILES:
        return []
    return [
        _check(
            "pr.size",
            f"Large PR ({len(ctx.files)} files, {lines} changed lines); review in parts.",
            CheckStatus.INFO,
            detail="large PR; review in parts. Size alone says nothing about validity.",
        )
    ]


def _check_tests_claim(ctx: _Context) -> list[Check]:
    phrase = _first_affirmed(ctx.text.prose, _TEST_CLAIMS)
    if phrase is None:
        return []
    claim_ids = _claims_about(ctx.claims, _TEST_TERMS)
    tests = [fd for fd in ctx.files if fd.category == "test"]
    inline = [] if tests else _added_matching(ctx.files, _INLINE_TEST)
    if inline:
        return [
            _check(
                "pr.tests_claim",
                "The claimed tests are in the diff, inside source files.",
                CheckStatus.PASS,
                Signal.SUPPORTS,
                0.3,
                detail=f'The description says "{phrase}"; test code is added in '
                + _quote([fd.path for fd, _ in inline]),
                claim_ids=claim_ids,
                evidence=[
                    _diff_evidence(fd, lines, "test code added", ctx.pr_url) for fd, lines in inline
                ],
            )
        ]
    if not tests:
        return [
            _check(
                "pr.tests_claim",
                "The description claims tests, but no test file is changed.",
                CheckStatus.FAIL,
                Signal.CONTRADICTS,
                0.6,
                detail=f'The description says "{phrase}", but none of the {len(ctx.files)} '
                "changed files is a test file.",
                claim_ids=claim_ids,
                evidence=[_file_list_evidence(ctx.files, "files changed by this PR")],
            )
        ]
    return [
        _check(
            "pr.tests_claim",
            f"The claimed tests are in the diff ({len(tests)} test file(s) changed).",
            CheckStatus.PASS,
            Signal.SUPPORTS,
            0.3,
            detail=f'The description says "{phrase}"; changed test files: '
            + _quote([fd.path for fd in tests]),
            claim_ids=claim_ids,
            evidence=[
                _diff_evidence(fd, _changes_excerpt(fd), "test file changed", ctx.pr_url)
                for fd in tests
            ],
        )
    ]


def _check_docs_claim(ctx: _Context) -> list[Check]:
    phrase = _first_affirmed(ctx.text.prose, _DOC_CLAIMS)
    if phrase is None:
        return []
    claim_ids = _claims_about(ctx.claims, _DOC_TERMS)
    docs = [fd for fd in ctx.files if fd.is_doc]
    comments = (
        [] if docs or _NAMED_DOC.search(phrase) else _added_matching(ctx.files, _COMMENT_LINE)
    )
    if comments:
        return [
            _check(
                "pr.docs_claim",
                "No documentation file is changed; the claimed docs may be docstrings or comments.",
                CheckStatus.INFO,
                detail=f'The description says "{phrase}"; comment or docstring lines are added in '
                + _quote([fd.path for fd, _ in comments]),
                claim_ids=claim_ids,
                evidence=[
                    _diff_evidence(fd, lines, "comment or docstring added", ctx.pr_url)
                    for fd, lines in comments
                ],
            )
        ]
    if not docs:
        return [
            _check(
                "pr.docs_claim",
                "The description claims documentation changes, but no doc file is changed.",
                CheckStatus.FAIL,
                Signal.CONTRADICTS,
                0.6,
                detail=f'The description says "{phrase}", but none of the {len(ctx.files)} '
                "changed files is documentation (README, docs/, changelog, *.md, *.rst, ...).",
                claim_ids=claim_ids,
                evidence=[_file_list_evidence(ctx.files, "files changed by this PR")],
            )
        ]
    real = [fd for fd in docs if not _whitespace_only(fd)]
    if not real:
        return [
            _check(
                "pr.docs_claim",
                "Documentation claim backed only by whitespace changes.",
                CheckStatus.WARN,
                Signal.CONTRADICTS,
                0.45,
                detail=f'The description says "{phrase}", but every changed doc file only '
                "changes whitespace: " + _quote([fd.path for fd in docs]),
                claim_ids=claim_ids,
                evidence=[
                    _diff_evidence(fd, _changes_excerpt(fd), "whitespace-only change", ctx.pr_url)
                    for fd in docs
                ],
            )
        ]
    return [
        _check(
            "pr.docs_claim",
            f"The claimed documentation changes are in the diff ({len(real)} doc file(s)).",
            CheckStatus.PASS,
            Signal.SUPPORTS,
            0.15,
            detail=f'The description says "{phrase}"; changed doc files: '
            + _quote([fd.path for fd in real]),
            claim_ids=claim_ids,
            evidence=[
                _diff_evidence(fd, _changes_excerpt(fd), "documentation changed", ctx.pr_url)
                for fd in real
            ],
        )
    ]


def _check_trivial_change(ctx: _Context) -> list[Check]:
    phrase = ctx.functional_claim
    if phrase is None:
        return []
    code = [fd for fd in ctx.files if not fd.is_doc]
    if code and not all(_whitespace_only(fd) for fd in code):
        return []
    what = "whitespace" if code else "documentation"
    shown = code or ctx.files
    return [
        _check(
            "pr.trivial_change",
            "The description claims a functional fix but the diff only touches whitespace/docs.",
            CheckStatus.WARN,
            Signal.CONTRADICTS,
            0.45,
            detail=f'The description says "{phrase}", but every change in the diff is {what} '
            f"({len(ctx.files)} file(s): {_quote([fd.path for fd in ctx.files])}).",
            claim_ids=_claims_about(ctx.claims, _FIX_TERMS),
            evidence=[
                _diff_evidence(fd, _changes_excerpt(fd), f"{what}-only change", ctx.pr_url)
                for fd in shown
            ],
        )
    ]


def _check_breaking_change(ctx: _Context) -> list[Check]:
    phrase = _first_affirmed(ctx.text.prose, _NON_BREAKING_CLAIMS, doubt=_BREAKING_DOUBT)
    if phrase is None:
        return []
    still = _names_still_provided(ctx.files)
    gone: dict[str, list[DiffLine]] = {}
    names: dict[str, None] = {}
    for fd in ctx.files:
        if not _is_api_file(fd):
            continue
        for qualified, bare, line in _removed_public_names(fd):
            if bare not in still:
                gone.setdefault(fd.path, []).append(line)
                names.setdefault(qualified, None)
    if not names:
        return []
    listed = list(names)
    by_path = {fd.path: fd for fd in ctx.files}
    return [
        _check(
            "pr.breaking_change",
            f"The description says there is no breaking change, but public names are removed: "
            f"{_quote(listed)}.",
            CheckStatus.FAIL,
            Signal.CONTRADICTS,
            0.6,
            detail=f'The description says "{phrase}", yet the diff removes {len(listed)} public '
            f"definition(s) without re-adding, aliasing or re-exporting them: {_quote(listed)}.",
            claim_ids=_claims_about(ctx.claims, (*_BREAKING_TERMS, *names)),
            evidence=[
                _diff_evidence(by_path[path], lines, "public definition removed", ctx.pr_url)
                for path, lines in gone.items()
            ],
        )
    ]


def _changed_file_for(mention: str, files: Sequence[_FileDiff]) -> _FileDiff | None:
    folded = mention.casefold()
    for fd in files:
        for path in (fd.path, fd.previous_path):
            p = (path or "").casefold()
            if p and (p == folded or p.endswith("/" + folded) or folded.endswith("/" + p)):
                return fd
    return None


def _repo_file_for(mention: str, ctx: _Context) -> str | None:
    """A workspace file the mention plausibly refers to, else None."""
    ws = ctx.ws
    if ws is None:
        return None
    exact = ws.resolve(mention)
    if exact:
        return exact
    candidates = sorted(ws.candidates(mention))
    if candidates:
        return candidates[0]
    base = mention.rsplit("/", 1)[-1].casefold()
    if base in ctx.ws_basenames:
        return next(f for f in ws.files if f.rsplit("/", 1)[-1].casefold() == base)
    return None


def _could_be_fabricated(mention: str, ctx: _Context) -> bool:
    """Absent from diff and tree: count it against the PR unless it is plausibly not a file.

    Bare capitalised "Name.js" tokens are usually product names, and a name the
    repository's own text refers to (ignored, generated or downloaded files) is real.
    """
    if ctx.ws is None:
        return False
    stem, _, ext = mention.rpartition(".")
    if "/" not in mention and ext.lower() == "js" and stem[:1].isupper():
        return False
    return ctx.grep_word(mention.rsplit("/", 1)[-1]) is None


def _check_mentioned_files(ctx: _Context) -> list[Check]:
    mentions = _path_mentions(ctx.text.mentions)
    changed: list[tuple[str, _FileDiff]] = []
    present: list[tuple[str, str]] = []
    unknown: list[str] = []
    missing: list[str] = []
    for mention in mentions:
        fd = _changed_file_for(mention, ctx.files)
        repo_path = None if fd else _repo_file_for(mention, ctx)
        if fd:
            changed.append((mention, fd))
        elif repo_path:
            present.append((mention, repo_path))
        elif _could_be_fabricated(mention, ctx):
            missing.append(mention)
        else:
            unknown.append(mention)
    checks: list[Check] = []
    if changed:
        checks.append(_files_changed_check(ctx, changed))
    if missing:
        checks.append(_files_missing_check(ctx, missing))
    if present and ctx.ws is not None:
        checks.append(_files_present_check(ctx, ctx.ws, present))
    if unknown:
        checks.append(_files_unknown_check(ctx, unknown))
    return checks


def _files_changed_check(ctx: _Context, changed: list[tuple[str, _FileDiff]]) -> Check:
    names = [m for m, _ in changed]
    return _check(
        "pr.mentioned_files",
        f"Files named in the description are changed by this PR: {_quote(names)}.",
        CheckStatus.PASS,
        Signal.SUPPORTS,
        0.15,
        claim_ids=_claims_about(ctx.claims, _path_terms(names)),
        evidence=[
            _diff_evidence(fd, _changes_excerpt(fd), f"`{m}` is changed ({fd.status})", ctx.pr_url)
            for m, fd in changed
        ],
    )


def _files_missing_check(ctx: _Context, missing: list[str]) -> Check:
    which = "which is" if len(missing) == 1 else "which are"
    return _check(
        "pr.mentioned_files",
        f"The description mentions {_quote(missing)}, {which} neither changed nor present "
        "in the repository.",
        CheckStatus.FAIL,
        Signal.CONTRADICTS,
        0.5,
        detail="No changed file, tracked file or repository text matches " + _quote(missing) + ".",
        claim_ids=_claims_about(ctx.claims, _path_terms(missing)),
        evidence=[_file_list_evidence(ctx.files, "files changed by this PR")],
    )


def _files_present_check(ctx: _Context, ws: Workspace, present: list[tuple[str, str]]) -> Check:
    names = [m for m, _ in present]
    verb = "is" if len(names) == 1 else "are"
    return _check(
        "pr.mentioned_files",
        f"{_quote(names)} {verb} named as changed but {verb} not touched by this PR.",
        CheckStatus.WARN,
        Signal.CONTRADICTS,
        0.25,
        detail="These files exist in the repository but the diff does not change them; "
        "check that the description matches the change.",
        claim_ids=_claims_naming(ctx.claims, _path_terms(names)),
        evidence=[
            ev
            for m, path in present
            for ev in _ws_evidence(ws, path, 1, f"`{m}` in the repository")
        ],
    )


def _files_unknown_check(ctx: _Context, unknown: list[str]) -> Check:
    why = (
        "no repository checkout was available to look them up"
        if ctx.ws is None
        else "they are referenced in the repository or look like product names, not files"
    )
    return _check(
        "pr.mentioned_files",
        f"Files named in the description are not changed by this PR: {_quote(unknown)}.",
        CheckStatus.INFO,
        detail=f"Not counted against the PR: {why}.",
        claim_ids=_claims_about(ctx.claims, _path_terms(unknown)),
    )


def _path_terms(paths: Iterable[str]) -> list[str]:
    return [t for p in paths for t in (p, p.rsplit("/", 1)[-1])]


@dataclass(frozen=True)
class _DiffHit:
    fd: _FileDiff
    lines: tuple[DiffLine, ...]  # empty when only a path or hunk heading matched


def _is_ident_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch in "_$")


def _find_word(word: str, text: str) -> int:
    """Index of `word` in `text` as a whole identifier, or -1.

    str.find plus a boundary test is far faster than a lookbehind regex on
    multi-megabyte diffs.
    """
    end = len(word)
    i = text.find(word)
    while i >= 0:
        if not _is_ident_char(text[i - 1 : i]) and not _is_ident_char(text[i + end : i + end + 1]):
            return i
        i = text.find(word, i + 1)
    return -1


def _find_in_diff(symbol: _Symbol, files: Sequence[_FileDiff]) -> _DiffHit | None:
    """Where the diff (lines, hunk headings, then paths) touches the symbol."""
    word = symbol.last
    for fd in files:
        at = _find_word(word, fd.blob)
        if at >= 0:
            first = fd.blob.count("\n", 0, at)
            hits = islice((ln for ln in fd.lines[first:] if _find_word(word, ln.text) >= 0), 3)
            return _DiffHit(fd, tuple(hits))
    for fd in files:
        if _find_word(word, fd.path) >= 0 or any(
            _find_word(word, h.heading) >= 0 for h in fd.hunks
        ):
            return _DiffHit(fd, ())
    return None


def _check_mentioned_symbols(ctx: _Context) -> list[Check]:
    touched: list[tuple[_Symbol, _DiffHit]] = []
    untouched: list[tuple[_Symbol, tuple[str, int]]] = []
    absent: list[_Symbol] = []
    unsure: list[_Symbol] = []
    for symbol in _symbol_mentions(ctx.text.mentions):
        hit = _find_in_diff(symbol, ctx.files)
        if hit:
            touched.append((symbol, hit))
            continue
        if not symbol.code_shaped:
            continue  # a plain backticked word: only its presence in the diff is informative
        if ctx.patches_incomplete or ctx.ws is None:
            unsure.append(symbol)
            continue
        where = ctx.grep_word(symbol.last)
        if where:
            untouched.append((symbol, where))
        else:
            absent.append(symbol)
    checks: list[Check] = []
    if touched:
        checks.append(_symbols_touched_check(ctx, touched))
    if absent:
        checks.append(_symbols_absent_check(ctx, absent))
    if untouched and ctx.ws is not None:
        checks.append(_symbols_untouched_check(ctx, ctx.ws, untouched))
    if unsure:
        checks.append(_symbols_unsure_check(ctx, unsure))
    return checks


def _symbol_terms(symbols: Iterable[_Symbol]) -> list[str]:
    return [t for s in symbols for t in (s.name, s.last)]


def _symbols_touched_check(ctx: _Context, touched: list[tuple[_Symbol, _DiffHit]]) -> Check:
    names = [s.name for s, _ in touched]
    evidence = [
        _diff_evidence(hit.fd, hit.lines, f"`{s.name}` in the diff", ctx.pr_url)
        if hit.lines
        else Evidence(
            kind=EvidenceKind.DIFF,
            description=f"`{s.name}` names a changed file or hunk",
            path=hit.fd.path,
            url=_diff_url(ctx.pr_url, hit.fd.path),
        )
        for s, hit in touched
    ]
    return _check(
        "pr.mentioned_symbols",
        f"Identifiers named in the description appear in the diff: {_quote(names)}.",
        CheckStatus.PASS,
        Signal.SUPPORTS,
        0.2,
        claim_ids=_claims_about(ctx.claims, _symbol_terms(s for s, _ in touched)),
        evidence=evidence,
    )


def _symbols_absent_check(ctx: _Context, absent: list[_Symbol]) -> Check:
    names = [s.name for s in absent]
    return _check(
        "pr.mentioned_symbols",
        f"The description names {_quote(names)}, which appear neither in the diff nor in "
        "the repository.",
        CheckStatus.FAIL,
        Signal.CONTRADICTS,
        0.5,
        detail="Searched every added, removed and context line of the diff and every tracked "
        "file for " + _quote([s.last for s in absent]) + ".",
        claim_ids=_claims_about(ctx.claims, _symbol_terms(absent)),
        evidence=[_file_list_evidence(ctx.files, "files changed by this PR")],
    )


def _symbols_untouched_check(
    ctx: _Context, ws: Workspace, untouched: list[tuple[_Symbol, tuple[str, int]]]
) -> Check:
    names = [s.name for s, _ in untouched]
    return _check(
        "pr.mentioned_symbols",
        f"{_quote(names)} mentioned but not touched by this PR.",
        CheckStatus.WARN,
        Signal.CONTRADICTS,
        0.2,
        detail="These identifiers exist in the repository but no line of the diff contains "
        "them; check that the description matches the change.",
        claim_ids=_claims_naming(ctx.claims, _symbol_terms(s for s, _ in untouched)),
        evidence=[
            ev
            for s, (path, line) in untouched
            for ev in _ws_evidence(ws, path, line, f"`{s.name}` in the repository")
        ],
    )


def _symbols_unsure_check(ctx: _Context, unsure: list[_Symbol]) -> Check:
    names = [s.name for s in unsure]
    why = (
        "GitHub omitted the patch of at least one changed file, so absence from the visible "
        "diff proves nothing"
        if ctx.patches_incomplete
        else "no repository checkout was available to look them up"
    )
    return _check(
        "pr.mentioned_symbols",
        f"Could not verify {_quote(names)}: not in the visible diff.",
        CheckStatus.INFO,
        detail=f"Not counted against the PR: {why}.",
        claim_ids=_claims_about(ctx.claims, _symbol_terms(unsure)),
    )


def _linked_issues(text: str, repo: str) -> list[str]:
    found: dict[str, None] = {}
    for m in _CLOSING.finditer(text):
        ref = m.group(1)
        url = _ISSUE_URL.fullmatch(ref)
        if url:
            same = url.group(1).casefold() == (repo or "").casefold()
            ref = f"#{url.group(2)}" if same else f"{url.group(1)}#{url.group(2)}"
        found.setdefault(ref, None)
    return list(found)


def _check_linked_issue(ctx: _Context) -> list[Check]:
    issues = _linked_issues(ctx.text.links, ctx.submission.repo)
    if issues:
        return [
            _check(
                "pr.linked_issue",
                f"Links issue(s) {', '.join(issues[:_MAX_LISTED])}.",
                CheckStatus.INFO,
                detail="Closing keywords (fixes/closes/resolves) found in the description.",
                claim_ids=_claims_about(ctx.claims, issues),
            )
        ]
    if ctx.functional_claim is None:
        return []
    return [
        _check(
            "pr.linked_issue",
            "No linked issue for the claimed fix.",
            CheckStatus.INFO,
            detail=f'The description claims a fix ("{ctx.functional_claim}") but does not link '
            "an issue with fixes/closes/resolves #N.",
        )
    ]


_FULL_CHECKS: tuple[Callable[[_Context], list[Check]], ...] = (
    _check_diff,
    _check_description,
    _check_size,
    _check_tests_claim,
    _check_docs_claim,
    _check_trivial_change,
    _check_breaking_change,
    _check_mentioned_files,
    _check_mentioned_symbols,
    _check_linked_issue,
)
_DESCRIPTION_ONLY_CHECKS: tuple[Callable[[_Context], list[Check]], ...] = (
    _check_diff,
    _check_description,
    _check_linked_issue,
)


def run_pr_checks(submission: Submission, claims: list[Claim], ws: Workspace | None) -> list[Check]:
    """Compare a pull request's description with its diff; stable order, empty for issues.

    Without changed files only the description checks run. `ws` (the repository at
    the analysed commit) is optional: without it, mentions that are absent from the
    diff are reported as unverifiable instead of contradicting the PR.
    """
    if submission.kind is not Kind.PULL_REQUEST:
        return []
    files = [fd for cf in submission.changed_files if (fd := _file_diff(cf)) is not None]
    ctx = _Context(
        submission=submission,
        text=_parse_text(submission),
        files=files,
        claims=list(claims),
        ws=ws,
        pr_url=_pr_url(submission),
    )
    steps = _FULL_CHECKS if files else _DESCRIPTION_ONLY_CHECKS
    return [check for step in steps for check in step(ctx)]
