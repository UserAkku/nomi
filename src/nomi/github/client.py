"""A small, typed GitHub REST client: exactly the calls Nomi needs, nothing more.

Why not a full SDK: Nomi reads one issue or pull request, lists a few
collections and writes one comment. A thin wrapper over httpx keeps the
dependency surface small, makes every request easy to mock in tests, and lets
us turn GitHub's many failure modes (404 for private repos, primary and
secondary rate limits, network errors) into two exception types with messages
a maintainer can act on.

Deliberately, :meth:`GitHubClient.get_submission` copies only what the
reporter wrote. Labels, comments and the open/closed state are the
maintainers' verdict; copying them would leak the answer when Nomi is
evaluated on historical issues.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import httpx

from nomi import __version__
from nomi.models import ChangedFile, Kind, Submission

DEFAULT_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"

PER_PAGE = 100
MAX_PR_FILES = 300
SEARCH_RESULT_CAP = 1000  # GitHub never returns more than 1000 search results
_MAX_PAGES = 100  # hard stop for pagination, whatever the server says
_MAX_ERROR_CHARS = 300

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_NAME = r"[A-Za-z0-9._-]{1,100}"
_NUMBER = r"[0-9]{1,10}"
_KIND_SEGMENT = r"issues|pulls?"

_URL_RE = re.compile(
    rf"(?:https?://)?(?:www\.)?github\.com/(?P<owner>{_OWNER})/(?P<name>{_NAME})"
    rf"/(?P<kind>{_KIND_SEGMENT})/(?P<number>{_NUMBER})(?:/[^?#\s]*)?(?:[?#]\S*)?",
    re.ASCII | re.IGNORECASE,
)
_SHORT_RE = re.compile(
    rf"(?P<owner>{_OWNER})/(?P<name>{_NAME})(?:#|/(?P<kind>{_KIND_SEGMENT})/)(?P<number>{_NUMBER})/?",
    re.ASCII,
)
_REPO_RE = re.compile(rf"{_OWNER}/{_NAME}", re.ASCII)

_TARGET_HELP = (
    "expected an issue or pull request reference such as "
    "'https://github.com/owner/repo/issues/12', 'https://github.com/owner/repo/pull/34', "
    "'owner/repo#12' or 'owner/repo/issues/12'"
)


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A parsed issue/PR reference. ``kind`` is only a hint; the API is authoritative."""

    repo: str
    number: int
    kind: Kind | None


def parse_target(text: str) -> Target:
    """Parse a GitHub issue/PR URL or an ``owner/repo#N`` shorthand."""
    raw = (text or "").strip()
    match = _URL_RE.fullmatch(raw) or _SHORT_RE.fullmatch(raw)
    if match is None:
        raise ValueError(f"could not parse {_clip(raw)!r}: {_TARGET_HELP}")
    owner, name = match["owner"], match["name"]
    if name in {".", ".."}:
        raise ValueError(f"invalid repository name {name!r}: {_TARGET_HELP}")
    number = int(match["number"])
    if number < 1:
        raise ValueError(f"issue and pull request numbers start at 1, got {number}")
    return Target(repo=f"{owner}/{name}", number=number, kind=_kind_from_segment(match["kind"]))


