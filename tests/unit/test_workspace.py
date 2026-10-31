import os
import signal
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from nomi.repo.workspace import (
    MAX_FILE_BYTES,
    Workspace,
    classify_path,
    normalize_path,
    open_github_workspace,
    resolve_commit,
    split_lines,
)
from tests.conftest import GIT_ENV, git


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


def test_each_commit_gets_its_own_worktree(make_repo, tmp_path):
    ws = make_repo({"a.py": "v = 1\n"}, name="upstream2")
    first = git(ws.root, "rev-parse", "HEAD").strip()
    (ws.root / "a.py").write_text("v = 2\n")
    git(ws.root, "commit", "-qam", "second")
    cache = tmp_path / "cache2"
    old = open_github_workspace("acme/up2", cache_dir=cache, ref=first, remote_url=str(ws.root))
    new = open_github_workspace("acme/up2", cache_dir=cache, remote_url=str(ws.root))
    assert old.root != new.root
    # opening the newer commit must not change what the older workspace reads
    assert old.read_text("a.py") == "v = 1\n"
    assert new.read_text("a.py") == "v = 2\n"


def test_worktrees_are_pruned(make_repo, tmp_path):
    ws = make_repo({"a.py": "v = 0\n"}, name="upstream3")
    shas = []
    for i in range(4):
        (ws.root / "a.py").write_text(f"v = {i + 1}\n")
        git(ws.root, "commit", "-qam", f"c{i}")
        shas.append(git(ws.root, "rev-parse", "HEAD").strip())
    cache = tmp_path / "cache3"
    for sha in shas:
        open_github_workspace(
            "acme/up3", cache_dir=cache, ref=sha, remote_url=str(ws.root), keep_worktrees=2
        )
    assert len(list((cache / "repos" / "worktrees").glob("acme__up3@*"))) == 2


@pytest.mark.parametrize("ref", ["--upload-pack=evil", "-x", "a b", "HEAD;rm"])
def test_rejects_option_like_refs(make_repo, tmp_path, ref):
    ws = make_repo({"a.py": "x = 1\n"}, name="upstream4")
    with pytest.raises(ValueError):
        open_github_workspace(
            "acme/up4", cache_dir=tmp_path / "c4", ref=ref, remote_url=str(ws.root)
        )


# --------------------------------------------------- review hardening (A1-A3)


class _Blocked(Exception):  # deliberately not an OSError, so readers cannot swallow it
    pass


@contextmanager
def _deadline(seconds: int):
    """Fail instead of hanging forever when a read blocks (e.g. on a FIFO)."""

    def _expired(signum, frame):
        raise _Blocked(f"read did not return within {seconds}s")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.alarm(seconds)
    started = time.perf_counter()
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert time.perf_counter() - started < 1


def _commit_symlink(ws: Workspace, rel: str, target: str) -> None:
    link = ws.root / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)
    git(ws.root, "add", "-A")
    git(ws.root, "commit", "-q", "-m", "add link")


