"""Tests for combining evidence into claim verdicts and the overall assessment."""

from __future__ import annotations

import math

import pytest

from nomi.assess import (
    DEFAULT_THRESHOLDS,
    LLM_LIMITATION,
    NO_EVIDENCE,
    NO_LLM_LIMITATION,
    Thresholds,
    assess,
    finalize_claims,
    limitations,
    missing_information,
    next_steps,
    summarize,
)
from nomi.models import (
    Assessment,
    Check,
    CheckStatus,
    Claim,
    ClaimAssessment,
    ClaimType,
    Completeness,
    Confidence,
    Evidence,
    EvidenceKind,
    Kind,
    OverallVerdict,
    Section,
    Signal,
    Verdict,
)
from nomi.repo.workspace import Workspace

FULL = Completeness(present=list(Section), score=1.0)
EMPTY = Completeness(missing=list(Section), score=0.0)
SHA = "abc1234def5678abc1234def5678abc1234def56"


def check(
    id: str,
    signal: Signal = Signal.NEUTRAL,
    strength: float = 0.0,
    *,
    status: CheckStatus | None = None,
    claims: tuple[str, ...] = (),
    title: str | None = None,
    detail: str = "",
    evidence: list[Evidence] | None = None,
) -> Check:
    default_status = {
        Signal.SUPPORTS: CheckStatus.PASS,
        Signal.CONTRADICTS: CheckStatus.FAIL,
        Signal.MISSING_INFO: CheckStatus.WARN,
    }.get(signal, CheckStatus.INFO)
    return Check(
        id=id,
        title=title if title is not None else f"title of {id}",
        status=status or default_status,
        signal=signal,
        strength=strength,
        detail=detail,
        claim_ids=list(claims),
        evidence=evidence or [],
    )


def claim(
    id: str = "C1",
    type: ClaimType = ClaimType.BEHAVIOR,
    *,
    text: str | None = None,
    checkable: bool = True,
    entities: tuple[str, ...] = (),
) -> Claim:
    return Claim(
        id=id,
        text=text or f"claim {id}",
        type=type,
        checkable=checkable,
        entities=list(entities),
    )


def judged(
    c: Claim,
    verdict: Verdict,
    confidence: Confidence = Confidence.HIGH,
    *,
    method: str = "llm",
    rationale: str = "the model says so",
) -> ClaimAssessment:
    return ClaimAssessment(
        claim=c, verdict=verdict, confidence=confidence, rationale=rationale, method=method
    )


def evidence(path: str = "src/widget/parser.py", start: int = 8, end: int = 9) -> Evidence:
    return Evidence(
        kind=EvidenceKind.CODE,
        description="code",
        path=path,
        line_start=start,
        line_end=end,
        url=f"https://github.com/acme/widget/blob/{SHA}/{path}#L{start}-L{end}",
    )


def one(c: Claim, checks: list[Check], judgments=(), completeness=FULL) -> ClaimAssessment:
    [result] = finalize_claims([c], checks, list(judgments), completeness)
    return result


# --------------------------------------------------------------------------
# finalize_claims
# --------------------------------------------------------------------------


