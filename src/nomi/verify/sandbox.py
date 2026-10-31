"""Dynamic reproduction of Python snippets from bug reports, in a locked-down container.

Why: the strongest evidence that a bug report is real is running the
reporter's own snippet against the repository and watching it fail the way
they said it does. Those snippets are untrusted code from strangers, so they
only ever run inside a throwaway Docker container with no network, no Linux
capabilities, a read-only root file system, a read-only view of the
repository, an unprivileged user, CPU / memory / process limits and a hard
timeout. No host environment variable is ever forwarded into the container,
and even the docker CLI itself runs with an allow-listed environment, so
tokens and secrets never get near the snippet.

Asymmetry on purpose: a run can *support* a report strongly (the reported
exception really happens at the pinned commit), but a clean run only counts
against it moderately, because the reporter's environment (Python version,
OS, optional dependencies) may differ from the sandbox's.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import threading
import time
import uuid
import warnings
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from pydantic import BaseModel, Field

from nomi.models import (
    Artifacts,
    Check,
    CheckStatus,
    CodeBlock,
    Evidence,
    EvidenceKind,
    Kind,
    Signal,
    Submission,
)
from nomi.repo.workspace import Workspace

DEFAULT_IMAGE = "python:3.12-slim"
SANDBOX_USER = "65534:65534"  # nobody:nogroup
SCRIPT_NAME = "snippet.py"
MAX_OUTPUT_BYTES = 16 * 1024  # kept per stream: first half and last half
MAX_SNIPPET_CHARS = 20_000
START_GRACE_S = 15  # container start (or an image pull) on top of the snippet timeout
PROBE_TIMEOUT_S = 5
CLEANUP_TIMEOUT_S = 10
EXCERPT_LINES = 20
PYTHON_LANGUAGES = frozenset({"python", "py", "python3", "pycon"})

# `docker run` exits with these when the container itself could not be run.
_DOCKER_ERROR_CODES = frozenset({125, 126, 127})
_KILLED_EXIT_CODE = 137
_READ_CHUNK = 64 * 1024
_MAX_LINE_CHARS = 300
_MAX_SNIPPET_EXCERPT = 4000
_SOURCE_ROOTS = ("src", "lib", "python")
_DOCKER_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "SYSTEMROOT",
    }
)
_EXCEPTION_SUFFIXES = ("Error", "Exception", "Warning", "Exit", "Interrupt", "Iteration", "Group")
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_BOM = "\ufeff"

_LANGUAGE = re.compile(r"[a-z0-9_+-]+")
_HAS_PROMPT = re.compile(r"^(?:>>>|In \[\d+\]:)", re.M)
_PROMPT_LINE = re.compile(r"(?:>>>|\.\.\.|In \[\d+\]:|\s*\.\.\.:)(?: |$)")
_PIP_INSTALL = re.compile(r"\bpip[0-9.]*\s+install\b", re.I)
_SHELL_PROMPT = re.compile(r"^\s*\$ ", re.M)
_TRACEBACK = re.compile(
    r"Traceback \(most recent call last\)|^\s*File \"[^\"\n]*\", line \d+", re.M
)
_TRACEBACK_START = re.compile(r"Traceback \(most recent call last\):|^ {2}File \"|^\s*\+-")
_GROUP_PREFIX = re.compile(r"^(?:\s*\|)+ ?")  # exception-group sub-traceback columns
_EXCEPTION_LINE = re.compile(r"(?P<name>[A-Za-z_][\w.]*)(?::(?: .*)?)?")
_MESSAGE_PREFIX = re.compile(r"\s*(?P<name>[A-Za-z_][\w.]*)\s*(?::|$)")
_CAMEL_CASE = re.compile(r"[A-Z]\w*")
_MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named '([^'\n]+)'")


class SandboxResult(BaseModel):
    """What happened when a snippet was run, or why it was not."""

    ran: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    reason: str | None = None  # why it did not run, or why it was stopped
    command: list[str] = Field(default_factory=list)


class DockerSandbox:
    """Runs untrusted Python in a disposable, network-less, read-only container."""

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        *,
        timeout_s: int = 60,
        memory: str = "512m",
        cpus: str = "1.0",
        pids_limit: int = 128,
        docker_bin: str = "docker",
    ) -> None:
        self.image = image
        self.timeout_s = timeout_s
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.docker_bin = docker_bin
        self._availability: tuple[bool, str] | None = None

    def available(self) -> tuple[bool, str]:
        """(usable, human-readable reason); probed once per instance."""
        if self._availability is None:
            self._availability = self._probe()
        return self._availability

    def _probe(self) -> tuple[bool, str]:
        if shutil.which(self.docker_bin) is None:
            return False, f"`{self.docker_bin}` was not found on PATH"
        try:
            proc = subprocess.run(
                [self.docker_bin, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=PROBE_TIMEOUT_S,
                env=_docker_env(),
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return False, f"the Docker daemon did not answer within {PROBE_TIMEOUT_S} s"
        except OSError as exc:
            return False, f"could not run `{self.docker_bin}`: {exc}"
        version = proc.stdout.strip()
        if proc.returncode != 0 or not version:
            why = _last_line(proc.stderr) or f"exit code {proc.returncode}"
            return False, f"the Docker daemon is not reachable: {why}"
        return True, f"Docker {_clip_line(version)} is available"

    def build_command(
        self, *, script_dir: Path, repo_dir: Path | None, pythonpath: list[str], name: str
    ) -> list[str]:
        """The full `docker run` invocation; only explicit NAME=value variables are set."""
        mounts = ["-v", f"{_mount_source(script_dir)}:/sandbox:ro"]
        if repo_dir is not None:
            mounts += ["-v", f"{_mount_source(repo_dir)}:/repo:ro"]
        return [
            self.docker_bin,
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,
            "--cpus",
            self.cpus,
            "--user",
            SANDBOX_USER,
            "--ipc",
            "none",
            "--log-driver",
            "none",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTHONUNBUFFERED=1",
            "-e",
            "HOME=/tmp",
            "-e",
            f"PYTHONPATH={':'.join(pythonpath)}",
            *mounts,
            "-w",
            "/tmp",
            self.image,
            "python",
            "-B",
            "-s",
            f"/sandbox/{SCRIPT_NAME}",
        ]

    def run_python(
        self, code: str, *, repo_dir: Path | None = None, pythonpath: list[str] | None = None
    ) -> SandboxResult:
        """Run `code` as a script in a fresh container; never raises for sandbox problems."""
        ok, why = self.available()
        if not ok:
            return SandboxResult(ran=False, reason=why)
        name = f"nomi-sbx-{uuid.uuid4().hex[:12]}"
        script_dir: Path | None = None
        try:
            script_dir = Path(tempfile.mkdtemp(prefix=f"{name}-")).resolve()
            _write_script(script_dir, code)
            command = self.build_command(
                script_dir=script_dir,
                repo_dir=repo_dir.resolve() if repo_dir is not None else None,
                pythonpath=list(pythonpath or []),
                name=name,
            )
            return self._execute(command, name)
        except (OSError, ValueError) as exc:
            return SandboxResult(ran=False, reason=f"could not prepare the sandbox: {exc}")
        finally:
            if script_dir is not None:
                shutil.rmtree(script_dir, ignore_errors=True)

    def _execute(self, command: list[str], name: str) -> SandboxResult:
        limit = self.timeout_s + START_GRACE_S
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_docker_env(),
            )
        except OSError as exc:
            return SandboxResult(
                ran=False, reason=f"could not start `{self.docker_bin}`: {exc}", command=command
            )
        stdout, stderr = _BoundedReader(proc.stdout), _BoundedReader(proc.stderr)
        exit_code: int | None
        try:
            exit_code = proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            exit_code = None
            self._stop_container(name)
            proc.kill()
            proc.wait()
        result = SandboxResult(
            ran=True,
            exit_code=exit_code,
            stdout=stdout.text(),
            stderr=stderr.text(),
            timed_out=exit_code is None,
            duration_s=round(time.monotonic() - started, 3),
            command=command,
        )
        if result.timed_out:
            result.reason = f"no result within {limit} s; the container was killed"
        elif exit_code in _DOCKER_ERROR_CODES:
            result.ran = False
            why = _last_line(result.stderr) or f"exit code {exit_code}"
            result.reason = f"Docker could not run the container: {why}"
        return result

    def _stop_container(self, name: str) -> None:
        """Kill (and, if it never started, remove) a container that outlived its timeout."""
        for args in (["kill", name], ["rm", "--force", name]):
            with suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    [self.docker_bin, *args],
                    capture_output=True,
                    timeout=CLEANUP_TIMEOUT_S,
                    env=_docker_env(),
                    stdin=subprocess.DEVNULL,
                )


class _BoundedReader:
    """Drains a pipe on a thread, keeping only its first and last bytes.

    A snippet can print forever; buffering all of it would let untrusted code
    exhaust the host's memory, so the middle is counted and dropped.
    """

    def __init__(self, stream: IO[bytes] | None, limit: int = MAX_OUTPUT_BYTES) -> None:
        self._keep = limit // 2
        self._head = bytearray()
        self._tail = bytearray()
        self._dropped = 0
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self._thread.start()

    def _drain(self, stream: IO[bytes] | None) -> None:
        if stream is None:
            return
        with suppress(OSError, ValueError):
            read = getattr(stream, "read1", stream.read)
            while chunk := read(_READ_CHUNK):
                self._feed(chunk)
        with suppress(OSError):
            stream.close()

    def _feed(self, chunk: bytes) -> None:
        room = self._keep - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        self._tail += chunk
        excess = len(self._tail) - self._keep
        if excess > 0:
            del self._tail[:excess]
            self._dropped += excess

    def text(self) -> str:
        self._thread.join(CLEANUP_TIMEOUT_S)
        if not self._dropped:
            return bytes(self._head + self._tail).decode("utf-8", errors="replace")
        head = self._head.decode("utf-8", errors="replace")
        tail = self._tail.decode("utf-8", errors="replace")
        return f"{head}\n[... {self._dropped} bytes of output truncated by nomi ...]\n{tail}"


def _docker_env() -> dict[str, str]:
    """The docker CLI's environment: what it needs to find the daemon, nothing else."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in _DOCKER_ENV_KEYS or key.startswith("DOCKER_")
    }


