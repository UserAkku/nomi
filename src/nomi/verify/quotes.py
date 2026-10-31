"""Check code that a report quotes *from the repository* against the repository.

Reports often paste "the vulnerable code". A genuine quote is copied, so most
of its distinctive lines are found verbatim at the analysed commit. A
fabricated one names a real function but invents its body. The reporter's
own reproduction script naturally appears nowhere in the repository and is
left alone: only code that presents itself as the project's own is judged.
A subclass that overrides a project method (`class Mine(Base)`, `extends`,
`super()`) or a definition nested in a class/function the project does not
have is the reporter's own code, however much its names clash.
"""

from __future__ import annotations

import re
from collections import defaultdict

from nomi.models import Artifacts, Check, CheckStatus, CodeBlock, Evidence, Signal
from nomi.repo.symbols import Definition, defined_names, find_definitions, language_family
from nomi.repo.workspace import GrepHit, Workspace, classify_path
from nomi.verify.grounding import line_evidence

_SKIP_LANGUAGES = frozenset(
    {
        "bash",
        "sh",
        "shell",
        "console",
        "text",
        "log",
        "txt",
        "diff",
        "zsh",
        "plaintext",
        "patch",
        "output",
        "shell-session",
        "shellsession",
        "terminal",
        "powershell",
        "ps1",
        "pwsh",
        "bat",
        "cmd",
    }
)
_TRACEBACK = re.compile(
    r"Traceback \(most recent call last\)|^\s*File \".*\", line \d+|^\s+at\s+\S.*[:(]\d+"
    r"|^\s*#\d+\s+0x[0-9a-fA-F]+|goroutine \d+ \[|panicked at|^==\d+==ERROR|Exception in thread",
    re.M,
)
_TRIVIAL = re.compile(r"[\s{}()\[\];,]*|end|else:?|pass|\.\.\.|…")
_PREPROCESSOR = re.compile(
    r"#\s*(?:define|if|ifdef|ifndef|elif|else|endif|undef|pragma|error|include)\b"
)
_COMMENT = re.compile(r"(?://|/\*|\*(?:\s|/|$)|<!--|--\s|;;|#)")
_IMPORT = re.compile(
    r"(?:import\s+[\w.,\s]+(?:\s+as\s+\w+)?|from\s+[\w.]+\s+import\s+.+|#\s*include\s*[<\"].*"
    r"|using\s+[\w.:]+;?|use\s+[\w:{}, *]+;|package\s+[\w.]+;?|require(?:_relative)?\s*\(?['\"].*"
    r"|(?:const|let|var)\s+.+=\s*require\(.*\);?|import\s.+\sfrom\s+['\"].*|import\s+['\"].*"
    r"|extern\s+crate\s+\w+;)"
)
_GENERIC_NAMES = frozenset(
    {
        "main",
        "run",
        "setup",
        "setUp",
        "tearDown",
        "init",
        "handler",
        "callback",
        "helper",
        "wrapper",
        "example",
        "demo",
        "func",
        "function",
        "process",
        "execute",
        "start",
        "stop",
        "update",
        "create",
        "build",
        "handle",
        "apply",
        "call",
    }
)
_SUPER_CALL = re.compile(
    r"\bsuper\s*(?:\(|\.\s*[A-Za-z_$])|^\s*super\b(?!::)|\bparent::\w+\s*\(", re.M
)
_CLASS_HEADER = re.compile(
    r"^[ \t]*(?:[\w@]+[ \t]+)*?(?:class|struct|interface|object|record)[ \t]+"
    r"([A-Za-z_$][\w$]*)([^\n]{0,300})",
    re.M,
)
_NOT_A_BASE = frozenset(
    {"extends", "implements", "public", "private", "protected", "virtual", "metaclass", "val"}
)
_RECEIVER = re.compile(r"^\s*func\s*\(\s*\w*\s*\*?\s*([A-Za-z_]\w*)")  # Go: func (c *T) Name(
_RUST_IMPL = re.compile(r"^\s*impl\b(?:\s*<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?([A-Za-z_]\w*)")
_MAX_SITES = 20  # definition sites of one name examined per block
_MAX_SCAN = 2000  # lines scanned upwards for an enclosing scope
_MIN_LINE = 12
_MAX_LINES = 15
_MAX_LOOKUP_CHARS = 200


