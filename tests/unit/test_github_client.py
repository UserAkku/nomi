"""Tests for the GitHub REST client, target parsing and GitHub App authentication."""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from nomi import __version__
from nomi.github.auth import get_installation_token, make_app_jwt
from nomi.github.client import (
    GitHubClient,
    GitHubError,
    RateLimitError,
    Target,
    parse_target,
)
from nomi.models import Kind

API = "https://api.github.com"


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client() -> Iterator[GitHubClient]:
    with GitHubClient("tok123") as gh:
        yield gh


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def page(items: Any, next_url: str | None = None, status: int = 200) -> httpx.Response:
    headers = (
        {"Link": f'<{next_url}>; rel="next", <{API}/x?page=9>; rel="last"'} if next_url else {}
    )
    return httpx.Response(status, json=items, headers=headers)


ISSUE_JSON: dict[str, Any] = {
    "number": 7,
    "title": "Crash in parse_url — ünïcödé ✓",
    "body": None,
    "html_url": "https://github.com/o/r/issues/7",
    "user": {"login": "alice", "type": "User"},
    "created_at": "2024-03-01T12:30:00Z",
    "labels": [{"name": "SECRET-LABEL-confirmed"}],
    "state": "closed",
    "comments": 12,
}


# --------------------------------------------------------------------------
# parse_target
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://github.com/o/r/issues/1", Target("o/r", 1, Kind.ISSUE)),
        ("https://github.com/o/r/pull/2", Target("o/r", 2, Kind.PULL_REQUEST)),
        ("https://github.com/o/r/pulls/2", Target("o/r", 2, Kind.PULL_REQUEST)),
        ("https://github.com/o/r/issues/1/", Target("o/r", 1, Kind.ISSUE)),
        ("https://github.com/o/r/pull/2?diff=split", Target("o/r", 2, Kind.PULL_REQUEST)),
        ("https://github.com/o/r/issues/3#issuecomment-99", Target("o/r", 3, Kind.ISSUE)),
        ("https://github.com/o/r/pull/2/files#diff-abc", Target("o/r", 2, Kind.PULL_REQUEST)),
        (
            "http://www.github.com/Own-er1/re.po_x/issues/9",
            Target("Own-er1/re.po_x", 9, Kind.ISSUE),
        ),
        ("github.com/o/r/issues/4", Target("o/r", 4, Kind.ISSUE)),
        ("HTTPS://GitHub.com/o/r/Issues/5", Target("o/r", 5, Kind.ISSUE)),
        ("  o/r#3\n", Target("o/r", 3, None)),
        ("o/r/issues/3", Target("o/r", 3, Kind.ISSUE)),
        ("o/r/pull/8", Target("o/r", 8, Kind.PULL_REQUEST)),
        ("python/cpython#007", Target("python/cpython", 7, None)),
    ],
)
def test_parse_target_accepts(text: str, expected: Target) -> None:
    assert parse_target(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "o/r",
        "o#3",
        "o/r#",
        "o/r#0",
        "o/r#-1",
        "o/r#٣",  # non-ASCII digit
        "o/r#12345678901",
        "o/..#1",
        "-o/r#1",
        "o/r/commits/3",
        "https://github.com/o/r",
        "https://github.com/o/r/issues/abc",
        "https://github.com/o/r/issues/0",
        "https://gitlab.com/o/r/issues/1",
        "https://evil.example/github.com/o/r/issues/1",
        "https://github.com.evil.example/o/r/issues/1",
        "https://github.com/o/r/issues/1 trailing words",
        "a" * 100_000,
        None,
    ],
)
def test_parse_target_rejects_with_helpful_message(text: Any) -> None:
    with pytest.raises(ValueError, match=r"owner/repo#12|start at 1|invalid repository"):
        parse_target(text)


def test_parse_target_error_is_clipped_for_huge_input() -> None:
    with pytest.raises(ValueError) as info:
        parse_target("x" * 50_000)
    assert len(str(info.value)) < 1000