class TestFinalizeClaims:
    def test_strong_contradiction_is_high_and_carries_check_evidence(self) -> None:
        ev = evidence()
        result = one(
            claim(),
            [check("grounding.symbol", Signal.CONTRADICTS, 0.8, claims=("C1",), evidence=[ev])],
        )
        assert result.verdict == Verdict.CONTRADICTED
        assert result.confidence == Confidence.HIGH
        assert result.method == "deterministic"
        assert result.evidence == [ev]
        assert result.rationale == "title of grounding.symbol"

    def test_medium_contradiction_and_repro_method(self) -> None:
        result = one(claim(), [check("repro.run", Signal.CONTRADICTS, 0.65, claims=("C1",))])
        assert (result.verdict, result.confidence, result.method) == (
            Verdict.CONTRADICTED,
            Confidence.MEDIUM,
            "execution",
        )

    def test_weak_contradiction_does_not_decide(self) -> None:
        result = one(claim(), [check("grounding.symbol", Signal.CONTRADICTS, 0.59, claims=("C1",))])
        assert result.verdict == Verdict.UNVERIFIED
        assert result.rationale == NO_EVIDENCE

    def test_strongest_contradiction_wins_and_beats_support(self) -> None:
        checks = [
            check("grounding.a", Signal.SUPPORTS, 0.9, claims=("C1",)),
            check("grounding.b", Signal.CONTRADICTS, 0.6, claims=("C1",)),
            check("grounding.c", Signal.CONTRADICTS, 0.9, claims=("C1",)),
        ]
        result = one(claim(), checks)
        assert result.verdict == Verdict.CONTRADICTED
        assert result.rationale == "title of grounding.c"
        assert result.confidence == Confidence.HIGH

    def test_support_is_medium_unless_reproduced(self) -> None:
        plain = one(claim(), [check("grounding.symbol", Signal.SUPPORTS, 0.9, claims=("C1",))])
        assert (plain.verdict, plain.confidence, plain.method) == (
            Verdict.SUPPORTED,
            Confidence.MEDIUM,
            "deterministic",
        )
        weak_repro = one(claim(), [check("repro.run", Signal.SUPPORTS, 0.7, claims=("C1",))])
        assert (weak_repro.confidence, weak_repro.method) == (Confidence.MEDIUM, "execution")
        repro = one(
            claim(),
            [
                check("grounding.symbol", Signal.SUPPORTS, 1.0, claims=("C1",)),
                check("repro.run", Signal.SUPPORTS, 0.8, claims=("C1",)),
            ],
        )
        assert (repro.confidence, repro.method) == (Confidence.HIGH, "execution")
        assert repro.rationale == "title of repro.run"

    def test_llm_judgment_is_capped_at_medium(self) -> None:
        c = claim()
        result = one(c, [], [judged(c, Verdict.SUPPORTED, Confidence.HIGH)])
        assert (result.verdict, result.confidence, result.method) == (
            Verdict.SUPPORTED,
            Confidence.MEDIUM,
            "llm",
        )
        assert result.rationale == "the model says so"
        low = one(c, [], [judged(c, Verdict.CONTRADICTED, Confidence.LOW)])
        assert (low.verdict, low.confidence) == (Verdict.CONTRADICTED, Confidence.LOW)

    def test_deterministic_check_beats_llm(self) -> None:
        c = claim()
        result = one(
            c,
            [check("grounding.symbol", Signal.CONTRADICTS, 0.8, claims=("C1",))],
            [judged(c, Verdict.SUPPORTED)],
        )
        assert result.verdict == Verdict.CONTRADICTED
        assert result.method == "deterministic"

    @pytest.mark.parametrize(
        ("verdict", "signal", "strength"),
        [
            (Verdict.SUPPORTED, Signal.CONTRADICTS, 0.3),
            (Verdict.SUPPORTED, Signal.CONTRADICTS, 0.55),
            (Verdict.CONTRADICTED, Signal.SUPPORTS, 0.45),
        ],
    )
    def test_llm_vs_deterministic_conflict_is_unverified(
        self, verdict: Verdict, signal: Signal, strength: float
    ) -> None:
        c = claim()
        ev = evidence()
        result = one(
            c,
            [check("grounding.x", signal, strength, claims=("C1",), evidence=[ev])],
            [judged(c, verdict)],
        )
        assert (result.verdict, result.confidence) == (Verdict.UNVERIFIED, Confidence.LOW)
        assert result.rationale.startswith("conflicting signals:")
        assert "grounding.x" in result.rationale
        assert result.evidence == [ev]

    def test_weak_opposing_check_does_not_block_llm(self) -> None:
        c = claim()
        result = one(
            c,
            [check("grounding.x", Signal.CONTRADICTS, 0.29, claims=("C1",))],
            [judged(c, Verdict.SUPPORTED)],
        )
        assert result.verdict == Verdict.SUPPORTED

    def test_agreeing_weak_check_keeps_llm_verdict(self) -> None:
        c = claim()
        result = one(
            c,
            [check("grounding.x", Signal.SUPPORTS, 0.4, claims=("C1",))],
            [judged(c, Verdict.SUPPORTED)],
        )
        assert (result.verdict, result.method) == (Verdict.SUPPORTED, "llm")

    def test_referenced_code_exists_for_code_claims_only(self) -> None:
        checks = [
            check("grounding.path", Signal.SUPPORTS, 0.1, claims=("C1",), evidence=[evidence()]),
            check("grounding.symbol", Signal.SUPPORTS, 0.05, claims=("C1",)),
        ]
        for kind in (ClaimType.CODE_FACT, ClaimType.LOCATION):
            result = one(claim(type=kind), checks)
            assert (result.verdict, result.confidence) == (Verdict.SUPPORTED, Confidence.LOW)
            assert "referenced code exists" in result.rationale
            assert result.evidence == [evidence()]
        assert one(claim(type=ClaimType.BEHAVIOR), checks).verdict == Verdict.UNVERIFIED
        too_weak = [check("grounding.path", Signal.SUPPORTS, 0.14, claims=("C1",))]
        assert one(claim(type=ClaimType.CODE_FACT), too_weak).verdict == Verdict.UNVERIFIED

    def test_not_checkable_needs_info(self) -> None:
        result = one(claim(checkable=False), [])
        assert (result.verdict, result.confidence) == (Verdict.NEEDS_INFO, Confidence.LOW)

    @pytest.mark.parametrize("missing", [Section.VERSION, Section.ENVIRONMENT])
    def test_environment_claim_needs_info_when_context_missing(self, missing: Section) -> None:
        completeness = Completeness(missing=[missing], score=0.7)
        result = one(claim(type=ClaimType.ENVIRONMENT), [], completeness=completeness)
        assert result.verdict == Verdict.NEEDS_INFO

    def test_environment_claim_with_context_is_unverified(self) -> None:
        assert one(claim(type=ClaimType.ENVIRONMENT), []).verdict == Verdict.UNVERIFIED

    def test_unverified_keeps_llm_rationale(self) -> None:
        c = claim()
        result = one(c, [], [judged(c, Verdict.UNVERIFIED, rationale="  code is ambiguous ")])
        assert result.verdict == Verdict.UNVERIFIED
        assert result.rationale == f"{NO_EVIDENCE} Model note: code is ambiguous"
        assert result.method == "llm"

    def test_order_linking_and_unknown_judgments(self) -> None:
        c1, c2, c3 = claim("C1"), claim("C2"), claim("C3")
        checks = [check("grounding.a", Signal.CONTRADICTS, 0.9, claims=("C2",))]
        judgments = [
            judged(claim("C9"), Verdict.SUPPORTED),
            judged(c3, Verdict.SUPPORTED),
            judged(c3, Verdict.CONTRADICTED),
        ]
        results = finalize_claims([c1, c2, c3], checks, judgments, FULL)
        assert [r.claim.id for r in results] == ["C1", "C2", "C3"]
        assert [r.verdict for r in results] == [
            Verdict.UNVERIFIED,
            Verdict.CONTRADICTED,
            Verdict.SUPPORTED,  # first judgment for a claim wins
        ]
        assert results[2].claim is c3

    def test_empty_inputs(self) -> None:
        assert finalize_claims([], [], [], EMPTY) == []

    def test_garbage_strengths_do_not_crash(self) -> None:
        nan = one(claim(), [check("grounding.a", Signal.CONTRADICTS, math.nan, claims=("C1",))])
        assert nan.verdict == Verdict.UNVERIFIED
        huge = one(claim(), [check("grounding.a", Signal.CONTRADICTS, 7.0, claims=("C1",))])
        assert (huge.verdict, huge.confidence) == (Verdict.CONTRADICTED, Confidence.HIGH)