def _kind_from_segment(segment: str | None) -> Kind | None:
    if segment is None:
        return None
    return Kind.ISSUE if segment.lower() == "issues" else Kind.PULL_REQUEST


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class GitHubError(RuntimeError):
    """Any failure talking to GitHub, with a message meant for humans."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimitError(GitHubError):
    """GitHub refused the request because a primary or secondary rate limit was hit."""

    def __init__(
        self, message: str, *, reset_at: datetime | None = None, status: int | None = None
    ) -> None:
        super().__init__(message, status=status)
        self.reset_at = reset_at


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


JsonList = list[dict[str, Any]]


class GitHubClient:
    """Synchronous GitHub REST client. Use as a context manager to close connections."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": f"nomi/{__version__}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._authenticated = bool(token)
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=True,  # renamed repositories answer with 301
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- high-level calls --------------------------------------------------

    def get_submission(self, repo: str, number: int) -> Submission:
        """Fetch an issue or pull request as the reporter submitted it."""
        repo, number = _check_repo(repo), _check_number(number)
        what = f"{repo}#{number}"
        issue = _as_dict(self.request("GET", f"/repos/{repo}/issues/{number}", what=what), what)
        fields: dict[str, Any] = {
            "kind": Kind.ISSUE,
            "repo": repo,
            "number": number,
            "url": _str_or_none(issue.get("html_url")),
            "title": _str_or_none(issue.get("title")) or "",
            "body": _str_or_none(issue.get("body")) or "",
            "author": _str_or_none(_as_dict_or_empty(issue.get("user")).get("login")),
            "created_at": _parse_time(issue.get("created_at")),
        }
        if issue.get("pull_request"):
            fields.update(self._pull_request_fields(repo, number))
        return Submission(**fields)

    def search_issues(self, query: str, *, max_items: int = 100) -> JsonList:
        """Run an issue/PR search; returns at most ``min(max_items, 1000)`` items."""
        cap = min(max_items, SEARCH_RESULT_CAP)
        if cap <= 0:
            return []
        return self._paginate(
            "/search/issues",
            params={"q": query, "per_page": min(PER_PAGE, cap)},
            what="issue search",
            max_items=cap,
            extract=_search_items,
        )

    def list_labels(self, repo: str) -> JsonList:
        repo = _check_repo(repo)
        return self._paginate(
            f"/repos/{repo}/labels", params={"per_page": PER_PAGE}, what=f"labels of {repo}"
        )

    def list_comments(self, repo: str, number: int) -> JsonList:
        repo, number = _check_repo(repo), _check_number(number)
        return self._paginate(
            f"/repos/{repo}/issues/{number}/comments",
            params={"per_page": PER_PAGE},
            what=f"comments of {repo}#{number}",
        )

    def create_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        repo, number = _check_repo(repo), _check_number(number)
        what = f"{repo}#{number}"
        data = self.request(
            "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body}, what=what
        )
        return _as_dict(data, what)

    def update_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        repo, comment_id = _check_repo(repo), _check_number(comment_id)
        what = f"comment {comment_id} in {repo}"
        data = self.request(
            "PATCH", f"/repos/{repo}/issues/comments/{comment_id}", json={"body": body}, what=what
        )
        return _as_dict(data, what)

    def get_repo(self, repo: str) -> dict[str, Any]:
        repo = _check_repo(repo)
        return _as_dict(self.request("GET", f"/repos/{repo}", what=f"repository {repo}"), repo)

    # -- low-level ---------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        what: str | None = None,
    ) -> Any:
        """Send one request and return the decoded JSON body (``None`` when empty)."""
        response = self._send(method, path, params=params, json=json, what=what or path)
        return _decode(response, what or path)

    def _send(
        self,
        method: str,
        url: str,
        *,
        what: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        try:
            response = self._http.request(method, url, params=params, json=json)
        except httpx.TimeoutException as exc:
            raise GitHubError(f"timed out talking to GitHub ({what})") from exc
        except httpx.HTTPError as exc:
            raise GitHubError(f"network error talking to GitHub ({what}): {exc}") from exc
        _raise_for_status(response, what, authenticated=self._authenticated)
        return response

    def _paginate(
        self,
        path: str,
        *,
        params: dict[str, Any],
        what: str,
        max_items: int | None = None,
        extract: Callable[[Any, str], JsonList] | None = None,
    ) -> JsonList:
        extract = extract or _list_items
        items: JsonList = []
        url: str | None = path
        query: dict[str, Any] | None = params
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            if url is None or url in seen or (max_items is not None and len(items) >= max_items):
                break
            seen.add(url)
            response = self._send("GET", url, params=query, what=what)
            page = extract(_decode(response, what), what)
            items.extend(page)
            url = self._next_link(response) if page else None
            query = None  # the next link already carries the query string
        return items if max_items is None else items[:max_items]

    def _next_link(self, response: httpx.Response) -> str | None:
        """Follow ``Link: rel=next`` only to our own API host (never leak the token)."""
        link = response.links.get("next", {}).get("url")
        if not link:
            return None
        try:
            target = httpx.URL(link)
        except (httpx.InvalidURL, TypeError):
            return None
        base = self._http.base_url
        if target.host != base.host or target.scheme != base.scheme:
            return None
        return str(target)

    def _pull_request_fields(self, repo: str, number: int) -> dict[str, Any]:
        what = f"pull request {repo}#{number}"
        pull = _as_dict(self.request("GET", f"/repos/{repo}/pulls/{number}", what=what), what)
        base = _as_dict_or_empty(pull.get("base"))
        head = _as_dict_or_empty(pull.get("head"))
        files = self._paginate(
            f"/repos/{repo}/pulls/{number}/files",
            params={"per_page": PER_PAGE},
            what=f"files of {what}",
            max_items=MAX_PR_FILES,
        )
        return {
            "kind": Kind.PULL_REQUEST,
            "base_ref": _str_or_none(base.get("ref")),
            "base_sha": _str_or_none(base.get("sha")),
            "head_sha": _str_or_none(head.get("sha")),
            "changed_files": [f for f in map(_changed_file, files) if f is not None],
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _check_repo(repo: str) -> str:
    value = repo.strip() if isinstance(repo, str) else ""
    if not _REPO_RE.fullmatch(value) or value.split("/")[1] in {".", ".."}:
        raise ValueError(f"invalid repository {_clip(str(repo))!r}: expected 'owner/name'")
    return value


def _check_number(number: int) -> int:
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"expected a positive integer, got {number!r}")
    return number


def _raise_for_status(response: httpx.Response, what: str, *, authenticated: bool) -> None:
    if response.is_success:
        return
    status = response.status_code
    message = _error_message(response)
    if status == 404:
        hint = "" if authenticated else "; set GITHUB_TOKEN if it is private"
        raise GitHubError(f"{what} not found or not public (HTTP 404){hint}", status=status)
    if status in (403, 429) and _is_rate_limited(response, message):
        raise _rate_limit_error(response, what, authenticated=authenticated)
    detail = f": {message}" if message else ""
    raise GitHubError(f"GitHub API error {status} for {what}{detail}", status=status)


def _is_rate_limited(response: httpx.Response, message: str) -> bool:
    return (
        response.status_code == 429
        or response.headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in message.lower()
    )


def _rate_limit_error(
    response: httpx.Response, what: str, *, authenticated: bool
) -> RateLimitError:
    reset_at = _reset_time(response)
    when = f"; it resets at {reset_at:%Y-%m-%d %H:%M:%S} UTC" if reset_at else ""
    advice = (
        "the GITHUB_TOKEN in use has exhausted its quota, wait for the reset"
        if authenticated
        else "unauthenticated requests are limited to 60 per hour; set GITHUB_TOKEN "
        "(or NOMI_GITHUB_TOKEN) to raise the limit to 5000"
    )
    return RateLimitError(
        f"GitHub rate limit hit while fetching {what}{when}: {advice}",
        reset_at=reset_at,
        status=response.status_code,
    )


def _reset_time(response: httpx.Response) -> datetime | None:
    reset = response.headers.get("x-ratelimit-reset", "")
    if _is_ascii_int(reset):
        try:
            return datetime.fromtimestamp(int(reset), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    retry_after = response.headers.get("retry-after", "")
    if _is_ascii_int(retry_after) and len(retry_after) < 9:
        return datetime.now(tz=UTC) + timedelta(seconds=int(retry_after))
    return None


def _is_ascii_int(text: str) -> bool:
    return text.isascii() and text.isdigit()


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return _clip(response.text.strip())
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return _clip(data["message"])
    return ""


def _decode(response: httpx.Response, what: str) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubError(f"GitHub returned invalid JSON for {what}") from exc


def _as_dict(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GitHubError(f"unexpected response shape from GitHub for {what}")
    return data


def _as_dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_items(data: Any, what: str) -> JsonList:
    if not isinstance(data, list):
        raise GitHubError(f"unexpected response shape from GitHub for {what}")
    return [item for item in data if isinstance(item, dict)]


def _search_items(data: Any, what: str) -> JsonList:
    return _list_items(_as_dict(data, what).get("items"), what)


def _changed_file(data: dict[str, Any]) -> ChangedFile | None:
    path = _str_or_none(data.get("filename"))
    if not path:
        return None
    return ChangedFile(
        path=path,
        status=_str_or_none(data.get("status")) or "modified",
        additions=_int_or_zero(data.get("additions")),
        deletions=_int_or_zero(data.get("deletions")),
        previous_path=_str_or_none(data.get("previous_filename")),
        patch=_str_or_none(data.get("patch")),
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _clip(text: str, limit: int = _MAX_ERROR_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