# --------------------------------------------------------------------------
# get_submission
# --------------------------------------------------------------------------


def test_get_submission_issue(api: respx.MockRouter, client: GitHubClient) -> None:
    route = api.get("/repos/o/r/issues/7").mock(return_value=httpx.Response(200, json=ISSUE_JSON))

    sub = client.get_submission("o/r", 7)

    assert sub.kind is Kind.ISSUE
    assert (sub.repo, sub.number, sub.url) == ("o/r", 7, "https://github.com/o/r/issues/7")
    assert sub.title == "Crash in parse_url — ünïcödé ✓"
    assert sub.body == ""
    assert sub.author == "alice"
    assert sub.created_at == datetime(2024, 3, 1, 12, 30, tzinfo=UTC)
    assert sub.changed_files == [] and sub.head_sha is None
    dumped = sub.model_dump_json()
    assert "SECRET-LABEL" not in dumped and "closed" not in dumped

    headers = route.calls.last.request.headers
    assert headers["Authorization"] == "Bearer tok123"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["User-Agent"] == f"nomi/{__version__}"


def test_no_token_sends_no_authorization(api: respx.MockRouter) -> None:
    route = api.get("/repos/o/r/issues/7").mock(return_value=httpx.Response(200, json=ISSUE_JSON))
    with GitHubClient() as gh:
        gh.get_submission("o/r", 7)
    assert "Authorization" not in route.calls.last.request.headers


def test_get_submission_tolerates_malformed_fields(
    api: respx.MockRouter, client: GitHubClient
) -> None:
    weird = {"title": None, "body": 42, "user": "not-a-dict", "created_at": "yesterday"}
    api.get("/repos/o/r/issues/1").mock(return_value=httpx.Response(200, json=weird))
    sub = client.get_submission("o/r", 1)
    assert (sub.title, sub.body, sub.author, sub.created_at) == ("", "", None, None)


def test_get_submission_pull_request_paginates_files(
    api: respx.MockRouter, client: GitHubClient
) -> None:
    issue = {**ISSUE_JSON, "body": "Fixes #3\r\nDetails", "pull_request": {"url": "x"}}
    api.get("/repos/o/r/issues/2").mock(return_value=httpx.Response(200, json=issue))
    api.get("/repos/o/r/pulls/2").mock(
        return_value=httpx.Response(
            200,
            json={"base": {"ref": "main", "sha": "b" * 40}, "head": {"sha": "h" * 40}},
        )
    )
    first = [{"filename": f"src/f{i}.py", "status": "modified", "additions": 1} for i in range(100)]
    second = [
        {
            "filename": "src/new.py",
            "status": "renamed",
            "additions": 3,
            "deletions": 2,
            "previous_filename": "src/old.py",
            "patch": "@@ -1 +1 @@\n-a\n+b",
        },
        {"status": "added"},  # no filename: skipped
        "garbage",  # not an object: skipped
    ]
    files = api.get("/repos/o/r/pulls/2/files").mock(
        side_effect=[
            page(first, f"{API}/repos/o/r/pulls/2/files?per_page=100&page=2"),
            page(second),
        ]
    )

    sub = client.get_submission("o/r", 2)

    assert sub.kind is Kind.PULL_REQUEST
    assert (sub.base_ref, sub.base_sha, sub.head_sha) == ("main", "b" * 40, "h" * 40)
    assert sub.body == "Fixes #3\r\nDetails"
    assert len(sub.changed_files) == 101
    last = sub.changed_files[-1]
    assert (last.path, last.status, last.previous_path) == ("src/new.py", "renamed", "src/old.py")
    assert (last.additions, last.deletions, last.patch) == (3, 2, "@@ -1 +1 @@\n-a\n+b")
    assert files.call_count == 2
    assert files.calls[0].request.url.params["per_page"] == "100"


