"""How complete a bug report is, judged by what a maintainer needs to reproduce it.

A claim can only be verified when the report says how to trigger the problem,
what happened, what should have happened, and on which version and platform.
This module finds which of those ingredients are present, both from section
headings that actually have content and from the artifacts already extracted,
so Nomi can ask for exactly what is missing instead of guessing. Unfilled
issue-template text (HTML comments, "A clear and concise description...",
"N/A", empty bullets) never counts as content, otherwise a template-only
issue would look thorough.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from nomi.models import Artifacts, CodeBlock, Completeness, Kind, Section, Submission

MAX_CHARS = 200_000

WEIGHTS: dict[Section, float] = {
    Section.STEPS_TO_REPRODUCE: 0.25,
    Section.EXPECTED_BEHAVIOR: 0.10,
    Section.ACTUAL_BEHAVIOR: 0.15,
    Section.VERSION: 0.10,
    Section.ENVIRONMENT: 0.10,
    Section.LOGS: 0.15,
    Section.CODE_SAMPLE: 0.15,
}

# ------------------------------------------------------------ template noise

_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
_EXAMPLE_BRACKET = re.compile(r"\[\s*(?:e\.g\.?|eg|ex|example)\b[^\]\n]*\]", re.I)
_PLACEHOLDER_LINE = re.compile(
    r"""
      ['"]\.{3,}['"]
    | a\ clear\ and\ concise\ description
    | steps\ to\ reproduce\ the\ behaviou?r
    | if\ applicable,?\ add
    | add\ any\ other\ context
    | please\ complete\ the\ following
    | ^\s*(?:\d+[.)]\s*|[-*+]\s*)?see\ error\.?\s*$
    | ^\s*(?:\d+[.)]|[-*+]|[-*+]\s+\[[\ xX]?\])?\s*(?:\.{2,}|…)?\s*$
    | ^\s*_?no\ response_?\s*$
    | ^\s*(?:\d+[.)]|[-*+])?\s*(?:n/?a|none|tbd|todo|unknown|-+|_+)\.?\s*$
    | ^\s*(?:\#|//|--|;)?\s*(?:\d+[.)]\s*|[-*+]\s*)?(?:please\ )?
        (?:paste|describe|provide|include|explain|tell\ us|list|write|put)\b.*\b(?:here|below)\b
    """,
    re.I | re.X,
)
_PLACEHOLDER_VALUE = re.compile(
    r"^(?:e\.g\.?\b.*|\[.*\]|\.{2,}|…|n/?a|none|tbd|unknown|-+|_?no response_?)$", re.I
)
_FENCE_LINE = re.compile(r"^\s*(?:`{3,}|~{3,})")

# ---------------------------------------------------------- section headings

_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<label>.+?)\s*#*\s*$")
_BOLD_LINE = re.compile(r"^\s*(?:\*\*|__)(?P<label>[^*_\n]{1,80}?)(?:\*\*|__)\s*:?\s*$")
_LABEL_LINE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?(?P<label>[A-Za-z][\w /()&.-]{0,40}?)(?:\*\*|__)?\s*:"
    r"(?:\*\*|__)?\s*(?P<rest>.*)$"
)
# Ordered: the first pattern that matches a heading decides its section.
_SECTION_LABELS: tuple[tuple[re.Pattern[str], Section], ...] = (
    (re.compile(r"reproduc|\brepro\b|\bsteps\b"), Section.STEPS_TO_REPRODUCE),
    (re.compile(r"expected"), Section.EXPECTED_BEHAVIOR),
    (re.compile(r"actual|observed|current behaviou?r|what happened"), Section.ACTUAL_BEHAVIOR),
    (
        re.compile(r"python version|system info|environment|\bos\b|operating system|platform"),
        Section.ENVIRONMENT,
    ),
    (re.compile(r"version"), Section.VERSION),
    (
        re.compile(r"\blogs?\b|traceback|stack ?trace|error output|error message|\boutput\b"),
        Section.LOGS,
    ),
    (
        re.compile(r"code sample|code example|minimal|reproducible example|mcve|snippet|\bcode\b"),
        Section.CODE_SAMPLE,
    ),
)

# ---------------------------------------------------------- prose evidence

_ENVIRONMENT = re.compile(
    r"\b(?:windows|macos|mac\s?os|os\s?x|linux|ubuntu|debian|fedora|alpine|centos|rhel|arch linux"
    r"|freebsd|wsl2?|docker|kubernetes|android|python\s?[23](?:\.\d+)*|py3\.\d+"
    r"|node(?:\.?js)?\s?v?\d+|go\s?1\.\d+|java\s?\d+|jdk\s?\d+|rust\s?1\.\d+|ruby\s?\d|php\s?\d)\b",
    re.I,
)
_NUMBERED_ITEM = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
_BULLET_ITEM = re.compile(r"^\s*[-*+]\s+(?!\[[ xX]?\])(?P<item>\S.*)$", re.M)
_ACTION_VERB = re.compile(
    r"\b(?:run|execute|call|click|open|install|create|start|go|type|enter|set|use|import|send"
    r"|navigate|build|clone|add|press|select|upload|configure|invoke)\b",
    re.I,
)
_RUN_PHRASE = re.compile(r"\b(?:run|execute)\s+[`\w./$-]", re.I)
_PROMPT_LINE = re.compile(r"^\s*(?:\$|>>>|PS>|C:\\>)\s+\S", re.M)
_EXPECTED = re.compile(r"\b(?:expected|expect|expecting|should)\b", re.I)
_ACTUAL = re.compile(
    r"\b(?:instead|actual(?:ly)?|but got|fail(?:s|ed|ing)?|crash(?:es|ed|ing)?|errors?|raises?"
    r"|raised|returns?|returned|throws?|exception|panic(?:s|ked)?|hangs?)\b",
    re.I,
)
_LOG_LANGUAGES = frozenset({"log", "logs", "output", "stderr", "stdout", "pytb", "traceback"})
_LOG_LINE = re.compile(
    r"^\s*(?:\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\[?\d{2}:\d{2}:\d{2}"
    r"|\[?(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\]?[\s:]"
    r"|Traceback \(|File \"|at\s+\S+[(:]|#\d+\s+0x|[A-Za-z_.]*(?:Error|Exception):)"
)


@dataclass
class _Parsed:
    from_headings: set[Section] = field(default_factory=set)
    prose: list[str] = field(default_factory=list)  # outside code, headings removed
    content: list[str] = field(default_factory=list)  # prose plus code lines


def _clean(text: str) -> str:
    text = _HTML_COMMENT.sub("", text[:MAX_CHARS].replace("\r\n", "\n").replace("\r", "\n"))
    lines = text.split("\n")
    return "\n".join(
        "" if _PLACEHOLDER_LINE.search(ln) or _EXAMPLE_BRACKET.search(ln) else ln for ln in lines
    )


def _section_for(label: str) -> Section | None:
    low = label.lower()
    for pattern, section in _SECTION_LABELS:
        if pattern.search(low):
            return section
    return None


def _has_value(value: str) -> bool:
    value = value.strip().strip("*_").strip()
    return bool(value) and not _PLACEHOLDER_VALUE.match(value)


def _parse(text: str) -> _Parsed:
    """Walk the cleaned body once, attributing content lines to the section they sit under."""
    parsed = _Parsed()
    current: Section | None = None
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            parsed.content.append(line)
            if current is not None and line.strip():
                parsed.from_headings.add(current)
            continue
        heading = _MD_HEADING.match(line) or _BOLD_LINE.match(line)
        if heading:
            current = _section_for(heading["label"].strip().rstrip(":"))
            continue
        label = _LABEL_LINE.match(line)
        section = _section_for(label["label"]) if label else None
        if label and section is not None:
            current = section
            line = label["rest"] if _has_value(label["rest"]) else ""
        if current is not None and _has_value(line):
            parsed.from_headings.add(current)
        parsed.prose.append(line)
        parsed.content.append(line)
    return parsed


def _block_lines(block: CodeBlock) -> list[str]:
    return [ln for ln in _clean(block.content).split("\n") if ln.strip()]


def _is_log_block(block: CodeBlock, lines: list[str], artifacts: Artifacts) -> bool:
    if (block.language or "") in _LOG_LANGUAGES:
        return True
    if any(
        t.raw and t.raw.split("\n", 1)[0].strip() in block.content for t in artifacts.stack_traces
    ):
        return True
    return sum(1 for ln in lines if _LOG_LINE.match(ln)) * 2 >= len(lines)


def _from_artifacts(cleaned: str, artifacts: Artifacts) -> set[Section]:
    found: set[Section] = set()
    for block in artifacts.code_blocks:
        lines = _block_lines(block)
        if lines:
            found.add(
                Section.LOGS if _is_log_block(block, lines, artifacts) else Section.CODE_SAMPLE
            )
    if artifacts.stack_traces or _any_in(artifacts.error_messages, cleaned):
        found.add(Section.LOGS)
    if _any_in(artifacts.versions, cleaned):
        found.add(Section.VERSION)
    return found


def _any_in(needles: Iterable[str], haystack: str) -> bool:
    return any(needle and needle in haystack for needle in needles)


def _steps_present(prose: str, content: str) -> bool:
    if len(_NUMBERED_ITEM.findall(prose)) >= 2:
        return True
    bullets = [m["item"] for m in _BULLET_ITEM.finditer(prose)]
    if len(bullets) >= 2 and any(_ACTION_VERB.search(item) for item in bullets):
        return True
    return bool(_RUN_PHRASE.search(prose) or _PROMPT_LINE.search(content))


def _from_prose(prose: str, content: str) -> set[Section]:
    found: set[Section] = set()
    if _steps_present(prose, content):
        found.add(Section.STEPS_TO_REPRODUCE)
    if _EXPECTED.search(prose):
        found.add(Section.EXPECTED_BEHAVIOR)
    if _ACTUAL.search(prose):
        found.add(Section.ACTUAL_BEHAVIOR)
    if _ENVIRONMENT.search(content):
        found.add(Section.ENVIRONMENT)
    return found


def assess_completeness(submission: Submission, artifacts: Artifacts) -> Completeness:
    """Which reproduction ingredients an issue contains, and a 0..1 weighted score.

    Pull requests always score 1.0: their description quality is judged
    elsewhere and a missing "steps to reproduce" says nothing about a PR.
    """
    if submission.kind is Kind.PULL_REQUEST:
        return Completeness(present=[], missing=[], score=1.0)
    cleaned = _clean(submission.text)
    parsed = _parse(cleaned)
    prose, content = "\n".join(parsed.prose), "\n".join(parsed.content)
    found = parsed.from_headings | _from_artifacts(cleaned, artifacts) | _from_prose(prose, content)
    present = [s for s in Section if s in found]
    missing = [s for s in Section if s not in found]
    return Completeness(
        present=present, missing=missing, score=round(sum(WEIGHTS[s] for s in present), 2)
    )
