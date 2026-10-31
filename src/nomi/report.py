"""Render a Report as a GitHub comment (Markdown) or as JSON.

The Markdown is posted by a bot into public threads, and much of it is
attacker-controlled: titles, claims and quotes come straight from the issue,
and check titles echo symbols the reporter typed. Everything is therefore
escaped on the way out so a report cannot ping people, inject links or HTML,
autolink URLs or cross-reference other issues (which would backlink them),
spoof the hidden marker, break out of a table or <details> block, or exceed
GitHub's 65,536-character comment limit (rows are dropped progressively and
a note says so instead).
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import quote

from nomi.models import (
    REPORT_MARKER,
    Check,
    CheckStatus,
    ClaimAssessment,
    Evidence,
    Kind,
    OverallVerdict,
    Report,
    Signal,
    Verdict,
)

DEFAULT_MAX_CHARS = 60_000
CELL_LIMIT = 200
ITEM_LIMIT = 300
SUMMARY_LIMIT = 1_000
MAX_EVIDENCE_LINKS = 2

HEADER = f"{REPORT_MARKER}\n### Nomi evidence report"
DISCLAIMER = "Automated, evidence-based triage — not a final decision; a maintainer should confirm."

VERDICT_BADGES: dict[OverallVerdict, str] = {
    OverallVerdict.LIKELY_INVALID: "🟥 Likely invalid",
    OverallVerdict.LIKELY_VALID: "🟩 Likely valid",
    OverallVerdict.NEEDS_INFO: "🟨 Needs more information",
    OverallVerdict.INCONCLUSIVE: "⬜ Inconclusive",
}
CLAIM_BADGES: dict[Verdict, str] = {
    Verdict.SUPPORTED: "✅ supported",
    Verdict.CONTRADICTED: "❌ contradicted",
    Verdict.UNVERIFIED: "❔ unverified",
    Verdict.NEEDS_INFO: "📝 needs info",
}
STATUS_BADGES: dict[CheckStatus, str] = {
    CheckStatus.PASS: "✅ pass",
    CheckStatus.FAIL: "❌ fail",
    CheckStatus.WARN: "⚠️ warning",
    CheckStatus.SKIPPED: "⏭️ skipped",
    CheckStatus.INFO: "ℹ️ info",
}
# Failures first, so the rows dropped to fit the size limit are the least telling.
_STATUS_ORDER = {
    CheckStatus.FAIL: 0,
    CheckStatus.WARN: 1,
    CheckStatus.PASS: 2,
    CheckStatus.SKIPPED: 3,
    CheckStatus.INFO: 4,
}
_METHOD_LABELS = {
    "deterministic": "repository check",
    "execution": "sandbox run",
    "llm": "language model, advisory",
    "none": "no direct evidence",
}

_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f\u0085  ‪-‮⁦-⁩]+")
_WHITESPACE = re.compile(r"\s+")
_MENTION = re.compile(r"(?<![A-Za-z0-9_])@(?=[A-Za-z0-9_-])")
_BLOCK_START = re.compile(r"[#+\-*=_~]")
_ORDERED_START = re.compile(r"(\d{1,9})([.)])")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA = re.compile(r"[0-9a-fA-F]{7,64}")
_HTTP_URL = re.compile(r"https?://\S+")
# GitHub autolinks bare URLs and www. hosts, and turns #12, GH-12 and owner/repo#12
# into cross-references that notify the other thread; a zero-width space defuses each.
_AUTOLINK = re.compile(r"(?i)(?<=https)(?=://)|(?<=http)(?=://)|(?<=www)(?=\.)")
_ISSUE_REF = re.compile(r"(?i)(?<=#)(?=\d)|(?<=\bGH-)(?=\d)")

T = TypeVar("T")


@dataclass(frozen=True)
class _Budget:
    """How much of a report to show; later budgets trade detail for size."""

    claim_rows: int | None = None  # None shows everything
    check_rows: int | None = None
    list_items: int | None = None
    cell: int = CELL_LIMIT
    rationale: bool = True
    reasons: bool = True


_BUDGETS = (
    _Budget(),
    _Budget(check_rows=100, cell=160),
    _Budget(check_rows=40, list_items=20, cell=120),
    _Budget(claim_rows=40, check_rows=15, list_items=10, cell=100, rationale=False),
    _Budget(claim_rows=15, check_rows=0, list_items=5, cell=80, rationale=False, reasons=False),
    _Budget(claim_rows=5, check_rows=0, list_items=3, cell=60, rationale=False, reasons=False),
)


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------


def _one_line(text: str, limit: int) -> str:
    """Collapse control characters and newlines; cut to `limit` characters."""
    raw = text[: limit * 4 + 64]
    flat = _WHITESPACE.sub(" ", _UNSAFE_CHARS.sub(" ", raw)).strip()
    if len(flat) > limit or len(text) > len(raw):
        flat = flat[: max(0, limit - 1)].rstrip() + "…"
    return flat


def _neutralize_mentions(text: str) -> str:
    return _MENTION.sub("@​", text)


def _neutralize_references(text: str) -> str:
    """Break autolinks and issue references with a zero-width space."""
    return _ISSUE_REF.sub("​", _AUTOLINK.sub("​", text))


def _plain(text: str) -> str:
    """Escape text so it renders literally: no HTML, links, table breaks or pings."""
    for char in ("\\", "[", "]", "|", "`"):
        text = text.replace(char, "\\" + char)
    return _neutralize_references(_neutralize_mentions(html.escape(text, quote=False)))


def _code_ok(content: str) -> bool:
    return bool(content.strip()) and not any(ch in content for ch in "`<>&|")


def _span(content: str) -> str:
    """Inline code when the content is harmless as-is, else literal escaped text."""
    if _code_ok(content):
        return f"`{_neutralize_mentions(content)}`"
    return f"\\`{_plain(content)}\\`"


def _inline(text: str, limit: int) -> str:
    """One safe line of Markdown that keeps the reporter's `code spans` readable."""
    parts = _one_line(text, limit).split("`")
    out = [_plain(parts[0])]
    for i in range(1, len(parts), 2):
        if i + 1 < len(parts):
            out.append(_span(parts[i]) + _plain(parts[i + 1]))
        else:
            out.append("\\`" + _plain(parts[i]))
    return "".join(out)