def test_pull_request_files_capped_at_300(api: respx.MockRouter, client: GitHubClient) -> None:
    api.get("/repos/o/r/issues/3").mock(
        return_value=httpx.Response(200, json={**ISSUE_JSON, "pull_request": {"url": "u"}})
    )
    api.get("/repos/o/r/pulls/3").mock(return_value=httpx.Response(200, json={}))
    batch = [{"filename": f"f{i}"} for i in range(100)]
    files = api.get("/repos/o/r/pulls/3/files").mock(
        side_effect=[page(batch, f"{API}/repos/o/r/pulls/3/files?page={n}") for n in range(2, 7)]
    )

    sub = client.get_submission("o/r", 3)

    assert len(sub.changed_files) == 300
    assert files.call_count == 3
    assert sub.base_ref is None


@pytest.mark.parametrize(("repo", "number"), [("../etc", 1), ("o/r/x", 1), ("", 1), ("o/..", 1)])
def test_invalid_repo_rejected_before_any_request(repo: str, number: int) -> None:
    with GitHubClient() as gh, pytest.raises(ValueError, match="owner/name"):
        gh.get_submission(repo, number)


@pytest.mark.parametrize("number", [0, -3, True])
def test_invalid_number_rejected(number: Any) -> None:
    with GitHubClient() as gh, pytest.raises(ValueError, match="positive integer"):
        gh.get_submission("o/r", number)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def test_404_is_not_found_or_not_public(api: respx.MockRouter) -> None:
    api.get("/repos/o/r/issues/5").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with GitHubClient() as gh, pytest.raises(GitHubError, match="not found or not public") as info:
        gh.get_submission("o/r", 5)
    assert info.value.status == 404
    assert "GITHUB_TOKEN" in str(info.value)
    assert not isinstance(info.value, RateLimitError)


def test_primary_rate_limit(api: respx.MockRouter) -> None:
    api.get("/repos/o/r/issues/5").mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded for 1.2.3.4."},
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"},
        )
    )
    with GitHubClient() as gh, pytest.raises(RateLimitError) as info:
        gh.get_submission("o/r", 5)
    assert info.value.reset_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert "GITHUB_TOKEN" in str(info.value)
    assert "60 per hour" in str(info.value)


def test_secondary_rate_limit_with_retry_after(api: respx.MockRouter, client: GitHubClient) -> None:
    api.get("/repos/o/r/issues/5").mock(
        return_value=httpx.Response(
            403,
            json={"message": "You have exceeded a secondary rate limit."},
            headers={"Retry-After": "60"},
        )
    )
    before = datetime.now(tz=UTC)
    with pytest.raises(RateLimitError) as info:
        client.get_submission("o/r", 5)
    assert info.value.reset_at is not None
    assert 55 <= (info.value.reset_at - before).total_seconds() <= 65
    assert "GITHUB_TOKEN" in str(info.value)


def test_429_is_rate_limit_even_without_headers(
    api: respx.MockRouter, client: GitHubClient
) -> None:
    api.get("/repos/o/r").mock(return_value=httpx.Response(429, text="slow down"))
    with pytest.raises(RateLimitError) as info:
        client.get_repo("o/r")
    assert info.value.reset_at is None


def test_rate_limit_reset_header_garbage_is_ignored(
    api: respx.MockRouter, client: GitHubClient
) -> None:
    api.get("/repos/o/r").mock(
        return_value=httpx.Response(
            403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9" * 40}
        )
    )
    with pytest.raises(RateLimitError) as info:
        client.get_repo("o/r")
    assert info.value.reset_at is None


def test_plain_403_is_not_a_rate_limit(api: respx.MockRouter, client: GitHubClient) -> None:
    api.get("/repos/o/r").mock(
        return_value=httpx.Response(
            403,
            json={"message": "Resource not accessible by integration"},
            headers={"X-RateLimit-Remaining": "4999"},
        )
    )
    with pytest.raises(GitHubError) as info:
        client.get_repo("o/r")
    assert not isinstance(info.value, RateLimitError)
    assert "403" in str(info.value) and "Resource not accessible" in str(info.value)