def test_tracked_symlink_outside_root_is_not_read(make_repo, tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("TOPSECRET_PRIVATE_KEY\n")
    ws = make_repo({"pkg/real.py": "x = 1\n"})
    _commit_symlink(ws, "pkg/keys.py", "../../id_rsa")
    fresh = Workspace(ws.root, repo="acme/widget")
    assert "pkg/keys.py" in fresh.files  # git tracks the link itself
    assert fresh.read_text("pkg/keys.py") is None
    assert fresh.lines("pkg/keys.py") is None
    assert fresh.excerpt("pkg/keys.py", 1) is None
    assert fresh.read_text("pkg/real.py") == "x = 1\n"
    assert fresh.grep("TOPSECRET_PRIVATE_KEY") == []
    # the Python fallback (no git grep) must use the same safe reader
    assert fresh._py_grep("TOPSECRET_PRIVATE_KEY", True, False, False, 10, None) == []


def test_tracked_symlink_inside_root_is_not_followed(make_repo):
    ws = make_repo({"pkg/real.py": "x = 1\n"})
    _commit_symlink(ws, "pkg/alias.py", "real.py")
    fresh = Workspace(ws.root)
    assert fresh.read_text("pkg/alias.py") is None


def test_tracked_symlink_to_dev_zero_returns_quickly(make_repo):
    ws = make_repo({"a.py": "x = 1\n"})
    _commit_symlink(ws, "zero.py", "/dev/zero")
    fresh = Workspace(ws.root)
    with _deadline(5):
        assert fresh.read_text("zero.py") is None


def test_fifo_in_checkout_returns_quickly(make_repo, tmp_path):
    ws = make_repo({"pipe.py": "x = 1\n", "ok.py": "y = 2\n"})
    (ws.root / "pipe.py").unlink()
    os.mkfifo(ws.root / "pipe.py")  # a tampered checkout: tracked path is now a FIFO
    fresh = Workspace(ws.root)
    with _deadline(5):
        assert fresh.read_text("pipe.py") is None
        assert fresh._py_grep("x = 1", True, False, False, 10, None) == []
    plain = tmp_path / "plain"
    plain.mkdir()
    os.mkfifo(plain / "fifo.py")
    (plain / "ok.py").write_text("y = 2\n")
    outside_git = Workspace(plain)
    assert "fifo.py" in outside_git.files
    with _deadline(5):
        assert outside_git.read_text("fifo.py") is None
        assert outside_git.read_text("ok.py") == "y = 2\n"


def test_oversized_file_is_not_read(make_repo):
    ws = make_repo({"big.txt": "a" * (MAX_FILE_BYTES + 1), "edge.txt": "b" * MAX_FILE_BYTES})
    assert ws.read_text("big.txt") is None
    assert ws.read_text("edge.txt") == "b" * MAX_FILE_BYTES


def test_lines_count_only_newlines(make_repo):
    text = "def alpha():\n    return 1\n\f\ndef beta():\n    return 2\n"
    ws = make_repo({"m.py": text, "odd.txt": "a\vb\x1cc\x85d\u2028e\u2029f\nsecond\r\nthird"})
    lines = ws.lines("m.py")
    assert lines is not None and lines[3] == "def beta():" and len(lines) == 5
    assert ws.excerpt("m.py", 4) == "4 | def beta():"
    assert ws.lines("odd.txt") == ["a\vb\x1cc\x85d\u2028e\u2029f", "second", "third"]
    assert [h.line for h in ws.grep("beta")] == [4]
    assert [h.line for h in ws._py_grep("beta", True, False, False, 10, None)] == [4]
    assert [h.line for h in ws.grep("second")] == [2]
    assert [h.line for h in ws._py_grep("second", True, False, False, 10, None)] == [2]


def test_split_lines_matches_git_numbering():
    assert split_lines("") == []
    assert split_lines("\n") == [""]
    assert split_lines("a\nb") == ["a", "b"]
    assert split_lines("a\r\nb\r\n") == ["a", "b"]
    assert split_lines("a\f\nb\n") == ["a\f", "b"]


def test_resolve_commit_follows_first_parent(make_repo):
    def commit_at(root, day: int, message: str) -> None:
        stamp = f"2030-01-{day:02d}T12:00:00+00:00"
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", message],
            cwd=root,
            check=True,
            env={**GIT_ENV, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp},
        )

    ws = make_repo({"a.py": "x = 1\n"}, name="history")
    root = ws.root
    commit_at(root, 1, "A")
    git(root, "checkout", "-q", "-b", "feature")
    commit_at(root, 5, "F")
    feature = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-q", "main")
    commit_at(root, 3, "B")
    main_b = git(root, "rev-parse", "HEAD").strip()
    merge_stamp = "2030-01-10T12:00:00+00:00"
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "-m", "merge feature", "feature"],
        cwd=root,
        check=True,
        env={**GIT_ENV, "GIT_COMMITTER_DATE": merge_stamp, "GIT_AUTHOR_DATE": merge_stamp},
    )
    resolved = resolve_commit(root, at=datetime(2030, 1, 6, tzinfo=UTC))
    assert resolved == main_b
    assert resolved != feature
