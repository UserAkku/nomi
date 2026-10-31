"""A read-only view of a repository pinned at one commit.

Every piece of evidence Nomi reports is a (path, line range) inside a
Workspace, so permalinks can be pinned to the exact commit that was analysed.
For historical issues the workspace is checked out at the last commit
*before the issue was filed*, so a bug fixed later does not look fabricated.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import stat
import subprocess
import time
from collections import defaultdict
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property, lru_cache
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_000_000
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    "dist",
    "build",
    ".mypy_cache",
}
DOC_EXTENSIONS = {".md", ".rst", ".txt", ".adoc", ".markdown", ".mdx"}
_TEST_PATH = re.compile(
    r"(^|/)(tests?|testing|spec|__tests__)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|\.test\.[a-z]+$|\.spec\.[a-z]+$"
)
_DOC_PATH = re.compile(
    r"(^|/)(docs?|documentation|man)(/|$)|(^|/)(README|CHANGELOG|CHANGES|NEWS|HISTORY|CONTRIBUTING)[^/]*$",
    re.I,
)


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GrepHit:
    path: str
    line: int
    text: str


def _git(args: list[str], cwd: Path, timeout: int = 300) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def split_lines(text: str) -> list[str]:
    r"""Lines numbered the way git, tracebacks and GitHub `#L` anchors count them.

    Only "\n" ends a line: str.splitlines() also breaks on \f, \v, \x1c-\x1e,
    \x85 and U+2028/2029, which would shift every later line number and make
    a correct line reference look fabricated. A trailing "\r" (CRLF) is dropped.
    """
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.rstrip("\r") for line in lines]


def classify_path(path: str) -> str:
    """Return 'test', 'doc' or 'code' for a repository path."""
    if _TEST_PATH.search(path):
        return "test"
    if _DOC_PATH.search(path) or Path(path).suffix.lower() in DOC_EXTENSIONS:
        return "doc"
    return "code"


def normalize_path(raw: str) -> str:
    """Strip quoting, diff prefixes, ./ and leading slashes from a path mention."""
    p = raw.strip().strip("`'\"()[]<>,;").replace("\\", "/")
    p = re.sub(r":\d+(:\d+)?$", "", p)  # trailing :line[:col]
    p = re.sub(r"#L\d+(-L\d+)?$", "", p)
    if p.startswith(("a/", "b/")) and "/" in p[2:]:
        p = p[2:]
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


class Workspace:
    """Files of one repository at one commit. Never writes to the tree."""

    def __init__(self, root: Path | str, *, commit: str | None = None, repo: str | None = None):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"workspace root does not exist: {self.root}")
        self.repo = repo
        self.is_git = (self.root / ".git").exists()
        self.commit = commit or self._head_commit()

    # ------------------------------------------------------------------ files

    def _head_commit(self) -> str | None:
        if not self.is_git:
            return None
        try:
            return _git(["rev-parse", "HEAD"], self.root).strip() or None
        except GitError:
            return None

    @cached_property
    def files(self) -> list[str]:
        """All tracked (or, outside git, all non-ignored) files as POSIX paths."""
        if self.is_git:
            try:
                out = _git(["ls-files", "-z"], self.root)
                return sorted(p for p in out.split("\0") if p)
            except GitError:
                pass
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                rel = Path(dirpath, name).relative_to(self.root).as_posix()
                found.append(rel)
        return sorted(found)

    @cached_property
    def _file_set(self) -> frozenset[str]:
        return frozenset(self.files)

    @cached_property
    def _by_basename(self) -> dict[str, list[str]]:
        index: dict[str, list[str]] = defaultdict(list)
        for f in self.files:
            index[f.rsplit("/", 1)[-1]].append(f)
        return index

    def exists(self, path: str) -> bool:
        return normalize_path(path) in self._file_set

    def resolve(self, path: str) -> str | None:
        """Map a path as written in a report to a real repository file.

        Accepts exact paths, paths with extra prefixes (absolute paths from
        stack traces, site-packages, other checkouts) and bare file names
        that are unique in the repository. Returns None when the path does
        not exist or is ambiguous, so a made-up path is never 'resolved'.
        """
        p = normalize_path(path)
        if not p:
            return None
        if p in self._file_set:
            return p
        parts = [x for x in p.split("/") if x and x != "."]
        if not parts:
            return None
        candidates = self._by_basename.get(parts[-1], [])
        if not candidates:
            return None
        best: str | None = None
        best_len, tie = 0, False
        for cand in candidates:
            cparts = cand.split("/")
            n = 0
            while n < min(len(parts), len(cparts)) and parts[-1 - n] == cparts[-1 - n]:
                n += 1
            if n > best_len:
                best, best_len, tie = cand, n, False
            elif n == best_len:
                tie = True
        if best is None or tie:
            return None
        if len(parts) == 1:  # bare file name: only if unique
            return best if len(candidates) == 1 else None
        return best if best_len >= 2 else None

    def candidates(self, path: str) -> list[str]:
        """All files sharing the mention's file name (for ambiguity/suggestions)."""
        p = normalize_path(path)
        return list(self._by_basename.get(p.rsplit("/", 1)[-1], [])) if p else []

    def read_text(self, path: str) -> str | None:
        """File content as text, or None if missing, too large or binary."""
        rel = normalize_path(path)
        if rel not in self._file_set:
            return None
        return _read_text_cached(self.root, rel)

    def lines(self, path: str) -> list[str] | None:
        text = self.read_text(path)
        return None if text is None else split_lines(text)

    def excerpt(
        self, path: str, start: int, end: int | None = None, *, context: int = 0
    ) -> str | None:
        """Lines [start-context, end+context] (1-based, inclusive), prefixed with line numbers."""
        lines = self.lines(path)
        if not lines:
            return None
        end = end or start
        lo = max(1, start - context)
        hi = min(len(lines), end + context)
        if lo > hi:
            return None
        width = len(str(hi))
        return "\n".join(f"{i:>{width}} | {lines[i - 1]}" for i in range(lo, hi + 1))

    # ----------------------------------------------------------------- search

    def grep(
        self,
        pattern: str,
        *,
        fixed: bool = True,
        word: bool = False,
        ignore_case: bool = False,
        max_hits: int = 200,
        paths: list[str] | None = None,
    ) -> list[GrepHit]:
        """Search tracked text files.

        `fixed=False` takes a Python/PCRE-style regex (\\s, \\b, \\w work):
        git grep -P is used when git has PCRE support, else a Python scan.
        """
        if not pattern:
            return []
        if self.is_git:
            try:
                return self._git_grep(pattern, fixed, word, ignore_case, max_hits, paths)
            except (GitError, OSError):
                pass
        return self._py_grep(pattern, fixed, word, ignore_case, max_hits, paths)

    def _git_grep(self, pattern, fixed, word, ignore_case, max_hits, paths) -> list[GrepHit]:
        args = ["git", "grep", "-n", "-I", "--no-color", "--full-name"]
        args.append("-F" if fixed else "-P")
        if word:
            args.append("-w")
        if ignore_case:
            args.append("-i")
        args += ["-e", pattern, "--"]
        if paths:
            args += paths
        hits: list[GrepHit] = []
        with subprocess.Popen(
            args,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        ) as proc:
            assert proc.stdout is not None
            for raw in proc.stdout:
                m = re.match(r"^(.*?):(\d+):(.*)$", raw.rstrip("\n"))
                if m:
                    hits.append(GrepHit(m.group(1), int(m.group(2)), m.group(3)[:500]))
                if len(hits) >= max_hits:
                    proc.kill()
                    break
            proc.wait()
            if proc.returncode not in (0, 1, -9) and not hits:
                err = proc.stderr.read() if proc.stderr else ""
                raise GitError(f"git grep failed: {err[:300]}")
        return hits

    def _py_grep(self, pattern, fixed, word, ignore_case, max_hits, paths) -> list[GrepHit]:
        flags = re.IGNORECASE if ignore_case else 0
        body = re.escape(pattern) if fixed else pattern
        if word:
            body = rf"(?<![\w]){body}(?![\w])"
        try:
            rx = re.compile(body, flags)
        except re.error:
            return []
        targets = [normalize_path(p) for p in paths] if paths else self.files
        hits: list[GrepHit] = []
        for rel in targets:
            lines = self.lines(rel)
            if not lines:
                continue
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    hits.append(GrepHit(rel, i, line[:500]))
                    if len(hits) >= max_hits:
                        return hits
        return hits

    # ------------------------------------------------------------- metadata

    def permalink(
        self, path: str, line_start: int | None = None, line_end: int | None = None
    ) -> str | None:
        """GitHub URL pinned to the analysed commit (stable even if main moves)."""
        if not self.repo or not self.commit:
            return None
        url = f"https://github.com/{self.repo}/blob/{self.commit}/{quote(normalize_path(path))}"
        if line_start:
            url += f"#L{line_start}"
            if line_end and line_end != line_start:
                url += f"-L{line_end}"
        return url

    @cached_property
    def tags(self) -> list[str]:
        """Tags reachable from the analysed commit (i.e. released before it)."""
        if not self.is_git:
            return []
        try:
            out = _git(["tag", "--merged", "HEAD", "--sort=-creatordate"], self.root)
        except GitError:
            return []
        return [t for t in out.splitlines() if t]

    @cached_property
    def python_packages(self) -> list[str]:
        """Top-level importable Python packages/modules defined by the repo."""
        names: set[str] = set()
        for f in self.files:
            parts = f.split("/")
            if parts[0] in ("tests", "test", "docs", "examples"):
                continue
            if parts[-1] == "__init__.py":
                if len(parts) == 2:
                    names.add(parts[0])
                elif len(parts) == 3 and parts[0] in ("src", "lib", "python"):
                    names.add(parts[1])
            elif (
                len(parts) == 1
                and f.endswith(".py")
                and f not in ("setup.py", "conftest.py", "noxfile.py")
            ):
                names.add(f[:-3])
        return sorted(names)

    def language_mix(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for f in self.files:
            ext = Path(f).suffix.lower()
            if ext:
                counts[ext] += 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _read_regular_file(root: Path, rel: str, limit: int = MAX_FILE_BYTES) -> bytes | None:
    """Bytes of the regular file `root/rel`, or None.

    A tracked symlink can point anywhere (~/.ssh/id_rsa, /dev/zero, a FIFO), so
    symlinks are never followed, the real path must stay inside `root`, only
    regular files are opened, and at most `limit` bytes are ever read.
    """
    path = os.path.join(root, rel)
    try:
        if os.path.commonpath([root, os.path.realpath(path)]) != str(root):
            return None
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        # O_NOFOLLOW/O_NONBLOCK: a file swapped for a link or FIFO since lstat cannot block us.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with open(os.open(path, flags), "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                return None
            data = fh.read(limit + 1)
    except (OSError, ValueError):
        return None
    return None if len(data) > limit else data


@lru_cache(maxsize=2048)
def _read_text_cached(root: Path, rel: str) -> str | None:
    data = _read_regular_file(root, rel)
    if data is None or b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- cloning


@contextmanager
def _locked(dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    lock_path = dest.parent / f".{dest.name}.lock"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def resolve_commit(dest: Path, *, at: datetime | None = None, ref: str | None = None) -> str:
    """Commit for `ref`, or the last default-branch commit before `at`, or HEAD of origin."""
    if ref:
        if ref.startswith("-") or not re.fullmatch(r"[\w./-]+", ref):
            raise ValueError(f"not a git ref: {ref!r}")
        return _git(
            ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], dest
        ).strip()
    default = "origin/HEAD"
    try:
        _git(["rev-parse", "--verify", default], dest)
    except GitError:
        default = "HEAD"
    if at is not None:
        stamp = (at if at.tzinfo else at.replace(tzinfo=UTC)).isoformat()
        # --first-parent: never a feature-branch commit that was merged only later
        out = _git(["rev-list", "-1", "--first-parent", f"--before={stamp}", default], dest).strip()
        if out:
            return out
    return _git(["rev-parse", default], dest).strip()


def _stamp(dest: Path) -> Path:
    return dest / ".git" / "nomi-fetched-at"


def _refresh(dest: Path, *, max_age_s: int) -> None:
    """Fetch from origin unless we did so recently; keep going offline."""
    with suppress(OSError):
        if time.time() - _stamp(dest).stat().st_mtime < max_age_s:
            return
    try:
        _git(["fetch", "--quiet", "--tags", "--force", "origin"], dest, timeout=900)
    except GitError as exc:
        log.warning("fetch failed for %s, using the cached clone: %s", dest.name, exc)
        return
    with suppress(GitError):
        _git(["remote", "set-head", "origin", "--auto"], dest, timeout=120)
    _stamp(dest).touch()


def open_github_workspace(
    repo: str,
    *,
    cache_dir: Path,
    at: datetime | None = None,
    ref: str | None = None,
    pr_number: int | None = None,
    remote_url: str | None = None,
    refresh_after_s: int = 900,
    keep_worktrees: int = 12,
) -> Workspace:
    """Clone (blobless) or refresh `owner/name`, check out the right commit.

    `at` time-travels to the repository state when an issue was filed;
    `pr_number` fetches the pull request head so `ref` can point at it.
    Fetches are throttled to one per `refresh_after_s` per repository, which
    keeps evaluation runs over many issues of one repository fast.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError(f"not a GitHub repository name: {repo!r}")
    dest = Path(cache_dir) / "repos" / repo.replace("/", "__")
    url = remote_url or f"https://github.com/{repo}.git"
    with _locked(dest):
        if not (dest / ".git").exists():
            _git(
                ["clone", "--filter=blob:none", "--no-checkout", "--quiet", url, str(dest)],
                dest.parent,
                timeout=1200,
            )
            _stamp(dest).touch()
        else:
            _refresh(dest, max_age_s=refresh_after_s)
        if pr_number is not None:
            _git(
                [
                    "fetch",
                    "--quiet",
                    "origin",
                    f"+pull/{pr_number}/head:refs/remotes/origin/pr/{pr_number}",
                ],
                dest,
                timeout=600,
            )
        commit = resolve_commit(dest, at=at, ref=ref)
        tree = _worktree(dest, commit, keep=keep_worktrees)
    return Workspace(tree, commit=commit, repo=repo)


def _worktree(dest: Path, commit: str, *, keep: int) -> Path:
    """A private checkout of `commit`, shared by nothing else.

    Each commit gets its own git worktree, so concurrent analyses (an
    evaluation run next to the webhook server, say) can never read files from
    each other's checkout. Least recently used worktrees beyond `keep` are pruned.
    """
    root = dest.parent / "worktrees"
    tree = root / f"{dest.name}@{commit[:16]}"
    if not (tree / ".git").exists():
        root.mkdir(parents=True, exist_ok=True)
        with suppress(GitError):
            _git(["worktree", "prune"], dest, timeout=120)
        _git(["worktree", "add", "--force", "--detach", str(tree), commit], dest, timeout=1200)
    tree.touch()
    siblings = sorted(
        (p for p in root.glob(f"{dest.name}@*") if p != tree),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in siblings[max(0, keep - 1) :]:
        with suppress(GitError):
            _git(["worktree", "remove", "--force", str(stale)], dest, timeout=300)
    return tree
