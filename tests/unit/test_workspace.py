from datetime import UTC, datetime

import pytest

from nomi.repo.workspace import (
    Workspace,
    classify_path,
    normalize_path,
    open_github_workspace,
    resolve_commit,
)
from tests.conftest import git


def test_files_and_exists(sample_repo):
    assert "src/widget/parser.py" in sample_repo.files
    assert sample_repo.exists("src/widget/parser.py")
    assert not sample_repo.exists("src/widget/missing.py")


@pytest.mark.parametrize(
    ("mention", "expected"),
    [
        ("src/widget/parser.py", "src/widget/parser.py"),
        ("./src/widget/parser.py", "src/widget/parser.py"),
        ("/home/u/.venv/lib/python3.12/site-packages/widget/parser.py", "src/widget/parser.py"),
        ("widget/net.py:12", "src/widget/net.py"),
        ("b/src/widget/cli.py", "src/widget/cli.py"),
        ("parser.py", "src/widget/parser.py"),  # unique bare file name
        ("lib/other/parser.py", None),  # wrong directory must not resolve
        ("src/widget/fabricated.py", None),
        ("", None),
    ],
)
def test_resolve(sample_repo, mention, expected):
    assert sample_repo.resolve(mention) == expected


def test_resolve_ambiguous_bare_name(make_repo):
    ws = make_repo({"a/util.py": "x = 1\n", "b/util.py": "y = 2\n"})
    assert ws.resolve("util.py") is None
    assert sorted(ws.candidates("util.py")) == ["a/util.py", "b/util.py"]
    assert ws.resolve("b/util.py") == "b/util.py"


def test_read_and_excerpt(sample_repo):
    lines = sample_repo.lines("src/widget/parser.py")
    assert lines is not None and lines[5].startswith("def parse_url")
    ex = sample_repo.excerpt("src/widget/parser.py", 8, 9)
    assert ex is not None and "8 |     if not url:" in ex and "raise ValueError" in ex
    assert sample_repo.excerpt("src/widget/parser.py", 999) is None
    assert sample_repo.read_text("nope.py") is None


def test_binary_files_are_not_text(make_repo):
    ws = make_repo({"blob.bin": "a\0b", "ok.txt": "fine\n"})
    assert ws.read_text("blob.bin") is None
    assert ws.read_text("ok.txt") == "fine\n"


def test_grep_fixed_word_regex(sample_repo):
    hits = sample_repo.grep("parse_url", word=True)
    assert {h.path for h in hits} >= {"src/widget/parser.py", "src/widget/net.py"}
    assert all(h.line > 0 for h in hits)
    assert sample_repo.grep(r"def\s+parse_url\b", fixed=False)[0].path == "src/widget/parser.py"
    assert sample_repo.grep("definitely_not_here_123") == []
    assert len(sample_repo.grep("import", max_hits=2)) == 2


def test_grep_without_git(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def hello():\n    return 1\n")
    ws = Workspace(tmp_path)
    assert ws.commit is None
    assert ws.grep("hello")[0].path == "pkg/m.py"
    assert ws.permalink("pkg/m.py", 1) is None  # no repo/commit -> no permalink


def test_permalink_pinned_to_commit(sample_repo):
    url = sample_repo.permalink("src/widget/parser.py", 6, 9)
    assert (
        url
        == f"https://github.com/acme/widget/blob/{sample_repo.commit}/src/widget/parser.py#L6-L9"
    )


def test_tags_and_packages(sample_repo):
    assert set(sample_repo.tags) == {"v1.0.0", "v1.1.0"}
    assert sample_repo.python_packages == ["widget"]


def test_classify_and_normalize():
    assert classify_path("tests/test_x.py") == "test"
    assert classify_path("src/foo.test.ts") == "test"
    assert classify_path("docs/usage.md") == "doc"
    assert classify_path("README.md") == "doc"
    assert classify_path("src/widget/net.py") == "code"
    assert normalize_path("`./src/a.py:12`") == "src/a.py"
    assert normalize_path("src/a.py#L3-L9") == "src/a.py"


def test_time_travel_checkout(make_repo, tmp_path):
    ws = make_repo({"a.py": "old = 1\n"}, name="upstream")
    root = ws.root
    first = git(root, "rev-parse", "HEAD").strip()
    (root / "a.py").write_text("new = 2\n")
    git(root, "add", "-A")
    env_date = "2030-01-01T00:00:00+00:00"
    import os
    import subprocess

    from tests.conftest import GIT_ENV

    subprocess.run(
        ["git", "commit", "-q", "-m", "later"],
        cwd=root,
        check=True,
        env={**GIT_ENV, "GIT_COMMITTER_DATE": env_date, "GIT_AUTHOR_DATE": env_date},
    )
    assert resolve_commit(root, at=datetime(2029, 1, 1, tzinfo=UTC)) == first

    cache = tmp_path / "cache"
    clone = open_github_workspace(
        "acme/upstream",
        cache_dir=cache,
        at=datetime(2029, 1, 1, tzinfo=UTC),
        remote_url=str(root),
    )
    assert clone.commit == first
    assert clone.read_text("a.py") == "old = 1\n"
    head = open_github_workspace("acme/upstream", cache_dir=cache, remote_url=str(root))
    assert head.read_text("a.py") == "new = 2\n"
    assert os.path.isdir(cache / "repos" / "acme__upstream")


def test_rejects_bad_repo_names(tmp_path):
    with pytest.raises(ValueError):
        open_github_workspace("not a repo", cache_dir=tmp_path)
    with pytest.raises(ValueError):
        open_github_workspace("../../etc", cache_dir=tmp_path)
