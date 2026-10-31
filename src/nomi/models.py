"""Domain model shared by every Nomi stage.

Design rule: anything shown to a maintainer as *evidence* must be machine
verifiable -- a path and line range that exist at a pinned commit, a sandbox
command and its output, or a diff hunk. Language-model output is never
evidence on its own; it can only point at evidence that we then re-check.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------


class Kind(StrEnum):
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"


class ChangedFile(BaseModel):
    path: str
    status: str = "modified"  # added | modified | removed | renamed
    additions: int = 0
    deletions: int = 0
    previous_path: str | None = None
    patch: str | None = None  # unified diff hunks for this file (may be absent for big files)


class Submission(BaseModel):
    """An issue or pull request exactly as the reporter submitted it.

    Labels, comments and the close state are deliberately not part of this
    model: they are the maintainers' verdict and would leak the answer when
    Nomi is evaluated on historical issues.
    """

    kind: Kind
    repo: str  # "owner/name"
    number: int | None = None
    url: str | None = None
    title: str
    body: str = ""
    author: str | None = None
    created_at: datetime | None = None
    # pull requests only
    base_ref: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)

    @property
    def text(self) -> str:
        """Title and body as one document (what extractors read)."""
        return f"{self.title}\n\n{self.body or ''}".strip()


# --------------------------------------------------------------------------
# Deterministic artifacts found in the submission text
# --------------------------------------------------------------------------


class CodeBlock(BaseModel):
    language: str | None = None
    content: str
    line: int = 0  # 1-based line of the opening fence within Submission.text


class FileRef(BaseModel):
    path: str
    line_start: int | None = None
    line_end: int | None = None
    raw: str


class StackFrame(BaseModel):
    path: str
    line: int | None = None
    function: str | None = None
    raw: str


class StackTrace(BaseModel):
    language: str  # python | javascript | java | go | rust | c | unknown
    frames: list[StackFrame] = Field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None
    raw: str = ""


class Artifacts(BaseModel):
    """Checkable references pulled out of a submission without any model."""

    code_blocks: list[CodeBlock] = Field(default_factory=list)
    inline_code: list[str] = Field(default_factory=list)
    file_refs: list[FileRef] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    stack_traces: list[StackTrace] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    cli_flags: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    issue_refs: list[int] = Field(default_factory=list)
    cve_ids: list[str] = Field(default_factory=list)


class Section(StrEnum):
    STEPS_TO_REPRODUCE = "steps_to_reproduce"
    EXPECTED_BEHAVIOR = "expected_behavior"
    ACTUAL_BEHAVIOR = "actual_behavior"
    VERSION = "version"
    ENVIRONMENT = "environment"
    LOGS = "logs"  # traceback, error output or logs
    CODE_SAMPLE = "code_sample"


class Completeness(BaseModel):
    present: list[Section] = Field(default_factory=list)
    missing: list[Section] = Field(default_factory=list)
    score: float = 0.0  # 0..1, weighted by how much each section helps reproduction


# --------------------------------------------------------------------------
# Claims, evidence, checks
# --------------------------------------------------------------------------


class ClaimType(StrEnum):
    BEHAVIOR = "behavior"  # "X happens when Y"
    CODE_FACT = "code_fact"  # "function X uses strcpy", "file Y defines Z"
    LOCATION = "location"  # "the bug is in X"
    CAUSE = "cause"  # "the root cause is X"
    REPRODUCTION = "reproduction"  # "running X raises Y"
    ENVIRONMENT = "environment"  # "happens on version/OS X"
    SECURITY_IMPACT = "security_impact"  # "this allows RCE"
    CHANGE = "change"  # PR: "this change does X"
    OTHER = "other"


class Claim(BaseModel):
    id: str  # "C1", "C2", ...
    text: str  # atomic, self-contained statement
    type: ClaimType = ClaimType.OTHER
    quote: str = ""  # verbatim span of the submission this claim came from (re-verified)
    entities: list[str] = Field(default_factory=list)  # symbols, paths, flags named in the claim
    checkable: bool = True
    origin: Literal["llm", "rules"] = "rules"


class EvidenceKind(StrEnum):
    CODE = "code"
    DOC = "doc"
    TEST = "test"
    DIFF = "diff"
    EXECUTION = "execution"  # sandbox run
    HISTORY = "history"  # tags, commits, releases


class Evidence(BaseModel):
    kind: EvidenceKind
    description: str
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    excerpt: str | None = None
    url: str | None = None  # permalink pinned to the analysed commit


class Signal(StrEnum):
    SUPPORTS = "supports"  # evidence the report is accurate
    CONTRADICTS = "contradicts"  # evidence a claim is false or fabricated
    MISSING_INFO = "missing_info"  # the report lacks what is needed to verify it
    NEUTRAL = "neutral"  # informational only


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIPPED = "skipped"  # could not be run; `detail` says why
    INFO = "info"


class Check(BaseModel):
    id: str  # stable machine id, e.g. "grounding.symbol"
    title: str  # one human-readable sentence
    status: CheckStatus
    signal: Signal = Signal.NEUTRAL
    strength: float = 0.0  # 0..1, how much this check should move the assessment
    detail: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Verdicts and the final report
# --------------------------------------------------------------------------


class Verdict(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNVERIFIED = "unverified"
    NEEDS_INFO = "needs_info"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ClaimAssessment(BaseModel):
    claim: Claim
    verdict: Verdict
    confidence: Confidence
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)
    method: Literal["deterministic", "llm", "execution", "none"] = "none"


class OverallVerdict(StrEnum):
    LIKELY_VALID = "likely_valid"
    LIKELY_INVALID = "likely_invalid"
    NEEDS_INFO = "needs_info"
    INCONCLUSIVE = "inconclusive"


# Hidden marker that lets the GitHub App find and update its own comment
# instead of posting a new one on every run.
REPORT_MARKER = "<!-- nomi:report -->"

# How an overall verdict maps onto the triage labels used for evaluation.
# INCONCLUSIVE is an explicit abstention, not a guess.
LABEL_FOR_VERDICT: dict[OverallVerdict, str | None] = {
    OverallVerdict.LIKELY_VALID: "confirmed",
    OverallVerdict.LIKELY_INVALID: "invalid",
    OverallVerdict.NEEDS_INFO: "cannot_reproduce",
    OverallVerdict.INCONCLUSIVE: None,
}


class Assessment(BaseModel):
    verdict: OverallVerdict
    confidence: Confidence
    score: float = 0.0  # signed: positive leans valid, negative leans invalid
    reasons: list[str] = Field(default_factory=list)


class Report(BaseModel):
    schema_version: str = "1"
    nomi_version: str
    kind: Kind
    repo: str
    number: int | None = None
    url: str | None = None
    title: str
    commit: str | None = None  # repository commit all evidence is pinned to
    analyzed_at: datetime
    model: str | None = None  # LLM used; None means deterministic-only analysis
    summary: str
    claims: list[ClaimAssessment] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    completeness: Completeness = Field(default_factory=Completeness)
    assessment: Assessment
    missing_information: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)

    @property
    def predicted_label(self) -> str | None:
        return LABEL_FOR_VERDICT[self.assessment.verdict]
