"""Run one triage job end to end: fetch, analyse, render, then publish or save.

This is the seam between the webhook server and the analysis pipeline. The
analysis and rendering functions are injected so the server can be tested
without a model, a clone or the network, and so that the server module never
imports the heavy pipeline at import time.

Posting to GitHub is opt-in: with ``dry_run`` (the default) the report is
written to ``output_dir`` as Markdown and JSON instead, so a misconfigured
installation can never comment on a stranger's issue.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nomi.config import Settings
from nomi.github.auth import get_installation_token
from nomi.github.client import GitHubClient
from nomi.github.publish import upsert_report_comment
from nomi.models import Report, Submission

logger = logging.getLogger(__name__)

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class TriageJob:
    """One unit of work for the background worker, built from a webhook delivery."""

    repo: str
    number: int
    kind: str
    installation_id: int | None
    delivery_id: str
    trigger: str


def process_job(
    job: TriageJob,
    settings: Settings,
    *,
    analyze_fn: Callable[[Submission], Report],
    render_fn: Callable[[Report], str],
    client_factory: Callable[[str | None], GitHubClient] | None = None,
) -> Path | dict[str, Any]:
    """Triage ``job``; returns the written ``.md`` path (dry run) or the GitHub comment."""
    logger.info(
        "triaging %s#%d (%s, trigger=%s, delivery=%s)",
        job.repo,
        job.number,
        job.kind,
        job.trigger,
        job.delivery_id,
    )
    factory = client_factory or GitHubClient
    with factory(resolve_token(job, settings)) as client:
        submission = client.get_submission(job.repo, job.number)
        report = analyze_fn(submission)
        markdown = render_fn(report)
        if settings.dry_run:
            path = write_report_files(report, markdown, settings.output_dir, job.repo, job.number)
            logger.info("dry run: wrote %s", path)
            return path
        comment = upsert_report_comment(
            client, job.repo, job.number, markdown, app_id=settings.app_id
        )
        logger.info("published report for %s#%d: %s", job.repo, job.number, comment.get("html_url"))
        return comment


def resolve_token(job: TriageJob, settings: Settings) -> str | None:
    """Installation token when running as a GitHub App, else the configured token."""
    if settings.app_id and settings.app_private_key_path and job.installation_id is not None:
        private_key = settings.app_private_key_path.read_text(encoding="utf-8")
        return get_installation_token(settings.app_id, private_key, job.installation_id)
    return settings.token


def write_report_files(
    report: Report, markdown: str, output_dir: Path, repo: str, number: int
) -> Path:
    """Write ``{owner}__{name}__{number}.md`` and ``.json``; returns the Markdown path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    owner, _, name = repo.partition("/")
    stem = f"{_safe(owner)}__{_safe(name)}__{number}"
    md_path = output_dir / f"{stem}.md"
    _atomic_write(md_path, markdown)
    _atomic_write(output_dir / f"{stem}.json", report.model_dump_json(indent=2))
    return md_path


def _safe(part: str) -> str:
    return _UNSAFE_FILENAME_CHARS.sub("_", part) or "_"


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written report."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
