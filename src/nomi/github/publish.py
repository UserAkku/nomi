"""Publish a report as ONE comment per issue/PR, updated in place on every run.

Re-running Nomi (a new push to a PR, a ``/nomi`` command) must never add
another comment: maintainers would rightly uninstall a bot that spams threads.
Every report carries the hidden :data:`~nomi.models.REPORT_MARKER`, and the
next run edits the comment that holds it. When several comments carry the
marker (for example a human pasted a report), a bot-authored one is preferred
so Nomi edits its own comment rather than someone else's.
"""

from __future__ import annotations

from typing import Any

from nomi.github.client import GitHubClient
from nomi.models import REPORT_MARKER

# GitHub rejects comment bodies longer than 65536 characters.
MAX_COMMENT_CHARS = 65_536
_TRUNCATION_NOTICE = "\n\n_Report truncated to fit GitHub's comment size limit._\n"


def upsert_report_comment(
    client: GitHubClient, repo: str, number: int, markdown: str
) -> dict[str, Any]:
    """Create Nomi's report comment, or update the existing one; returns the comment."""
    body = prepare_body(markdown)
    existing = find_report_comment(client.list_comments(repo, number))
    if existing is None:
        return client.create_comment(repo, number, body)
    if existing.get("body") == body:
        return existing
    return client.update_comment(repo, int(existing["id"]), body)


def prepare_body(markdown: str) -> str:
    """Ensure the marker is present and the body fits GitHub's size limit."""
    body = markdown or ""
    prefix = "" if REPORT_MARKER in body else f"{REPORT_MARKER}\n"
    if len(prefix) + len(body) <= MAX_COMMENT_CHARS:
        return prefix + body
    # Truncating may cut a marker placed late in the text: pin one to the top instead.
    prefix = "" if body.startswith(REPORT_MARKER) else f"{REPORT_MARKER}\n"
    room = MAX_COMMENT_CHARS - len(prefix) - len(_TRUNCATION_NOTICE)
    return prefix + body[:room] + _TRUNCATION_NOTICE


def find_report_comment(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the comment holding the marker, preferring bot-authored, then oldest."""
    candidates = [c for c in comments if _is_report(c)]
    if not candidates:
        return None
    by_bot = [c for c in candidates if _is_bot(c)]
    return (by_bot or candidates)[0]


def _is_report(comment: dict[str, Any]) -> bool:
    body = comment.get("body")
    comment_id = comment.get("id")
    return (
        isinstance(body, str)
        and REPORT_MARKER in body
        and isinstance(comment_id, int)
        and not isinstance(comment_id, bool)
    )


def _is_bot(comment: dict[str, Any]) -> bool:
    user = comment.get("user")
    return isinstance(user, dict) and user.get("type") == "Bot"
