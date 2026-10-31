"""Combine check results into per-claim verdicts and one overall assessment.

The rules are hand-written and ordered on purpose: a maintainer reading a
report must be able to see exactly which piece of evidence moved the verdict.
Two principles shape every threshold below:

* Calling a genuine report fabricated is the costliest mistake Nomi can make,
  so a contradiction needs strong, repository-anchored evidence, and a
  successful sandbox reproduction always overrides it.
* A language model may nudge a verdict but never decide it: model judgements
  are capped at medium confidence, lose to conflicting deterministic checks,
  and add only a small fixed weight to the overall score.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from nomi.models import (
    Assessment,
    Check,
    CheckStatus,
    Claim,
    ClaimAssessment,
    ClaimType,
    Completeness,
    Confidence,
    Kind,
    OverallVerdict,
    Section,
    Signal,
    Verdict,
)


@dataclass(frozen=True)
class Thresholds:
    """Score thresholds for :func:`assess`; override to tune precision vs. recall."""

    hard_contradiction: float = 0.7
    reproduced: float = 0.8
    valid: float = 0.6
    valid_medium: float = 1.0
    invalid: float = 0.6
    invalid_medium: float = 1.0
    needs_info_completeness: float = 0.35
    low_completeness: float = 0.6
    grounding_support_cap: float = 1.0


DEFAULT_THRESHOLDS = Thresholds()

# Per-claim rules (finalize_claims).
CLAIM_CONTRADICTED = 0.6
CLAIM_CONTRADICTED_HIGH = 0.75
CLAIM_SUPPORTED = 0.5
CLAIM_REPRODUCED_HIGH = 0.8
CLAIM_CONFLICT = 0.3
CLAIM_CODE_EXISTS = 0.15

# Overall rules (assess).
LLM_CLAIM_WEIGHT = 0.3
HIGH_CONTRADICTION_TOTAL = 1.4
WEAK_SUPPORT = 0.5
VERY_INCOMPLETE = 0.2

MAX_REASON_CHECKS = 5
MAX_NEXT_STEPS = 5
MAX_ASKS_IN_STEP = 4

REPRO_PREFIX = "repro."
GROUNDING_PREFIX = "grounding."
# Uncertain contradictions ("not found here; may be a dependency") must not add
# up to certainty: their combined weight is capped.
WARN_CONTRADICTION_CAP = 0.4
# Reference checks whose failures look like version skew when most references hold.
_REFERENCE_IDS = ("grounding.file", "grounding.line", "grounding.symbol", "trace.frames")
# Only positional mismatches can be explained by version drift; a project
# function or file that exists nowhere cannot.
_SKEWABLE_IDS = ("grounding.line", "trace.frames")

NO_EVIDENCE = "No verifiable evidence for or against this claim."
LLM_LIMITATION = (
    "LLM judgements come from a small local model and are advisory "
    "(never high confidence on their own)"
)
NO_LLM_LIMITATION = "No language model was used (deterministic checks only)"

SECTION_ASKS: dict[Section, str] = {
    Section.STEPS_TO_REPRODUCE: "Step-by-step instructions to reproduce the problem",
    Section.EXPECTED_BEHAVIOR: "What you expected to happen vs. what happened",
    Section.ACTUAL_BEHAVIOR: "What you expected to happen vs. what happened",
    Section.VERSION: "The exact version where this happens",
    Section.ENVIRONMENT: "Your environment (OS, runtime version)",
    Section.LOGS: "The full error output or traceback",
    Section.CODE_SAMPLE: "A minimal code sample or command that reproduces it",
}

_EPS = 1e-9
_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
_VERDICT_WORDS: dict[Verdict, str] = {
    Verdict.SUPPORTED: "supported",
    Verdict.CONTRADICTED: "contradicted",
    Verdict.UNVERIFIED: "unverified",
    Verdict.NEEDS_INFO: "needs info",
}
_SIGNAL_SIGNS = {Signal.SUPPORTS: "+", Signal.CONTRADICTS: "-", Signal.MISSING_INFO: "?"}
_CODE_SPAN = re.compile(r"`([^`\n]{1,80})`")
_WHITESPACE = re.compile(r"\s+")
_HEX_SHA = re.compile(r"[0-9a-fA-F]{7,64}")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _strength(check: Check) -> float:
    """Check strength clamped to 0..1 (NaN and garbage count as zero)."""
    value = check.strength
    return min(1.0, max(0.0, value)) if math.isfinite(value) else 0.0


def _at_least(value: float, threshold: float) -> bool:
    return value >= threshold - _EPS


def _is_repro(check: Check) -> bool:
    return check.id.startswith(REPRO_PREFIX)


def _clip(text: str, limit: int = 200) -> str:
    """One line of at most `limit` characters, with an ellipsis when cut."""
    raw = text[: limit * 4 + 64]
    flat = _WHITESPACE.sub(" ", raw).strip()
    if len(flat) > limit or len(text) > len(raw):
        flat = flat[: max(0, limit - 1)].rstrip() + "…"
    return flat


def _title(check: Check) -> str:
    return _clip(check.title).rstrip(".") or f"check {check.id}"


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = item.strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _with_signal(checks: Iterable[Check], signal: Signal) -> list[Check]:
    return [c for c in checks if c.signal == signal]


def _strongest(checks: Iterable[Check], signal: Signal, minimum: float = 0.0) -> Check | None:
    """Strongest check with `signal` and strength >= minimum; first wins ties."""
    best: Check | None = None
    for check in checks:
        if check.signal != signal or not _at_least(_strength(check), minimum):
            continue
        if best is None or _strength(check) > _strength(best):
            best = check
    return best


def _by_strength(checks: Iterable[Check]) -> list[Check]:
    indexed = list(enumerate(checks))
    indexed.sort(key=lambda pair: (-_strength(pair[1]), pair[0]))
    return [check for _, check in indexed]


def _cap(confidence: Confidence, ceiling: Confidence) -> Confidence:
    return ceiling if _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[ceiling] else confidence


def _short_sha(commit: str | None) -> str | None:
    if not commit:
        return None
    commit = commit.strip()
    return commit[:7] if _HEX_SHA.fullmatch(commit) else _clip(commit, 40)


def _lower_first(text: str) -> str:
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


# --------------------------------------------------------------------------
# Per-claim verdicts
# --------------------------------------------------------------------------


def finalize_claims(
    claims: list[Claim],
    checks: list[Check],
    judgments: list[ClaimAssessment],
    completeness: Completeness,
) -> list[ClaimAssessment]:
    """One verdict per claim (same order), deterministic evidence first, the model second."""
    by_claim: dict[str, ClaimAssessment] = {}
    for judgment in judgments:
        by_claim.setdefault(judgment.claim.id, judgment)
    return [
        _finalize_claim(
            claim,
            [c for c in checks if claim.id in c.claim_ids],
            by_claim.get(claim.id),
            completeness,
        )
        for claim in claims
    ]


def _finalize_claim(
    claim: Claim,
    linked: list[Check],
    judgment: ClaimAssessment | None,
    completeness: Completeness,
) -> ClaimAssessment:
    decided = _from_checks(claim, linked)
    if decided is not None:
        return decided
    if judgment is not None and judgment.verdict != Verdict.UNVERIFIED:
        return _from_judgment(claim, linked, judgment)
    supports = _with_signal(linked, Signal.SUPPORTS)
    doubted = any(_strength(c) > 0 for c in _with_signal(linked, Signal.CONTRADICTS))
    if (
        claim.type in (ClaimType.CODE_FACT, ClaimType.LOCATION)
        and not doubted  # one existing name must not vouch for a claim whose other parts fail
        and _at_least(sum(_strength(c) for c in supports), CLAIM_CODE_EXISTS)
    ):
        best = max(supports, key=_strength)
        return ClaimAssessment(
            claim=claim,
            verdict=Verdict.SUPPORTED,
            confidence=Confidence.LOW,
            rationale=f"Only partly verified: the referenced code exists ({_title(best)})",
            evidence=[ev for c in supports for ev in c.evidence][:5],
            method="deterministic",
        )
    needs_info = _needs_info_reason(claim, completeness)
    if needs_info is not None:
        return ClaimAssessment(
            claim=claim,
            verdict=Verdict.NEEDS_INFO,
            confidence=Confidence.LOW,
            rationale=needs_info,
        )
    return _unverified(claim, judgment)


def _from_checks(claim: Claim, linked: list[Check]) -> ClaimAssessment | None:
    """Rules 1-2: a strong linked check decides the claim outright."""
    contra = _strongest(linked, Signal.CONTRADICTS, CLAIM_CONTRADICTED)
    if contra is not None:
        strong = _at_least(_strength(contra), CLAIM_CONTRADICTED_HIGH)
        return ClaimAssessment(
            claim=claim,
            verdict=Verdict.CONTRADICTED,
            confidence=Confidence.HIGH if strong else Confidence.MEDIUM,
            rationale=_title(contra),
            evidence=list(contra.evidence),
            method="execution" if _is_repro(contra) else "deterministic",
        )
    reproduced = _strongest(
        [c for c in linked if _is_repro(c)], Signal.SUPPORTS, CLAIM_REPRODUCED_HIGH
    )
    support = reproduced or _strongest(linked, Signal.SUPPORTS, CLAIM_SUPPORTED)
    if support is None:
        return None
    return ClaimAssessment(
        claim=claim,
        verdict=Verdict.SUPPORTED,
        confidence=Confidence.HIGH if reproduced is not None else Confidence.MEDIUM,
        rationale=_title(support),
        evidence=list(support.evidence),
        method="execution" if _is_repro(support) else "deterministic",
    )


def _from_judgment(claim: Claim, linked: list[Check], judgment: ClaimAssessment) -> ClaimAssessment:
    """Rule 3: accept the model's verdict unless a deterministic check disagrees."""
    opposite = {
        Verdict.SUPPORTED: Signal.CONTRADICTS,
        Verdict.CONTRADICTED: Signal.SUPPORTS,
    }.get(judgment.verdict)
    conflict = _strongest(linked, opposite, CLAIM_CONFLICT) if opposite is not None else None
    if conflict is not None:
        direction = "contradicts" if conflict.signal == Signal.CONTRADICTS else "supports"
        return ClaimAssessment(
            claim=claim,
            verdict=Verdict.UNVERIFIED,
            confidence=Confidence.LOW,
            rationale=(
                f"conflicting signals: the language model judged this claim "
                f"{_VERDICT_WORDS[judgment.verdict]}, but check `{conflict.id}` {direction} it "
                f"({_title(conflict)})."
            ),
            evidence=list(conflict.evidence),
        )
    return ClaimAssessment(
        claim=claim,
        verdict=judgment.verdict,
        confidence=_cap(judgment.confidence, Confidence.MEDIUM),
        rationale=judgment.rationale.strip() or "Judged by the language model.",
        evidence=list(judgment.evidence),
        method="llm",
    )


