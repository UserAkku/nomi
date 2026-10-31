"""The GitHub App webhook server: authenticate, filter, enqueue, return fast.

GitHub expects a webhook response within ten seconds, but a triage run (clone,
static checks, a local language model) takes minutes. So the request handler
only verifies, filters and enqueues; ONE background thread drains the queue.
One thread is deliberate: the local model serves one request at a time, and
running jobs in parallel would only make all of them slow.

Safety rules, in the order they are applied:

* fail closed: without a webhook secret every delivery is refused (503);
* every body must carry a valid ``X-Hub-Signature-256`` HMAC (401 otherwise);
* redelivered events are recognised by ``X-GitHub-Delivery`` and run once;
* events sent by bots (including Nomi's own comments) are ignored, so the App
  can never trigger itself in a loop;
* the ``/nomi`` re-run command is honoured only from owners, members and
  collaborators, so strangers cannot burn the maintainer's compute;
* at most one job per issue / pull request waits in the queue (a burst of
  pushes runs once, plus one follow-up if it arrives mid-run), and the queue
  is bounded: when it is full new events are refused with 503.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nomi import __version__
from nomi.config import Settings, get_settings
from nomi.github.client import parse_target
from nomi.github.service import TriageJob, process_job
from nomi.models import Kind

logger = logging.getLogger(__name__)

JobHandler = Callable[[TriageJob], None]

MAX_PAYLOAD_BYTES = 25 * 1024 * 1024  # GitHub caps webhook payloads at 25 MB
DELIVERY_MEMORY = 1000
MAX_QUEUED_JOBS = 100
ISSUE_ACTIONS = frozenset({"opened", "reopened"})
PULL_REQUEST_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_COMMAND_RE = re.compile(r"/nomi(?:\s|$)")
_STOP = object()


class _MalformedPayloadError(ValueError):
    """A signed payload of a handled event type lacks the fields we need."""


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check GitHub's ``X-Hub-Signature-256: sha256=<hex>`` HMAC in constant time."""
    if not secret or not header:
        return False
    scheme, _, digest = header.strip().partition("=")
    if scheme != "sha256" or not digest or not digest.isascii():
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest.lower())


# --------------------------------------------------------------------------
# Delivery de-duplication and the background worker
# --------------------------------------------------------------------------


class _DeliveryLog:
    """The most recent delivery ids, so a redelivered event runs only once."""

    def __init__(self, capacity: int = DELIVERY_MEMORY) -> None:
        self._capacity = capacity
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def seen_before(self, delivery_id: str) -> bool:
        """Record ``delivery_id``; True when it was already recorded."""
        with self._lock:
            if delivery_id in self._ids:
                self._ids.move_to_end(delivery_id)
                return True
            self._ids[delivery_id] = None
            while len(self._ids) > self._capacity:
                self._ids.popitem(last=False)
            return False

    def forget(self, delivery_id: str) -> None:
        """Drop ``delivery_id`` so a redelivery of a refused event is processed."""
        with self._lock:
            self._ids.pop(delivery_id, None)


class _PendingTargets:
    """The issues / PRs with a job waiting in the queue, so each waits at most once."""

    def __init__(self) -> None:
        self._keys: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    def enqueue(self, jobs: queue.Queue[Any], job: TriageJob) -> str:
        """Queue ``job``: "queued", "coalesced" (its target already waits) or "full"."""
        key = _target_key(job)
        with self._lock:
            if key in self._keys:
                return "coalesced"
            try:
                jobs.put_nowait(job)
            except queue.Full:
                return "full"
            self._keys.add(key)
        return "queued"

    def started(self, job: TriageJob) -> None:
        """The worker took ``job``: a later event for its target needs a new run."""
        with self._lock:
            self._keys.discard(_target_key(job))


def _target_key(job: TriageJob) -> tuple[str, int]:
    return job.repo.casefold(), job.number