def _item(text: str, limit: int = ITEM_LIMIT) -> str:
    """Like _inline, but also safe at the start of a line (no headings, lists, fences)."""
    line = _inline(text, limit)
    if _BLOCK_START.match(line):
        return "\\" + line
    ordered = _ORDERED_START.match(line)
    if ordered:
        return f"{ordered.group(1)}\\{line[len(ordered.group(1)) :]}"
    return line


def _safe_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if len(url) > 2_000 or not _HTTP_URL.fullmatch(url):
        return None
    return quote(url, safe=":/?#@!$&'*+,;=%-._~", errors="replace")


# --------------------------------------------------------------------------
# Pieces
# --------------------------------------------------------------------------


def _evidence_label(evidence: Evidence) -> str:
    if not evidence.path:
        return evidence.kind.value
    label = evidence.path
    if evidence.line_start:
        label += f"#L{evidence.line_start}"
        if evidence.line_end and evidence.line_end != evidence.line_start:
            label += f"-L{evidence.line_end}"
    return label


def _evidence_link(evidence: Evidence) -> str:
    text = _plain(_one_line(_evidence_label(evidence), 100))
    url = _safe_url(evidence.url)
    return f"[{text}]({url})" if url else text


def _evidence_cell(evidence: list[Evidence]) -> str:
    if not evidence:
        return "—"
    links = [_evidence_link(ev) for ev in evidence[:MAX_EVIDENCE_LINKS]]
    extra = len(evidence) - MAX_EVIDENCE_LINKS
    return ", ".join(links) + (f" +{extra} more" if extra > 0 else "")


def _limited(items: list[T], limit: int | None) -> list[T]:
    return items if limit is None else items[:limit]


def _bullets(items: list[str], limit: int | None) -> str:
    shown = _limited(items, limit)
    lines = [f"- {_item(text)}" for text in shown]
    if len(items) > len(shown):
        lines.append(f"- _…and {len(items) - len(shown)} more (omitted to fit the size limit)._")
    return "\n".join(lines)