def _needs_info_reason(claim: Claim, completeness: Completeness) -> str | None:
    if not claim.checkable:
        return "This claim cannot be checked against the repository without more detail from the reporter."
    missing = set(completeness.missing)
    if claim.type == ClaimType.ENVIRONMENT and missing & {Section.VERSION, Section.ENVIRONMENT}:
        return "The report does not state the version or environment needed to check this claim."
    return None


def _unverified(claim: Claim, judgment: ClaimAssessment | None) -> ClaimAssessment:
    note = judgment.rationale.strip() if judgment is not None else ""
    return ClaimAssessment(
        claim=claim,
        verdict=Verdict.UNVERIFIED,
        confidence=Confidence.LOW,
        rationale=f"{NO_EVIDENCE} Model note: {note}" if note else NO_EVIDENCE,
        evidence=list(judgment.evidence) if judgment is not None else [],
        method="llm" if judgment is not None else "none",
    )


# --------------------------------------------------------------------------
# Overall assessment
# --------------------------------------------------------------------------


def _totals(
    claims: list[ClaimAssessment], checks: list[Check], thresholds: Thresholds
) -> tuple[float, float]:
    """(S, C): weighted supporting and contradicting evidence."""
    grounding = sum(
        _strength(c)
        for c in _with_signal(checks, Signal.SUPPORTS)
        if c.id.startswith(GROUNDING_PREFIX)
    )
    other = sum(
        _strength(c)
        for c in _with_signal(checks, Signal.SUPPORTS)
        if not c.id.startswith(GROUNDING_PREFIX)
    )
    llm_supported = sum(
        1 for ca in claims if ca.verdict == Verdict.SUPPORTED and ca.method == "llm"
    )
    llm_contradicted = sum(
        1 for ca in claims if ca.verdict == Verdict.CONTRADICTED and ca.method == "llm"
    )
    support = (
        min(thresholds.grounding_support_cap, grounding) + other + LLM_CLAIM_WEIGHT * llm_supported
    )
    skew = _version_skew(checks)
    firm = sum(
        _strength(c) * (0.5 if c in skew else 1.0)
        for c in _with_signal(checks, Signal.CONTRADICTS)
        if c.status != CheckStatus.WARN
    )
    uncertain = sum(
        _strength(c)
        for c in _with_signal(checks, Signal.CONTRADICTS)
        if c.status == CheckStatus.WARN
    )
    contra = firm + min(WARN_CONTRADICTION_CAP, uncertain)
    return support, contra + LLM_CLAIM_WEIGHT * llm_contradicted