def test_server_error_with_html_body_is_clipped(
    api: respx.MockRouter, client: GitHubClient
) -> None:
    api.get("/repos/o/r").mock(return_value=httpx.Response(502, text="<html>" + "x" * 10_000))
    with pytest.raises(GitHubError, match="502") as info:
        client.get_repo("o/r")
    assert info.value.status == 502
    assert len(str(info.value)) < 500


def test_invalid_json_body(api: respx.MockRouter, client: GitHubClient) -> None:
    api.get("/repos/o/r").mock(return_value=httpx.Response(200, text="{not json"))
    with pytest.raises(GitHubError, match="invalid JSON"):
        client.get_repo("o/r")


def test_unexpected_shape(api: respx.MockRouter, client: GitHubClient) -> None:
    api.get("/repos/o/r").mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(GitHubError, match="unexpected response shape"):
        client.get_repo("o/r")


@pytest.mark.parametrize(
    ("error", "pattern"),
    [(httpx.ConnectError("refused"), "network error"), (httpx.ReadTimeout("slow"), "timed out")],
)
def test_transport_errors_become_github_errors(
    api: respx.MockRouter, client: GitHubClient, error: Exception, pattern: str
) -> None:
    api.get("/repos/o/r").mock(side_effect=error)
    with pytest.raises(GitHubError, match=pattern):
        client.get_repo("o/r")


# --------------------------------------------------------------------------
# Pagination and other calls
# --------------------------------------------------------------------------


def test_search_issues_paginates_and_respects_max_items(
    api: respx.MockRouter, client: GitHubClient
) -> None:
    batch = {"total_count": 999, "items": [{"number": i} for i in range(100)]}
    route = api.get("/search/issues").mock(
        side_effect=[page(batch, f"{API}/search/issues?q=x&page={n}") for n in range(2, 6)]
    )
    items = client.search_issues("repo:o/r is:issue label:bug", max_items=150)
    assert len(items) == 150
    assert route.call_count == 2
    first = route.calls[0].request.url.params
    assert first["q"] == "repo:o/r is:issue label:bug" and first["per_page"] == "100"


def test_search_issues_caps_at_1000(api: respx.MockRouter, client: GitHubClient) -> None:
    batch = {"items": [{"number": i} for i in range(100)]}
    pages = itertools.count(2)
    route = api.get("/search/issues").mock(
        side_effect=lambda request: page(batch, f"{API}/search/issues?page={next(pages)}")
    )
    items = client.search_issues("x", max_items=50_000)
    assert len(items) == 1000
    assert route.call_count == 10


def test_search_issues_small_and_empty(api: respx.MockRouter, client: GitHubClient) -> None:
    route = api.get("/search/issues").mock(return_value=page({"items": [{"n": 1}]}))
    assert client.search_issues("x", max_items=0) == []
    assert route.call_count == 0
    assert client.search_issues("x", max_items=5) == [{"n": 1}]
    assert route.calls.last.request.url.params["per_page"] == "5"


def test_pagination_never_follows_foreign_hosts(
    api: respx.MockRouter, client: GitHubClient
) -> None:
    route = api.get("/repos/o/r/labels").mock(
        return_value=page([{"name": "bug"}], "https://evil.example/steal?page=2")
    )
    assert client.list_labels("o/r") == [{"name": "bug"}]
    assert route.call_count == 1


def test_pagination_stops_on_self_loop(api: respx.MockRouter, client: GitHubClient) -> None:
    loop = f"{API}/repos/o/r/labels?per_page=100&page=2"
    route = api.get("/repos/o/r/labels").mock(return_value=page([{"name": "bug"}], loop))
    labels = client.list_labels("o/r")
    assert route.call_count == 2
    assert len(labels) == 2


def test_list_comments_paginates(api: respx.MockRouter, client: GitHubClient) -> None:
    api.get("/repos/o/r/issues/4/comments").mock(
        side_effect=[
            page([{"id": 1}], f"{API}/repos/o/r/issues/4/comments?page=2"),
            page([{"id": 2}]),
        ]
    )
    assert [c["id"] for c in client.list_comments("o/r", 4)] == [1, 2]