# --------------------------------------------------------------------------
# assess
# --------------------------------------------------------------------------


def llm_claims(verdict: Verdict, n: int) -> list[ClaimAssessment]:
    return [judged(claim(f"C{i}"), verdict, Confidence.MEDIUM) for i in range(n)]


class TestAssess:
    def test_single_hard_contradiction_is_medium_invalid(self) -> None:
        checks = [check("grounding.symbol", Signal.CONTRADICTS, 0.8)]
        result = assess(Kind.ISSUE, [], checks, FULL)
        assert (result.verdict, result.confidence) == (
            OverallVerdict.LIKELY_INVALID,
            Confidence.MEDIUM,
        )
        assert result.score == -0.8
        assert result.reasons[0].startswith("Strong contradicting evidence")

    def test_two_hard_contradictions_are_high(self) -> None:
        checks = [
            check("grounding.a", Signal.CONTRADICTS, 0.7),
            check("grounding.b", Signal.CONTRADICTS, 0.7),
        ]
        assert assess(Kind.ISSUE, [], checks, FULL).confidence == Confidence.HIGH

    def test_large_total_contradiction_is_high(self) -> None:
        checks = [
            check("grounding.a", Signal.CONTRADICTS, 0.8),
            check("docs.default", Signal.CONTRADICTS, 0.6),
        ]
        assert assess(Kind.PULL_REQUEST, [], checks, FULL).confidence == Confidence.HIGH

    def test_hard_contradiction_wins_over_plenty_of_support(self) -> None:
        checks = [
            check("grounding.a", Signal.CONTRADICTS, 0.75),
            check("grounding.b", Signal.SUPPORTS, 1.0),
            check("docs.c", Signal.SUPPORTS, 1.0),
        ]
        assert assess(Kind.ISSUE, [], checks, FULL).verdict == OverallVerdict.LIKELY_INVALID

    def test_reproduction_against_hard_contradiction_is_inconclusive(self) -> None:
        checks = [
            check("grounding.a", Signal.CONTRADICTS, 0.9),
            check("repro.run", Signal.SUPPORTS, 0.85),
        ]
        result = assess(Kind.ISSUE, [], checks, FULL)
        assert (result.verdict, result.confidence) == (
            OverallVerdict.INCONCLUSIVE,
            Confidence.MEDIUM,
        )
        assert "Conflicting strong evidence" in result.reasons[0]

    def test_reproduced_is_high_valid(self) -> None:
        checks = [check("repro.run", Signal.SUPPORTS, 0.8), check("x.a", Signal.CONTRADICTS, 0.5)]
        result = assess(Kind.ISSUE, [], checks, EMPTY)
        assert (result.verdict, result.confidence) == (OverallVerdict.LIKELY_VALID, Confidence.HIGH)

    def test_weak_reproduction_is_just_support(self) -> None:
        result = assess(Kind.ISSUE, [], [check("repro.run", Signal.SUPPORTS, 0.7)], FULL)
        assert (result.verdict, result.confidence) == (OverallVerdict.LIKELY_VALID, Confidence.LOW)

    @pytest.mark.parametrize(
        ("score", "confidence"), [(0.1, Confidence.MEDIUM), (0.3, Confidence.LOW)]
    )
    def test_incomplete_issue_needs_info(self, score: float, confidence: Confidence) -> None:
        completeness = Completeness(missing=[Section.VERSION], score=score)
        checks = [check("grounding.a", Signal.SUPPORTS, 0.4)]
        result = assess(Kind.ISSUE, [], checks, completeness)
        assert (result.verdict, result.confidence) == (OverallVerdict.NEEDS_INFO, confidence)

    def test_incomplete_issue_with_strong_support_is_valid(self) -> None:
        checks = [check("grounding.a", Signal.SUPPORTS, 0.7)]
        result = assess(Kind.ISSUE, [], checks, Completeness(score=0.1))
        assert result.verdict == OverallVerdict.LIKELY_VALID

    def test_pull_requests_ignore_completeness(self) -> None:
        result = assess(Kind.PULL_REQUEST, [], [], EMPTY)
        assert (result.verdict, result.confidence) == (OverallVerdict.INCONCLUSIVE, Confidence.LOW)

    @pytest.mark.parametrize(
        ("strengths", "confidence"),
        [((0.3, 0.3), Confidence.LOW), ((0.5, 0.5), Confidence.MEDIUM)],
    )
    def test_score_thresholds_valid(self, strengths: tuple[float, ...], confidence) -> None:
        checks = [check(f"docs.{i}", Signal.SUPPORTS, s) for i, s in enumerate(strengths)]
        result = assess(Kind.ISSUE, [], checks, FULL)
        assert (result.verdict, result.confidence) == (OverallVerdict.LIKELY_VALID, confidence)

    @pytest.mark.parametrize(
        ("strengths", "confidence"),
        [((0.3, 0.3), Confidence.LOW), ((0.5, 0.5), Confidence.MEDIUM)],
    )
    def test_score_thresholds_invalid(self, strengths: tuple[float, ...], confidence) -> None:
        checks = [check(f"docs.{i}", Signal.CONTRADICTS, s) for i, s in enumerate(strengths)]
        result = assess(Kind.ISSUE, [], checks, FULL)
        assert (result.verdict, result.confidence) == (OverallVerdict.LIKELY_INVALID, confidence)
        assert "outweighs" in result.reasons[0]

    def test_grounding_support_is_capped(self) -> None:
        checks = [check(f"grounding.{i}", Signal.SUPPORTS, 0.9) for i in range(4)]
        assert assess(Kind.ISSUE, [], checks, FULL).score == 1.0
        checks.append(check("docs.x", Signal.SUPPORTS, 0.25))
        assert assess(Kind.ISSUE, [], checks, FULL).score == 1.25

    def test_llm_claims_add_fixed_weight(self) -> None:
        valid = assess(Kind.ISSUE, llm_claims(Verdict.SUPPORTED, 2), [], FULL)
        assert (valid.verdict, valid.score) == (OverallVerdict.LIKELY_VALID, 0.6)
        invalid = assess(Kind.ISSUE, llm_claims(Verdict.CONTRADICTED, 2), [], FULL)
        assert (invalid.verdict, invalid.score) == (OverallVerdict.LIKELY_INVALID, -0.6)
        deterministic = [judged(claim(), Verdict.SUPPORTED, method="deterministic")]
        assert assess(Kind.ISSUE, deterministic, [], FULL).score == 0.0

    def test_llm_alone_never_reaches_high(self) -> None:
        result = assess(Kind.ISSUE, llm_claims(Verdict.CONTRADICTED, 10), [], FULL)
        assert result.verdict == OverallVerdict.LIKELY_INVALID
        assert result.confidence != Confidence.HIGH

    def test_partly_complete_issue_needs_info_low(self) -> None:
        checks = [check("grounding.a", Signal.SUPPORTS, 0.6)]
        completeness = Completeness(score=0.5)
        weak = [check("grounding.a", Signal.SUPPORTS, 0.2)]
        assert assess(Kind.ISSUE, [], checks, completeness).verdict == OverallVerdict.LIKELY_VALID
        result = assess(Kind.ISSUE, [], weak, completeness)
        assert (result.verdict, result.confidence) == (OverallVerdict.NEEDS_INFO, Confidence.LOW)

    def test_inconclusive_by_default(self) -> None:
        checks = [check("a.b", Signal.SUPPORTS, 0.3), check("c.d", Signal.CONTRADICTS, 0.2)]
        result = assess(Kind.ISSUE, [], checks, FULL)
        assert (result.verdict, result.confidence, result.score) == (
            OverallVerdict.INCONCLUSIVE,
            Confidence.LOW,
            0.1,
        )

    def test_thresholds_override(self) -> None:
        checks = [check("grounding.a", Signal.CONTRADICTS, 0.8)]
        lenient = Thresholds(hard_contradiction=0.9, invalid=1.0)
        assert assess(Kind.ISSUE, [], checks, FULL, thresholds=lenient).verdict == (
            OverallVerdict.INCONCLUSIVE
        )
        support = [check("docs.a", Signal.SUPPORTS, 0.4)]
        assert assess(Kind.ISSUE, [], support, FULL).verdict == OverallVerdict.INCONCLUSIVE
        eager = Thresholds(valid=0.3)
        assert assess(Kind.ISSUE, [], support, FULL, thresholds=eager).verdict == (
            OverallVerdict.LIKELY_VALID
        )
        capped = Thresholds(grounding_support_cap=0.5)
        grounded = [check("grounding.a", Signal.SUPPORTS, 1.0)]
        assert assess(Kind.ISSUE, [], grounded, FULL, thresholds=capped).score == 0.5
        assert Thresholds() == DEFAULT_THRESHOLDS

    def test_reasons_list_top_five_contributions(self) -> None:
        checks = [
            check("grounding.symbol", Signal.CONTRADICTS, 0.8, title="Symbol `foo` is missing."),
            check("docs.x", Signal.SUPPORTS, 0.3),
            check("completeness.version", Signal.MISSING_INFO, 0.0, title="No version"),
            check("neutral.y", Signal.NEUTRAL, 0.9),
            check("zero.z", Signal.SUPPORTS, 0.0),
            *[check(f"docs.extra{i}", Signal.SUPPORTS, 0.1) for i in range(5)],
        ]
        reasons = assess(Kind.ISSUE, [], checks, FULL).reasons
        assert len(reasons) == 6
        assert reasons[1] == "-0.80 grounding.symbol: Symbol `foo` is missing"
        assert reasons[2] == "+0.30 docs.x: title of docs.x"
        assert all("neutral.y" not in r and "zero.z" not in r for r in reasons)
        missing_only = assess(Kind.ISSUE, [], checks[2:3], FULL).reasons
        assert missing_only[1] == "?0.00 completeness.version: No version"

    def test_score_is_rounded(self) -> None:
        checks = [check("a.b", Signal.SUPPORTS, 0.123456)]
        assert assess(Kind.PULL_REQUEST, [], checks, FULL).score == 0.123

    def test_float_noise_does_not_flip_threshold(self) -> None:
        checks = [check(f"docs.{i}", Signal.SUPPORTS, s) for i, s in enumerate((0.1, 0.2, 0.3))]
        assert assess(Kind.ISSUE, [], checks, FULL).verdict == OverallVerdict.LIKELY_VALID

    def test_empty_issue(self) -> None:
        result = assess(Kind.ISSUE, [], [], EMPTY)
        assert (result.verdict, result.confidence) == (OverallVerdict.NEEDS_INFO, Confidence.MEDIUM)
        assert len(result.reasons) == 1

    def test_garbage_completeness_score(self) -> None:
        result = assess(Kind.ISSUE, [], [], Completeness(score=math.nan))
        assert result.verdict == OverallVerdict.NEEDS_INFO


