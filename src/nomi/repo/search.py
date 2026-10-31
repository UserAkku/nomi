"""Lexical code retrieval (BM25) over a Workspace pinned at one commit.

Why BM25 rather than embeddings: Nomi runs next to a small local model, so
retrieval has to be dependency-free, deterministic and fast enough to rebuild
for every analysed commit. Bug reports name identifiers (``parse_url``,
``fetchUser``), error strings and file names, which lexical matching handles
well once identifiers are also split into their parts. Every Chunk keeps its
real path and 1-based line range, so whatever is retrieved can later be cited
and mechanically re-verified against the repository.
"""

from __future__ import annotations

import math
import re
from array import array
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath

from nomi.repo.workspace import Workspace, classify_path, normalize_path

K1 = 1.5
B = 0.75
BOOST = 1.5
MAX_CHUNKS_PER_FILE = 2
_MAX_TOKEN_LEN = 64

_WORD = re.compile(r"\w+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

STOPWORDS = frozenset(
    {
        # English
        "the", "and", "or", "but", "is", "are", "was", "were", "be", "been", "being",
        "in", "on", "at", "of", "to", "for", "with", "as", "by", "an", "it", "its",
        "this", "that", "these", "those", "from", "into", "if", "then", "when", "which",
        "who", "what", "there", "their", "they", "we", "you", "he", "she", "has", "have",
        "had", "do", "does", "did", "so", "such", "can", "will", "would", "should",
        "could", "may", "might", "not", "no", "all", "any", "some", "also", "just",
        "than", "too", "very", "our", "my", "me", "us", "your", "here", "about",
        # code
        "self", "cls", "def", "return", "import", "none", "null", "nil", "true",
        "false", "var", "let", "const", "pass", "elif", "else", "end", "fn", "func",
        "public", "private", "static", "void",
    }
)  # fmt: skip

LOCKFILES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
    }
)
_SKIP_DIR_SEGMENTS = frozenset({"vendor", "third_party", "dist", "node_modules"})
_SKIP_SUFFIXES = (".min.js", ".min.css", ".js.map", ".css.map", "_pb2.py", "_pb2_grpc.py", ".pb.go")
_BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".psd",
        ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".tar", ".jar", ".war",
        ".class", ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".pyc", ".whl",
        ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav", ".ogg",
        ".webm", ".mov", ".avi", ".db", ".sqlite", ".npy", ".npz", ".pkl", ".parquet",
    }
)  # fmt: skip
_GENERATED_MARKERS = ("@generated", "DO NOT EDIT")


@dataclass(frozen=True)
class Chunk:
    """A contiguous slice of one file; ``start``/``end`` are 1-based and inclusive."""

    path: str
    start: int
    end: int
    text: str
    kind: str


# ----------------------------------------------------------------- tokenizer


def _keep(token: str) -> bool:
    return 2 <= len(token) <= _MAX_TOKEN_LEN and token not in STOPWORDS


@lru_cache(maxsize=1 << 16)
def _word_tokens(word: str) -> tuple[str, ...]:
    parts = [p.lower() for piece in word.split("_") for p in _CAMEL_BOUNDARY.split(piece) if p]
    out: list[str] = []
    full = word.strip("_").lower()
    if len(parts) > 1 and _keep(full):
        out.append(full)
    out.extend(p for p in parts if _keep(p))
    return tuple(out)


def tokenize(text: str) -> list[str]:
    """Lowercase search terms: whole identifiers plus their snake/camel parts.

    ``parseURL_v2`` yields ``parseurl_v2``, ``parse``, ``url`` and ``v2``, so a
    query naming either the identifier or its words finds the same code.
    """
    tokens: list[str] = []
    for word in _WORD.findall(text):
        tokens.extend(_word_tokens(word))
    return tokens


# ------------------------------------------------------------------ indexing


def is_indexable(path: str) -> bool:
    """False for lockfiles, vendored/minified/generated paths and binary formats."""
    pure = PurePosixPath(path)
    if pure.name in LOCKFILES or pure.suffix.lower() in _BINARY_EXTENSIONS:
        return False
    if any(seg in _SKIP_DIR_SEGMENTS for seg in pure.parts[:-1]):
        return False
    return not pure.name.lower().endswith(_SKIP_SUFFIXES)


def _looks_generated(text: str) -> bool:
    head = text[:600]
    if any(marker in head for marker in _GENERATED_MARKERS):
        return True
    newlines = text.count("\n") + 1
    return len(text) > 20_000 and len(text) / newlines > 500  # minified bundle


def _read_indexable(ws: Workspace, path: str, max_file_bytes: int) -> str | None:
    abs_path = ws.root / path
    try:
        if abs_path.is_symlink() or abs_path.stat().st_size > max_file_bytes:
            return None
    except OSError:
        return None
    text = ws.read_text(path)
    if text is None or _looks_generated(text):
        return None
    return text