def _mount_source(path: Path) -> str:
    text = str(path)
    if not path.is_absolute() or any(ch in text for ch in ":,\n\0"):
        raise ValueError(f"cannot bind-mount {text!r} safely")
    return text


def _write_script(directory: Path, code: str) -> None:
    """World-readable so the container's unprivileged user can read it."""
    directory.chmod(0o755)
    script = directory / SCRIPT_NAME
    script.write_bytes(code.encode("utf-8", errors="replace"))
    script.chmod(0o644)


# --------------------------------------------------------------------------
# Choosing the snippet
# --------------------------------------------------------------------------


@dataclass
class _RepoImports:
    names: set[str] = field(default_factory=set)  # local names bound to repository code
    packages: list[str] = field(default_factory=list)  # repository packages imported, in order
    star: bool = False


def select_snippet(artifacts: Artifacts, ws: Workspace) -> CodeBlock | None:
    """The first code block that is a self-contained Python reproduction.

    A candidate is tagged python / py / python3 / pycon (or untagged but valid
    Python), has interactive prompts stripped and their output dropped, and
    contains no `pip install`, shell prompt or traceback. It must import one of
    the repository's packages and actually reach that code from a top-level
    statement, directly or through a function or class it defines, so quoted
    library source or a proposed patch is never mistaken for a reproduction.
    """
    found = _find_snippet(artifacts, frozenset(ws.python_packages))
    return found[0] if found is not None else None