# --------------------------------------------------------------------------
# missing_information / next_steps / limitations / summarize
# --------------------------------------------------------------------------


class TestMissingInformation:
    def test_sections_in_stable_order_without_duplicates(self) -> None:
        completeness = Completeness(
            missing=[
                Section.LOGS,
                Section.ACTUAL_BEHAVIOR,
                Section.VERSION,
                Section.EXPECTED_BEHAVIOR,
            ]
        )
        asks = missing_information(Kind.ISSUE, completeness, [], [])
        assert asks == [
            "What you expected to happen vs. what happened",
            "The exact version where this happens",
            "The full error output or traceback",
        ]

    def test_all_sections_have_asks(self) -> None:
        asks = missing_information(Kind.ISSUE, EMPTY, [], [])
        assert "A minimal code sample or command that reproduces it" in asks
        assert "Your environment (OS, runtime version)" in asks
        assert len(asks) == 6

    def test_claims_and_checks_are_included(self) -> None:
        needs = ClaimAssessment(
            claim=claim("C2", text="Breaks\non Windows"),
            verdict=Verdict.NEEDS_INFO,
            confidence=Confidence.LOW,
            rationale="",
        )
        checks = [
            check("completeness.logs", Signal.MISSING_INFO, title="No traceback was included."),
            check("completeness.dup", Signal.MISSING_INFO, title="No traceback was included"),
        ]
        asks = missing_information(Kind.ISSUE, FULL, [needs], checks)
        assert asks == [
            "More detail to verify claim C2: “Breaks on Windows”",
            "No traceback was included",
        ]

    def test_pull_requests_only_list_check_asks(self) -> None:
        assert missing_information(Kind.PULL_REQUEST, EMPTY, [], []) == []
        checks = [check("pr.tests", Signal.MISSING_INFO, title="No tests accompany the change")]
        assert missing_information(Kind.PULL_REQUEST, EMPTY, [], checks) == [
            "No tests accompany the change"
        ]


