"""Where is a name defined, used, or enclosing a line -- across languages.

Reports cite functions, classes and macros by name, and stack traces claim
that a line sits inside a given function. Verifying either needs a small,
language-aware index of the repository. A real parser per language is far
too heavy for triage, so this module uses conservative line-oriented
definition patterns per language family, run through `Workspace.grep`.

The patterns only decide between "defined here" and "merely mentioned";
callers fall back to plain mentions before ever calling a name fabricated,
so a missed definition costs a little support, never a false accusation.
"""

from __future__ import annotations

import builtins
import io
import re
import sys
import tokenize
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath

from nomi.repo.workspace import GrepHit, Workspace, split_lines

_SEPARATORS = re.compile(r"\.|::|#|->")
_PART = re.compile(r"^(?:[^\W\d]|\$)[\w$]*[?!]?$")
_NAME_SLOT = "@NAME@"
_CAPTURE = r"(?P<name>[A-Za-z_$~][\w$]*[?!]?)"
_MAX_NAME_LEN = 200
_BRACE_LOOKBACK = 300


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_EXTENSIONS: dict[str, str] = {
    **dict.fromkeys((".py", ".pyi", ".pyw", ".pyx"), "python"),
    **dict.fromkeys(
        (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte"), "js"
    ),
    **dict.fromkeys(
        (
            ".c",
            ".h",
            ".cc",
            ".cpp",
            ".cxx",
            ".c++",
            ".hpp",
            ".hh",
            ".hxx",
            ".h++",
            ".inl",
            ".ipp",
            ".m",
            ".mm",
            ".cu",
        ),
        "c",
    ),
    ".go": "go",
    ".rs": "rust",
    **dict.fromkeys((".java", ".kt", ".kts", ".cs", ".scala", ".groovy", ".swift", ".dart"), "jvm"),
    **dict.fromkeys((".rb", ".rake", ".gemspec"), "ruby"),
    **dict.fromkeys((".php", ".phtml"), "php"),
}

_LANGUAGE_ALIASES: dict[str, str] = {
    **dict.fromkeys(("python", "py", "python3", "py3", "pycon", "ipython"), "python"),
    **dict.fromkeys(
        ("js", "javascript", "jsx", "ts", "typescript", "tsx", "node", "mjs", "vue"), "js"
    ),
    **dict.fromkeys(("c", "h", "cpp", "c++", "cxx", "cc", "hpp", "objc", "objective-c"), "c"),
    **dict.fromkeys(("go", "golang"), "go"),
    **dict.fromkeys(("rust", "rs"), "rust"),
    **dict.fromkeys(
        ("java", "kotlin", "kt", "csharp", "cs", "c#", "scala", "groovy", "swift", "dart"), "jvm"
    ),
    **dict.fromkeys(("ruby", "rb"), "ruby"),
    **dict.fromkeys(("php",), "php"),
}

_CONTROL_WORDS = _words(
    """
    if else elif for while do switch case return sizeof typeof catch try with goto defined
    new delete throw await yield function fn func def and or not in assert print
    """
)


@dataclass(frozen=True)
class Definition:
    path: str
    line: int
    kind: str
    text: str


@dataclass(frozen=True)
class _Pattern:
    kind: str
    template: str  # regex with _NAME_SLOT where the defined name goes
    no_semicolon: bool = False  # reject lines ending in ';' (C prototypes, calls)

    @property
    def is_callable(self) -> bool:
        return self.kind in ("function", "method")


_C_PREFIX = r"(?:[A-Za-z_][\w\s\*&:<>,~]*[\s\*&:])?"
_RUST_VIS = r"(?:pub(?:\s*\([^)]*\))?\s+)?"
_JVM_MODIFIERS = (
    r"(?:(?:public|private|protected|internal|static|final|abstract|synchronized|native|"
    r"override|virtual|async|sealed|default|extern|unsafe|partial|strictfp|open|suspend|"
    r"inline|operator|infix|tailrec|external|lateinit|data|private\[\w+\])\s+)*"
)

_FAMILY_PATTERNS: dict[str, tuple[_Pattern, ...]] = {
    "python": (
        _Pattern("function", r"^\s*(?:async\s+)?def\s+@NAME@"),
        _Pattern("class", r"^\s*class\s+@NAME@"),
        _Pattern("variable", r"^(?:type\s+)?@NAME@\s*(?::[^=]*)?=(?!=)"),
    ),
    "js": (
        _Pattern(
            "function",
            r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*@NAME@",
        ),
        _Pattern("class", r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+@NAME@"),
        _Pattern(
            "function",
            r"^\s*(?:export\s+)?(?:const|let|var)\s+@NAME@\s*(?::[^=]+)?="
            r"\s*(?:async\s+)?(?:function\b|\(|[\w$]+\s*=>)",
        ),
        _Pattern(
            "type",
            r"^\s*(?:export\s+)?(?:declare\s+)?(?:const\s+)?(?:interface|type|enum)\s+@NAME@",
        ),
        _Pattern(
            "method",
            r"^\s*(?:(?:static|async|get|set|public|private|protected|readonly|override|"
            r"abstract)\s+)*\*?@NAME@\s*(?:<[^>]*>)?\s*\([^;()]*\)\s*(?::\s*[^{;=]+)?\{",
        ),
        _Pattern(
            "method",
            r"^\s*@NAME@\s*:\s*(?:async\s+)?(?:function\b|\([^()]*\)\s*=>)",
        ),
        _Pattern("variable", r"^\s*(?:module\.)?exports\.@NAME@\s*="),
    ),
    "c": (
        _Pattern("function", rf"^{_C_PREFIX}@NAME@\s*\(", no_semicolon=True),
        _Pattern(
            "method",
            rf"^\s+{_C_PREFIX}@NAME@\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?"
            r"(?:override\s*)?(?:final\s*)?\{",
            no_semicolon=True,
        ),
        _Pattern("macro", r"^\s*#\s*define\s+@NAME@"),
        _Pattern(
            "type",
            r"^\s*(?:typedef\s+)?(?:struct|enum|union|class)\s+(?:\w+\s+)?@NAME@"
            r"\s*(?:final\s*)?(?::[^;{]*)?(?:\{.*)?$",
        ),
        _Pattern(
            "type",
            r"^\s*typedef\b.*[\s\*\(]@NAME@\s*(?:\)|\[[^\]]*\]\s*)?(?:\(.*\))?\s*;",
        ),
        _Pattern("type", r"^\}\s*@NAME@\s*;"),
    ),
    "go": (
        _Pattern("function", r"^func\s+@NAME@\s*[\(\[]"),
        _Pattern("method", r"^func\s*\([^)]*\)\s*@NAME@\s*[\(\[]"),
        _Pattern("type", r"^\s*type\s+@NAME@"),
        _Pattern("type", r"^\s+@NAME@\s+(?:struct|interface)\s*\{"),
        _Pattern("variable", r"^(?:var|const)\s+@NAME@"),
    ),
    "rust": (
        _Pattern(
            "function",
            rf"^\s*{_RUST_VIS}(?:default\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?"
            r"(?:extern\s+(?:\"[^\"]*\"\s+)?)?fn\s+@NAME@",
        ),
        _Pattern("type", rf"^\s*{_RUST_VIS}(?:struct|enum|trait|type|union)\s+@NAME@"),
        _Pattern("variable", rf"^\s*{_RUST_VIS}(?:mod|const|static(?:\s+mut)?)\s+@NAME@"),
        _Pattern("macro", r"macro_rules!\s*@NAME@"),
    ),
    "jvm": (
        _Pattern(
            "class",
            r"^\s*(?:[\w@]+(?:\([^)]*\))?\s+)*(?:class|@?interface|enum|record|object|struct|"
            r"trait|protocol|extension)\s+@NAME@",
        ),
        _Pattern(
            "function",
            r"^\s*(?:[\w@]+\s+)*(?:fun|func|def)\s+(?:<[^>]*>\s*)?(?:[\w.<>?]+\.)?@NAME@",
        ),
        _Pattern(
            "method",
            r"^\s*(?!(?:return|new|else|throw|await|yield|case|if|while|for|switch|catch|"
            rf"using|lock|goto)\b){_JVM_MODIFIERS}(?:<[^>]*>\s+)?[\w\[\]<>?,.]+(?:<[^>]*>)?"
            r"\s+@NAME@\s*\(",
        ),
    ),
    "ruby": (
        _Pattern("function", r"^\s*def\s+(?:self\.)?@NAME@"),
        _Pattern("class", r"^\s*(?:class|module)\s+(?:[\w:]+::)?@NAME@"),
    ),
    "php": (
        _Pattern(
            "function",
            r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+&?@NAME@",
        ),
        _Pattern(
            "class",
            r"^\s*(?:(?:abstract|final|readonly)\s+)*(?:class|interface|trait|enum)\s+@NAME@",
        ),
    ),
}

_CLOSER = {"ruby": re.compile(r"^\s*end\b")}
_BRACE_CLOSER = re.compile(r"^\s*\}")
_CONTAINER = re.compile(
    r"^\s*(?:[\w@]+\s+)*(?:class|struct|impl|interface|trait|module|object|enum|namespace|"
    r"extension|protocol|record)\b"
)

# --------------------------------------------------------------------------- names


def symbol_parts(name: str) -> list[str]:
    """Split `pkg.mod.Class.method`, `ns::f`, `Class#m` or `f()` into identifier parts.

    Returns [] when the text is not an identifier path, so callers never grep
    for prose or regex metacharacters by accident.
    """
    text = (name or "").strip().strip("`'\"")
    text = re.sub(r"\([^()]*\)$", "", text)
    if not text or len(text) > _MAX_NAME_LEN:
        return []
    parts = [p.strip() for p in _SEPARATORS.split(text)]
    if not all(_PART.match(p) for p in parts):
        return []
    return parts


def language_of(path: str) -> str | None:
    """Language family of a repository path from its extension, or None."""
    return _EXTENSIONS.get(PurePosixPath(path).suffix.lower())


def language_family(language: str | None) -> str | None:
    """Language family of a code-fence info string such as `python` or `c++`."""
    if not language:
        return None
    key = language.strip().lower().split()[0] if language.strip() else ""
    return _LANGUAGE_ALIASES.get(key) or _EXTENSIONS.get(f".{key}")


def _name_regex(name: str) -> str:
    return rf"(?<![\w$]){re.escape(name)}(?![\w$])"


def _fill(template: str, slot: str) -> str:
    return template.replace(_NAME_SLOT, slot)


@lru_cache(maxsize=256)
def _compiled_for(name: str) -> dict[str, tuple[tuple[_Pattern, re.Pattern[str]], ...]]:
    slot = _name_regex(name)
    return {
        family: tuple((p, re.compile(_fill(p.template, slot))) for p in patterns)
        for family, patterns in _FAMILY_PATTERNS.items()
    }


@lru_cache(maxsize=1)
def _compiled_capture() -> dict[str, tuple[tuple[_Pattern, re.Pattern[str]], ...]]:
    slot = rf"(?<![\w$]){_CAPTURE}(?![\w$])"
    return {
        family: tuple((p, re.compile(_fill(p.template, slot))) for p in patterns)
        for family, patterns in _FAMILY_PATTERNS.items()
    }


def _union_pattern(name: str) -> str:
    slot = _name_regex(name)
    seen: dict[str, None] = {}
    for patterns in _FAMILY_PATTERNS.values():
        for p in patterns:
            seen.setdefault(_fill(p.template, slot), None)
    return "|".join(f"(?:{t})" for t in seen)


def _accepts(pattern: _Pattern, line: str, captured: str | None = None) -> bool:
    if pattern.no_semicolon and line.rstrip().endswith(";"):
        return False
    if captured is not None and captured in _CONTROL_WORDS:
        return False
    stripped = line.lstrip()
    first = re.match(r"[A-Za-z_]+", stripped)
    return not (pattern.kind in ("function", "method") and first and first.group() == "return")


def _definition_kind(family: str, name: str, line: str) -> str | None:
    for pattern, rx in _compiled_for(name)[family]:
        if rx.search(line) and _accepts(pattern, line):
            return pattern.kind
    return None


# --------------------------------------------------------------------- definitions


def find_definitions(ws: Workspace, name: str, *, max_hits: int = 20) -> list[Definition]:
    """Definition sites of `name` (last dotted component), ordered by (path, line).

    For a qualified name such as `Client.fetch` or `widget.parser.parse_url`
    the definitions inside the named class or module are returned when there
    are any; otherwise every definition of the bare name is.
    """
    parts = symbol_parts(name)
    if not parts or max_hits <= 0:
        return []
    target = parts[-1]
    hits = ws.grep(_union_pattern(target), fixed=False, max_hits=max(200, max_hits * 10))
    found: dict[tuple[str, int], Definition] = {}
    for hit in hits:
        family = language_of(hit.path)
        if family is None:
            continue
        kind = _definition_kind(family, target, hit.text)
        if kind is not None:
            found[(hit.path, hit.line)] = Definition(hit.path, hit.line, kind, _clip(hit.text))
    for definition in _module_definitions(ws, parts):
        found.setdefault((definition.path, definition.line), definition)
    ordered = sorted(found.values(), key=lambda d: (d.path, d.line))
    if len(parts) > 1:
        preferred = [d for d in ordered if _inside_qualifier(ws, d, parts)]
        ordered = preferred or ordered
    return ordered[:max_hits]


def _clip(text: str) -> str:
    return text.strip()[:300]


def _module_definitions(ws: Workspace, parts: list[str]) -> list[Definition]:
    """A dotted name like `widget.parser` can name a module file rather than a def."""
    if len(parts) < 2:
        return []
    suffixes = ("/".join(parts) + ".py", "/".join(parts) + "/__init__.py")
    found = []
    for path in ws.files:
        if any(path == s or path.endswith("/" + s) for s in suffixes):
            lines = ws.lines(path) or [""]
            found.append(Definition(path, 1, "module", _clip(lines[0])))
    return found


def _inside_qualifier(ws: Workspace, definition: Definition, parts: list[str]) -> bool:
    qualifier = parts[:-1]
    owner = qualifier[-1]
    if definition.kind == "module":
        return True
    owner_rx = re.compile(_name_regex(owner))
    head = definition.text.split(parts[-1], 1)[0]
    if owner_rx.search(head):
        return True
    stem = str(PurePosixPath(definition.path).with_suffix(""))
    stem = stem.removesuffix("/__init__")
    module = "/".join(qualifier)
    if stem == module or stem.endswith("/" + module):
        return True
    return owner in _containers_above(ws, definition.path, definition.line)


def _containers_above(ws: Workspace, path: str, line: int) -> list[str]:
    """Names declared by the class/impl/module blocks that enclose `line` by indentation."""
    lines = ws.lines(path) or []
    if not 1 <= line <= len(lines):
        return []
    current = _indent(lines[line - 1])
    names: list[str] = []
    for idx in range(line - 2, max(-1, line - 5002), -1):
        if current == 0:
            break
        text = lines[idx]
        if not text.strip():
            continue
        indent = _indent(text)
        if indent >= current:
            continue
        current = indent
        if _CONTAINER.match(text):
            names.extend(re.findall(r"[A-Za-z_$][\w$]*", text))
    return names


def _indent(text: str) -> int:
    lead = text[: len(text) - len(text.lstrip())]
    # a form feed in the indentation resets the column, as in Python's tokenizer
    return len(lead.rsplit("\f", 1)[-1].expandtabs(4))


def find_mentions(ws: Workspace, name: str, *, max_hits: int = 50) -> list[GrepHit]:
    """Word-bounded fixed-string occurrences of the last component of `name`."""
    parts = symbol_parts(name)
    if not parts or max_hits <= 0:
        return []
    hits = ws.grep(parts[-1], fixed=True, word=True, max_hits=max_hits)
    unique = {(h.path, h.line): h for h in hits}
    return [unique[k] for k in sorted(unique)]


def defined_names(code: str, language: str | None = None) -> list[str]:
    """Names that a code snippet itself defines (functions, classes, macros...).

    With an unknown language only keyword-introduced definitions (`def`,
    `class`, `function`, `func`, `fn`) count: the bare C function pattern
    would otherwise read Python calls such as `parse(url)` as definitions.
    """
    family = language_family(language) or _guess_family(code)
    families = [family] if family else ["python", "js", "go", "rust", "ruby", "php"]
    skipped = {"variable"} if family else {"variable", "method"}
    names: dict[str, None] = {}
    for raw in _dedent_lines(code):
        for fam in families:
            match = _first_capture(fam, raw, skipped)
            if match:
                names.setdefault(match, None)
                break
    return list(names)


def _first_capture(family: str, line: str, skipped: set[str]) -> str | None:
    for pattern, rx in _compiled_capture()[family]:
        if pattern.kind in skipped:
            continue
        if family == "c" and pattern.kind == "type" and line.rstrip().endswith(";"):
            continue
        m = rx.search(line)
        if m and _accepts(pattern, line, m.group("name")):
            return m.group("name")
    return None


def _dedent_lines(code: str) -> list[str]:
    lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    widths = [_indent(line) for line in lines if line.strip()]
    cut = min(widths, default=0)
    return [line.expandtabs(4)[cut:] if line.strip() else "" for line in lines]


def _guess_family(code: str) -> str | None:
    if re.search(r"^\s*#\s*include\b", code, re.M):
        return "c"
    if re.search(r"^\s*(?:async\s+)?def\s+\w+\s*\(.*\)\s*(?:->.*)?:\s*$", code, re.M):
        return "python"
    if re.search(r"^\s*func\s", code, re.M) and "package " in code:
        return "go"
    if re.search(r"^\s*(?:pub\s+)?fn\s+\w+", code, re.M):
        return "rust"
    if re.search(r"\bfunction\b|=>|^\s*(?:const|let)\s+\w+\s*=", code, re.M):
        return "js"
    return None


# --------------------------------------------------------------- enclosing scope


def enclosing_function(ws: Workspace, path: str, line: int) -> str | None:
    """Name of the function (or Python class body / `<module>`) containing `line`.

    Python is resolved by indentation over logical lines, so multi-line strings
    and bracketed continuations cannot fool it. Brace languages and Ruby use
    the nearest definition above whose block has not visibly closed. Returns
    None when the language is unknown or no enclosing function is evident.
    """
    resolved = path if ws.exists(path) else ws.resolve(path)
    if resolved is None:
        return None
    lines = ws.lines(resolved)
    if not lines or not 1 <= line <= len(lines):
        return None
    family = language_of(resolved)
    if family == "python":
        text = ws.read_text(resolved) or ""
        return _python_enclosing(lines, _python_logical_starts(text), line)
    if family is None:
        return None
    return _block_enclosing(lines, line, family)


_PY_SCOPE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([^\W\d]\w*)")


def _python_enclosing(lines: list[str], starts: tuple[int, ...], line: int) -> str:
    idx = bisect_right(starts, line) - 1
    own = starts[idx] if idx >= 0 else line
    current = _indent(lines[own - 1])
    if current == 0:
        return "<module>"
    for pos in range(idx - 1, -1, -1):
        row = starts[pos]
        text = lines[row - 1]
        if not text.strip():
            continue
        indent = _indent(text)
        if indent >= current:
            continue
        scope = _PY_SCOPE.match(text)
        if scope:
            return scope.group(1)
        current = indent
        if current == 0:
            break
    return "<module>"


@lru_cache(maxsize=32)
def _python_logical_starts(text: str) -> tuple[int, ...]:
    """1-based rows where a logical Python line begins (code, not strings/continuations)."""
    starts: list[int] = []
    at_start = True
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.NEWLINE:
                at_start = True
            elif tok.type in (
                tokenize.NL,
                tokenize.COMMENT,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENDMARKER,
            ):
                continue
            elif at_start:
                starts.append(tok.start[0])
                at_start = False
    except (tokenize.TokenError, SyntaxError):
        rows = split_lines(text)
        return tuple(
            i for i, row in enumerate(rows, 1) if row.strip() and not row.lstrip().startswith("#")
        )
    return tuple(starts)


def _block_enclosing(lines: list[str], line: int, family: str) -> str | None:
    closer = _CLOSER.get(family, _BRACE_CLOSER)
    callables = [(p, rx) for p, rx in _compiled_capture()[family] if p.is_callable]
    closed_at = 10**9
    for row in range(line, max(0, line - _BRACE_LOOKBACK) - 1, -1):
        text = lines[row - 1]
        if not text.strip():
            continue
        indent = _indent(text)
        for pattern, rx in callables:
            m = rx.search(text)
            if m and _accepts(pattern, text, m.group("name")):
                if indent < closed_at:
                    return m.group("name")
                break
        if row != line and closer.match(text):
            closed_at = min(closed_at, indent)
    return None


# ------------------------------------------------------------ external symbols

_C_STDLIB = _words(
    """
    strcpy strncpy strcat strncat strlcpy strlcat memcpy memmove memset memcmp memchr malloc
    calloc realloc free alloca printf fprintf sprintf snprintf vsnprintf vsprintf vprintf
    vfprintf scanf sscanf fscanf strlen strnlen strcmp strncmp strcasecmp strncasecmp strchr
    strrchr strstr strdup strndup strtok strtok_r strtol strtoul strtoll strtoull strtod
    atoi atol atof fopen fclose fread fwrite fgets fputs fputc fgetc fflush fseek ftell feof
    ferror read write open close exit abort assert getenv setenv puts putchar getchar perror
    errno strerror qsort bsearch time sleep usleep nanosleep select poll epoll_wait socket
    connect bind listen accept send recv sendto recvfrom getaddrinfo freeaddrinfo ioctl
    fcntl mmap munmap stat fstat lstat unlink pthread_create pthread_join pthread_mutex_lock
    pthread_mutex_unlock signal raise longjmp setjmp gets isalpha isdigit isspace isalnum
    toupper tolower sizeof NULL size_t ssize_t uint8_t uint16_t uint32_t uint64_t int8_t
    int16_t int32_t int64_t FILE std
    """
)
_JS_GLOBALS = _words(
    """
    console JSON Promise Math Object Array fetch require process window document module
    exports setTimeout setInterval clearTimeout clearInterval Number String Boolean Symbol
    Map Set WeakMap WeakSet Date RegExp Error TypeError RangeError undefined null this
    globalThis Buffer Reflect Proxy Intl BigInt navigator localStorage async await
    """
)
_OTHER_GLOBALS = _words(
    """
    numpy np pandas pd os sys re json requests torch tf scipy sklearn matplotlib plt self
    cls super System Integer Long Boolean Thread Exception RuntimeException fmt strings
    strconv errors context bytes bufio ioutil sync http Vec Option Result Some Ok Err Box Rc
    Arc println format panic core alloc
    """
)


@lru_cache(maxsize=1)
def _python_external() -> frozenset[str]:
    return frozenset(dir(builtins)) | frozenset(sys.stdlib_module_names)


def is_external_symbol(name: str, ws: Workspace) -> bool:
    """True when `name` is a language builtin, a standard library or a well-known library.

    A first component that is one of the repository's own Python packages is
    always internal, so `json.decode` in a project that ships `json/` is checked.
    """
    parts = symbol_parts(name)
    if not parts:
        return False
    head = parts[0]
    if head in ws.python_packages:
        return False
    return (
        head in _python_external()
        or head in _C_STDLIB
        or head in _JS_GLOBALS
        or head in _OTHER_GLOBALS
    )