def _find_snippet(
    artifacts: Artifacts, packages: frozenset[str]
) -> tuple[CodeBlock, _RepoImports] | None:
    if not packages:
        return None
    for block in artifacts.code_blocks:
        code = _clean_candidate(block)
        tree = _parse(code) if code is not None else None
        if tree is None:
            continue
        imports = _repo_imports(tree, packages)
        if _reaches_repo(tree, imports):
            return block.model_copy(update={"content": code}), imports
    return None


def _clean_candidate(block: CodeBlock) -> str | None:
    language = _language(block.language)
    if language is not None and language not in PYTHON_LANGUAGES:
        return None
    if len(block.content) > MAX_SNIPPET_CHARS:
        return None
    text = block.content.replace("\r\n", "\n").replace("\r", "\n").lstrip(_BOM)
    text = textwrap.dedent(text).strip("\n")
    if language == "pycon" or _HAS_PROMPT.search(text):
        text = _strip_prompts(text)
    if not text.strip() or "\0" in text:
        return None
    if _PIP_INSTALL.search(text) or _SHELL_PROMPT.search(text) or _TRACEBACK.search(text):
        return None
    return text + "\n"


def _language(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    match = _LANGUAGE.match(raw.strip().lower().lstrip("{.").strip())
    return match.group(0) if match else ""


def _strip_prompts(text: str) -> str:
    """Keep the input of a REPL transcript (>>> / ... / IPython) and drop its output."""
    kept: list[str] = []
    for line in text.split("\n"):
        match = _PROMPT_LINE.match(line)
        if match:
            kept.append(line[match.end() :])
    return "\n".join(kept)


def _parse(code: str) -> ast.Module | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return ast.parse(code)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None


def _repo_imports(tree: ast.Module, packages: frozenset[str]) -> _RepoImports:
    found = _RepoImports()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in packages:
                    found.names.add(alias.asname or top)
                    _append_once(found.packages, top)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            if top not in packages:
                continue
            _append_once(found.packages, top)
            for alias in node.names:
                if alias.name == "*":
                    found.star = True
                else:
                    found.names.add(alias.asname or alias.name)
    return found


def _reaches_repo(tree: ast.Module, imports: _RepoImports) -> bool:
    """Whether a top-level statement uses repository code (possibly via local defs)."""
    roots = [s for s in tree.body if not isinstance(s, (ast.Import, ast.ImportFrom, *_DEFINITIONS))]
    if imports.star:
        return any(isinstance(node, ast.Call) for root in roots for node in ast.walk(root))
    if not imports.names:
        return False
    local_defs = {s.name: s for s in tree.body if isinstance(s, _DEFINITIONS)}
    pending = _loaded_names(roots)
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in imports.names:
            return True
        if name in seen or name not in local_defs:
            continue
        seen.add(name)
        pending |= _loaded_names([local_defs[name]])
    return False


def _loaded_names(nodes: list[ast.stmt]) -> set[str]:
    return {
        node.id
        for root in nodes
        for node in ast.walk(root)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _append_once(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


# --------------------------------------------------------------------------
# Running it and judging the outcome
# --------------------------------------------------------------------------


def run_reproduction(
    submission: Submission,
    artifacts: Artifacts,
    ws: Workspace,
    sandbox: DockerSandbox | None,
) -> list[Check]:
    """Run the report's Python snippet in the sandbox and compare with what it claims."""
    if sandbox is None:
        return [
            _skipped(
                "repro.sandbox", "no Docker sandbox for this run (disabled or Docker unavailable)"
            )
        ]
    ok, why = sandbox.available()
    if not ok:
        return [_skipped("repro.sandbox", why)]
    packages = ws.python_packages
    if not packages:
        return [
            _skipped("repro.unsupported", "dynamic reproduction currently supports Python projects")
        ]
    found = _find_snippet(artifacts, frozenset(packages))
    if found is None:
        return [
            _skipped("repro.snippet", "no self-contained Python reproduction snippet in the report")
        ]
    block, imports = found
    result = sandbox.run_python(
        block.content, repo_dir=ws.root, pythonpath=_pythonpath(imports.packages, ws)
    )
    if not result.ran:
        return [_skipped("repro.sandbox", result.reason or "the sandbox could not run the snippet")]
    evidence = _evidence(result, block, submission)
    return [_judge(result, expected_errors(artifacts), set(packages), ws, evidence)]


def expected_errors(artifacts: Artifacts) -> list[str]:
    """Exception class names the report says happen (short names, report order)."""
    names: list[str] = []
    for trace in artifacts.stack_traces:
        if trace.language in ("python", "unknown") and trace.error_type:
            name = _short_name(trace.error_type.strip())
            if _CAMEL_CASE.fullmatch(name):
                _append_once(names, name)
    for message in artifacts.error_messages:
        match = _MESSAGE_PREFIX.match(message)
        if match and _is_exception_name(_short_name(match.group("name"))):
            _append_once(names, _short_name(match.group("name")))
    return names


def _judge(
    result: SandboxResult,
    expected: list[str],
    packages: set[str],
    ws: Workspace,
    evidence: list[Evidence],
) -> Check:
    where = f"in an isolated sandbox at {_short_sha(ws.commit)}"
    ran_for = f"after {result.duration_s:.1f} s"
    if result.timed_out:
        return _run_check(
            CheckStatus.WARN,
            f"Reproduction timed out: the report's snippet did not finish {where}",
            strength=0.1,
            detail=f"Stopped {ran_for}: {result.reason or 'timed out'}.",
            evidence=evidence,
        )
    raised = _raised_errors(result.stderr)
    missing = _missing_module(raised[0][1]) if raised else None
    if missing is not None and missing.split(".")[0] not in packages:
        return Check(
            id="repro.dependency",
            title=f"Reproduction needs third-party dependency {missing}",
            status=CheckStatus.SKIPPED,
            detail=(
                f"The snippet stopped with ModuleNotFoundError: it needs third-party dependency "
                f"`{missing}` (the sandbox has no network to install it)."
            ),
            evidence=evidence,
        )
    hit = next(((name, line) for name, line in raised if name in expected), None)
    if hit is not None:
        return _run_check(
            CheckStatus.PASS,
            f"Reproduced: the report's snippet raises {hit[0]} {where}",
            signal=Signal.SUPPORTS,
            strength=0.9,
            detail=f"Exit code {result.exit_code} {ran_for}; it raised `{_clip_line(hit[1])}`.",
            evidence=evidence,
        )
    reported = " or ".join(expected[:3])
    if result.exit_code != 0 or raised:
        got = raised[0][0] if raised else _describe_exit(result.exit_code)
        last = raised[0][1] if raised else (_last_line(result.stderr) or got)
        title = (
            f"Reproduction failed differently (got {got}, report says {reported})"
            if expected
            else f"The report's snippet fails {where}: {_clip_line(last, 160)}"
        )
        return _run_check(
            CheckStatus.WARN,
            title,
            strength=0.2,
            detail=f"Exit code {result.exit_code} {ran_for}; last error: `{_clip_line(last)}`.",
            evidence=evidence,
        )
    if expected:
        mentioned = [name for name in expected if _mentions(result, name)]
        if mentioned:
            return _run_check(
                CheckStatus.WARN,
                f"The report's snippet exited normally, but its output mentions {mentioned[0]}",
                strength=0.2,
                detail=f"Exit code 0 {ran_for}; the snippet may catch or only log the error.",
                evidence=evidence,
            )
        return _run_check(
            CheckStatus.FAIL,
            f"Did not reproduce: ran without the reported {reported} {where}",
            signal=Signal.CONTRADICTS,
            strength=0.5,
            detail=(
                f"Exit code 0 {ran_for}. The reporter's environment (Python version, OS, "
                "dependencies) may differ from the sandbox's, so this alone is not proof."
            ),
            evidence=evidence,
        )
    output = _tail(result.stdout) or "(no output)"
    return _run_check(
        CheckStatus.INFO,
        f"The report's snippet ran without error {where}",
        detail=f"Exit code 0 {ran_for}. Output:\n{output}",
        evidence=evidence,
    )


def _raised_errors(stderr: str) -> list[tuple[str, str]]:
    """(short exception name, exception line) of each traceback in stderr, last first.

    When no traceback header survives (e.g. truncated output), a final line
    shaped like `SomeError: message` still counts.
    """
    found: list[tuple[str, str]] = []
    in_traceback = False
    for raw in stderr.splitlines():
        line = _GROUP_PREFIX.sub("", raw)
        if _TRACEBACK_START.search(line):
            in_traceback = True
            continue
        if not in_traceback or not line.strip() or line[0] in " \t":
            continue
        in_traceback = False
        match = _EXCEPTION_LINE.fullmatch(line.rstrip())
        if match:
            found.append((_short_name(match.group("name")), line.strip()))
    if not found:
        last = _last_line(stderr)
        match = _EXCEPTION_LINE.fullmatch(last)
        if match and _is_exception_name(_short_name(match.group("name"))):
            found.append((_short_name(match.group("name")), last))
    return found[::-1]


def _missing_module(exception_line: str) -> str | None:
    match = _MISSING_MODULE.match(exception_line)
    return match.group(1) if match else None


def _mentions(result: SandboxResult, name: str) -> bool:
    pattern = re.compile(rf"(?<!\w){re.escape(name)}\b")
    return bool(pattern.search(result.stdout) or pattern.search(result.stderr))


def _is_exception_name(name: str) -> bool:
    return bool(_CAMEL_CASE.fullmatch(name)) and name.endswith(_EXCEPTION_SUFFIXES)


def _short_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _describe_exit(code: int | None) -> str:
    if code == _KILLED_EXIT_CODE:
        return f"exit code {code} (killed, likely by the memory or process limit)"
    return f"exit code {code}"


def _pythonpath(imported: list[str], ws: Workspace) -> list[str]:
    """/repo/src for a src-layout package, /repo for a top-level one."""
    paths: list[str] = []
    for package in imported:
        root = next((r for r in _SOURCE_ROOTS if ws.exists(f"{r}/{package}/__init__.py")), None)
        flat = ws.exists(f"{package}/__init__.py") or ws.exists(f"{package}.py")
        _append_once(paths, f"/repo/{root}" if root and not flat else "/repo")
    return paths or ["/repo"]


def _evidence(result: SandboxResult, block: CodeBlock, submission: Submission) -> list[Evidence]:
    failed = result.exit_code != 0 or result.timed_out
    primary, secondary = (
        (result.stderr, result.stdout) if failed else (result.stdout, result.stderr)
    )
    source = "pull request" if submission.kind is Kind.PULL_REQUEST else "issue"
    origin = f"{source} #{submission.number}" if submission.number is not None else "the report"
    return [
        Evidence(
            kind=EvidenceKind.EXECUTION,
            description=shlex.join(result.command) if result.command else "sandbox run",
            excerpt=_tail(primary) or _tail(secondary) or "(no output)",
        ),
        Evidence(
            kind=EvidenceKind.EXECUTION,
            description=f"Snippet taken from {origin} (code block at line {block.line})",
            excerpt=_clip_block(block.content),
        ),
    ]


def _run_check(
    status: CheckStatus,
    title: str,
    *,
    signal: Signal = Signal.NEUTRAL,
    strength: float = 0.0,
    detail: str = "",
    evidence: list[Evidence],
) -> Check:
    return Check(
        id="repro.run",
        title=title,
        status=status,
        signal=signal,
        strength=strength,
        detail=detail,
        evidence=evidence,
    )


def _skipped(check_id: str, reason: str) -> Check:
    return Check(
        id=check_id,
        title="Dynamic reproduction not run",
        status=CheckStatus.SKIPPED,
        detail=reason[:1].upper() + reason[1:],
    )


def _short_sha(commit: str | None) -> str:
    return commit[:7] if commit else "the working tree"


def _tail(text: str, lines: int = EXCERPT_LINES) -> str:
    kept = text.rstrip().splitlines()[-lines:]
    return "\n".join(_clip_line(line, _MAX_LINE_CHARS, strip=False) for line in kept).strip("\n")


def _last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return _clip_line(line)
    return ""


def _clip_line(text: str, limit: int = 200, *, strip: bool = True) -> str:
    line = text.strip() if strip else text.rstrip()
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _clip_block(text: str) -> str:
    if len(text) <= _MAX_SNIPPET_EXCERPT:
        return text.rstrip("\n")
    return text[: _MAX_SNIPPET_EXCERPT - 1] + "…"