def _version_skew(checks: list[Check]) -> list[Check]:
    """Failed reference checks better explained by a version mismatch than by fabrication.

    A made-up report rarely gets most of its references right; a genuine one
    written against another release gets most right and a few wrong. When at
    least three references hold and they outnumber the failures two to one,
    the failures are treated as probable version skew.
    """
    refs = [c for c in checks if c.id in _REFERENCE_IDS]
    if any(
        c.status == CheckStatus.FAIL and c.id not in _SKEWABLE_IDS and _strength(c) >= 0.7
        for c in refs
    ):
        return []  # something that exists nowhere is not a version difference
    held = [c for c in refs if c.status == CheckStatus.PASS]
    failed = [c for c in refs if c.status == CheckStatus.FAIL and c.id in _SKEWABLE_IDS]
    if failed and len(held) >= 3 and len(held) >= 2 * len(failed):
        return failed
    return []


def _completeness_score(completeness: Completeness) -> float:
    value = completeness.score
    return min(1.0, max(0.0, value)) if math.isfinite(value) else 0.0


def assess(
    kind: Kind,
    claims: list[ClaimAssessment],
    checks: list[Check],
    completeness: Completeness,
    *,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> Assessment:
    """Overall verdict; the first matching rule wins and is explained in reasons[0]."""
    t = thresholds
    support, contra = _totals(claims, checks, t)
    score = round(support - contra, 9)
    skew = _version_skew(checks)
    hard = [
        c
        for c in _with_signal(checks, Signal.CONTRADICTS)
        if _at_least(_strength(c), t.hard_contradiction) and c not in skew
    ]
    reproduced = _strongest([c for c in checks if _is_repro(c)], Signal.SUPPORTS, t.reproduced)
    not_reproduced = next(
        (c for c in checks if _is_repro(c) and c.status == CheckStatus.FAIL), None
    )
    is_issue = kind == Kind.ISSUE
    filled = _completeness_score(completeness)

    verdict: OverallVerdict
    confidence: Confidence
    if hard and reproduced is not None:
        verdict, confidence = OverallVerdict.INCONCLUSIVE, Confidence.MEDIUM
        why = (
            f"Conflicting strong evidence: the report was reproduced in the sandbox, "
            f"but {len(hard)} check(s) strongly contradict it."
        )
    elif hard:
        severe = _at_least(contra, HIGH_CONTRADICTION_TOTAL) or len(hard) >= 2
        verdict = OverallVerdict.LIKELY_INVALID
        confidence = Confidence.HIGH if severe else Confidence.MEDIUM
        why = (
            f"Strong contradicting evidence: {len(hard)} check(s) contradict the report "
            f"(strongest {max(_strength(c) for c in hard):.2f}, total {contra:.2f})."
        )
    elif reproduced is not None:
        verdict, confidence = OverallVerdict.LIKELY_VALID, Confidence.HIGH
        why = f"The reported behaviour was reproduced in the sandbox ({_title(reproduced)})."
    elif is_issue and not_reproduced is not None:
        # A clean run means "cannot reproduce as described", never "invalid":
        # the reporter's environment (OS, versions, data) may simply differ.
        verdict, confidence = OverallVerdict.NEEDS_INFO, Confidence.MEDIUM
        why = (
            "The report's own snippet ran in the sandbox without the reported error, "
            f"so it could not be reproduced as described ({_title(not_reproduced)})."
        )
    elif is_issue and filled < t.needs_info_completeness and support < WEAK_SUPPORT:
        verdict = OverallVerdict.NEEDS_INFO
        confidence = Confidence.MEDIUM if filled < VERY_INCOMPLETE else Confidence.LOW
        why = (
            f"The issue lacks most of what is needed to verify it (completeness {filled:.2f}) "
            f"and little supporting evidence was found."
        )
    elif _at_least(score, t.valid):
        verdict = OverallVerdict.LIKELY_VALID
        confidence = Confidence.MEDIUM if _at_least(score, t.valid_medium) else Confidence.LOW
        why = f"Supporting evidence outweighs contradicting evidence (score {score:+.2f})."
    elif score <= -t.invalid + _EPS:
        verdict = OverallVerdict.LIKELY_INVALID
        confidence = Confidence.MEDIUM if score <= -t.invalid_medium + _EPS else Confidence.LOW
        why = f"Contradicting evidence outweighs supporting evidence (score {score:+.2f})."
    elif is_issue and filled < t.low_completeness:
        verdict, confidence = OverallVerdict.NEEDS_INFO, Confidence.LOW
        why = (
            f"Evidence is weak either way and the issue is missing information needed to "
            f"verify it (completeness {filled:.2f})."
        )
    else:
        verdict, confidence = OverallVerdict.INCONCLUSIVE, Confidence.LOW
        why = f"Not enough evidence either way (score {score:+.2f})."

    reasons = [why]
    if skew:
        held = sum(1 for c in checks if c.id in _REFERENCE_IDS and c.status == CheckStatus.PASS)
        reasons.append(
            f"{held} cited references exist, so {len(skew)} mismatching one(s) are treated as a "
            "probable version difference rather than fabrication."
        )
    return Assessment(
        verdict=verdict,
        confidence=confidence,
        score=round(score, 3),
        reasons=[*reasons, *_contributions(checks)],
    )


def _contributions(checks: list[Check]) -> list[str]:
    """Top checks that moved (or would need to move) the score, strongest first."""
    relevant = [
        c
        for c in checks
        if c.signal == Signal.MISSING_INFO
        or (c.signal in (Signal.SUPPORTS, Signal.CONTRADICTS) and _strength(c) > 0)
    ]
    return [
        f"{_SIGNAL_SIGNS[c.signal]}{_strength(c):.2f} {_clip(c.id, 80)}: {_title(c)}"
        for c in _by_strength(relevant)[:MAX_REASON_CHECKS]
    ]


# --------------------------------------------------------------------------
# What to tell the maintainer
# --------------------------------------------------------------------------


def missing_information(
    kind: Kind,
    completeness: Completeness,
    claims: list[ClaimAssessment],
    checks: list[Check],
) -> list[str]:
    """Concrete things to ask the reporter for (only check-driven asks for PRs)."""
    asks: list[str] = []
    if kind == Kind.ISSUE:
        missing = set(completeness.missing)
        asks += [SECTION_ASKS[section] for section in Section if section in missing]
        asks += [
            f"More detail to verify claim {_clip(ca.claim.id, 20)}: “{_clip(ca.claim.text, 160)}”"
            for ca in claims
            if ca.verdict == Verdict.NEEDS_INFO
        ]
    asks += [_title(c) for c in _with_signal(checks, Signal.MISSING_INFO)]
    return _dedupe(asks)


def next_steps(
    assessment: Assessment,
    claims: list[ClaimAssessment],
    checks: list[Check],
    *,
    commit: str | None = None,
    missing: Sequence[str] = (),
) -> list[str]:
    """Up to five evidence-anchored suggestions; Nomi never acts on its own.

    `commit` names the analysed commit in invalid-report suggestions and
    `missing` (usually :func:`missing_information`) feeds the needs-info ask.
    """
    builders = {
        OverallVerdict.LIKELY_INVALID: lambda: _steps_invalid(claims, checks, commit),
        OverallVerdict.LIKELY_VALID: lambda: _steps_valid(claims, checks),
        OverallVerdict.NEEDS_INFO: lambda: _steps_needs_info(claims, checks, missing),
        OverallVerdict.INCONCLUSIVE: lambda: _steps_inconclusive(claims, checks),
    }
    return _dedupe(builders[assessment.verdict]())[:MAX_NEXT_STEPS]


def _subject(check: Check, claims: list[ClaimAssessment]) -> str | None:
    """The thing a contradicting check is about: a `code span` in its title, or a claim entity."""
    match = _CODE_SPAN.search(check.title)
    if match:
        return match.group(1)
    for ca in claims:
        if ca.claim.id in check.claim_ids:
            for entity in ca.claim.entities:
                cleaned = _clip(entity.replace("`", "'"), 80)
                if cleaned:
                    return cleaned
    return None


def _steps_invalid(
    claims: list[ClaimAssessment], checks: list[Check], commit: str | None
) -> list[str]:
    sha = _short_sha(commit)
    where = f" (checked at `{sha}`)" if sha else ""
    steps: list[str] = []
    for check in _by_strength(_with_signal(checks, Signal.CONTRADICTS))[:3]:
        if _is_repro(check):
            steps.append(
                f"Share the sandbox run with the reporter and ask how their setup differs: "
                f"{_title(check)}"
            )
            continue
        subject = _subject(check, claims)
        if subject:
            steps.append(f"Ask the reporter where `{subject}` comes from: {_title(check)}{where}")
        else:
            steps.append(f"Ask the reporter to clarify: {_title(check)}{where}")
    if not steps:
        steps += [
            f"Ask the reporter about claim {_clip(ca.claim.id, 20)}: {_clip(ca.rationale, 160)}"
            for ca in claims
            if ca.verdict == Verdict.CONTRADICTED
        ][:3]
    steps.append(
        "Wait for the reporter's answer before deciding; a maintainer should confirm "
        "before closing or labelling anything."
    )
    return steps


def _locations(claims: list[ClaimAssessment], checks: list[Check]) -> list[tuple[str, int, str]]:
    """(path, line, why) worth opening first: reproductions, then the strongest support."""
    supports = _by_strength(_with_signal(checks, Signal.SUPPORTS))
    ordered = [c for c in supports if _is_repro(c)] + [c for c in supports if not _is_repro(c)]
    found = [(ev, _title(c)) for c in ordered for ev in c.evidence]
    found += [
        (ev, f"claim {_clip(ca.claim.id, 20)}")
        for ca in claims
        if ca.verdict == Verdict.SUPPORTED
        for ev in ca.evidence
    ]
    return [(ev.path, ev.line_start, why) for ev, why in found if ev.path and ev.line_start]


def _steps_valid(claims: list[ClaimAssessment], checks: list[Check]) -> list[str]:
    steps: list[str] = []
    seen: set[tuple[str, int]] = set()
    for path, line, why in _locations(claims, checks):
        if (path, line) in seen:
            continue
        seen.add((path, line))
        shown = _clip(path.replace("`", "'"), 120)
        steps.append(f"Start from `{shown}:{line}` ({_clip(why, 100)})")
        if len(seen) == 2:
            break
    reproduced = _strongest([c for c in checks if _is_repro(c)], Signal.SUPPORTS)
    if reproduced is not None:
        steps.append(f"Turn the sandbox reproduction into a regression test ({_title(reproduced)})")
    steps += [
        f"Double-check claim {_clip(ca.claim.id, 20)} by hand; it could not be verified: "
        f"“{_clip(ca.claim.text, 120)}”"
        for ca in claims
        if ca.verdict in (Verdict.UNVERIFIED, Verdict.NEEDS_INFO)
    ][:2]
    if not steps:
        steps.append("Confirm the behaviour on the analysed commit, then triage as usual.")
    return steps


def _steps_needs_info(
    claims: list[ClaimAssessment], checks: list[Check], missing: Sequence[str]
) -> list[str]:
    asks = _dedupe(
        [
            *missing,
            *(_title(c) for c in _with_signal(checks, Signal.MISSING_INFO)),
            *(
                f"details on claim {_clip(ca.claim.id, 20)}"
                for ca in claims
                if ca.verdict == Verdict.NEEDS_INFO
            ),
        ]
    )
    if asks:
        listed = "; ".join(_lower_first(_clip(a, 120)) for a in asks[:MAX_ASKS_IN_STEP])
        more = f" (and {len(asks) - MAX_ASKS_IN_STEP} more)" if len(asks) > MAX_ASKS_IN_STEP else ""
        first = f"Ask the reporter for: {listed}{more}"
    else:
        first = (
            "Ask the reporter for: steps to reproduce, the exact version and the full error output"
        )
    return [first, "Re-run Nomi once the report has been updated."]


def _steps_inconclusive(claims: list[ClaimAssessment], checks: list[Check]) -> list[str]:
    skipped = [c for c in checks if c.status == CheckStatus.SKIPPED]
    if skipped:
        settle = ", ".join(
            f"{_clip(c.id, 60)} ({_clip(c.detail, 80) or 'skipped'})" for c in skipped[:3]
        )
    else:
        settle = "a manual reproduction on the analysed commit"
    steps = [f"Manual review needed; checks that could settle it: {settle}"]
    hard = _strongest(checks, Signal.CONTRADICTS)
    reproduced = _strongest([c for c in checks if _is_repro(c)], Signal.SUPPORTS)
    if hard is not None and reproduced is not None:
        steps.append(
            f"Compare the sandbox reproduction ({_title(reproduced)}) with the contradicting "
            f"evidence ({_title(hard)})"
        )
    steps += [
        f"Verify claim {_clip(ca.claim.id, 20)} by hand: “{_clip(ca.claim.text, 120)}”"
        for ca in claims
        if ca.verdict == Verdict.UNVERIFIED
    ][:3]
    return steps


def limitations(checks: list[Check], model: str | None, commit: str | None) -> list[str]:
    """What this report could not do, so nobody mistakes silence for a clean bill."""
    notes = [
        f"{_title(c)}: skipped ({_clip(c.detail, 200) or 'no reason given'})"
        for c in checks
        if c.status == CheckStatus.SKIPPED
    ]
    notes.append(LLM_LIMITATION if model else NO_LLM_LIMITATION)
    if commit and commit.strip():
        notes.append(f"Evidence is pinned to commit {_clip(commit, 64)}")
    return _dedupe(notes)


def summarize(
    title: str,
    claims: list[ClaimAssessment],
    checks: list[Check],
    assessment: Assessment,
) -> str:
    """One or two deterministic sentences: claim counts per verdict and the top finding."""
    if claims:
        counts = Counter(ca.verdict for ca in claims)
        parts = ", ".join(f"{counts[v]} {_VERDICT_WORDS[v]}" for v in _VERDICT_WORDS if counts[v])
        noun = "claim" if len(claims) == 1 else "claims"
        first = f"Checked {len(claims)} {noun}: {parts}."
    else:
        noun = "check" if len(checks) == 1 else "checks"
        subject = _clip(title, 80) or "this report"
        first = f"No verifiable claims were extracted from “{subject}”; {len(checks)} {noun} ran."
    finding = _top_finding(checks, assessment)
    return f"{first} Top finding: {finding}." if finding else first


def _top_finding(checks: list[Check], assessment: Assessment) -> str | None:
    aligned = {
        OverallVerdict.LIKELY_INVALID: Signal.CONTRADICTS,
        OverallVerdict.LIKELY_VALID: Signal.SUPPORTS,
        OverallVerdict.NEEDS_INFO: Signal.MISSING_INFO,
    }.get(assessment.verdict)
    candidates = (
        _with_signal(checks, aligned)
        if aligned is not None
        else [c for c in checks if c.signal in (Signal.SUPPORTS, Signal.CONTRADICTS)]
    )
    ranked = _by_strength(candidates)
    if not ranked:
        ranked = _by_strength(
            c for c in checks if c.signal in (Signal.SUPPORTS, Signal.CONTRADICTS)
        )
    if not ranked or (ranked[0].signal != Signal.MISSING_INFO and _strength(ranked[0]) <= 0):
        return None
    return _title(ranked[0])
