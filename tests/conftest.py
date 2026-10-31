"""Shared fixtures: tiny throwaway git repositories to verify claims against."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from nomi.repo.workspace import Workspace

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Nomi Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Nomi Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=GIT_ENV
    )
    return proc.stdout


# A small but realistic Python project ("widget") reused across the suite.
SAMPLE_FILES: dict[str, str] = {
    "README.md": (
        "# widget\n\n"
        "Fetch and parse URLs.\n\n"
        "Usage: `widget fetch URL --timeout 10 --retries 3`\n\n"
        "The default timeout is 10 seconds.\n"
    ),
    "pyproject.toml": '[project]\nname = "widget"\nversion = "1.1.0"\n',
    "src/widget/__init__.py": 'from widget.parser import parse_url\n\n__version__ = "1.1.0"\n',
    "src/widget/parser.py": (
        '"""URL parsing helpers."""\n'
        "\n"
        "from urllib.parse import urlsplit\n"
        "\n"
        "\n"
        "def parse_url(url: str) -> dict:\n"
        '    """Split a URL into its parts. Raises ValueError on empty input."""\n'
        "    if not url:\n"
        '        raise ValueError("empty url")\n'
        "    parts = urlsplit(url)\n"
        "    if not parts.scheme:\n"
        '        raise ValueError(f"missing scheme in {url!r}")\n'
        '    return {"scheme": parts.scheme, "host": parts.hostname, "path": parts.path}\n'
        "\n"
        "\n"
        "def normalize_host(host: str) -> str:\n"
        "    return host.strip().lower()\n"
    ),
    "src/widget/net.py": (
        '"""Tiny HTTP client."""\n'
        "\n"
        "from widget.parser import parse_url\n"
        "\n"
        "DEFAULT_TIMEOUT = 10\n"
        "\n"
        "\n"
        "class Client:\n"
        "    def __init__(self, retries: int = 3):\n"
        "        self.retries = retries\n"
        "\n"
        "    def fetch(self, url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:\n"
        "        parts = parse_url(url)\n"
        '        if parts["scheme"] not in ("http", "https"):\n'
        '            raise ValueError("unsupported scheme")\n'
        '        return b""\n'
    ),
    "src/widget/cli.py": (
        "import argparse\n"
        "\n"
        "\n"
        "def main(argv=None):\n"
        '    parser = argparse.ArgumentParser(prog="widget")\n'
        '    parser.add_argument("url")\n'
        '    parser.add_argument("--timeout", type=int, default=10)\n'
        '    parser.add_argument("--retries", type=int, default=3)\n'
        "    return parser.parse_args(argv)\n"
    ),
    "docs/usage.md": (
        "# Usage\n\n"
        "`Client.fetch(url, timeout=10)` downloads a URL.\n\n"
        "Pass `--retries` to change how often a request is retried (default 3).\n"
    ),
    "tests/test_parser.py": (
        "import pytest\n"
        "\n"
        "from widget.parser import parse_url\n"
        "\n"
        "\n"
        "def test_empty():\n"
        "    with pytest.raises(ValueError):\n"
        '        parse_url("")\n'
    ),
}


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., Workspace]:
    """Create a committed git repo from {path: content}; returns a Workspace."""

    def _make(
        files: dict[str, str],
        *,
        repo: str = "acme/widget",
        tags: tuple[str, ...] = (),
        name: str = "repo",
    ) -> Workspace:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "initial")
        for tag in tags:
            git(root, "tag", tag)
        return Workspace(root, repo=repo)

    return _make


@pytest.fixture
def sample_repo(make_repo: Callable[..., Workspace]) -> Workspace:
    return make_repo(SAMPLE_FILES, tags=("v1.0.0", "v1.1.0"))
