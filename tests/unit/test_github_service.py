"""Tests for comment publishing (one comment per thread) and the triage job runner."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from nomi.config import Settings
from nomi.github.client import GitHubClient
from nomi.github.publish import (
    MAX_COMMENT_CHARS,
    find_report_comment,
    prepare_body,
    upsert_report_comment,
)
from nomi.github.service import TriageJob, process_job, resolve_token, write_report_files
from nomi.models import (
    REPORT_MARKER,
    Assessment,
    Confidence,
    Kind,
    OverallVerdict,
    Report,
    Submission,
)

API = "https://api.github.com"
COMMENTS = "/repos/o/r/issues/7/comments"
BOT = "nomi-triage[bot]"


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client() -> Iterator[GitHubClient]:
    with GitHubClient("tok") as gh:
        yield gh


def make_report(sub: Submission) -> Report:
    return Report(
        nomi_version="0.1.0",
        kind=sub.kind,
        repo=sub.repo,
        number=sub.number,
        title=sub.title,
        analyzed_at=datetime(2024, 1, 1, tzinfo=UTC),
        summary="résumé ✓",
        assessment=Assessment(verdict=OverallVerdict.INCONCLUSIVE, confidence=Confidence.LOW),
    )


def render(report: Report) -> str:
    return f"{REPORT_MARKER}\n## Nomi report for {report.title}\n"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "github_token": "settings-token",
        "app_id": None,
        "app_private_key_path": None,
        "dry_run": True,
        "output_dir": tmp_path / "out",
    }
    values.update(overrides)
    return Settings(**values)


def make_job(**overrides: Any) -> TriageJob:
    values: dict[str, Any] = {
        "repo": "o/r",
        "number": 7,
        "kind": "issue",
        "installation_id": None,
        "delivery_id": "d-1",
        "trigger": "issues.opened",
    }
    values.update(overrides)
    return TriageJob(**values)


def issue_json(title: str = "Bug ✓") -> dict[str, Any]:
    return {"number": 7, "title": title, "body": "body", "user": {"login": "a"}}


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------


def test_upsert_creates_when_no_report_exists(api: respx.MockRouter, client: GitHubClient) -> None:
    api.get(COMMENTS).mock(
        return_value=httpx.Response(200, json=[{"id": 1, "body": "thanks!", "user": {}}])
    )
    create = api.post(COMMENTS).mock(return_value=httpx.Response(201, json={"id": 2}))

    assert upsert_report_comment(client, "o/r", 7, "## Report") == {"id": 2}

    sent = json.loads(create.calls.last.request.read())["body"]
    assert sent == f"{REPORT_MARKER}\n## Report"


def test_upsert_updates_existing_report(api: respx.MockRouter, client: GitHubClient) -> None:
    old = {"id": 5, "body": f"{REPORT_MARKER}\nold", "user": {"type": "Bot", "login": BOT}}
    api.get(COMMENTS).mock(return_value=httpx.Response(200, json=[{"id": 1, "body": "hi"}, old]))
    create = api.post(COMMENTS).mock(return_value=httpx.Response(201, json={}))
    update = api.patch("/repos/o/r/issues/comments/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "body": "new"})
    )

    result = upsert_report_comment(client, "o/r", 7, f"{REPORT_MARKER}\nnew")

    assert result == {"id": 5, "body": "new"}
    assert create.call_count == 0
    assert json.loads(update.calls.last.request.read())["body"] == f"{REPORT_MARKER}\nnew"


def test_upsert_skips_identical_update(api: respx.MockRouter, client: GitHubClient) -> None:
    same = {"id": 5, "body": f"{REPORT_MARKER}\nsame", "user": {"type": "Bot", "login": BOT}}
    api.get(COMMENTS).mock(return_value=httpx.Response(200, json=[same]))
    update = api.patch("/repos/o/r/issues/comments/5").mock(return_value=httpx.Response(200))

    assert upsert_report_comment(client, "o/r", 7, "same") == same
    assert update.call_count == 0


def test_find_prefers_bot_comment_then_oldest() -> None:
    human = {"id": 1, "body": f"quoting {REPORT_MARKER}", "user": {"type": "User"}}
    bot_old = {"id": 2, "body": REPORT_MARKER, "user": {"type": "Bot", "login": BOT}}
    bot_new = {"id": 3, "body": REPORT_MARKER, "user": {"type": "Bot", "login": BOT}}
    assert find_report_comment([human, bot_old, bot_new]) is bot_old
    assert find_report_comment([human]) is None
    assert find_report_comment([]) is None


def comment(
    comment_id: int, *, login: str, kind: str = "Bot", app: int | None = None, body: str = ""
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": body or f"{REPORT_MARKER}\n### Nomi evidence report",
        "user": {"login": login, "type": kind},
        "performed_via_github_app": {"id": app, "slug": "x"} if app is not None else None,
    }


def test_upsert_never_edits_a_human_comment_with_the_marker(
    api: respx.MockRouter, client: GitHubClient
) -> None:
    human = comment(1, login="mallory", kind="User")
    api.get(COMMENTS).mock(return_value=httpx.Response(200, json=[human]))
    create = api.post(COMMENTS).mock(return_value=httpx.Response(201, json={"id": 2}))
    update = api.patch("/repos/o/r/issues/comments/1").mock(return_value=httpx.Response(200))

    assert upsert_report_comment(client, "o/r", 7, "## Report") == {"id": 2}
    assert upsert_report_comment(client, "o/r", 7, "## Report", app_id="123") == {"id": 2}
    assert create.call_count == 2
    assert update.call_count == 0


def test_find_report_comment_only_matches_nomi_itself() -> None:
    human = comment(1, login="mallory", kind="User", app=123)
    other_app = comment(2, login="other-bot[bot]", app=999)
    ours = comment(3, login=BOT, app=123)
    assert find_report_comment([human, other_app, ours], app_id="123") is ours
    assert find_report_comment([human, other_app], app_id="123") is None
    assert find_report_comment([human, other_app, ours], bot_login="NOMI-triage[bot]") is ours
    assert find_report_comment([other_app], bot_login=BOT) is None
    assert find_report_comment([comment(4, login=BOT)], bot_login=BOT, app_id="123") is not None
    # With no identity configured: a [bot] account whose comment starts with the marker.
    assert find_report_comment([human, other_app]) is other_app
    quoted = comment(5, login=BOT, body=f"see the old report:\n{REPORT_MARKER}")
    assert find_report_comment([quoted]) is None
    assert find_report_comment([comment(6, login="sneaky", kind="Bot")]) is None


def test_process_job_updates_only_the_configured_apps_comment(
    api: respx.MockRouter, tmp_path: Path
) -> None:
    api.get("/repos/o/r/issues/7").mock(return_value=httpx.Response(200, json=issue_json()))
    other_app = comment(2, login="other-bot[bot]", app=999)
    ours = comment(3, login=BOT, app=123)
    api.get(COMMENTS).mock(return_value=httpx.Response(200, json=[other_app, ours]))
    stranger = api.patch("/repos/o/r/issues/comments/2").mock(return_value=httpx.Response(200))
    update = api.patch("/repos/o/r/issues/comments/3").mock(
        return_value=httpx.Response(200, json={"id": 3})
    )
    settings = make_settings(tmp_path, dry_run=False, app_id="123")

    result = process_job(make_job(), settings, analyze_fn=make_report, render_fn=render)

    assert result == {"id": 3}
    assert update.call_count == 1 and stranger.call_count == 0


def test_find_ignores_malformed_comments() -> None:
    comments: list[dict[str, Any]] = [
        {"id": None, "body": REPORT_MARKER},
        {"id": True, "body": REPORT_MARKER},
        {"id": 4, "body": None},
        {"body": REPORT_MARKER},
        {"id": 9, "body": "no marker", "user": "weird"},
    ]
    assert find_report_comment(comments) is None


def test_prepare_body_keeps_existing_marker_once() -> None:
    body = prepare_body(f"text\n{REPORT_MARKER}\n")
    assert body.count(REPORT_MARKER) == 1
    assert prepare_body("") == f"{REPORT_MARKER}\n"


def test_prepare_body_truncates_huge_reports_and_keeps_marker_on_top() -> None:
    huge = "é" * (MAX_COMMENT_CHARS * 2) + REPORT_MARKER
    body = prepare_body(huge)
    assert len(body) <= MAX_COMMENT_CHARS
    assert body.startswith(REPORT_MARKER)
    assert body.rstrip().endswith("comment size limit._")

    exact = REPORT_MARKER + "x" * (MAX_COMMENT_CHARS - len(REPORT_MARKER))
    assert prepare_body(exact) == exact


# --------------------------------------------------------------------------
# process_job
# --------------------------------------------------------------------------


def test_process_job_dry_run_writes_md_and_json(api: respx.MockRouter, tmp_path: Path) -> None:
    api.get("/repos/o/r/issues/7").mock(return_value=httpx.Response(200, json=issue_json()))
    post = api.post(COMMENTS).mock(return_value=httpx.Response(201, json={}))
    seen: list[Submission] = []

    def analyze(sub: Submission) -> Report:
        seen.append(sub)
        return make_report(sub)

    result = process_job(make_job(), make_settings(tmp_path), analyze_fn=analyze, render_fn=render)

    assert result == tmp_path / "out" / "o__r__7.md"
    assert isinstance(result, Path)
    assert result.read_text(encoding="utf-8") == f"{REPORT_MARKER}\n## Nomi report for Bug ✓\n"
    report = Report.model_validate_json((tmp_path / "out" / "o__r__7.json").read_text("utf-8"))
    assert (report.repo, report.number, report.summary) == ("o/r", 7, "résumé ✓")
    assert seen[0].kind is Kind.ISSUE and seen[0].title == "Bug ✓"
    assert post.call_count == 0
    assert not list((tmp_path / "out").glob("*.tmp"))


def test_process_job_dry_run_overwrites_previous_report(
    api: respx.MockRouter, tmp_path: Path
) -> None:
    route = api.get("/repos/o/r/issues/7").mock(
        side_effect=[
            httpx.Response(200, json=issue_json("first")),
            httpx.Response(200, json=issue_json("second")),
        ]
    )
    settings = make_settings(tmp_path)
    for _ in range(2):
        path = process_job(make_job(), settings, analyze_fn=make_report, render_fn=render)
    assert isinstance(path, Path)
    assert "second" in path.read_text(encoding="utf-8")
    assert route.call_count == 2
    assert sorted(p.name for p in path.parent.iterdir()) == ["o__r__7.json", "o__r__7.md"]


def test_process_job_publishes_when_not_dry_run(api: respx.MockRouter, tmp_path: Path) -> None:
    api.get("/repos/o/r/issues/7").mock(return_value=httpx.Response(200, json=issue_json()))
    api.get(COMMENTS).mock(return_value=httpx.Response(200, json=[]))
    post = api.post(COMMENTS).mock(
        return_value=httpx.Response(201, json={"id": 1, "html_url": "https://x"})
    )

    result = process_job(
        make_job(), make_settings(tmp_path, dry_run=False), analyze_fn=make_report, render_fn=render
    )

    assert result == {"id": 1, "html_url": "https://x"}
    assert post.calls.last.request.headers["Authorization"] == "Bearer settings-token"
    assert not (tmp_path / "out").exists()


def test_process_job_uses_injected_client_factory(api: respx.MockRouter, tmp_path: Path) -> None:
    api.get("/repos/o/r/issues/7").mock(return_value=httpx.Response(200, json=issue_json()))
    made: list[GitHubClient] = []
    tokens: list[str | None] = []

    def factory(token: str | None) -> GitHubClient:
        tokens.append(token)
        made.append(GitHubClient(token))
        return made[-1]

    process_job(
        make_job(),
        make_settings(tmp_path),
        analyze_fn=make_report,
        render_fn=render,
        client_factory=factory,
    )
    assert tokens == ["settings-token"]
    assert made[0]._http.is_closed


def test_process_job_propagates_analysis_errors(api: respx.MockRouter, tmp_path: Path) -> None:
    api.get("/repos/o/r/issues/7").mock(return_value=httpx.Response(200, json=issue_json()))

    def boom(sub: Submission) -> Report:
        raise RuntimeError("model crashed")

    with pytest.raises(RuntimeError, match="model crashed"):
        process_job(make_job(), make_settings(tmp_path), analyze_fn=boom, render_fn=render)
    assert not (tmp_path / "out").exists()


def test_installation_token_used_for_app_jobs(api: respx.MockRouter, tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "app.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    exchange = api.post("/app/installations/77/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_install"})
    )
    settings = make_settings(tmp_path, app_id="123", app_private_key_path=key_path)

    assert resolve_token(make_job(installation_id=77), settings) == "ghs_install"
    assert exchange.call_count == 1
    assert resolve_token(make_job(installation_id=None), settings) == "settings-token"
    assert resolve_token(make_job(installation_id=77), make_settings(tmp_path)) == "settings-token"


def test_resolve_token_without_any_token(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, github_token=None)
    assert resolve_token(make_job(), settings) is None


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("o/r", "o__r__3.md"),
        ("Own-er/re.po_x", "Own-er__re.po_x__3.md"),
        ("a/b\\c:d", "a__b_c_d__3.md"),
    ],
)
def test_write_report_files_names_are_safe(tmp_path: Path, repo: str, expected: str) -> None:
    sub = Submission(kind=Kind.ISSUE, repo="o/r", title="t")
    path = write_report_files(make_report(sub), "# r\r\n✓", tmp_path / "x" / "y", repo, 3)
    assert path.name == expected
    assert path.parent == tmp_path / "x" / "y"
    assert path.read_bytes().decode("utf-8") == "# r\r\n✓"


def test_write_report_files_survives_lone_surrogates(tmp_path: Path) -> None:
    sub = Submission(kind=Kind.ISSUE, repo="o/r", title="t")
    path = write_report_files(make_report(sub), "bad \ud800 text", tmp_path, "o/r", 1)
    assert path.read_text(encoding="utf-8") == "bad ? text"
