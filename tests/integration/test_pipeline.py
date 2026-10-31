"""End-to-end behaviour of the analysis pipeline (no network, no real model)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nomi.llm.fake import FakeLLM
from nomi.models import (
    ChangedFile,
    CheckStatus,
    Kind,
    OverallVerdict,
    Report,
    Submission,
    Verdict,
)
from nomi.pipeline import AnalyzeOptions, analyze
from nomi.repo.workspace import Workspace

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "fabricated-security-report"


def assert_evidence_is_real(report: Report, ws: Workspace) -> None:
    """Every cited path/line must exist in the analysed tree: zero fabricated citations."""
    items = [e for c in report.checks for e in c.evidence] + [
        e for c in report.claims for e in c.evidence
    ]
    for ev in items:
        if ev.path is None or ev.kind.value in ("diff", "execution", "history"):
            continue
        lines = ws.lines(ev.path)
        assert lines is not None, f"evidence cites a missing file: {ev.path}"
        if ev.line_start is not None:
            assert 1 <= ev.line_start <= len(lines), f"{ev.path}:{ev.line_start} out of range"
        if ev.line_end is not None:
            assert ev.line_start is not None and ev.line_start <= ev.line_end <= len(lines)


def fabricated_submission() -> Submission:
    return Submission.model_validate_json((EXAMPLE / "input.json").read_text())


def test_fabricated_security_report_is_contradicted_offline():
    ws = Workspace(EXAMPLE / "repo", repo="example/minihttp")
    report = analyze(fabricated_submission(), ws)

    assert report.assessment.verdict is OverallVerdict.LIKELY_INVALID
    failed = {c.id for c in report.checks if c.status is CheckStatus.FAIL}
    assert "grounding.symbol" in failed  # mh_easy_setopt_url_safe() does not exist
    assert "grounding.line" in failed  # lib/easy.c has 62 lines, report cites 412
    assert "grounding.file" in failed  # lib/vtls/openssl_ext.c does not exist
    assert any(c.verdict is Verdict.CONTRADICTED for c in report.claims)
    assert report.model is None  # deterministic run is labelled as such
    assert_evidence_is_real(report, ws)


GENUINE_BODY = """Calling `parse_url("")` raises an exception instead of returning an empty result.

### Steps to reproduce
1. Install widget 1.1.0
2. Run:

```python
from widget.parser import parse_url
parse_url("")
```

### Expected behavior
An empty dict.

### Actual behavior
```
Traceback (most recent call last):
  File "/home/me/app.py", line 2, in <module>
    parse_url("")
  File "/home/me/.venv/lib/python3.12/site-packages/widget/parser.py", line 9, in parse_url
    raise ValueError("empty url")
ValueError: empty url
```

### Environment
widget version 1.1.0, Python 3.12, Ubuntu 22.04
"""


def test_genuine_report_is_grounded(sample_repo):
    sub = Submission(
        kind=Kind.ISSUE,
        repo="acme/widget",
        number=7,
        title="parse_url crashes on empty string",
        body=GENUINE_BODY,
    )
    report = analyze(sub, sample_repo)

    assert report.assessment.verdict is not OverallVerdict.LIKELY_INVALID
    trace = [c for c in report.checks if c.id == "trace.frames"]
    assert trace and trace[0].status is CheckStatus.PASS
    assert not [c for c in report.checks if c.status is CheckStatus.FAIL]
    assert report.completeness.score >= 0.8
    assert_evidence_is_real(report, sample_repo)


def test_vague_report_needs_information(sample_repo):
    sub = Submission(
        kind=Kind.ISSUE, repo="acme/widget", title="It does not work", body="Please fix ASAP!!!"
    )
    report = analyze(sub, sample_repo)
    assert report.assessment.verdict is OverallVerdict.NEEDS_INFO
    assert report.missing_information


def test_without_repository_still_reports(sample_repo):
    sub = Submission(
        kind=Kind.ISSUE, repo="acme/widget", title="Crash in `parse_url`", body="It crashes."
    )
    report = analyze(sub, None)
    assert report.commit is None
    assert any("could not be loaded" in note for note in report.limitations)


def test_pull_request_claims_checked_against_diff(sample_repo):
    patch = "@@ -16,2 +16,2 @@\n def normalize_host(host: str) -> str:\n-    return host.strip().lower()\n+    return host.lower()\n"
    sub = Submission(
        kind=Kind.PULL_REQUEST,
        repo="acme/widget",
        number=12,
        title="Fix host normalisation",
        body="This fixes a bug in `normalize_host` and adds unit tests for it. No breaking changes.",
        changed_files=[
            ChangedFile(path="src/widget/parser.py", additions=1, deletions=1, patch=patch)
        ],
    )
    report = analyze(sub, sample_repo)
    tests_claim = [c for c in report.checks if c.id == "pr.tests_claim"]
    assert tests_claim and tests_claim[0].status is CheckStatus.FAIL
    assert_evidence_is_real(report, sample_repo)


def _fake_llm_handler(system: str, prompt: str, schema):
    name = schema.__name__
    if "Claim" in name:
        return {
            "claims": [
                {
                    "text": "parse_url raises ValueError on empty input",
                    "type": "behavior",
                    "quote": 'Calling `parse_url("")` raises an exception',
                    "entities": ["parse_url"],
                    "checkable": True,
                }
            ]
        }
    # Judge: cite the line that actually raises, plus one hallucinated quote.
    eid = None
    for block in prompt.split("\nE")[1:]:
        if 'raise ValueError("empty url")' in block:
            eid = "E" + block.split(":", 1)[0].strip()
            break
    return {
        "verdict": "supports",
        "citations": [
            {"evidence_id": eid or "E1", "quote": 'raise ValueError("empty url")'},
            {"evidence_id": eid or "E1", "quote": "raise ValueError('this line does not exist')"},
        ],
        "rationale": "The function raises on empty input.",
    }


def test_model_output_is_rechecked(sample_repo):
    llm = FakeLLM(_fake_llm_handler, name="fake-qwen")
    sub = Submission(
        kind=Kind.ISSUE, repo="acme/widget", title="parse_url crashes", body=GENUINE_BODY
    )
    report = analyze(sub, sample_repo, llm=llm, options=AnalyzeOptions(judge_max_claims=2))

    assert report.model == "fake-qwen"
    llm_claims = [c for c in report.claims if c.claim.origin == "llm"]
    assert llm_claims, "model claims should be used"
    for c in llm_claims:
        assert c.claim.quote in sub.text  # quotes are verbatim spans of the report
    assert_evidence_is_real(report, sample_repo)


def test_report_is_json_serialisable(sample_repo):
    report = analyze(fabricated_submission(), Workspace(EXAMPLE / "repo", repo="example/minihttp"))
    data = json.loads(report.model_dump_json())
    assert data["assessment"]["verdict"] == "likely_invalid"
    assert Report.model_validate(data) == report


@pytest.mark.parametrize("body", ["", "\x00\x01 binary junk �", "🔥" * 5000, "```\nunclosed fence"])
def test_hostile_or_empty_input_never_crashes(sample_repo, body):
    sub = Submission(kind=Kind.ISSUE, repo="acme/widget", title="t", body=body)
    report = analyze(sub, sample_repo)
    assert report.assessment.verdict in OverallVerdict