def verdict_of(v: OverallVerdict) -> Assessment:
    return Assessment(verdict=v, confidence=Confidence.MEDIUM)


class TestNextSteps:
    def test_invalid_asks_where_the_symbol_comes_from(self) -> None:
        checks = [
            check(
                "grounding.symbol",
                Signal.CONTRADICTS,
                0.9,
                title="Symbol `parse_uri` does not exist in the repository",
            ),
            check("repro.run", Signal.CONTRADICTS, 0.7, title="The command ran without error"),
            check("docs.default", Signal.CONTRADICTS, 0.6, claims=("C1",), title="Default differs"),
        ]
        claims = [judged(claim("C1", entities=("timeout",)), Verdict.CONTRADICTED)]
        steps = next_steps(verdict_of(OverallVerdict.LIKELY_INVALID), claims, checks, commit=SHA)
        assert steps[0] == (
            "Ask the reporter where `parse_uri` comes from: Symbol `parse_uri` does not exist "
            "in the repository (checked at `abc1234`)"
        )
        assert steps[1].startswith("Share the sandbox run with the reporter")
        assert steps[2].startswith("Ask the reporter where `timeout` comes from")
        assert "maintainer should confirm" in steps[-1]
        assert not any(s.lower().startswith(("close", "auto")) for s in steps)

    def test_invalid_from_model_only(self) -> None:
        claims = [judged(claim("C1"), Verdict.CONTRADICTED, rationale="no such flag")]
        steps = next_steps(verdict_of(OverallVerdict.LIKELY_INVALID), claims, [])
        assert steps[0] == "Ask the reporter about claim C1: no such flag"
        unknown = next_steps(
            verdict_of(OverallVerdict.LIKELY_INVALID),
            [],
            [check("x.y", Signal.CONTRADICTS, 0.8, title="Mismatch")],
        )
        assert unknown[0] == "Ask the reporter to clarify: Mismatch"

    def test_valid_points_at_evidence(self, sample_repo: Workspace) -> None:
        url = sample_repo.permalink("src/widget/parser.py", 8, 9)
        ev = Evidence(
            kind=EvidenceKind.CODE,
            description="raise on empty url",
            path="src/widget/parser.py",
            line_start=8,
            line_end=9,
            excerpt=sample_repo.excerpt("src/widget/parser.py", 8, 9),
            url=url,
        )
        checks = [
            check("grounding.symbol", Signal.SUPPORTS, 0.5, evidence=[ev]),
            check("repro.run", Signal.SUPPORTS, 0.9, title="ValueError reproduced"),
        ]
        unverified = ClaimAssessment(
            claim=claim("C2"), verdict=Verdict.UNVERIFIED, confidence=Confidence.LOW, rationale=""
        )
        steps = next_steps(verdict_of(OverallVerdict.LIKELY_VALID), [unverified], checks)
        assert steps[0] == "Start from `src/widget/parser.py:8` (title of grounding.symbol)"
        assert "regression test" in steps[1]
        assert steps[2].startswith("Double-check claim C2")
        assert "raise ValueError" in (ev.excerpt or "")

    def test_valid_without_evidence(self) -> None:
        steps = next_steps(verdict_of(OverallVerdict.LIKELY_VALID), [], [])
        assert steps == ["Confirm the behaviour on the analysed commit, then triage as usual."]

    def test_needs_info_lists_asks(self) -> None:
        missing = missing_information(Kind.ISSUE, EMPTY, [], [])
        steps = next_steps(verdict_of(OverallVerdict.NEEDS_INFO), [], [], missing=missing)
        assert steps[0].startswith(
            "Ask the reporter for: step-by-step instructions to reproduce the problem; "
            "what you expected"
        )
        assert steps[0].endswith("(and 2 more)")
        fallback = next_steps(verdict_of(OverallVerdict.NEEDS_INFO), [], [])
        assert fallback[0].startswith("Ask the reporter for: steps to reproduce")
        from_checks = next_steps(
            verdict_of(OverallVerdict.NEEDS_INFO),
            [],
            [check("c.v", Signal.MISSING_INFO, title="OS not stated")],
        )
        assert from_checks[0] == "Ask the reporter for: OS not stated"

    def test_inconclusive_names_checks_that_could_settle_it(self) -> None:
        checks = [
            check("repro.sandbox", status=CheckStatus.SKIPPED, detail="Docker is not available"),
            check("grounding.a", Signal.CONTRADICTS, 0.8, title="Missing"),
            check("repro.run", Signal.SUPPORTS, 0.9, title="Reproduced"),
        ]
        claims = [judged(claim("C1"), Verdict.UNVERIFIED)]
        steps = next_steps(verdict_of(OverallVerdict.INCONCLUSIVE), claims, checks)
        assert steps[0] == (
            "Manual review needed; checks that could settle it: "
            "repro.sandbox (Docker is not available)"
        )
        assert "Compare the sandbox reproduction" in steps[1]
        assert steps[2].startswith("Verify claim C1 by hand")
        bare = next_steps(verdict_of(OverallVerdict.INCONCLUSIVE), [], [])
        assert bare == [
            "Manual review needed; checks that could settle it: a manual reproduction on the "
            "analysed commit"
        ]

    @pytest.mark.parametrize("verdict", list(OverallVerdict))
    def test_at_most_five_steps(self, verdict: OverallVerdict) -> None:
        checks = [
            check(f"grounding.{i}", signal, 0.9, evidence=[evidence(start=i + 1)], claims=("C1",))
            for i, signal in enumerate([Signal.SUPPORTS, Signal.CONTRADICTS] * 10)
        ]
        checks += [check(f"s.{i}", status=CheckStatus.SKIPPED) for i in range(10)]
        claims = [judged(claim(f"C{i}"), Verdict.UNVERIFIED) for i in range(10)]
        steps = next_steps(verdict_of(verdict), claims, checks, missing=["a"] * 10)
        assert 1 <= len(steps) <= 5
        assert len(set(steps)) == len(steps)