def check_quoted_code(artifacts: Artifacts, ws: Workspace) -> list[Check]:
    """One `quote.code` check per code block that presents itself as repository code."""
    checks: list[Check] = []
    seen: set[str] = set()
    for block in artifacts.code_blocks:
        if block.content in seen:
            continue
        seen.add(block.content)
        check = _check_block(block, ws)
        if check is not None:
            checks.append(check)
    return checks


def distinctive_lines(code: str, language: str | None = None) -> list[str]:
    """Stripped lines specific enough to look up verbatim (no comments, imports, braces)."""
    is_c = language_family(language) == "c"
    found: dict[str, None] = {}
    for raw in code.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if len(line) < _MIN_LINE or _IMPORT.fullmatch(line) or _TRIVIAL.fullmatch(line):
            continue
        if _COMMENT.match(line) and not (is_c and _PREPROCESSOR.match(line)):
            continue
        found.setdefault(line, None)
    return list(found)[:_MAX_LINES]


def _looks_like_quote(block: CodeBlock) -> bool:
    language = (block.language or "").strip().lower()
    if language.split()[:1] and language.split()[0] in _SKIP_LANGUAGES:
        return False
    if _TRACEBACK.search(block.content):
        return False
    lines = block.content.splitlines()
    return sum(1 for line in lines if not _TRIVIAL.fullmatch(line.strip())) >= 3


def _check_block(block: CodeBlock, ws: Workspace) -> Check | None:
    if not _looks_like_quote(block):
        return None
    lines = distinctive_lines(block.content, block.language)
    if not lines:
        return None
    found = {line: hits for line in lines if (hits := _lookup(ws, line))}
    names = defined_names(block.content, block.language)
    repo_defs = {name: defs for name in names if (defs := _repo_definitions(ws, name))}
    suspicious = [
        n for n in repo_defs if _distinctive_name(n) and not _in_reporters_scope(block, n, ws)
    ]
    own_code = bool(suspicious) and _reporters_own_code(block.content, ws)
    if not found and (not suspicious or own_code):
        return None
    ratio = len(found) / len(lines)
    counted = f"{len(found)}/{len(lines)} lines found"
    if ratio >= 0.6:
        path, lo, hi = _best_range(found, span=max(20, 2 * len(block.content.splitlines())))
        return Check(
            id="quote.code",
            title=f"Quoted code matches `{path}:{lo}-{hi}` ({counted} verbatim)",
            status=CheckStatus.PASS,
            signal=Signal.SUPPORTS,
            strength=0.3,
            evidence=[line_evidence(ws, path, lo, hi, description="quoted code in the repository")],
        )
    if ratio < 0.3 and suspicious and len(lines) >= 3 and not own_code:
        name = suspicious[0]
        real = repo_defs[name][0]
        return Check(
            id="quote.code",
            title=(
                f"Code block quotes `{name}` but the body does not match the repository "
                f"({counted}): possibly fabricated or from another version"
            ),
            status=CheckStatus.FAIL,
            signal=Signal.CONTRADICTS,
            strength=0.6,
            detail="The evidence shows the repository's real definition.",
            evidence=[_definition_evidence(ws, real)],
        )
    return Check(
        id="quote.code",
        title=f"Quoted code only partly matches the repository ({counted})",
        status=CheckStatus.WARN,
        signal=Signal.NEUTRAL,
        strength=0.15,
        detail="It may be adapted from the project, simplified, or from another version.",
        evidence=[
            line_evidence(ws, hits[0].path, hits[0].line, description="matching line")
            for hits in list(found.values())[:3]
        ],
    )


def _lookup(ws: Workspace, line: str) -> list[GrepHit]:
    return ws.grep(line[:_MAX_LOOKUP_CHARS], fixed=True, max_hits=20)


def _repo_definitions(ws: Workspace, name: str) -> list[Definition]:
    return [d for d in find_definitions(ws, name, max_hits=5) if classify_path(d.path) == "code"]


def _distinctive_name(name: str) -> bool:
    return (
        len(name) >= 4
        and name not in _GENERIC_NAMES
        and not (name.startswith("__") and name.endswith("__"))
        and not name.lower().startswith("test")
    )


