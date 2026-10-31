"""One analysis, end to end: extract -> verify -> reproduce -> judge -> assess.

Cheap deterministic checks run first and pin their evidence to the analysed
commit. The language model only splits the report into claims and judges
retrieved code, and everything it returns is re-checked before it can count.
Each stage degrades gracefully: no repository, no model or no Docker still
yields a report that states what could not be checked.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from nomi import __version__
from nomi.assess import (
    assess,
    finalize_claims,
    limitations,
    missing_information,
    next_steps,
    summarize,
)
from nomi.config import Settings
from nomi.extract.artifacts import extract_artifacts
from nomi.extract.claims import extract_claims
from nomi.extract.completeness import assess_completeness
from nomi.llm.base import LLM
from nomi.models import Check, ClaimAssessment, Kind, Report, Submission
from nomi.repo.search import CodeIndex
from nomi.repo.workspace import GitError, Workspace, open_github_workspace
from nomi.verify.grounding import run_grounding_checks
from nomi.verify.judge import judge_claims
from nomi.verify.pr import run_pr_checks
from nomi.verify.quotes import check_quoted_code
from nomi.verify.sandbox import DockerSandbox, run_reproduction
from nomi.verify.traces import check_stack_traces
from nomi.verify.versions import check_versions

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalyzeOptions:
    max_claims: int = 6
    judge_top_k: int = 6
    judge_max_claims: int = 4  # each judged claim is one model call (~10-30 s on an 8 GB laptop)


def analyze(
    submission: Submission,
    workspace: Workspace | None,
    *,
    llm: LLM | None = None,
    sandbox: DockerSandbox | None = None,
    options: AnalyzeOptions | None = None,
    notes: list[str] | None = None,
) -> Report:
    """Analyse one submission. `notes` are extra limitations to surface (e.g. why no model)."""
    options = options or AnalyzeOptions()
    timings: dict[str, float] = {}

    @contextmanager
    def timed(stage: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            timings[stage] = round(time.perf_counter() - start, 3)

    with timed("extract"):
        artifacts = extract_artifacts(submission.text)
        completeness = assess_completeness(submission, artifacts)
    with timed("claims"):
        claims = extract_claims(submission, artifacts, llm, max_claims=options.max_claims)

    checks: list[Check] = []
    with timed("verify"):
        if workspace is not None:
            checks += run_grounding_checks(artifacts, claims, workspace)
            checks += check_stack_traces(artifacts, workspace)
            checks += check_quoted_code(artifacts, workspace)
            checks += check_versions(
                artifacts,
                workspace,
                text=submission.text,
                project_name=_project_name(submission.repo),
            )
        if submission.kind is Kind.PULL_REQUEST:
            checks += run_pr_checks(submission, claims, workspace)
    if workspace is not None and submission.kind is Kind.ISSUE:
        with timed("reproduce"):
            checks += run_reproduction(submission, artifacts, workspace, sandbox)

    judgments: list[ClaimAssessment] = []
    if llm is not None and workspace is not None and claims:
        with timed("judge"):
            index = CodeIndex.build(workspace)
            judgments = judge_claims(
                claims,
                workspace,
                index,
                llm,
                top_k=options.judge_top_k,
                max_claims=options.judge_max_claims,
            )

    claim_results = finalize_claims(claims, checks, judgments, completeness)
    assessment = assess(submission.kind, claim_results, checks, completeness)
    commit = workspace.commit if workspace is not None else None
    # Only credit the model if it actually contributed; a fallback run is deterministic.
    model_used = llm.name if llm is not None and _model_contributed(claims, judgments) else None

    extra = list(notes or [])
    if workspace is None:
        extra.append("The repository could not be loaded, so no claim was checked against code.")
    if llm is not None and model_used is None:
        extra.append(
            f"The language model ({llm.name}) did not return usable output; rules were used instead."
        )

    missing = missing_information(submission.kind, completeness, claim_results, checks)
    return Report(
        nomi_version=__version__,
        kind=submission.kind,
        repo=submission.repo,
        number=submission.number,
        url=submission.url,
        title=submission.title,
        commit=commit,
        analyzed_at=datetime.now(UTC),
        model=model_used,
        summary=summarize(submission.title, claim_results, checks, assessment),
        claims=claim_results,
        checks=checks,
        completeness=completeness,
        assessment=assessment,
        missing_information=missing,
        next_steps=next_steps(assessment, claim_results, checks, commit=commit, missing=missing),
        limitations=extra + limitations(checks, model_used, commit),
        timings=timings,
    )


def _model_contributed(claims: list, judgments: list[ClaimAssessment]) -> bool:
    return any(c.origin == "llm" for c in claims) or any(j.method == "llm" for j in judgments)


def _project_name(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


# ---------------------------------------------------------------- factories


def build_llm(settings: Settings, *, enabled: bool = True) -> tuple[LLM | None, str | None]:
    """The configured local model, or (None, reason) when it cannot be used."""
    if not enabled or not settings.llm_enabled:
        return None, "Language model disabled; deterministic checks only."
    from nomi.llm.ollama import OllamaLLM

    llm = OllamaLLM(
        settings.llm_model,
        settings.ollama_host,
        num_ctx=settings.llm_num_ctx,
        timeout_s=settings.llm_timeout_s,
        cache_dir=settings.cache_dir,
    )
    ok, message = llm.health()
    if not ok:
        return None, f"Language model unavailable ({message}); deterministic checks only."
    return llm, None


def build_sandbox(
    settings: Settings, *, enabled: bool = True
) -> tuple[DockerSandbox | None, str | None]:
    if not enabled or not settings.sandbox_enabled:
        return None, None  # run_reproduction reports the skip itself
    sandbox = DockerSandbox(
        settings.sandbox_image,
        timeout_s=settings.sandbox_timeout_s,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
    )
    ok, message = sandbox.available()
    return (sandbox, None) if ok else (None, f"Sandbox unavailable: {message}")


def open_workspace_for(
    submission: Submission, settings: Settings, *, at_head: bool = False
) -> tuple[Workspace | None, str | None]:
    """Check out the code the reporter was looking at.

    Issues: the default branch as it was when the issue was filed (so a bug
    fixed later is not mistaken for a fabricated one). Pull requests: the PR
    head commit, which is what the description talks about.
    """
    try:
        if submission.kind is Kind.PULL_REQUEST and submission.number is not None:
            ref = submission.head_sha or f"refs/remotes/origin/pr/{submission.number}"
            ws = open_github_workspace(
                submission.repo, cache_dir=settings.cache_dir, ref=ref, pr_number=submission.number
            )
        else:
            at = None if at_head else submission.created_at
            ws = open_github_workspace(submission.repo, cache_dir=settings.cache_dir, at=at)
    except (GitError, ValueError, OSError) as exc:
        log.warning("could not open %s: %s", submission.repo, exc)
        return None, f"Repository checkout failed: {exc}"
    return ws, None


def analyze_submission(submission: Submission, settings: Settings) -> Report:
    """Everything the GitHub App needs: checkout, model, sandbox, analysis."""
    workspace, ws_note = open_workspace_for(submission, settings)
    llm, llm_note = build_llm(settings)
    sandbox, sb_note = build_sandbox(settings)
    notes = [n for n in (ws_note, llm_note, sb_note) if n]
    return analyze(submission, workspace, llm=llm, sandbox=sandbox, notes=notes)