class TestLimitations:
    def test_skipped_model_and_commit(self) -> None:
        checks = [
            check(
                "repro.sandbox", status=CheckStatus.SKIPPED, title="Reproduce", detail="No Docker"
            ),
            check("x.y", status=CheckStatus.SKIPPED, title="Other"),
            check("ok", Signal.SUPPORTS, 0.5),
        ]
        notes = limitations(checks, "qwen3:4b", SHA)
        assert notes == [
            "Reproduce: skipped (No Docker)",
            "Other: skipped (no reason given)",
            LLM_LIMITATION,
            f"Evidence is pinned to commit {SHA}",
        ]

    def test_no_model_no_commit(self) -> None:
        assert limitations([], None, None) == [NO_LLM_LIMITATION]


class TestSummarize:
    def test_counts_and_top_finding(self) -> None:
        claims = [
            judged(claim("C1"), Verdict.CONTRADICTED),
            judged(claim("C2"), Verdict.CONTRADICTED),
            judged(claim("C3"), Verdict.SUPPORTED),
            judged(claim("C4"), Verdict.NEEDS_INFO),
        ]
        checks = [
            check("a.weak", Signal.CONTRADICTS, 0.6, title="Weak"),
            check("a.strong", Signal.CONTRADICTS, 0.9, title="Symbol `x` missing."),
            check("a.support", Signal.SUPPORTS, 1.0, title="Exists"),
        ]
        text = summarize("t", claims, checks, verdict_of(OverallVerdict.LIKELY_INVALID))
        assert text == (
            "Checked 4 claims: 1 supported, 2 contradicted, 1 needs info. "
            "Top finding: Symbol `x` missing."
        )
        valid = summarize("t", claims[:1], checks, verdict_of(OverallVerdict.LIKELY_VALID))
        assert valid == "Checked 1 claim: 1 contradicted. Top finding: Exists."

    def test_no_claims_and_no_finding(self) -> None:
        text = summarize("Crash\non start", [], [], verdict_of(OverallVerdict.INCONCLUSIVE))
        assert text == "No verifiable claims were extracted from “Crash on start”; 0 checks ran."

    def test_needs_info_finding(self) -> None:
        checks = [check("c.v", Signal.MISSING_INFO, title="No version given")]
        text = summarize("", [], checks, verdict_of(OverallVerdict.NEEDS_INFO))
        assert text.endswith("1 check ran. Top finding: No version given.")
        assert "“this report”" in text

    def test_deterministic(self) -> None:
        claims = [judged(claim(f"C{i}"), Verdict.UNVERIFIED) for i in range(3)]
        checks = [check(f"x.{i}", Signal.SUPPORTS, 0.5, title=f"T{i}") for i in range(3)]
        a = verdict_of(OverallVerdict.LIKELY_VALID)
        assert summarize("t", claims, checks, a) == summarize("t", claims, checks, a)
        assert summarize("t", claims, checks, a).endswith("Top finding: T0.")