def _reporters_own_code(code: str, ws: Workspace) -> bool:
    """Code that uses the project (imports it, subclasses it, calls super()) is not a quote."""
    return (
        _imports_project(code, ws)
        or bool(_SUPER_CALL.search(code))
        or _subclasses_repo_class(code, ws)
    )


def _subclasses_repo_class(code: str, ws: Workspace) -> bool:
    """`class Mine(RepoBase)` / `class Mine extends RepoBase` where only the base is the project's."""
    for match in _CLASS_HEADER.finditer(code):
        name, rest = match.groups()
        bases = [
            b for b in dict.fromkeys(re.findall(r"[A-Za-z_$][\w$]*", rest)) if b not in _NOT_A_BASE
        ]
        if not bases or _repo_definitions(ws, name):
            continue
        for base in bases[:5]:
            if any(d.kind in ("class", "type") for d in _repo_definitions(ws, base)):
                return True
    return False


def _in_reporters_scope(block: CodeBlock, name: str, ws: Workspace) -> bool:
    """True when every definition of `name` in the block sits in a class/function the repo lacks.

    A clashing definition at the top level (or in an indented fragment) still
    counts as presented as the project's own; so does one inside a class or
    function that the repository itself defines.
    """
    lines = block.content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    sites = [
        i
        for i, line in enumerate(lines)
        if word.search(line) and name in defined_names(line, block.language)
    ][:_MAX_SITES]
    scopes = {_enclosing_scope(lines, i, name, block.language) for i in sites}
    if not scopes or None in scopes:
        return False
    return not any(_repo_definitions(ws, scope) for scope in scopes if scope)


def _enclosing_scope(lines: list[str], index: int, name: str, language: str | None) -> str | None:
    """Name of the class/impl/function enclosing `lines[index]` in the block, by indentation."""
    line = lines[index]
    owner = _RECEIVER.match(line) or re.search(rf"([A-Za-z_]\w*)::{re.escape(name)}\s*\(", line)
    if owner:
        return owner.group(1)
    current = _indent(line)
    for row in range(index - 1, max(-1, index - _MAX_SCAN), -1):
        text = lines[row]
        if current == 0:
            break
        if _TRIVIAL.fullmatch(text.strip()):  # blank lines, lone braces, `end`
            continue
        indent = _indent(text)
        if indent >= current:
            continue
        current = indent
        impl = _RUST_IMPL.match(text)
        scope = impl.group(1) if impl else next(iter(defined_names(text, language)), None)
        if scope:
            return scope
    return None


def _indent(text: str) -> int:
    expanded = text.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _imports_project(code: str, ws: Workspace) -> bool:
    """A snippet that imports the project is the reporter's own code using it."""
    for package in ws.python_packages:
        if re.search(rf"^\s*(?:from|import)\s+{re.escape(package)}\b", code, re.M):
            return True
    for header in re.findall(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", code, re.M):
        if ws.resolve(header):
            return True
    repo_name = (ws.repo or "").split("/")[-1]
    if not repo_name:
        return False
    modules = re.findall(r"(?:require\(|from\s+|import\s+)['\"]([^'\"]+)['\"]", code)
    crate = repo_name.replace("-", "_")
    return (
        any(m.split("/")[0] == repo_name for m in modules)
        or bool(ws.repo and ws.repo in code)
        or bool(re.search(rf"^\s*use\s+{re.escape(crate)}::", code, re.M))
    )


def _best_range(found: dict[str, list[GrepHit]], *, span: int) -> tuple[str, int, int]:
    """File and tight line window holding the most quoted lines."""
    by_path: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for text, hits in found.items():
        for hit in hits:
            by_path[hit.path][text].add(hit.line)
    best = max(sorted(by_path), key=lambda p: len(by_path[p]))
    points = sorted({n for lines in by_path[best].values() for n in lines})
    lo, hi, count, j = points[0], points[0], 0, 0
    for i, start in enumerate(points):
        while j < len(points) and points[j] - start <= span:
            j += 1
        if j - i > count:
            lo, hi, count = start, points[j - 1], j - i
    return best, lo, hi


def _definition_evidence(ws: Workspace, definition: Definition) -> Evidence:
    total = len(ws.lines(definition.path) or [])
    end = max(definition.line, min(definition.line + 8, total))
    return line_evidence(
        ws, definition.path, definition.line, end, description="the repository's definition"
    )
