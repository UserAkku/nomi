"""Tests for the webhook server: signatures, filtering, de-duplication and the worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
import threading
import types
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import nomi.github.app as app_module
from nomi import __version__
from nomi.config import Settings
from nomi.github.app import _DeliveryLog, create_app, verify_signature
from nomi.github.service import TriageJob

SECRET = "s3cret-✓"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_settings(secret: str | None = SECRET) -> Settings:
    return Settings(_env_file=None, webhook_secret=secret, dry_run=True, github_token=None)


class Recorder:
    """A thread-safe fake job handler."""

    def __init__(self, fail_on: Callable[[TriageJob], bool] | None = None) -> None:
        self.jobs: list[TriageJob] = []
        self.thread_names: set[str] = set()
        self._fail_on = fail_on
        self._lock = threading.Lock()

    def __call__(self, job: TriageJob) -> None:
        with self._lock:
            self.jobs.append(job)
            self.thread_names.add(threading.current_thread().name)
        if self._fail_on and self._fail_on(job):
            raise RuntimeError(f"boom on #{job.number}")


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def client(recorder: Recorder) -> Iterator[TestClient]:
    app = create_app(make_settings(), handler=recorder)
    with TestClient(app) as test_client:
        yield test_client


def deliver(
    c: TestClient,
    event: str,
    payload: Any,
    *,
    delivery: str | None = "d-1",
    signature: str | None = "auto",
    raw: bytes | None = None,
) -> Any:
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    if signature == "auto":
        headers["X-Hub-Signature-256"] = sign(body)
    elif signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return c.post("/webhook", content=body, headers=headers)


def drain(c: TestClient) -> None:
    c.app.state.queue.join()  # type: ignore[attr-defined]


def issue_payload(action: str = "opened", **extra: Any) -> dict[str, Any]:
    payload = {
        "action": action,
        "issue": {"number": 12, "title": "Crash ✓"},
        "repository": {"full_name": "octo/widget"},
        "installation": {"id": 555},
        "sender": {"login": "alice", "type": "User"},
    }
    payload.update(extra)
    return payload


def pr_payload(action: str = "opened", draft: bool = False) -> dict[str, Any]:
    return {
        "action": action,
        "pull_request": {"number": 34, "draft": draft},
        "repository": {"full_name": "octo/widget"},
        "sender": {"login": "bob", "type": "User"},
    }


def comment_payload(
    body: str = "/nomi", association: str = "OWNER", on_pr: bool = False, action: str = "created"
) -> dict[str, Any]:
    issue: dict[str, Any] = {"number": 56}
    if on_pr:
        issue["pull_request"] = {"url": "https://api.github.com/repos/octo/widget/pulls/56"}
    return {
        "action": action,
        "issue": issue,
        "comment": {"body": body, "author_association": association},
        "repository": {"full_name": "octo/widget"},
        "installation": {"id": 9},
        "sender": {"login": "carol", "type": "User"},
    }


# --------------------------------------------------------------------------
# verify_signature
# --------------------------------------------------------------------------


def test_verify_signature_valid_and_invalid() -> None:
    body = b'{"a": 1}'
    good = sign(body)
    assert verify_signature(SECRET, body, good)
    assert verify_signature(SECRET, body, good.upper().replace("SHA256=", "sha256="))
    assert not verify_signature(SECRET, body + b" ", good)
    assert not verify_signature("other", body, good)
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "")
    assert not verify_signature("", body, good)
    assert not verify_signature(SECRET, body, good.replace("sha256=", "sha1="))
    assert not verify_signature(SECRET, body, "sha256=")
    assert not verify_signature(SECRET, body, "sha256=ü" + "0" * 63)
    assert not verify_signature(SECRET, body, "garbage")


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


@pytest.mark.parametrize("secret", [None, ""])
def test_no_secret_fails_closed(recorder: Recorder, secret: str | None) -> None:
    with TestClient(create_app(make_settings(secret), handler=recorder)) as c:
        response = deliver(c, "issues", issue_payload(), signature=None)
        assert response.status_code == 503
        assert "NOMI_WEBHOOK_SECRET" in response.json()["detail"]
    assert recorder.jobs == []


@pytest.mark.parametrize("signature", [None, "sha256=" + "0" * 64, "sha256=nothex", "nonsense"])
def test_bad_or_missing_signature_is_401(
    client: TestClient, recorder: Recorder, signature: str | None
) -> None:
    response = deliver(client, "issues", issue_payload(), signature=signature)
    assert response.status_code == 401
    drain(client)
    assert recorder.jobs == []


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b"", b"\xff\xfe\x00", b"[" * 100_000])
def test_unparseable_or_non_object_body_is_400(client: TestClient, raw: bytes) -> None:
    response = deliver(client, "issues", None, raw=raw)
    assert response.status_code == 400


def test_payload_too_large_is_413(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "MAX_PAYLOAD_BYTES", 64)
    response = deliver(client, "issues", issue_payload(pad="x" * 200))
    assert response.status_code == 413


def test_issue_opened_is_queued_with_job_fields(client: TestClient, recorder: Recorder) -> None:
    response = deliver(client, "issues", issue_payload(), delivery="abc-123")
    assert response.status_code == 202
    assert response.json() == {"status": "queued"}
    drain(client)
    assert recorder.jobs == [
        TriageJob(
            repo="octo/widget",
            number=12,
            kind="issue",
            installation_id=555,
            delivery_id="abc-123",
            trigger="issues.opened",
        )
    ]
    assert recorder.thread_names == {"nomi-triage-worker"}


def test_duplicate_delivery_runs_once(client: TestClient, recorder: Recorder) -> None:
    assert deliver(client, "issues", issue_payload(), delivery="same").status_code == 202
    second = deliver(client, "issues", issue_payload(), delivery="same")
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert deliver(client, "issues", issue_payload(), delivery="other").status_code == 202
    drain(client)
    assert [j.delivery_id for j in recorder.jobs] == ["same", "other"]


def test_missing_delivery_header_is_still_processed(client: TestClient, recorder: Recorder) -> None:
    for _ in range(2):
        assert deliver(client, "issues", issue_payload(), delivery=None).status_code == 202
    drain(client)
    assert len(recorder.jobs) == 2
    assert all(j.delivery_id.startswith("local-") for j in recorder.jobs)


def test_bot_sender_is_ignored(client: TestClient, recorder: Recorder) -> None:
    payload = comment_payload()
    payload["sender"] = {"login": "nomi-triage[bot]", "type": "Bot"}
    response = deliver(client, "issue_comment", payload)
    assert response.status_code == 202
    assert response.json() == {"status": "ignored", "reason": "sender is a bot"}
    drain(client)
    assert recorder.jobs == []


@pytest.mark.parametrize(
    ("event", "payload", "trigger", "kind", "number"),
    [
        ("issues", issue_payload("opened"), "issues.opened", "issue", 12),
        ("issues", issue_payload("reopened"), "issues.reopened", "issue", 12),
        ("pull_request", pr_payload("opened"), "pull_request.opened", "pull_request", 34),
        ("pull_request", pr_payload("reopened"), "pull_request.reopened", "pull_request", 34),
        ("pull_request", pr_payload("synchronize"), "pull_request.synchronize", "pull_request", 34),
        (
            "pull_request",
            pr_payload("ready_for_review", draft=True),
            "pull_request.ready_for_review",
            "pull_request",
            34,
        ),
        ("issue_comment", comment_payload(), "issue_comment.created", "issue", 56),
        ("issue_comment", comment_payload(on_pr=True), "issue_comment.created", "pull_request", 56),
        (
            "issue_comment",
            comment_payload("  /nomi please re-run\r\nthanks", "MEMBER"),
            "issue_comment.created",
            "issue",
            56,
        ),
        (
            "issue_comment",
            comment_payload("/nomi", "COLLABORATOR"),
            "issue_comment.created",
            "issue",
            56,
        ),
    ],
)
def test_accepted_events(
    client: TestClient,
    recorder: Recorder,
    event: str,
    payload: dict[str, Any],
    trigger: str,
    kind: str,
    number: int,
) -> None:
    response = deliver(client, event, payload)
    assert response.status_code == 202, response.json()
    assert response.json() == {"status": "queued"}
    drain(client)
    (job,) = recorder.jobs
    assert (job.repo, job.number, job.kind, job.trigger) == ("octo/widget", number, kind, trigger)


@pytest.mark.parametrize(
    ("event", "payload", "reason"),
    [
        ("issues", issue_payload("closed"), "issues.closed is not handled"),
        ("issues", issue_payload("labeled"), "issues.labeled is not handled"),
        ("pull_request", pr_payload("closed"), "pull_request.closed is not handled"),
        ("pull_request", pr_payload("opened", draft=True), "draft pull request"),
        ("pull_request", pr_payload("synchronize", draft=True), "draft pull request"),
        ("push", {"ref": "refs/heads/main"}, "event 'push' is not handled"),
        ("ping", {"zen": "Keep it logically awesome."}, "event 'ping' is not handled"),
        ("", issue_payload(), "missing X-GitHub-Event"),
        ("issue_comment", comment_payload(action="edited"), "issue_comment.edited is not handled"),
        ("issue_comment", comment_payload("thanks!"), "not a /nomi command"),
        ("issue_comment", comment_payload("/nominate me"), "not a /nomi command"),
        ("issue_comment", comment_payload("please /nomi"), "not a /nomi command"),
        ("issue_comment", comment_payload("/NOMI"), "not a /nomi command"),
        ("issue_comment", comment_payload("/nomi", "CONTRIBUTOR"), "only owners, members"),
        ("issue_comment", comment_payload("/nomi", "NONE"), "only owners, members"),
        ("issue_comment", comment_payload("/nomi", "FIRST_TIME_CONTRIBUTOR"), "only owners"),
    ],
)
def test_ignored_events(
    client: TestClient, recorder: Recorder, event: str, payload: dict[str, Any], reason: str
) -> None:
    response = deliver(client, event, payload)
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "ignored"
    assert reason in data["reason"]
    drain(client)
    assert recorder.jobs == []


def test_comment_with_non_string_body_is_ignored(client: TestClient) -> None:
    payload = comment_payload()
    payload["comment"]["body"] = None
    response = deliver(client, "issue_comment", payload)
    assert response.json()["status"] == "ignored"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("repository"),
        lambda p: p["repository"].update(full_name="no-slash"),
        lambda p: p["repository"].update(full_name="../../etc"),
        lambda p: p["issue"].update(number="12"),
        lambda p: p["issue"].update(number=True),
        lambda p: p["issue"].update(number=0),
        lambda p: p.update(issue=None),
    ],
)
def test_malformed_payload_is_400(
    client: TestClient, recorder: Recorder, mutate: Callable[[dict[str, Any]], Any]
) -> None:
    payload = issue_payload()
    mutate(payload)
    response = deliver(client, "issues", payload)
    assert response.status_code == 400
    drain(client)
    assert recorder.jobs == []


def test_missing_or_bad_installation_id_becomes_none(
    client: TestClient, recorder: Recorder
) -> None:
    deliver(client, "issues", issue_payload(installation={"id": "555"}), delivery="a")
    deliver(client, "issues", issue_payload(installation=None), delivery="b")
    drain(client)
    assert [j.installation_id for j in recorder.jobs] == [None, None]


def test_worker_survives_failing_jobs(caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder(fail_on=lambda job: job.delivery_id == "first")
    with TestClient(create_app(make_settings(), handler=recorder)) as c:
        with caplog.at_level(logging.ERROR, logger="nomi.github.app"):
            deliver(c, "issues", issue_payload(), delivery="first")
            deliver(c, "issues", issue_payload(), delivery="second")
            drain(c)
    assert [j.delivery_id for j in recorder.jobs] == ["first", "second"]
    assert "triage job failed" in caplog.text
    assert "boom on #12" in caplog.text


def test_jobs_run_serially_in_order(client: TestClient, recorder: Recorder) -> None:
    for i in range(20):
        deliver(client, "issues", issue_payload(), delivery=f"d{i}")
    drain(client)
    assert [j.delivery_id for j in recorder.jobs] == [f"d{i}" for i in range(20)]
    assert recorder.thread_names == {"nomi-triage-worker"}


def test_default_handler_runs_pipeline_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings()
    calls: list[tuple[TriageJob, Any, str]] = []

    def analyze_submission(submission: Any, passed_settings: Settings) -> str:
        assert passed_settings is settings
        return f"report({submission})"

    def fake_process_job(job: TriageJob, s: Settings, *, analyze_fn: Any, render_fn: Any) -> None:
        report = analyze_fn("SUB")
        calls.append((job, s, render_fn(report)))

    pipeline = types.ModuleType("nomi.pipeline")
    pipeline.analyze_submission = analyze_submission  # type: ignore[attr-defined]
    report_mod = types.ModuleType("nomi.report")
    report_mod.render_markdown = lambda report: f"md:{report}"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nomi.pipeline", pipeline)
    monkeypatch.setitem(sys.modules, "nomi.report", report_mod)
    monkeypatch.setattr(app_module, "process_job", fake_process_job)

    with TestClient(create_app(settings)) as c:
        assert deliver(c, "issues", issue_payload()).status_code == 202
        drain(c)

    ((job, passed, markdown),) = calls
    assert job.number == 12 and passed is settings
    assert markdown == "md:report(SUB)"


def test_delivery_log_is_bounded() -> None:
    log = _DeliveryLog(capacity=2)
    assert not log.seen_before("a")
    assert not log.seen_before("b")
    assert log.seen_before("a")  # refreshes "a"
    assert not log.seen_before("c")  # evicts "b"
    assert log.seen_before("a")
    assert not log.seen_before("b")