def test_end_to_end_on_sample_repo(sample_repo: Workspace) -> None:
    """Real workspace evidence flows through to verdicts and suggestions."""
    path = "src/widget/parser.py"
    ev = Evidence(
        kind=EvidenceKind.CODE,
        description="parse_url definition",
        path=path,
        line_start=6,
        line_end=6,
        excerpt=sample_repo.excerpt(path, 6),
        url=sample_repo.permalink(path, 6),
    )
    claims = [
        claim("C1", ClaimType.CODE_FACT, text="`parse_url` raises ValueError on empty input"),
        claim("C2", ClaimType.CODE_FACT, text="`parse_uri` strips whitespace"),
    ]
    checks = [
        check("grounding.symbol", Signal.SUPPORTS, 0.5, claims=("C1",), evidence=[ev]),
        check(
            "grounding.symbol.missing",
            Signal.CONTRADICTS,
            0.8,
            claims=("C2",),
            title="No symbol `parse_uri` exists in the repository",
        ),
    ]
    results = finalize_claims(claims, checks, [], FULL)
    assert [r.verdict for r in results] == [Verdict.SUPPORTED, Verdict.CONTRADICTED]
    assessment = assess(Kind.ISSUE, results, checks, FULL)
    assert assessment.verdict == OverallVerdict.LIKELY_INVALID
    steps = next_steps(assessment, results, checks, commit=sample_repo.commit)
    assert f"`{sample_repo.commit[:7]}`" in steps[0]
    assert ev.url and ev.url.endswith("#L6")
    assert ev.excerpt and "def parse_url" in ev.excerpt