def _target(report: Report) -> str:
    if report.url:
        return report.url
    if report.number is not None and _REPO.fullmatch(report.repo):
        kind = "pull" if report.kind == Kind.PULL_REQUEST else "issues"
        return f"https://github.com/{report.repo}/{kind}/{report.number}"
    return report.repo or "<url>"


def _verdict_line(report: Report) -> str:
    a = report.assessment
    return (
        f"**{VERDICT_BADGES[a.verdict]}** · {a.confidence.value} confidence · score {a.score:+.2f}"
    )


def _provenance(report: Report) -> str:
    commit = (report.commit or "").strip()
    if not commit:
        where = "No commit pinned"
    elif _SHA.fullmatch(commit) and _REPO.fullmatch(report.repo):
        link = f"https://github.com/{report.repo}/commit/{commit}"
        where = f"Analysed commit [`{commit[:7]}`]({link})"
    elif _SHA.fullmatch(commit):
        where = f"Analysed commit `{commit[:7]}`"
    else:
        where = f"Analysed commit {_span(_one_line(commit, 64))}"
    model = (
        f"model {_span(_one_line(report.model, 100))}"
        if report.model
        else "deterministic checks only"
    )
    return f"{where} · {model}"


def _reasons(report: Report, budget: _Budget) -> str:
    reasons = report.assessment.reasons
    if not budget.reasons or not reasons:
        return ""
    return (
        "<details><summary>How the verdict was reached</summary>\n\n"
        f"{_bullets(reasons, budget.list_items)}\n\n</details>"
    )


def _claims(report: Report, budget: _Budget) -> str:
    if not report.claims:
        return "#### Claims\n\n_No checkable claims were extracted._"
    shown = _limited(report.claims, budget.claim_rows)
    rows = ["| # | Claim | Verdict | Evidence |", "| --- | --- | --- | --- |"]
    rows += [_claim_row(ca, budget.cell) for ca in shown]
    if len(shown) < len(report.claims):
        rows.append(f"| … | _{len(report.claims) - len(shown)} more claims omitted_ | | |")
    block = "#### Claims\n\n" + "\n".join(rows)
    if budget.rationale:
        block += "\n\n" + _rationale(shown, budget.cell * 2)
    return block


def _claim_row(ca: ClaimAssessment, cell: int) -> str:
    verdict = f"{CLAIM_BADGES[ca.verdict]} · {ca.confidence.value}"
    return (
        f"| {_inline(ca.claim.id, 20)} | {_inline(ca.claim.text, cell)} | {verdict} "
        f"| {_evidence_cell(ca.evidence)} |"
    )


def _rationale(claims: list[ClaimAssessment], limit: int) -> str:
    lines = [
        f"- **{_inline(ca.claim.id, 20)}** ({_METHOD_LABELS.get(ca.method, ca.method)}): "
        f"{_inline(ca.rationale, limit) or '—'}"
        for ca in claims
    ]
    return (
        "<details><summary>Why each claim got its verdict</summary>\n\n"
        + "\n".join(lines)
        + "\n\n</details>"
    )


def _section(title: str, items: list[str], budget: _Budget) -> str:
    if not items:
        return ""
    return f"#### {title}\n\n{_bullets(items, budget.list_items)}"


def _check_counts(checks: list[Check]) -> str:
    counts = Counter(c.status for c in checks)
    warnings = counts[CheckStatus.WARN]
    return (
        f"Checks performed ({len(checks)}: {counts[CheckStatus.PASS]} passed, "
        f"{counts[CheckStatus.FAIL]} failed, {warnings} warning{'' if warnings == 1 else 's'}, "
        f"{counts[CheckStatus.INFO]} info, {counts[CheckStatus.SKIPPED]} skipped)"
    )


