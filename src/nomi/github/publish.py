"""Publish a report as ONE comment per issue/PR, updated in place on every run.

Re-running Nomi (a new push to a PR, a ``/nomi`` command) must never add
another comment: maintainers would rightly uninstall a bot that spams threads.
Every report carries the hidden :data:`~nomi.models.REPORT_MARKER`, and the
next run edits the comment that holds it, but only a comment Nomi itself
wrote: anyone can paste the marker into their own comment, and Nomi must never
rewrite a stranger's words. Without a recognisable Nomi comment it posts a new one.
"""

from __future__ import annotations

from typing import Any

from nomi.github.client import GitHubClient
from nomi.models import REPORT_MARKER

# GitHub rejects comment bodies longer than 65536 characters.
MAX_COMMENT_CHARS = 65_536
_TRUNCATION_NOTICE = "\n\n_Report truncated to fit GitHub's comment size limit._\n"


def upsert_report_comment(
    client: GitHubClient,
    repo: str,
    number: int,
    markdown: str,
    *,
    bot_login: str | None = None,
    app_id: str | None = None,
) -> dict[str, Any]:
    """Create Nomi's report comment, or update its existing one; returns the comment.

    ``app_id`` (the GitHub App's id) and ``bot_login`` identify Nomi's own
    comments; see :func:`find_report_comment`.
    """
    body = prepare_body(markdown)
    comments = client.list_comments(repo, number)
    existing = find_report_comment(comments, bot_login=bot_login, app_id=app_id)
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


def find_report_comment(
    comments: list[dict[str, Any]],
    *,
    bot_login: str | None = None,
    app_id: str | None = None,
) -> dict[str, Any] | None:
    """The oldest report comment written by Nomi itself, or None; never a human's.

    A Nomi comment is authored by a Bot account and, when known, was posted via
    the GitHub App ``app_id`` or by the account ``bot_login``. With neither
    known, only a ``...[bot]`` account's comment that starts with the marker counts.
    """
    for comment in comments:
        if _is_report(comment) and _is_own(comment, bot_login, app_id):
            return comment
    return None


def _is_report(comment: dict[str, Any]) -> bool:
    body = comment.get("body")
    comment_id = comment.get("id")
    return (
        isinstance(body, str)
        and REPORT_MARKER in body
        and isinstance(comment_id, int)
        and not isinstance(comment_id, bool)
    )


def _is_own(comment: dict[str, Any], bot_login: str | None, app_id: str | None) -> bool:
    user = comment.get("user")
    if not isinstance(user, dict) or user.get("type") != "Bot":
        return False
    login = user.get("login")
    login = login.casefold() if isinstance(login, str) else ""
    if app_id or bot_login:
        app = comment.get("performed_via_github_app")
        app_matches = (
            bool(app_id) and isinstance(app, dict) and str(app.get("id")) == str(app_id).strip()
        )
        login_matches = bool(bot_login) and login == str(bot_login).casefold()
        return app_matches or login_matches
    first_line = str(comment.get("body")).partition("\n")[0]
    return login.endswith("[bot]") and REPORT_MARKER in first_line