def test_clean_sandbox_run_means_cannot_reproduce_not_invalid() -> None:
    checks = [check("repro.run", Signal.CONTRADICTS, 0.5, title="Did not reproduce")]
    result = assess(Kind.ISSUE, [], checks, FULL)
    assert result.verdict is OverallVerdict.NEEDS_INFO
    assert "could not be reproduced" in result.reasons[0]


def test_fabrication_still_beats_a_clean_sandbox_run() -> None:
    checks = [
        check("repro.run", Signal.CONTRADICTS, 0.5),
        check("grounding.symbol", Signal.CONTRADICTS, 0.8),
    ]
    assert assess(Kind.ISSUE, [], checks, FULL).verdict is OverallVerdict.LIKELY_INVALID


def test_many_uncertain_warnings_never_add_up_to_invalid() -> None:
    # e.g. a traceback full of the reporter's own paths and other tools' flags
    warns = [
        check(f"grounding.flag{i}", Signal.CONTRADICTS, 0.35, status=CheckStatus.WARN)
        for i in range(10)
    ]
    result = assess(Kind.ISSUE, [], warns, FULL)
    assert result.verdict is not OverallVerdict.LIKELY_INVALID


def test_isolated_line_mismatch_amid_valid_references_is_version_skew() -> None:
    held = [check("grounding.line", Signal.SUPPORTS, 0.2) for _ in range(6)]
    off = check("grounding.line", Signal.CONTRADICTS, 0.75, title="line past end of file")
    result = assess(Kind.ISSUE, [], [*held, off], FULL)
    assert result.verdict is not OverallVerdict.LIKELY_INVALID
    assert any("version difference" in r for r in result.reasons)


def test_fabrication_with_few_valid_references_stays_invalid() -> None:
    checks = [
        check("grounding.file", Signal.SUPPORTS, 0.15),
        check("grounding.line", Signal.CONTRADICTS, 0.75),
        check("grounding.symbol", Signal.CONTRADICTS, 0.8),
    ]
    assert assess(Kind.ISSUE, [], checks, FULL).verdict is OverallVerdict.LIKELY_INVALID


def test_version_skew_never_excuses_a_nonexistent_function() -> None:
    held = [check("grounding.symbol", Signal.SUPPORTS, 0.15) for _ in range(6)]
    missing = check("grounding.symbol", Signal.CONTRADICTS, 0.8, title="not defined anywhere")
    result = assess(Kind.ISSUE, [], [*held, missing], FULL)
    assert result.verdict is OverallVerdict.LIKELY_INVALID