def _check_row(check: Check, cell: int) -> str:
    result = STATUS_BADGES[check.status]
    if check.signal in (Signal.SUPPORTS, Signal.CONTRADICTS) and check.strength > 0:
        sign = "+" if check.signal == Signal.SUPPORTS else "−"
        result += f" · {sign}{min(check.strength, 1.0):.2f}"
    detail = _inline(check.detail, cell)
    if check.evidence:
        link = _evidence_link(check.evidence[0])
        detail = f"{detail} · {link}" if detail else link
    title = f"{_inline(check.title, cell)} {_span(_one_line(check.id, 60))}"
    return f"| {title} | {result} | {detail or '—'} |"


def _checks(report: Report, budget: _Budget) -> str:
    ordered = sorted(report.checks, key=lambda c: _STATUS_ORDER[c.status])
    shown = _limited(ordered, budget.check_rows)
    if not report.checks:
        body = "No checks were run."
    elif not shown:
        body = "_Check rows omitted to fit GitHub's comment size limit._"
    else:
        rows = ["| Check | Result | Detail |", "| --- | --- | --- |"]
        rows += [_check_row(check, budget.cell) for check in shown]
        if len(shown) < len(ordered):
            rows.append(f"| _{len(ordered) - len(shown)} more checks omitted_ | | |")
        body = "\n".join(rows)
    return f"<details><summary>{_check_counts(report.checks)}</summary>\n\n{body}\n\n</details>"


def _truncation_note(report: Report, budget: _Budget) -> str:
    claims = len(_limited(report.claims, budget.claim_rows))
    checks = len(_limited(report.checks, budget.check_rows))
    return (
        f"> ✂️ This report was truncated to fit GitHub's comment size limit "
        f"({claims} of {len(report.claims)} claims and {checks} of {len(report.checks)} "
        f"check rows shown); reproduce it locally for the full report."
    )


def _footer(report: Report) -> str:
    version = _inline(report.nomi_version, 40)
    if report.url or (report.number is not None and _REPO.fullmatch(report.repo)):
        command = _span(_one_line(f"nomi analyze {_target(report)}", 320))
    else:
        # A submission read from a JSON file has no URL to re-fetch; `nomi analyze
        # owner/repo` would not parse, so point at the input-file form instead.
        command = "`nomi analyze --input-file <input.json>`"
    return f"---\n<sub>Generated by Nomi v{version} · reproduce locally: {command}</sub>"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def _render(report: Report, budget: _Budget, *, truncated: bool) -> str:
    blocks = [
        HEADER,
        _verdict_line(report),
        f"_{DISCLAIMER}_",
        _item(report.summary, SUMMARY_LIMIT),
        _provenance(report),
        _reasons(report, budget),
        _claims(report, budget),
        _section("Missing information", report.missing_information, budget),
        _section("Suggested next steps", report.next_steps, budget),
        _checks(report, budget),
        _section("Limitations & uncertainty", report.limitations, budget),
        _truncation_note(report, budget) if truncated else "",
        _footer(report),
    ]
    return "\n\n".join(b for b in blocks if b) + "\n"


def _render_minimal(report: Report) -> str:
    blocks = [
        HEADER,
        _verdict_line(report),
        f"_{DISCLAIMER}_",
        _item(report.summary, SUMMARY_LIMIT),
        _provenance(report),
        "> ✂️ Most of this report was truncated to fit GitHub's comment size limit; "
        "reproduce it locally for the full report.",
        _footer(report),
    ]
    return "\n\n".join(blocks) + "\n"


def _hard_limit(markdown: str, max_chars: int) -> str:
    if len(markdown) <= max_chars:
        return markdown
    note = "\n\n_…truncated._\n"
    if max_chars <= len(note):
        return markdown[: max(0, max_chars)]
    cut = markdown[: max_chars - len(note)]
    newline = cut.rfind("\n")
    return (cut[:newline] if newline > 0 else cut) + note


def render_markdown(report: Report, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """GitHub-flavoured Markdown comment, never longer than `max_chars`."""
    for level, budget in enumerate(_BUDGETS):
        markdown = _render(report, budget, truncated=level > 0)
        if len(markdown) <= max_chars:
            return markdown
    return _hard_limit(_render_minimal(report), max_chars)


def render_json(report: Report) -> str:
    """The full report as JSON; `Report.model_validate_json` round-trips it."""
    return report.model_dump_json(indent=2)