def _chunk_file(path: str, text: str, size: int, step: int) -> Iterator[Chunk]:
    lines = text.splitlines()
    kind = classify_path(path)
    for lo in range(0, len(lines), step):
        hi = min(lo + size, len(lines))
        body = "\n".join(lines[lo:hi])
        if body.strip():
            yield Chunk(path=path, start=lo + 1, end=hi, text=body, kind=kind)
        if hi >= len(lines):
            break


class CodeIndex:
    """BM25 over line chunks with an inverted index (term -> postings)."""

    def __init__(
        self, chunks: Iterable[Chunk], *, files_indexed: int | None = None, truncated: bool = False
    ) -> None:
        self.chunks: tuple[Chunk, ...] = tuple(chunks)
        self.files_indexed = (
            len({c.path for c in self.chunks}) if files_indexed is None else files_indexed
        )
        self.truncated = truncated  # True when max_files stopped indexing early
        self._docs: dict[str, array] = {}
        self._tfs: dict[str, array] = {}
        self._norm: list[float] = []
        self._index()

    def __len__(self) -> int:
        return len(self.chunks)

    @classmethod
    def build(
        cls,
        ws: Workspace,
        *,
        chunk_lines: int = 40,
        overlap: int = 10,
        max_files: int = 20000,
        max_file_bytes: int = 300_000,
    ) -> CodeIndex:
        """Chunk every indexable tracked text file of `ws` and index it."""
        size = max(1, chunk_lines)
        step = max(1, size - max(0, overlap))
        chunks: list[Chunk] = []
        files = 0
        truncated = False
        for path in ws.files:
            if not is_indexable(path):
                continue
            if files >= max_files:
                truncated = True
                break
            text = _read_indexable(ws, path, max_file_bytes)
            if text is None:
                continue
            files += 1
            chunks.extend(_chunk_file(path, text, size, step))
        return cls(chunks, files_indexed=files, truncated=truncated)

    def _index(self) -> None:
        docs: defaultdict[str, array] = defaultdict(lambda: array("I"))
        tfs: defaultdict[str, array] = defaultdict(lambda: array("I"))
        path_terms: dict[str, list[str]] = {}
        lengths: list[int] = []
        for doc, chunk in enumerate(self.chunks):
            if chunk.path not in path_terms:
                path_terms[chunk.path] = tokenize(chunk.path)
            counts = Counter(tokenize(chunk.text))
            counts.update(path_terms[chunk.path])
            for term, tf in counts.items():
                docs[term].append(doc)
                tfs[term].append(tf)
            lengths.append(sum(counts.values()))
        avgdl = (sum(lengths) / len(lengths)) if lengths else 0.0
        self._norm = [K1 * (1 - B + B * dl / avgdl) if avgdl else K1 for dl in lengths]
        self._docs, self._tfs = dict(docs), dict(tfs)

    def _idf(self, df: int) -> float:
        n = len(self.chunks)
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def _scores(self, query: str) -> dict[int, float]:
        scores: dict[int, float] = defaultdict(float)
        for term in sorted(set(tokenize(query))):
            docs = self._docs.get(term)
            if docs is None:
                continue
            idf = self._idf(len(docs))
            norm = self._norm
            for doc, tf in zip(docs, self._tfs[term], strict=True):
                scores[doc] += idf * tf * (K1 + 1) / (tf + norm[doc])
        return scores

    def search(
        self,
        query: str,
        *,
        k: int = 8,
        kinds: set[str] | None = None,
        boost_paths: set[str] | None = None,
    ) -> list[tuple[Chunk, float]]:
        """Top-k chunks by BM25 score, at most two per file.

        `kinds` keeps only chunks of those classify_path kinds; chunks of files
        in `boost_paths` (e.g. paths the report names) score 1.5x.
        """
        if k <= 0 or not self.chunks or not query:
            return []
        boost = {normalize_path(p) for p in boost_paths or ()}
        ranked: list[tuple[float, int]] = []
        for doc, score in self._scores(query).items():
            chunk = self.chunks[doc]
            if kinds is not None and chunk.kind not in kinds:
                continue
            ranked.append((score * BOOST if chunk.path in boost else score, doc))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        per_file: Counter[str] = Counter()
        results: list[tuple[Chunk, float]] = []
        for score, doc in ranked:
            chunk = self.chunks[doc]
            if per_file[chunk.path] >= MAX_CHUNKS_PER_FILE:
                continue
            per_file[chunk.path] += 1
            results.append((chunk, score))
            if len(results) >= k:
                break
        return results