def test_create_update_comment_and_get_repo(api: respx.MockRouter, client: GitHubClient) -> None:
    create = api.post("/repos/o/r/issues/4/comments").mock(
        return_value=httpx.Response(201, json={"id": 10, "body": "hi"})
    )
    update = api.patch("/repos/o/r/issues/comments/10").mock(
        return_value=httpx.Response(200, json={"id": 10, "body": "bye"})
    )
    api.get("/repos/o/r").mock(return_value=httpx.Response(200, json={"full_name": "o/r"}))

    assert client.create_comment("o/r", 4, "hi ✓") == {"id": 10, "body": "hi"}
    assert client.update_comment("o/r", 10, "bye") == {"id": 10, "body": "bye"}
    assert client.get_repo("o/r") == {"full_name": "o/r"}
    assert json.loads(create.calls.last.request.read()) == {"body": "hi ✓"}
    assert update.calls.last.request.method == "PATCH"


def test_context_manager_closes_http_client() -> None:
    with GitHubClient("t") as gh:
        pass
    assert gh._http.is_closed


# --------------------------------------------------------------------------
# GitHub App auth
# --------------------------------------------------------------------------


def test_make_app_jwt_claims(rsa_keys: tuple[str, str]) -> None:
    private_pem, public_pem = rsa_keys
    token = make_app_jwt("12345", private_pem, now=1_700_000_000)
    claims = jwt.decode(
        token, public_pem, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    assert claims == {"iat": 1_700_000_000 - 60, "exp": 1_700_000_000 + 540, "iss": "12345"}
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


def test_make_app_jwt_uses_current_time(rsa_keys: tuple[str, str]) -> None:
    private_pem, public_pem = rsa_keys
    claims = jwt.decode(make_app_jwt("1", private_pem), public_pem, algorithms=["RS256"])
    assert abs(claims["iat"] + 60 - time.time()) < 5


@pytest.mark.parametrize("key", ["", "not a key", "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n"])
def test_make_app_jwt_rejects_bad_key(key: str) -> None:
    with pytest.raises(ValueError, match="private key"):
        make_app_jwt("1", key)


def test_make_app_jwt_rejects_empty_app_id(rsa_keys: tuple[str, str]) -> None:
    with pytest.raises(ValueError, match="App id"):
        make_app_jwt("  ", rsa_keys[0])


def test_get_installation_token(api: respx.MockRouter, rsa_keys: tuple[str, str]) -> None:
    private_pem, public_pem = rsa_keys
    route = api.post("/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_abc", "expires_at": "2030-01-01"})
    )

    assert get_installation_token("99", private_pem, 42) == "ghs_abc"

    auth = route.calls.last.request.headers["Authorization"]
    assert auth.startswith("Bearer ")
    claims = jwt.decode(auth.removeprefix("Bearer "), public_pem, algorithms=["RS256"])
    assert claims["iss"] == "99"


def test_get_installation_token_custom_transport(rsa_keys: tuple[str, str]) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"token": "ghs_ghe"})

    token = get_installation_token(
        "1",
        rsa_keys[0],
        7,
        base_url="https://ghe.example/api/v3",
        transport=httpx.MockTransport(handler),
    )
    assert token == "ghs_ghe"
    assert str(seen[0].url) == "https://ghe.example/api/v3/app/installations/7/access_tokens"


def test_get_installation_token_errors(api: respx.MockRouter, rsa_keys: tuple[str, str]) -> None:
    api.post("/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(201, json={"expires_at": "x"})
    )
    with pytest.raises(GitHubError, match="no access token"):
        get_installation_token("1", rsa_keys[0], 1)
    for bad in (0, -1, True, "5"):
        with pytest.raises(ValueError, match="installation id"):
            get_installation_token("1", rsa_keys[0], bad)  # type: ignore[arg-type]