class _Worker:
    """One daemon thread running jobs in order; a failing job never kills it."""

    def __init__(
        self,
        jobs: queue.Queue[Any],
        handler: JobHandler,
        *,
        on_start: Callable[[TriageJob], None] | None = None,
    ) -> None:
        self._jobs = jobs
        self._handler = handler
        self._on_start = on_start
        self._thread = threading.Thread(target=self._run, name="nomi-triage-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        try:
            self._jobs.put(_STOP, timeout=timeout)
        except queue.Full:
            logger.warning("triage queue still full at shutdown; abandoning queued jobs")
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is _STOP:
                    return
                if self._on_start is not None:
                    self._on_start(job)
                self._handler(job)
            except Exception:
                logger.exception(
                    "triage job failed: %s#%s (delivery %s)",
                    getattr(job, "repo", "?"),
                    getattr(job, "number", "?"),
                    getattr(job, "delivery_id", "?"),
                )
            finally:
                self._jobs.task_done()


# --------------------------------------------------------------------------
# Event routing
# --------------------------------------------------------------------------


def _route(event: str, payload: dict[str, Any], delivery_id: str) -> TriageJob | str:
    """Return the job to run, or the reason the event is ignored."""
    action = payload.get("action")
    if event == "issues":
        if action not in ISSUE_ACTIONS:
            return f"issues.{action} is not handled"
        return _job(payload, "issue", Kind.ISSUE, delivery_id, f"issues.{action}")
    if event == "pull_request":
        if action not in PULL_REQUEST_ACTIONS:
            return f"pull_request.{action} is not handled"
        pull = _section(payload, "pull_request")
        if pull.get("draft") is True and action != "ready_for_review":
            return "draft pull request (analysed once it is marked ready for review)"
        return _job(
            payload, "pull_request", Kind.PULL_REQUEST, delivery_id, f"pull_request.{action}"
        )
    if event == "issue_comment":
        return _route_comment(payload, delivery_id)
    return f"event {event or '(missing X-GitHub-Event)'!r} is not handled"


def _route_comment(payload: dict[str, Any], delivery_id: str) -> TriageJob | str:
    if payload.get("action") != "created":
        return f"issue_comment.{payload.get('action')} is not handled"
    comment = _section(payload, "comment")
    body = comment.get("body")
    if not isinstance(body, str) or not _COMMAND_RE.match(body.lstrip()):
        return "comment is not a /nomi command"
    if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
        return "only owners, members and collaborators can run /nomi"
    kind = Kind.PULL_REQUEST if _section(payload, "issue").get("pull_request") else Kind.ISSUE
    return _job(payload, "issue", kind, delivery_id, "issue_comment.created")


def _job(
    payload: dict[str, Any], subject_key: str, kind: Kind, delivery_id: str, trigger: str
) -> TriageJob:
    full_name = _section(payload, "repository").get("full_name")
    number = _section(payload, subject_key).get("number")
    if not isinstance(full_name, str) or isinstance(number, bool) or not isinstance(number, int):
        raise _MalformedPayloadError(f"payload lacks repository.full_name or {subject_key}.number")
    try:
        target = parse_target(f"{full_name}#{number}")
    except ValueError as exc:
        raise _MalformedPayloadError(str(exc)) from exc
    installation_id = _section(payload, "installation").get("id")
    if isinstance(installation_id, bool) or not isinstance(installation_id, int):
        installation_id = None
    return TriageJob(
        repo=target.repo,
        number=target.number,
        kind=kind.value,
        installation_id=installation_id,
        delivery_id=delivery_id,
        trigger=trigger,
    )


def _section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------


def create_app(settings: Settings | None = None, *, handler: JobHandler | None = None) -> FastAPI:
    """Build the webhook server; ``handler`` replaces the real pipeline in tests."""
    settings = settings or get_settings()
    job_handler = handler or _pipeline_handler(settings)
    jobs: queue.Queue[Any] = queue.Queue(maxsize=MAX_QUEUED_JOBS)
    pending = _PendingTargets()
    deliveries = _DeliveryLog()
    secret = settings.webhook_secret.get_secret_value() if settings.webhook_secret else ""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        worker = _Worker(jobs, job_handler, on_start=pending.started)
        worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(title="Nomi", version=__version__, lifespan=lifespan)
    app.state.queue = jobs

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        if not secret:
            return _error(503, "webhook secret is not configured; set NOMI_WEBHOOK_SECRET")
        body = await _read_body(request, MAX_PAYLOAD_BYTES)
        if body is None:
            return _error(413, "payload too large")
        if not verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
            return _error(401, "invalid or missing X-Hub-Signature-256")
        payload = _parse_json(body)
        if payload is None:
            return _error(400, "body is not a JSON object")
        delivery_id = request.headers.get("x-github-delivery") or f"local-{uuid.uuid4().hex}"
        if deliveries.seen_before(delivery_id):
            return JSONResponse({"status": "duplicate"}, status_code=200)
        if _section(payload, "sender").get("type") == "Bot":
            return _ignored("sender is a bot")
        try:
            decision = _route(request.headers.get("x-github-event", ""), payload, delivery_id)
        except _MalformedPayloadError as exc:
            return _error(400, str(exc))
        if isinstance(decision, str):
            return _ignored(decision)
        status = pending.enqueue(jobs, decision)
        if status == "full":
            deliveries.forget(delivery_id)
            logger.warning("triage queue full; refused %s#%d", decision.repo, decision.number)
            return _error(503, "triage queue is full; redeliver the event later")
        logger.info("%s %s#%d (%s)", status, decision.repo, decision.number, decision.trigger)
        return JSONResponse({"status": status}, status_code=202)

    return app


def _pipeline_handler(settings: Settings) -> JobHandler:
    """The production handler; imports the pipeline lazily so this module stays light."""

    def handle(job: TriageJob) -> None:
        from nomi.pipeline import analyze_submission
        from nomi.report import render_markdown

        process_job(
            job,
            settings,
            analyze_fn=lambda submission: analyze_submission(submission, settings),
            render_fn=render_markdown,
        )

    return handle


async def _read_body(request: Request, limit: int) -> bytes | None:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_json(body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _ignored(reason: str) -> JSONResponse:
    return JSONResponse({"status": "ignored", "reason": reason}, status_code=202)


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)
