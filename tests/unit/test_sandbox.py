"""Tests for the Docker sandbox and dynamic reproduction.

Everything except the `docker`-marked class runs without Docker: the docker
CLI is replaced by fakes of `subprocess.run` / `subprocess.Popen` /
`shutil.which` that pass every other command through to the real thing.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import subprocess
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nomi.models import (
    Artifacts,
    CheckStatus,
    CodeBlock,
    EvidenceKind,
    Kind,
    Signal,
    StackTrace,
    Submission,
)
from nomi.repo.workspace import Workspace
from nomi.verify import sandbox as sbx
from nomi.verify.sandbox import (
    MAX_OUTPUT_BYTES,
    DockerSandbox,
    SandboxResult,
    expected_errors,
    run_reproduction,
    select_snippet,
)

REPRO = 'from widget.parser import parse_url\n\nparse_url("")\n'
ISSUE = Submission(kind=Kind.ISSUE, repo="acme/widget", number=7, title="parse_url crashes")
TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/sandbox/snippet.py", line 3, in <module>\n'
    '    parse_url("")\n'
    '  File "/repo/src/widget/parser.py", line 9, in parse_url\n'
    '    raise ValueError("empty url")\n'
    "ValueError: empty url\n"
)


def artifacts(
    *blocks: CodeBlock | str, errors: tuple[str, ...] = (), traces: tuple[StackTrace, ...] = ()
) -> Artifacts:
    code_blocks = [
        b if isinstance(b, CodeBlock) else CodeBlock(language="python", content=b, line=3)
        for b in blocks
    ]
    return Artifacts(
        code_blocks=code_blocks, error_messages=list(errors), stack_traces=list(traces)
    )


def flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def values(cmd: list[str], name: str) -> list[str]:
    return [cmd[i + 1] for i, part in enumerate(cmd) if part == name]


# --------------------------------------------------------------------------
# A fake docker CLI
# --------------------------------------------------------------------------


class FakeProcess:
    """Stands in for the Popen object of `docker run`."""

    def __init__(
        self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"", hang: bool = False
    ) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode: int | None = None
        self._code = returncode
        self._hang = hang
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self._hang and not self.killed:
            raise subprocess.TimeoutExpired("docker", timeout or 0)
        self.returncode = -9 if self.killed else self._code
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@dataclass
class FakeDocker:
    process: FakeProcess = field(default_factory=FakeProcess)
    info: Any = field(default_factory=lambda: subprocess.CompletedProcess([], 0, "29.4.3\n", ""))
    which: str | None = "/usr/local/bin/docker"
    popen_error: BaseException | None = None
    runs: list[list[str]] = field(default_factory=list)
    run_envs: list[dict[str, str] | None] = field(default_factory=list)
    popen_cmd: list[str] | None = None
    popen_env: dict[str, str] | None = None
    script: str | None = None
    script_mode: int | None = None
    dir_mode: int | None = None
    script_dir: Path | None = None


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    real_run, real_popen, real_which = subprocess.run, subprocess.Popen, shutil.which

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(cmd, list) or cmd[0] != "docker":
            return real_run(cmd, *args, **kwargs)
        fake.runs.append(list(cmd))
        fake.run_envs.append(kwargs.get("env"))
        if cmd[1] == "info":
            if isinstance(fake.info, BaseException):
                raise fake.info
            return fake.info
        return subprocess.CompletedProcess(cmd, 1, b"", b"Error: No such container")

    def popen(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(cmd, list) or cmd[0] != "docker":
            return real_popen(cmd, *args, **kwargs)
        fake.popen_cmd = list(cmd)
        fake.popen_env = kwargs.get("env")
        script_dir = Path(flag(cmd, "-v").rsplit(":", 2)[0])
        script = script_dir / "snippet.py"
        fake.script_dir = script_dir
        fake.script = script.read_text(encoding="utf-8")
        fake.script_mode = stat.S_IMODE(script.stat().st_mode)
        fake.dir_mode = stat.S_IMODE(script_dir.stat().st_mode)
        if fake.popen_error is not None:
            raise fake.popen_error
        return fake.process

    def which(name: str, *args: Any, **kwargs: Any) -> str | None:
        return fake.which if name == "docker" else real_which(name, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(shutil, "which", which)
    return fake


# --------------------------------------------------------------------------
# available()
# --------------------------------------------------------------------------


class TestAvailable:
    def test_missing_binary(self, fake_docker: FakeDocker) -> None:
        fake_docker.which = None
        ok, why = DockerSandbox().available()
        assert not ok
        assert "not found" in why
        assert fake_docker.runs == []

    def test_daemon_answers_and_result_is_cached(
        self, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
        box = DockerSandbox()
        assert box.available() == (True, "Docker 29.4.3 is available")
        assert box.available()[0]
        assert fake_docker.runs == [["docker", "info", "--format", "{{.ServerVersion}}"]]
        env = fake_docker.run_envs[0]
        assert env is not None and "GITHUB_TOKEN" not in env and "PATH" in env

    def test_daemon_down(self, fake_docker: FakeDocker) -> None:
        fake_docker.info = subprocess.CompletedProcess(
            [], 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.\n"
        )
        ok, why = DockerSandbox().available()
        assert not ok
        assert "Cannot connect to the Docker daemon" in why

    def test_empty_version_is_not_available(self, fake_docker: FakeDocker) -> None:
        fake_docker.info = subprocess.CompletedProcess([], 0, "\n", "")
        assert not DockerSandbox().available()[0]

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (subprocess.TimeoutExpired("docker", 5), "did not answer within 5 s"),
            (PermissionError("denied"), "could not run"),
        ],
    )
    def test_probe_errors(self, fake_docker: FakeDocker, error: Exception, message: str) -> None:
        fake_docker.info = error
        ok, why = DockerSandbox().available()
        assert not ok
        assert message in why


# --------------------------------------------------------------------------
# build_command()
# --------------------------------------------------------------------------


class TestBuildCommand:
    def command(self, tmp_path: Path, **kwargs: Any) -> list[str]:
        params: dict[str, Any] = {
            "script_dir": tmp_path / "script",
            "repo_dir": tmp_path / "repo",
            "pythonpath": ["/repo/src", "/repo"],
            "name": "nomi-sbx-0123456789ab",
        }
        params.update(kwargs)
        return DockerSandbox(memory="256m", cpus="0.5", pids_limit=64).build_command(**params)

    def test_security_flags(self, tmp_path: Path) -> None:
        cmd = self.command(tmp_path)
        assert cmd[:3] == ["docker", "run", "--rm"]
        assert flag(cmd, "--name") == "nomi-sbx-0123456789ab"
        assert flag(cmd, "--network") == "none"
        assert "--read-only" in cmd
        assert flag(cmd, "--tmpfs") == "/tmp:rw,nosuid,nodev,size=64m"
        assert flag(cmd, "--cap-drop") == "ALL"
        assert flag(cmd, "--security-opt") == "no-new-privileges"
        assert flag(cmd, "--pids-limit") == "64"
        assert flag(cmd, "--memory") == "256m"
        assert flag(cmd, "--memory-swap") == "256m"
        assert flag(cmd, "--cpus") == "0.5"
        assert flag(cmd, "--user") == "65534:65534"
        assert flag(cmd, "--ipc") == "none"
        assert flag(cmd, "-w") == "/tmp"
        for forbidden in ("--privileged", "--env-file", "--cap-add", "--pid", "--net"):
            assert forbidden not in cmd

    def test_read_only_mounts_and_entrypoint(self, tmp_path: Path) -> None:
        cmd = self.command(tmp_path)
        assert values(cmd, "-v") == [
            f"{tmp_path / 'script'}:/sandbox:ro",
            f"{tmp_path / 'repo'}:/repo:ro",
        ]
        assert cmd[-5:] == ["python:3.12-slim", "python", "-B", "-s", "/sandbox/snippet.py"]

    def test_without_repository(self, tmp_path: Path) -> None:
        cmd = self.command(tmp_path, repo_dir=None, pythonpath=[])
        assert values(cmd, "-v") == [f"{tmp_path / 'script'}:/sandbox:ro"]
        assert "PYTHONPATH=" in values(cmd, "-e")

    def test_only_explicit_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
        monkeypatch.setenv("NOMI_GITHUB_TOKEN", "ghp_should_not_leak")
        cmd = self.command(tmp_path)
        assert values(cmd, "-e") == [
            "PYTHONDONTWRITEBYTECODE=1",
            "PYTHONUNBUFFERED=1",
            "HOME=/tmp",
            "PYTHONPATH=/repo/src:/repo",
        ]
        assert "ghp_should_not_leak" not in " ".join(cmd)
        assert "GITHUB_TOKEN" not in " ".join(cmd)
        assert "--env" not in cmd

    @pytest.mark.parametrize("bad", ["we:ird", "a,b", "line\nbreak"])
    def test_rejects_unsafe_mount_paths(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ValueError):
            self.command(tmp_path, repo_dir=tmp_path / bad)

    def test_rejects_relative_paths(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            self.command(tmp_path, script_dir=Path("relative"))


# --------------------------------------------------------------------------
# run_python()
# --------------------------------------------------------------------------


class TestRunPython:
    def test_runs_the_code_in_a_fresh_readable_dir(
        self, fake_docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
        fake_docker.process = FakeProcess(
            returncode=1, stdout="héllo ✓\n".encode(), stderr=TRACEBACK.encode()
        )
        code = "print('héllo ✓')\nraise ValueError('x')\n"
        result = DockerSandbox(timeout_s=7).run_python(
            code, repo_dir=tmp_path, pythonpath=["/repo/src"]
        )
        assert result.ran and result.exit_code == 1 and not result.timed_out
        assert result.stdout == "héllo ✓\n"
        assert result.stderr == TRACEBACK
        assert result.reason is None
        assert result.duration_s >= 0
        assert result.command == fake_docker.popen_cmd
        assert fake_docker.script == code
        assert fake_docker.script_mode == 0o644
        assert fake_docker.dir_mode == 0o755
        assert fake_docker.script_dir is not None and not fake_docker.script_dir.exists()
        cmd = fake_docker.popen_cmd
        assert cmd is not None
        assert re.fullmatch(r"nomi-sbx-[0-9a-f]{12}", flag(cmd, "--name"))
        assert f"{tmp_path.resolve()}:/repo:ro" in values(cmd, "-v")
        assert "PYTHONPATH=/repo/src" in values(cmd, "-e")
        assert fake_docker.process.wait_timeouts == [7 + 15]
        env = fake_docker.popen_env
        assert env is not None and "GITHUB_TOKEN" not in env
        assert "ghp_should_not_leak" not in " ".join(cmd)

    def test_unique_container_names(self, fake_docker: FakeDocker) -> None:
        box = DockerSandbox()
        names = set()
        for _ in range(3):
            fake_docker.process = FakeProcess()
            box.run_python("pass\n")
            assert fake_docker.popen_cmd is not None
            names.add(flag(fake_docker.popen_cmd, "--name"))
        assert len(names) == 3

    def test_timeout_kills_the_container(self, fake_docker: FakeDocker) -> None:
        fake_docker.process = FakeProcess(stdout=b"partial\n", hang=True)
        result = DockerSandbox(timeout_s=3).run_python("while True: pass\n")
        assert result.ran and result.timed_out
        assert result.exit_code is None
        assert result.stdout == "partial\n"
        assert result.reason is not None and "killed" in result.reason
        assert fake_docker.popen_cmd is not None
        name = flag(fake_docker.popen_cmd, "--name")
        assert ["docker", "kill", name] in fake_docker.runs
        assert fake_docker.process.killed
        assert fake_docker.script_dir is not None and not fake_docker.script_dir.exists()

    def test_truncates_huge_output(self, fake_docker: FakeDocker) -> None:
        huge = b"HEAD" + b"x" * 200_000 + b"TAIL"
        fake_docker.process = FakeProcess(returncode=1, stdout=huge, stderr=b"e" * 50_000)
        result = DockerSandbox().run_python("print('x' * 10**9)\n")
        for text in (result.stdout, result.stderr):
            assert "truncated by nomi" in text
            assert len(text.encode()) <= MAX_OUTPUT_BYTES + 100
        assert result.stdout.startswith("HEAD")
        assert result.stdout.endswith("TAIL")

    def test_output_at_the_limit_is_kept_whole(self, fake_docker: FakeDocker) -> None:
        exact = "é" * (MAX_OUTPUT_BYTES // 2)
        fake_docker.process = FakeProcess(stdout=exact.encode())
        result = DockerSandbox().run_python("pass\n")
        assert result.stdout == exact

    def test_truncation_never_breaks_on_split_characters(self, fake_docker: FakeDocker) -> None:
        fake_docker.process = FakeProcess(stdout=("✓" * 30_000).encode())
        result = DockerSandbox().run_python("pass\n")
        assert "truncated by nomi" in result.stdout

    def test_unavailable_docker_never_starts_a_container(self, fake_docker: FakeDocker) -> None:
        fake_docker.which = None
        result = DockerSandbox().run_python("print(1)\n")
        assert not result.ran
        assert result.reason is not None and "not found" in result.reason
        assert fake_docker.popen_cmd is None

    def test_docker_error_exit_code_means_not_run(self, fake_docker: FakeDocker) -> None:
        fake_docker.process = FakeProcess(
            returncode=125, stderr=b"docker: Error response from daemon: no such image.\n"
        )
        result = DockerSandbox().run_python("print(1)\n")
        assert not result.ran
        assert result.reason is not None and "no such image" in result.reason

    def test_popen_failure_cleans_up(self, fake_docker: FakeDocker) -> None:
        fake_docker.popen_error = OSError("exec format error")
        result = DockerSandbox().run_python("print(1)\n")
        assert not result.ran
        assert result.reason is not None and "exec format error" in result.reason
        assert fake_docker.script_dir is not None and not fake_docker.script_dir.exists()

    def test_unsafe_repo_path_is_refused(self, fake_docker: FakeDocker, tmp_path: Path) -> None:
        bad = tmp_path / "a:b"
        bad.mkdir()
        result = DockerSandbox().run_python("print(1)\n", repo_dir=bad)
        assert not result.ran
        assert result.reason is not None and "bind-mount" in result.reason
        assert fake_docker.popen_cmd is None

    def test_lone_surrogates_do_not_crash(self, fake_docker: FakeDocker) -> None:
        result = DockerSandbox().run_python("print('\ud800')\n")
        assert result.ran
        assert fake_docker.script == "print('?')\n"


# --------------------------------------------------------------------------
# select_snippet()
# --------------------------------------------------------------------------


def block(content: str, language: str | None = "python", line: int = 3) -> CodeBlock:
    return CodeBlock(language=language, content=content, line=line)


class TestSelectSnippet:
    @pytest.mark.parametrize("language", ["python", "py", "python3", "Python", "py title=x"])
    def test_python_tags(self, sample_repo: Workspace, language: str) -> None:
        chosen = select_snippet(artifacts(block(REPRO, language)), sample_repo)
        assert chosen is not None
        assert chosen.content == REPRO
        assert chosen.line == 3

    def test_untagged_python_with_import(self, sample_repo: Workspace) -> None:
        assert select_snippet(artifacts(block(REPRO, None)), sample_repo) is not None

    def test_untagged_prose_is_ignored(self, sample_repo: Workspace) -> None:
        prose = block("When I call widget parse_url it crashes", None)
        assert select_snippet(artifacts(prose), sample_repo) is None

    @pytest.mark.parametrize("language", ["bash", "console", "text", "javascript", "pytb"])
    def test_other_languages_are_ignored(self, sample_repo: Workspace, language: str) -> None:
        assert select_snippet(artifacts(block(REPRO, language)), sample_repo) is None

    def test_pycon_prompts_and_output(self, sample_repo: Workspace) -> None:
        transcript = (
            ">>> from widget.parser import parse_url\n"
            ">>> for u in ['']:\n"
            "...     parse_url(u)\n"
            "...\n"
            "Traceback (most recent call last):\n"
            '  File "<stdin>", line 2, in <module>\n'
            "ValueError: empty url\n"
            ">>> print('done')\n"
            "done\n"
        )
        for language in ("pycon", "python", None):
            chosen = select_snippet(artifacts(block(transcript, language)), sample_repo)
            assert chosen is not None
            assert chosen.content == (
                "from widget.parser import parse_url\n"
                "for u in ['']:\n"
                "    parse_url(u)\n"
                "\n"
                "print('done')\n"
            )

    def test_ipython_prompts(self, sample_repo: Workspace) -> None:
        transcript = "In [1]: import widget\n\nIn [2]: widget.parse_url('')\nOut[2]: oops\n"
        chosen = select_snippet(artifacts(block(transcript)), sample_repo)
        assert chosen is not None
        assert chosen.content == "import widget\nwidget.parse_url('')\n"

    @pytest.mark.parametrize(
        "content",
        [
            "pip install widget\n" + REPRO,
            "# first: pip3 install widget\n" + REPRO,
            "$ python repro.py\n" + REPRO,
            REPRO + TRACEBACK,
            REPRO + 'File "/x.py", line 3, in <module>\n',
            "from widget.parser import parse_url\nparse_url(\n",
            "from widget.parser import parse_url\x00\nparse_url('')\n",
            "",
            "   \n\n",
        ],
    )
    def test_rejected_blocks(self, sample_repo: Workspace, content: str) -> None:
        assert select_snippet(artifacts(block(content)), sample_repo) is None

    def test_must_import_a_repository_package(self, sample_repo: Workspace) -> None:
        stdlib = "import json\njson.loads('{')\n"
        relative = "from .parser import parse_url\nparse_url('')\n"
        assert select_snippet(artifacts(block(stdlib), block(relative)), sample_repo) is None

    def test_quoted_library_source_is_not_a_reproduction(self, sample_repo: Workspace) -> None:
        library = sample_repo.read_text("src/widget/net.py") or ""
        with_logger = (
            "import logging\nfrom widget.parser import parse_url\n"
            "log = logging.getLogger(__name__)\n\n"
            "def fetch(url):\n    return parse_url(url)\n"
        )
        definitions = "from widget import parse_url\n\ndef fixed(url):\n    return parse_url(url)\n"
        chosen = select_snippet(
            artifacts(block(library), block(with_logger), block(definitions)), sample_repo
        )
        assert chosen is None

    @pytest.mark.parametrize(
        "content",
        [
            "import widget as w\nw.parse_url('')\n",
            "import widget.parser\nwidget.parser.parse_url('')\n",
            "from widget.net import Client\nClient().fetch('ftp://x')\n",
            "from widget.parser import *\nparse_url('')\n",
            "from widget import parse_url\n\ndef main():\n    parse_url('')\n\n"
            "if __name__ == '__main__':\n    main()\n",
            "from widget.net import Client\n\nclass Mine(Client):\n    pass\n\nMine().fetch('x')\n",
        ],
    )
    def test_reaches_repository_code(self, sample_repo: Workspace, content: str) -> None:
        assert select_snippet(artifacts(block(content)), sample_repo) is not None

    def test_imported_but_never_used(self, sample_repo: Workspace) -> None:
        unused = "def main():\n    from widget import parse_url\n    return 1\n\nmain()\n"
        assert select_snippet(artifacts(block(unused)), sample_repo) is None

    def test_first_valid_block_wins(self, sample_repo: Workspace) -> None:
        second = "import widget\nwidget.parse_url('x')\n"
        chosen = select_snippet(
            artifacts(block("$ widget fetch x", "bash"), block(REPRO, line=9), block(second)),
            sample_repo,
        )
        assert chosen is not None and chosen.line == 9

    def test_crlf_indentation_and_bom(self, sample_repo: Workspace) -> None:
        messy = "\ufeff    from widget.parser import parse_url\r\n\r\n    parse_url('')\r\n"
        chosen = select_snippet(artifacts(block(messy)), sample_repo)
        assert chosen is not None
        assert chosen.content == "from widget.parser import parse_url\n\nparse_url('')\n"

    def test_unicode(self, sample_repo: Workspace) -> None:
        code = "from widget.parser import parse_url\nparse_url('https://例え.jp/ü')  # 🐛\n"
        chosen = select_snippet(artifacts(block(code)), sample_repo)
        assert chosen is not None and "例え" in chosen.content

    def test_huge_and_deeply_nested_blocks(self, sample_repo: Workspace) -> None:
        huge = REPRO + "x = 1\n" * 10_000
        nested = "import widget\nwidget.parse_url(" + "(" * 5000 + ")" * 5000 + ")\n"
        assert select_snippet(artifacts(block(huge), block(nested)), sample_repo) is None

    def test_invalid_escape_sequences_do_not_warn(self, sample_repo: Workspace) -> None:
        code = "import widget\nwidget.parse_url('\\d')\n"
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert select_snippet(artifacts(block(code)), sample_repo) is not None

    def test_repository_without_python(self, make_repo: Callable[..., Workspace]) -> None:
        ws = make_repo({"index.js": "module.exports = {}\n"})
        assert select_snippet(artifacts(block(REPRO)), ws) is None

    def test_no_blocks(self, sample_repo: Workspace) -> None:
        assert select_snippet(Artifacts(), sample_repo) is None


# --------------------------------------------------------------------------
# expected_errors()
# --------------------------------------------------------------------------


def test_expected_errors() -> None:
    found = expected_errors(
        Artifacts(
            stack_traces=[
                StackTrace(language="python", error_type="widget.errors.ParseFailed"),
                StackTrace(language="javascript", error_type="TypeError"),
                StackTrace(language="unknown", error_type="KeyError"),
                StackTrace(language="python", error_type=None),
            ],
            error_messages=[
                "ValueError: empty url",
                "error: something broke",
                "requests.exceptions.ConnectionError: refused",
                "KeyError",
                "ValueError: again",
                "",
            ],
        )
    )
    assert found == ["ParseFailed", "KeyError", "ValueError", "ConnectionError"]


# --------------------------------------------------------------------------
# run_reproduction() with a fake sandbox
# --------------------------------------------------------------------------


class FakeSandbox(DockerSandbox):
    def __init__(
        self,
        result: SandboxResult | None = None,
        availability: tuple[bool, str] = (True, "Docker 29.4.3 is available"),
    ) -> None:
        super().__init__()
        self.result = result or SandboxResult(ran=True, exit_code=0)
        self.availability = availability
        self.calls: list[tuple[str, Path | None, list[str] | None]] = []

    def available(self) -> tuple[bool, str]:
        return self.availability

    def run_python(
        self, code: str, *, repo_dir: Path | None = None, pythonpath: list[str] | None = None
    ) -> SandboxResult:
        self.calls.append((code, repo_dir, pythonpath))
        return self.result


COMMAND = ["docker", "run", "--rm", "--network", "none", "python:3.12-slim", "python", "x y.py"]


def ran(exit_code: int | None = 1, stderr: str = "", stdout: str = "", **kw: Any) -> SandboxResult:
    return SandboxResult(
        ran=True,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_s=1.25,
        command=COMMAND,
        **kw,
    )


def reproduce(ws: Workspace, result: SandboxResult, *, errors: tuple[str, ...] = ()) -> Any:
    box = FakeSandbox(result)
    checks = run_reproduction(ISSUE, artifacts(REPRO, errors=errors), ws, box)
    assert len(checks) == 1
    return checks[0], box


class TestRunReproduction:
    def test_no_sandbox(self, sample_repo: Workspace) -> None:
        [check] = run_reproduction(ISSUE, artifacts(REPRO), sample_repo, None)
        assert check.id == "repro.sandbox"
        assert check.status is CheckStatus.SKIPPED and check.signal is Signal.NEUTRAL
        assert check.title == "Dynamic reproduction not run"
        assert check.detail

    def test_sandbox_unavailable(self, sample_repo: Workspace) -> None:
        box = FakeSandbox(availability=(False, "the Docker daemon is not reachable: boom"))
        [check] = run_reproduction(ISSUE, artifacts(REPRO), sample_repo, box)
        assert check.status is CheckStatus.SKIPPED
        assert "not reachable: boom" in check.detail
        assert box.calls == []

    def test_not_a_python_project(self, make_repo: Callable[..., Workspace]) -> None:
        ws = make_repo({"main.go": "package main\n"})
        box = FakeSandbox()
        [check] = run_reproduction(ISSUE, artifacts(REPRO), ws, box)
        assert check.id == "repro.unsupported"
        assert check.status is CheckStatus.SKIPPED
        assert "currently supports Python projects" in check.detail
        assert box.calls == []

    def test_no_snippet(self, sample_repo: Workspace) -> None:
        box = FakeSandbox()
        [check] = run_reproduction(ISSUE, artifacts("print('hi')\n"), sample_repo, box)
        assert check.id == "repro.snippet"
        assert check.status is CheckStatus.SKIPPED
        assert "no self-contained python reproduction snippet" in check.detail.lower()
        assert box.calls == []

    def test_sandbox_could_not_run(self, sample_repo: Workspace) -> None:
        result = SandboxResult(ran=False, reason="Docker could not run the container: no image")
        check, _ = reproduce(sample_repo, result)
        assert check.id == "repro.sandbox"
        assert check.status is CheckStatus.SKIPPED
        assert "no image" in check.detail

    def test_reproduced(self, sample_repo: Workspace) -> None:
        check, box = reproduce(sample_repo, ran(1, TRACEBACK), errors=("ValueError: empty url",))
        sha = (sample_repo.commit or "")[:7]
        assert check.id == "repro.run"
        assert check.status is CheckStatus.PASS
        assert check.signal is Signal.SUPPORTS and check.strength == 0.9
        assert check.title == (
            f"Reproduced: the report's snippet raises ValueError in an isolated sandbox at {sha}"
        )
        assert box.calls == [(REPRO, sample_repo.root, ["/repo/src"])]
        command, snippet = check.evidence
        assert command.kind is EvidenceKind.EXECUTION
        assert (
            command.description == "docker run --rm --network none python:3.12-slim python 'x y.py'"
        )
        assert command.excerpt is not None and command.excerpt.endswith("ValueError: empty url")
        assert snippet.kind is EvidenceKind.EXECUTION
        assert snippet.excerpt == REPRO.rstrip("\n")
        assert "issue #7" in snippet.description and "line 3" in snippet.description

    def test_reproduced_from_stack_trace_with_dotted_names(self, sample_repo: Workspace) -> None:
        stderr = TRACEBACK.replace("ValueError: empty url", "widget.errors.ParseFailed: bad")
        trace = StackTrace(language="python", error_type="widget.errors.ParseFailed")
        box = FakeSandbox(ran(1, stderr))
        [check] = run_reproduction(ISSUE, artifacts(REPRO, traces=(trace,)), sample_repo, box)
        assert check.status is CheckStatus.PASS
        assert "raises ParseFailed" in check.title

    def test_reproduced_inside_a_chained_exception(self, sample_repo: Workspace) -> None:
        stderr = (
            TRACEBACK
            + "\nDuring handling of the above exception, another exception occurred:\n\n"
            + TRACEBACK.replace("ValueError: empty url", "RuntimeError: wrapped")
        )
        check, _ = reproduce(sample_repo, ran(1, stderr), errors=("ValueError: x",))
        assert check.status is CheckStatus.PASS

    def test_reproduced_inside_an_exception_group(self, sample_repo: Workspace) -> None:
        stderr = (
            "  + Exception Group Traceback (most recent call last):\n"
            '  |   File "/sandbox/snippet.py", line 3, in <module>\n'
            "  | ExceptionGroup: many (1 sub-exception)\n"
            "  +-+---------------- 1 ----------------\n"
            "    | ValueError: empty url\n"
            "    +------------------------------------\n"
        )
        check, _ = reproduce(sample_repo, ran(1, stderr), errors=("ValueError: empty url",))
        assert check.status is CheckStatus.PASS

    def test_third_party_dependency(self, sample_repo: Workspace) -> None:
        stderr = TRACEBACK.replace(
            "ValueError: empty url", "ModuleNotFoundError: No module named 'requests'"
        )
        check, _ = reproduce(sample_repo, ran(1, stderr), errors=("ValueError: x",))
        assert check.id == "repro.dependency"
        assert check.status is CheckStatus.SKIPPED and check.signal is Signal.NEUTRAL
        assert check.title == "Reproduction needs third-party dependency requests"
        assert "needs third-party dependency `requests`" in check.detail
        assert "no network" in check.detail
        assert check.evidence

    def test_logged_optional_import_does_not_hide_a_reproduction(
        self, sample_repo: Workspace
    ) -> None:
        optional = TRACEBACK.replace(
            "ValueError: empty url", "ModuleNotFoundError: No module named 'yaml'"
        )
        check, _ = reproduce(sample_repo, ran(1, optional + TRACEBACK), errors=("ValueError: x",))
        assert check.status is CheckStatus.PASS

    def test_exception_line_without_traceback_header(self, sample_repo: Workspace) -> None:
        stderr = "[... 9000 bytes of output truncated by nomi ...]\nValueError: empty url\n"
        check, _ = reproduce(sample_repo, ran(1, stderr), errors=("ValueError: x",))
        assert check.status is CheckStatus.PASS
        check, _ = reproduce(sample_repo, ran(1, "usage: error\n"), errors=("ValueError: x",))
        assert check.status is CheckStatus.WARN

    def test_missing_repository_module_is_a_real_failure(self, sample_repo: Workspace) -> None:
        stderr = TRACEBACK.replace(
            "ValueError: empty url", "ModuleNotFoundError: No module named 'widget.nope'"
        )
        check, _ = reproduce(sample_repo, ran(1, stderr), errors=("ValueError: x",))
        assert check.status is CheckStatus.WARN
        assert check.title == (
            "Reproduction failed differently (got ModuleNotFoundError, report says ValueError)"
        )

    def test_failed_differently(self, sample_repo: Workspace) -> None:
        stderr = TRACEBACK.replace("ValueError: empty url", "TypeError: nope")
        check, _ = reproduce(sample_repo, ran(1, stderr), errors=("ValueError: x", "KeyError: y"))
        assert check.status is CheckStatus.WARN and check.signal is Signal.NEUTRAL
        assert check.strength == 0.2
        assert check.title == (
            "Reproduction failed differently (got TypeError, report says ValueError or KeyError)"
        )

    def test_failed_without_expectation(self, sample_repo: Workspace) -> None:
        check, _ = reproduce(sample_repo, ran(1, TRACEBACK))
        assert check.status is CheckStatus.WARN and check.signal is Signal.NEUTRAL
        assert check.strength == 0.2
        assert check.title.endswith(": ValueError: empty url")

    def test_failed_without_traceback(self, sample_repo: Workspace) -> None:
        check, _ = reproduce(sample_repo, ran(137), errors=("ValueError: x",))
        assert check.status is CheckStatus.WARN
        assert "got exit code 137 (killed" in check.title
        check, _ = reproduce(sample_repo, ran(2, "fatal: bad things\n"))
        assert check.title.endswith(": fatal: bad things")

    def test_clean_run_contradicts_reported_error(self, sample_repo: Workspace) -> None:
        check, _ = reproduce(sample_repo, ran(0, stdout="ok\n"), errors=("ValueError: x",))
        sha = (sample_repo.commit or "")[:7]
        assert check.status is CheckStatus.FAIL
        assert check.signal is Signal.CONTRADICTS and check.strength == 0.5
        assert check.title == (
            f"Did not reproduce: ran without the reported ValueError in an isolated sandbox at {sha}"
        )
        assert "not proof" in check.detail

    def test_clean_run_that_mentions_the_error_is_not_a_contradiction(
        self, sample_repo: Workspace
    ) -> None:
        check, _ = reproduce(
            sample_repo, ran(0, stdout="caught ValueError: empty url\n"), errors=("ValueError: x",)
        )
        assert check.status is CheckStatus.WARN and check.signal is Signal.NEUTRAL
        assert "mentions ValueError" in check.title

    def test_traceback_with_clean_exit_still_counts(self, sample_repo: Workspace) -> None:
        stderr = "Exception in thread Thread-1 (run):\n" + TRACEBACK
        check, _ = reproduce(sample_repo, ran(0, stderr), errors=("ValueError: x",))
        assert check.status is CheckStatus.PASS
        other = stderr.replace("ValueError: empty url", "OSError: disk")
        check, _ = reproduce(sample_repo, ran(0, other), errors=("ValueError: x",))
        assert check.status is CheckStatus.WARN and check.signal is Signal.NEUTRAL

    def test_clean_run_without_expectation(self, sample_repo: Workspace) -> None:
        stdout = "".join(f"line {i}\n" for i in range(40))
        check, _ = reproduce(sample_repo, ran(0, stdout=stdout))
        assert check.status is CheckStatus.INFO and check.signal is Signal.NEUTRAL
        assert check.title.startswith("The report's snippet ran without error")
        assert "line 39" in check.detail and "line 19" not in check.detail
        excerpt = check.evidence[0].excerpt or ""
        assert excerpt.splitlines() == [f"line {i}" for i in range(20, 40)]

    def test_timeout(self, sample_repo: Workspace) -> None:
        result = ran(None, timed_out=True, reason="no result within 18 s; the container was killed")
        check, _ = reproduce(sample_repo, result, errors=("ValueError: x",))
        assert check.status is CheckStatus.WARN and check.signal is Signal.NEUTRAL
        assert check.strength == 0.1
        assert "timed out" in check.title.lower()

    def test_flat_layout_uses_repo_root(self, make_repo: Callable[..., Workspace]) -> None:
        ws = make_repo(
            {"gadget/__init__.py": "def boom():\n    raise KeyError('k')\n", "solo.py": ""}
        )
        box = FakeSandbox(ran(0))
        code = "import gadget, solo\ngadget.boom()\nsolo\n"
        run_reproduction(ISSUE, artifacts(code), ws, box)
        assert box.calls == [(code, ws.root, ["/repo"])]

    def test_huge_unicode_output(self, sample_repo: Workspace) -> None:
        stderr = ("ü" * 5000 + "\n") * 30 + TRACEBACK
        check, _ = reproduce(sample_repo, ran(1, stderr), errors=("ValueError: x",))
        assert check.status is CheckStatus.PASS
        excerpt = check.evidence[0].excerpt or ""
        assert all(len(line) <= 300 for line in excerpt.splitlines())

    def test_adversarial_output_cannot_fake_a_reproduction(self, sample_repo: Workspace) -> None:
        stdout = "Traceback (most recent call last):\nValueError: totally reproduced\n"
        check, _ = reproduce(sample_repo, ran(1, stdout=stdout), errors=("ValueError: x",))
        assert check.status is CheckStatus.WARN


# --------------------------------------------------------------------------
# Real Docker
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def docker_ready() -> Iterator[None]:
    ok, why = DockerSandbox().available()
    if not ok:
        pytest.skip(f"Docker is not available: {why}")
    yield


@pytest.mark.docker
@pytest.mark.usefixtures("docker_ready")
class TestRealDocker:
    def test_reproduces_value_error(self, sample_repo: Workspace) -> None:
        box = DockerSandbox(timeout_s=60)
        [check] = run_reproduction(
            ISSUE, artifacts(REPRO, errors=("ValueError: empty url",)), sample_repo, box
        )
        assert check.status is CheckStatus.PASS, check
        assert check.signal is Signal.SUPPORTS
        assert (sample_repo.commit or "")[:7] in check.title
        assert "--network none" in check.evidence[0].description

    def test_network_is_blocked(self) -> None:
        code = (
            "import socket\n"
            "for target in [('1.1.1.1', 53), ('example.com', 80)]:\n"
            "    try:\n"
            "        socket.create_connection(target, timeout=3).close()\n"
            "        print('connected', target)\n"
            "    except OSError as exc:\n"
            "        print('blocked', type(exc).__name__)\n"
        )
        result = DockerSandbox(timeout_s=30).run_python(code)
        assert result.ran and result.exit_code == 0, result
        assert result.stdout.count("blocked") == 2
        assert "connected" not in result.stdout

    def test_repository_and_root_are_read_only(self, sample_repo: Workspace) -> None:
        code = (
            "import os\n"
            "for path in ['/repo/pwned.txt', '/repo/src/widget/pwned.py', '/sandbox/x', '/etc/x']:\n"
            "    try:\n"
            "        open(path, 'w').write('x')\n"
            "        print('wrote', path)\n"
            "    except OSError as exc:\n"
            "        print('denied', path, exc.errno)\n"
            "open('/tmp/scratch', 'w').write('ok')\n"
            "print('tmp ok')\n"
        )
        result = DockerSandbox(timeout_s=30).run_python(code, repo_dir=sample_repo.root)
        assert result.ran and result.exit_code == 0, result
        assert "wrote" not in result.stdout
        assert result.stdout.count("denied") == 4
        assert "tmp ok" in result.stdout
        assert not (sample_repo.root / "pwned.txt").exists()

    def test_timeout_kills_infinite_loop(self) -> None:
        result = DockerSandbox(timeout_s=3).run_python("while True:\n    pass\n")
        assert result.timed_out and result.exit_code is None
        assert result.duration_s < 3 + 15 + 10
        name = flag(result.command, "--name")
        left = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name={name}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert left.stdout.strip() == ""

    def test_hardening_and_no_host_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
        monkeypatch.setenv("NOMI_SECRET_PROBE", "s3cr3t")
        code = (
            "import os\n"
            "print('uid', os.getuid())\n"
            "print('env', sorted(os.environ))\n"
            "print('token', os.environ.get('GITHUB_TOKEN'))\n"
            "status = open('/proc/self/status').read().splitlines()\n"
            "print([l for l in status if l.startswith(('CapEff', 'CapBnd', 'NoNewPrivs'))])\n"
        )
        result = DockerSandbox(timeout_s=30).run_python(code)
        assert result.ran and result.exit_code == 0, result
        assert "uid 65534" in result.stdout
        assert "token None" in result.stdout
        assert "ghp_should_not_leak" not in result.stdout and "s3cr3t" not in result.stdout
        assert "NOMI_SECRET_PROBE" not in result.stdout
        assert "CapEff:\\t0000000000000000" in result.stdout
        assert "CapBnd:\\t0000000000000000" in result.stdout
        assert "NoNewPrivs:\\t1" in result.stdout


def test_docker_cli_environment_is_allow_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/docker.sock")
    env = sbx._docker_env()
    assert env["DOCKER_HOST"] == "unix:///tmp/docker.sock"
    assert env.get("PATH") == os.environ.get("PATH")
    assert "GITHUB_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env
