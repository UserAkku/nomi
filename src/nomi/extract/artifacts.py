"""Deterministic extraction of checkable artifacts from issue and PR text.

Nomi verifies a report by checking the concrete things it mentions (files,
line numbers, symbols, stack frames, versions, flags) against the repository.
Those references are pulled out without a language model so they are
reproducible, cheap and impossible to hallucinate. Every pattern here is
regex or line based, the input is bounded and every output list is capped.
The rules favour precision: a token is reported only when it is clearly
code-like, because each artifact later becomes something Nomi tries to check
and a false artifact can turn into a false accusation.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import unquote

from nomi.models import Artifacts, CodeBlock, FileRef, StackFrame, StackTrace

MAX_CHARS = 200_000
MAX_INLINE_CODE = 100
MAX_SYMBOLS = 50
MAX_BLOCK_SYMBOLS = 20
MAX_VERSIONS = 30
MAX_FLAGS = 30
MAX_ITEMS = 100  # cap for lists the contract leaves open (file refs, urls, issue refs, ...)
MAX_ERRORS = 50
MAX_TRACES = 50
MAX_ERROR_LEN = 300

Span = tuple[int, int]
T = TypeVar("T")

# ----------------------------------------------------------------- utilities


def _words(spaced: str) -> frozenset[str]:
    return frozenset(spaced.split())


def _normalize(text: str | None) -> str:
    return (text or "")[:MAX_CHARS].replace("\r\n", "\n").replace("\r", "\n")


def _unique(items: Iterable[T], cap: int) -> list[T]:
    seen: set[T] = set()
    out: list[T] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= cap:
            break
    return out


def _by_position(pairs: Iterable[tuple[int, T]], cap: int) -> list[T]:
    return _unique((value for _, value in sorted(pairs, key=lambda p: p[0])), cap)


_NOT_NEWLINE = re.compile(r"[^\n]")


def _mask(text: str, spans: Iterable[Span]) -> str:
    """Blank out spans (keeping newlines) so offsets and line numbers stay valid."""
    merged: list[list[int]] = []
    for start, end in sorted(s for s in spans if s[1] > s[0]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if not merged:
        return text
    pieces: list[str] = []
    prev = 0
    for start, end in merged:
        pieces.append(text[prev:start])
        pieces.append(_NOT_NEWLINE.sub(" ", text[start:end]))
        prev = end
    pieces.append(text[prev:])
    return "".join(pieces)


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


# ------------------------------------------------------------ fenced blocks

_FENCE_OPEN = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>.*?)[ \t]*$")
# Fenced blocks in these languages hold program source, where numbers such as
# 3.14 are literals and `-x` is arithmetic, not versions or command-line flags.
_SOURCE_LANGUAGES = _words(
    "python py python3 javascript js jsx typescript ts tsx c h cpp c++ cc cxx hpp java go golang "
    "rust rs ruby rb php csharp cs c# kotlin kt swift scala objc objective-c"
)
_SHELL_LANGUAGES = _words(
    "sh bash shell console zsh fish terminal powershell ps1 pwsh cmd bat text txt plaintext "
    "shell-session shellsession"
)


@dataclass(frozen=True)
class _Fenced:
    block: CodeBlock
    start: int  # offset of the opening fence line
    end: int  # offset just past the closing fence line (or end of text)
    content_start: int


def _fence_language(info: str) -> str | None:
    word = info.split()[0] if info.split() else ""
    word = word.strip("{}").lstrip(".").split(",")[0].lower()
    return word or None


def _find_fences(text: str) -> list[_Fenced]:
    if "```" not in text and "~~~" not in text:
        return []
    lines = text.split("\n")
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    fenced: list[_Fenced] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _FENCE_OPEN.match(line) if ("```" in line or "~~~" in line) else None
        if m is None or (m["fence"][0] == "`" and "`" in m["info"]):
            i += 1
            continue
        fence = m["fence"]
        close = re.compile(rf"^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        j = i + 1
        while j < len(lines) and not (fence[:3] in lines[j] and close.match(lines[j])):
            j += 1
        last = min(j, len(lines) - 1)
        block = CodeBlock(
            language=_fence_language(m["info"]),
            content="\n".join(lines[i + 1 : j]),
            line=i + 1,
        )
        content_start = offsets[i + 1] if i + 1 < len(lines) else len(text)
        fenced.append(_Fenced(block, offsets[i], offsets[last] + len(lines[last]), content_start))
        i = j + 1
    return fenced


# -------------------------------------------------------------- inline code

_INLINE = re.compile(r"(?<!`)(`{1,2})(?!`)([^`\n]{1,500})\1(?!`)")


@dataclass(frozen=True)
class _Inline:
    start: int
    end: int
    content_start: int
    content: str


def _find_inline(prose: str) -> list[_Inline]:
    if "`" not in prose:
        return []
    found: list[_Inline] = []
    for m in _INLINE.finditer(prose):
        if m[2].strip():
            found.append(_Inline(m.start(), m.end(), m.start(2), m[2]))
    return found


# --------------------------------------------------------------------- urls

_URL = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_URL_TRAILING = ".,;:!?*'\""
_GITHUB_BLOB = re.compile(
    r"^https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+/blob/[^/\s]+/(?P<path>[^?#\s]+)"
    r"(?:\?[^#\s]*)?(?:#L(?P<a>\d{1,7})(?:C\d+)?(?:-L(?P<b>\d{1,7})(?:C\d+)?)?)?",
    re.I,
)


def _trim_url(url: str) -> str:
    while url:
        last = url[-1]
        if (
            last in _URL_TRAILING
            or (last == ")" and url.count(")") > url.count("("))
            or (last == "]" and url.count("]") > url.count("["))
        ):
            url = url[:-1]
            continue
        break
    return url


def _find_urls(text: str) -> list[tuple[int, int, str]]:
    if "http" not in text.lower():
        return []
    found: list[tuple[int, int, str]] = []
    for m in _URL.finditer(text):
        url = _trim_url(m[0])
        host = url.split("//", 1)[1].split("/", 1)[0]
        if re.search(r"[A-Za-z0-9]", host):
            found.append((m.start(), m.start() + len(url), url))
    return found


# ---------------------------------------------------------------- file refs

FILE_EXTENSIONS = _words(
    "py pyi js jsx ts tsx mjs cjs c h cc cpp hpp cxx go rs java kt rb php cs swift m scala sh "
    "md rst txt toml yaml yml json cfg ini"
)
_EXT_ALT = "|".join(sorted(FILE_EXTENSIONS, key=lambda e: (-len(e), e)))
_SEG = r"[\w.@+~-]{1,200}"
_PATH = rf"(?:[A-Za-z]:[/\\]|/)?(?:{_SEG}[/\\]){{0,40}}{_SEG}\.(?:{_EXT_ALT})"
_FILE_TOKEN = re.compile(
    rf"(?<![\w.@+~/\\:-])(?P<path>{_PATH})"
    r"(?::(?P<a>\d{1,7})(?:(?P<sep>[:-])(?P<b>\d{1,7}))?|#L(?P<c>\d{1,7})(?:-L(?P<d>\d{1,7}))?)?"
    r"(?![\w/\\-])(?!\.\w)(?!\()"
)
_LINE_PREFIX = re.compile(
    r"(?:\b(?:at|on)\s+)?\blines?\s+(?P<a>\d{1,7})(?:\s*(?:-|–|to)\s*(?P<b>\d{1,7}))?"
    r"\s+(?:of|in)\s+(?:(?:the\s+)?file\s+)?[`'\"]?$",
    re.I,
)
_LINE_SUFFIX = re.compile(
    r"[`'\"]?(?:(?:\s*,\s*|\s+)(?:(?:on|at)\s+)?line\s+(?P<a>\d{1,7})\b"
    r"|\s*\(\s*line\s+(?P<b>\d{1,7})\s*\))",
    re.I,
)
# Dotted names that look like file names but are products or prose.
_NOT_FILES = _words(
    "node.js vue.js next.js nuxt.js react.js d3.js three.js express.js ember.js angular.js "
    "backbone.js chart.js alpine.js solid.js deno.js p5.js"
)


def _clean_path(raw: str) -> str:
    path = raw.strip("`'\"").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _plausible_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    stem, _, ext = name.rpartition(".")
    if not re.search(r"[A-Za-z_]", stem):  # 3.12.md, 1.rs: numbers, not files
        return False
    if "/" not in path:
        if name.lower() in _NOT_FILES:
            return False
        if len(stem) == 1 and len(ext) == 1:  # "a.m.", "p.m."
            return False
    return True


def _line_pair(start: str | None, end: str | None) -> tuple[int | None, int | None]:
    first = int(start) if start and int(start) > 0 else None
    last = int(end) if end and first and int(end) > first else None
    return first, last


def _file_ref_at(text: str, m: re.Match[str]) -> tuple[int, int, FileRef] | None:
    path = _clean_path(m["path"])
    if not path or not _plausible_path(path):
        return None
    start, end = m.start(), m.end()
    if m["a"]:
        lines = _line_pair(m["a"], m["b"] if m["sep"] == "-" else None)
    elif m["c"]:
        lines = _line_pair(m["c"], m["d"])
    else:
        lines = (None, None)
        suffix = _LINE_SUFFIX.match(text, end, min(len(text), end + 60))
        prefix = _LINE_PREFIX.search(text, max(0, start - 80), start)
        if suffix:
            lines, end = _line_pair(suffix["a"] or suffix["b"], None), suffix.end()
        elif prefix:
            lines, start = _line_pair(prefix["a"], prefix["b"]), prefix.start()
    ref = FileRef(path=path, line_start=lines[0], line_end=lines[1], raw=text[start:end].strip())
    return start, end, ref


def _blob_ref(url: str) -> FileRef | None:
    m = _GITHUB_BLOB.match(url)
    if m is None:
        return None
    path = unquote(m["path"]).strip("/")
    if not path:
        return None
    first, last = _line_pair(m["a"], m["b"])
    return FileRef(path=path, line_start=first, line_end=last, raw=url)


def _find_file_refs(
    text: str, urls: list[tuple[int, int, str]]
) -> tuple[list[FileRef], list[Span]]:
    """File references in `text` (URLs already masked) plus GitHub blob links."""
    found: list[tuple[int, FileRef]] = []
    spans: list[Span] = []
    for m in _FILE_TOKEN.finditer(text):
        hit = _file_ref_at(text, m)
        if hit is not None:
            found.append((hit[0], hit[2]))
            spans.append((m.start(), m.end()))
    for start, _, url in urls:
        ref = _blob_ref(url)
        if ref is not None:
            found.append((start, ref))
    unique: dict[tuple[str, int | None, int | None], FileRef] = {}
    for _, ref in sorted(found, key=lambda pair: pair[0]):
        unique.setdefault((ref.path, ref.line_start, ref.line_end), ref)
        if len(unique) >= MAX_ITEMS:
            break
    return list(unique.values()), spans


# ------------------------------------------------------------------ symbols

_SYMBOL_TOKEN = re.compile(r"(?<![\w.$@/\\#-])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\(\))?")
_PLURAL_PAREN = re.compile(r"\((?:s|es)\)")
_DEFINITION = re.compile(
    r"\b(?:def|class|function|func|fn)\s+(?:\([^)\n]{0,200}\)\s*)?([A-Za-z_$][\w$]*)"
)
_CALL = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\(")
_COMMON_WORDS = _words("""a an the and or not no yes if else elif then for while do in is it its to of on at by as
    be was are were from with without this that these those there here see note example value
    values item items name names data file files code test tests type types true false none null
    nil undefined function method class object string number list dict set map array error errors
    issue issues bug bugs fix feature user users thing things way step steps e.g i.e etc vs a.k.a
    self cls this super new return print""")
_KEYWORDS = _words("""if for while switch catch return elif with sizeof typeof alignof decltype defined assert
    and or not in is lambda yield await async def class function func fn match case when try
    except finally raise throw import from require include using new delete""")
# Builtins called in almost every snippet carry no information about the repository.
_NOISE_CALLS = _words("""print len str int float bool list dict set tuple range open isinstance issubclass super
    type repr hasattr getattr setattr enumerate zip map filter sorted reversed min max sum any
    all abs round id iter next format printf println puts echo console.log console.error
    fmt.println fmt.printf system.out.println""")
# Product names that happen to look camelCased.
_BRAND_WORDS = _words("""github gitlab bitbucket javascript typescript coffeescript macos ios ipados watchos tvos
    iphone ipad ipod imac macbook youtube linkedin postgresql mysql mariadb mongodb pypi npm
    vscode jetbrains pycharm intellij webstorm chatgpt openai devops wordpress powershell paypal
    latex mcdonald powerpoint onedrive sharepoint dropbox openssl libreoffice fastapi numpy scipy
    pytorch tensorflow""")
_TLDS = _words("com org net io dev ai co edu gov uk de fr app info me us ca jp cn ru")


def _dotted_ok(token: str) -> bool:
    parts = token.split(".")
    if all(len(p) == 1 for p in parts):
        return False
    tail = parts[-1].lower()
    return tail not in _TLDS and tail not in FILE_EXTENSIONS


def _code_like(token: str, called: bool) -> bool:
    low = token.lower()
    if len(token) < 2 or low in _COMMON_WORDS or low in _KEYWORDS or low in _BRAND_WORDS:
        return False
    if "." in token:
        return _dotted_ok(token) or called
    if called:
        return True
    if "_" in token:
        core = token.strip("_")
        is_emphasis = token.startswith("_") and token.endswith("_") and "_" not in core
        return bool(core) and not is_emphasis and bool(re.search(r"[A-Za-z]", core))
    return bool(re.search(r"[a-z0-9][A-Z]|[A-Z]{2,}[a-z]{2,}", token))


def _prose_symbols(prose: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for m in _SYMBOL_TOKEN.finditer(prose):
        token = m[0]
        nxt = m.end()
        called = prose.startswith("(", nxt) and not _PLURAL_PAREN.match(prose, nxt)
        if token not in seen and _code_like(token, called):
            seen.add(token)
            found.append((m.start(), token))
            if len(found) >= MAX_SYMBOLS:
                break
    return found


def _inline_symbols(spans: list[_Inline]) -> list[tuple[int, str]]:
    """A span that is exactly one identifier (`fetch`, `Client.fetch()`) names a symbol."""
    found: list[tuple[int, str]] = []
    for span in spans:
        content = span.content.strip()
        if not _IDENTIFIER.fullmatch(content):
            continue
        name = content.removesuffix("()")
        low = name.lower()
        if len(name) < 2 or low in _COMMON_WORDS or low in _KEYWORDS:
            continue
        if "." in name and name.rsplit(".", 1)[-1].lower() in FILE_EXTENSIONS:
            continue
        found.append((span.content_start, name))
    return found


def _block_symbols(fenced: list[_Fenced]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for item in fenced:
        content = item.block.content
        hits = [(m.start(1), m[1], True) for m in _DEFINITION.finditer(content)]
        hits += [(m.start(1), m[1], False) for m in _CALL.finditer(content)]
        for pos, name, defined in sorted(hits):
            low = name.lower()
            noise = low in _NOISE_CALLS or low in _COMMON_WORDS
            if (
                name in seen
                or len(name.strip("$")) < 2
                or low in _KEYWORDS
                or (noise and not defined)
            ):
                continue
            seen.add(name)
            found.append((item.content_start + pos, name))
            if len(found) >= MAX_BLOCK_SYMBOLS:
                return found
    return found


def _find_symbols(prose: str, inline: list[_Inline], fenced: list[_Fenced]) -> list[str]:
    pairs = _prose_symbols(prose) + _inline_symbols(inline) + _block_symbols(fenced)
    return _by_position(pairs, MAX_SYMBOLS)


# ------------------------------------------------------------------ versions

_VERSION = re.compile(
    r"(?<![\w.,:/\\@#$+])"
    r"(?P<v>[vV]?(?P<core>\d{1,6}\.\d{1,6}(?:\.\d{1,6})?)"
    r"(?:[-.]?(?:(?:a|b|c|rc)\d+|(?:alpha|beta|dev|post|pre|preview)(?:\.?\d+)?))?"
    r"(?:\+[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)?)"
    r"(?![\w@/\\+])(?!\.\w)(?!:\d)"
    r"(?!\s?(?:%|°|(?:ms|s|secs?|seconds?|mins?|minutes?|h|hrs?|hours?|days?|[KMGT]i?B|"
    r"[kKMG]b|px|em|rem|pt|fps|[KMG]?Hz)\b))"
)


def _is_date(core: str) -> bool:
    parts = [int(p) for p in core.split(".")]
    if len(parts) != 3:
        return False
    year_first = 1900 <= parts[0] <= 2100 and 1 <= parts[1] <= 12 and 1 <= parts[2] <= 31
    year_last = 1900 <= parts[2] <= 2100 and 1 <= parts[1] <= 12 and 1 <= parts[0] <= 31
    return year_first or year_last


def _find_versions(text: str) -> list[str]:
    found: list[str] = []
    for m in _VERSION.finditer(text):
        if not _is_date(m["core"]):
            found.append(m["v"])
    return _unique(found, MAX_VERSIONS)


# --------------------------------------------------------------------- flags

_LONG_FLAG = re.compile(r"(?<![\w./\\-])(--[A-Za-z][\w-]*[A-Za-z0-9])(?![\w-])")
_SHORT_FLAG = re.compile(r"(?<![\w./\\-])(-[A-Za-z][A-Za-z0-9]{0,3})(?![\w-])")


def _flags_in(content: str, base: int, short: bool) -> list[tuple[int, str]]:
    hits = [(base + m.start(1), m[1]) for m in _LONG_FLAG.finditer(content)]
    if short:
        hits += [(base + m.start(1), m[1]) for m in _SHORT_FLAG.finditer(content)]
    return hits


def _find_flags(inline: list[_Inline], fenced: list[_Fenced]) -> list[str]:
    """Flags only count inside code: in prose `-x` is a dash and `--` an em dash."""
    pairs: list[tuple[int, str]] = []
    for span in inline:
        pairs += _flags_in(span.content, span.content_start, short=True)
    for item in fenced:
        lang = item.block.language
        shell_like = lang is None or lang in _SHELL_LANGUAGES
        pairs += _flags_in(item.block.content, item.content_start, short=shell_like)
    return _by_position(pairs, MAX_FLAGS)


# ------------------------------------------------------ issue refs and CVEs

_ISSUE_REF = re.compile(r"(?:^|(?<=[\s(]))#(\d{1,7})(?![\w-])", re.M)
_HEADING = re.compile(r"[ \t]{0,3}#{1,6}(?:[ \t]|$)")
_CVE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I)


def _find_issue_refs(text: str) -> list[int]:
    refs: list[int] = []
    for m in _ISSUE_REF.finditer(text):
        if not _HEADING.match(text, _line_start(text, m.start())) and int(m[1]) > 0:
            refs.append(int(m[1]))
    return _unique(refs, MAX_ITEMS)


def _find_cves(text: str) -> list[str]:
    return _unique((m[0].upper() for m in _CVE.finditer(text)), MAX_ITEMS)


# ------------------------------------------------------------ error messages

# Ordered by priority: the first pattern that matches a line wins.
_ERROR_PATTERNS = (
    re.compile(
        r"(?<![\w.])(?<!except )(?<!raise )(?<!catch \()"
        r"(?P<msg>(?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception)(?:\[\w+\])?:[ \t]*\S.*)$",
        re.M,
    ),
    re.compile(
        r"^[ \t>]*(?P<msg>(?:(?i:fatal error|error|fatal|panic)(?:\[\w+\])?:[ \t]*\S|"
        r"npm ERR!|Exception in thread\b|thread '[^'\n]*' panicked at\b).*)$",
        re.M,
    ),
    re.compile(r"^[ \t]*(?P<msg>\S+:\d+(?::\d+)?:[ \t]*(?:fatal )?error:[ \t]*\S.*)$", re.M),
    re.compile(
        r"(?P<msg>(?:\bSegmentation fault\b|(?<![(\w])SIG(?:SEGV|ABRT|BUS|ILL|FPE)\b(?![,)])|"
        r"\b\w*Sanitizer:[ \t]*\S|\bruntime error:[ \t]*\S|\bBus error\b|\bdouble free\b).*)$",
        re.M,
    ),
)


_SENTENCE_END = re.compile(r"(?<=\w)[.!?]\s+(?=[A-Z])")


def _clean_error(raw: str) -> str:
    """Keep the message itself: stop at inline-code ends and at the end of a prose sentence."""
    message = _SENTENCE_END.split(raw.split("`", 1)[0], maxsplit=1)[0]
    return message.strip()[:MAX_ERROR_LEN].rstrip()


def _find_errors(text: str) -> list[str]:
    best: dict[int, tuple[int, int, str]] = {}  # line start -> (priority, pos, message)
    for priority, pattern in enumerate(_ERROR_PATTERNS):
        for m in pattern.finditer(text):
            line = _line_start(text, m.start())
            message = _clean_error(m["msg"])
            if message and (line not in best or best[line][0] > priority):
                best[line] = (priority, line, message)
    return _unique((msg for _, _, msg in sorted(best.values(), key=lambda b: b[1])), MAX_ERRORS)


# -------------------------------------------------------------- stack traces

TraceHit = tuple[int, int, StackTrace]  # first line, end line (exclusive), trace
FrameFn = Callable[[str], StackFrame | None]

_QUOTE_PREFIX = re.compile(r"^[ \t]*(?:>[ \t]?)+")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _raw(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start:end]).strip("\n")


def _header_before(
    lines: list[str], consumed: set[int], start: int, pattern: re.Pattern[str], max_back: int
) -> tuple[int, re.Match[str]] | None:
    """Nearest matching line above a frame run, looking at most `max_back` non-blank lines."""
    b, looked = start - 1, 0
    while b >= 0 and looked < max_back and b not in consumed:
        if lines[b].strip():
            m = pattern.search(lines[b])
            if m:
                return b, m
            looked += 1
        b -= 1
    return None


def _frame_runs(
    lines: list[str],
    consumed: set[int],
    trigger: str,
    frame_fn: FrameFn,
    skip: re.Pattern[str],
) -> Iterator[tuple[int, int, list[StackFrame]]]:
    """Maximal runs of frame lines, allowing interleaved lines that match `skip`."""
    i, n = 0, len(lines)
    while i < n:
        first = None if i in consumed or trigger not in lines[i] else frame_fn(lines[i])
        if first is None:
            i += 1
            continue
        frames, last, j = [first], i, i + 1
        while j < n and j not in consumed:
            frame = frame_fn(lines[j])
            if frame is not None:
                frames.append(frame)
                last = j
            elif not skip.match(lines[j]):
                break
            j += 1
        yield i, last + 1, frames
        i = last + 1


# Python ---------------------------------------------------------------------

_PY_HEADER = "Traceback (most recent call last):"
_PY_FRAME = re.compile(
    r'^[ \t]*File "(?P<path>[^"\n]+)", line (?P<line>\d+)(?:, in (?P<func>.+?))?[ \t]*$'
)
_PY_ERROR = re.compile(r"^(?P<type>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?::[ \t]?(?P<msg>.*))?$")
_PY_EXC_NAME = re.compile(r"(?:Error|Exception|Interrupt|Exit|Iteration|Warning|Failure)$")


def _py_frame(line: str) -> StackFrame | None:
    m = _PY_FRAME.match(line) if 'File "' in line else None
    if m is None:
        return None
    return StackFrame(path=m["path"], line=int(m["line"]), function=m["func"], raw=line.strip())


def _py_error(line: str) -> tuple[str, str | None] | None:
    m = _PY_ERROR.match(line.strip())
    if m is None or not m["type"].rsplit(".", 1)[-1][:1].isupper():
        return None
    if m["msg"] is None and not _PY_EXC_NAME.search(m["type"]):
        return None
    return m["type"], (m["msg"] or "").strip() or None


def _python_traces(lines: list[str], consumed: set[int]) -> Iterator[TraceHit]:
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        header = i not in consumed and _PY_HEADER in line
        first = None if header or i in consumed else _py_frame(line)
        if not header and first is None:
            i += 1
            continue
        base = _indent(line) if header else max(0, _indent(line) - 2)
        frames: list[StackFrame] = []
        error: tuple[str, str | None] | None = None
        last, j = i, i + 1 if header else i
        while j < n and j not in consumed:
            cur = lines[j]
            frame = _py_frame(cur)
            if frame is not None:
                frames.append(frame)
            elif not cur.strip():
                break
            elif _indent(cur) <= base and (error := _py_error(cur)) is not None:
                last = j
                break
            elif _indent(cur) <= base and not (
                frames and j + 1 < n and (_py_frame(lines[j + 1]) or _py_error(lines[j + 1]))
            ):
                break
            last, j = j, j + 1
        if frames or error:
            trace = StackTrace(
                language="python",
                frames=frames,
                error_type=error[0] if error else None,
                error_message=error[1] if error else None,
                raw=_raw(lines, i, last + 1),
            )
            yield i, last + 1, trace
        i = last + 1


# Rust -----------------------------------------------------------------------

_RUST_PANIC = re.compile(
    r"thread '(?P<thread>[^'\n]*)' panicked at (?:'(?P<msg>.*)', )?"
    r"(?P<path>[^\s:'\"]+):(?P<line>\d+):(?P<col>\d+):?[ \t]*(?P<rest>.*)$"
)
_RUST_BT_FRAME = re.compile(r"^\s*\d+:\s+(?P<func>\S.*?)\s*$")
_RUST_BT_AT = re.compile(r"^\s+at\s+(?P<path>.+?):(?P<line>\d+)(?::\d+)?\s*$")


def _rust_backtrace(
    lines: list[str], consumed: set[int], start: int, frames: list[StackFrame]
) -> int:
    """Append frames of a `stack backtrace:` block that starts at `start`; return its last line."""
    last, func, k = start, None, start + 1
    while k < len(lines) and k not in consumed:
        at = _RUST_BT_AT.match(lines[k])
        numbered = None if at else _RUST_BT_FRAME.match(lines[k])
        if at:
            frames.append(
                StackFrame(
                    path=at["path"], line=int(at["line"]), function=func, raw=lines[k].strip()
                )
            )
            func = None
        elif numbered:
            func = numbered["func"]
        else:
            break
        last, k = k, k + 1
    return last


def _rust_traces(lines: list[str], consumed: set[int]) -> Iterator[TraceHit]:
    n = len(lines)
    for i, line in enumerate(lines):
        m = None if i in consumed or "panicked at" not in line else _RUST_PANIC.search(line)
        if m is None:
            continue
        frames = [StackFrame(path=m["path"], line=int(m["line"]), raw=line.strip())]
        message, last = m["msg"] or m["rest"].strip() or None, i
        nxt = lines[i + 1].strip() if i + 1 < n and i + 1 not in consumed else ""
        if message is None and nxt and not nxt.startswith(("note:", "stack backtrace:")):
            message, last = nxt, i + 1
        for k in range(last + 1, min(n, last + 4)):
            if k in consumed:
                break
            if "stack backtrace:" in lines[k]:
                last = _rust_backtrace(lines, consumed, k, frames)
                break
        trace = StackTrace(
            language="rust",
            frames=frames,
            error_type="panic",
            error_message=message,
            raw=_raw(lines, i, last + 1),
        )
        yield i, last + 1, trace


# Go -------------------------------------------------------------------------

_GO_GOROUTINE = re.compile(r"^goroutine \d+ \[[^\]\n]*\]:\s*$")
_GO_LOC = re.compile(r"^\s+(?P<path>\S+?\.go):(?P<line>\d+)(?:\s+\+0x[0-9a-fA-F]+)?\s*$")
_GO_PANIC = re.compile(r"^\s*(?P<type>panic|fatal error):\s*(?P<msg>.*?)\s*$")
_GO_ARGS = re.compile(r"\((?:[^()]|\([^()]*\))*\)$")


def _go_traces(lines: list[str], consumed: set[int]) -> Iterator[TraceHit]:
    n = len(lines)
    for i, line in enumerate(lines):
        if i in consumed or not line.startswith("goroutine ") or not _GO_GOROUTINE.match(line):
            continue
        frames: list[StackFrame] = []
        last, k = i, i + 1
        while k + 1 < n and k not in consumed and k + 1 not in consumed:
            func_line, loc = lines[k], _GO_LOC.match(lines[k + 1])
            if not func_line.strip() or func_line[0].isspace() or loc is None:
                break
            func = re.sub(r"\s+in goroutine \d+$", "", func_line.strip()).removeprefix(
                "created by "
            )
            frames.append(
                StackFrame(
                    path=loc["path"],
                    line=int(loc["line"]),
                    function=_GO_ARGS.sub("", func) or func,
                    raw=f"{func_line.strip()}\n{lines[k + 1].strip()}",
                )
            )
            last, k = k + 1, k + 2
        header = _header_before(lines, consumed, i, _GO_PANIC, max_back=3)
        start = header[0] if header else i
        trace = StackTrace(
            language="go",
            frames=frames,
            error_type=header[1]["type"] if header else None,
            error_message=(header[1]["msg"] or None) if header else None,
            raw=_raw(lines, start, last + 1),
        )
        yield start, last + 1, trace


# Java -----------------------------------------------------------------------

_JAVA_FRAME = re.compile(
    r"^\s*at\s+(?P<func>[\w$.<>/\[\]-]+)\((?P<file>[\w$.-]+\.(?:java|kt|kts|scala|groovy|clj))"
    r":(?P<line>\d+)\)(?:\s*~?\[[^\]\n]*\])?\s*$"
)
_JAVA_SKIP = re.compile(
    r"^\s*(?:at\s+[\w$.<>/\[\]-]+\([^()\n]*\)|\.\.\. \d+ (?:more|common frames omitted)"
    r"|Caused by:|Suppressed:)"
)
_JAVA_HEADER = re.compile(
    r'^\s*(?:Exception in thread "[^"\n]*"\s+)?'
    r"(?P<type>(?:[A-Za-z_$][\w$]*\.)+[A-Za-z_$][\w$]*|[A-Z]\w*(?:Error|Exception|Throwable))"
    r"(?::\s*(?P<msg>.*?))?\s*$"
)


def _java_path(func: str, file: str) -> str:
    """`com.acme.Foo.run` + `Foo.java` -> `com/acme/Foo.java`, which resolves unambiguously."""
    parts = func.rsplit("/", 1)[-1].split(".")
    if len(parts) < 3:
        return file
    cls = parts[-2].split("$")[0]
    stem = file.rsplit(".", 1)[0]
    if cls == stem or cls.removesuffix("Kt") == stem:
        return "/".join([*parts[:-2], file])
    return file


def _java_frame(line: str) -> StackFrame | None:
    m = _JAVA_FRAME.match(line)
    if m is None:
        return None
    path = _java_path(m["func"], m["file"])
    return StackFrame(path=path, line=int(m["line"]), function=m["func"], raw=line.strip())


def _java_traces(lines: list[str], consumed: set[int]) -> Iterator[TraceHit]:
    for start, end, frames in _frame_runs(lines, consumed, "at ", _java_frame, _JAVA_SKIP):
        header = _header_before(lines, consumed, start, _JAVA_HEADER, max_back=1)
        first = header[0] if header else start
        trace = StackTrace(
            language="java",
            frames=frames,
            error_type=header[1]["type"] if header else None,
            error_message=(header[1]["msg"] or None) if header else None,
            raw=_raw(lines, first, end),
        )
        yield first, end, trace


# JavaScript -----------------------------------------------------------------

_JS_FRAME = re.compile(
    r"^\s*at\s+(?:(?P<func>[^()\n]+?)\s+\((?P<loc>[^()\n]+?):(?P<line>\d+)(?::\d+)?\)"
    r"|(?P<bare>[^\s()]+?):(?P<bline>\d+)(?::\d+)?)\s*$"
)
_JS_SKIP = re.compile(r"^\s*(?:at\s|\.\.\. \d+ (?:more|lines matching))")
_JS_HEADER = re.compile(
    r"^\s*(?:Uncaught\s+)?\[?(?P<type>[A-Za-z_$][\w$.]*?(?:Error|Exception))\]?"
    r"(?::\s*(?P<msg>.*?))?\s*$"
)


def _js_path(loc: str) -> str:
    path = re.sub(r"^webpack(?:-internal)?:///?", "", loc)
    return _clean_path(re.sub(r"^file://", "", path))


def _js_frame(line: str) -> StackFrame | None:
    m = _JS_FRAME.match(line)
    if m is None:
        return None
    loc, num = (m["loc"], m["line"]) if m["loc"] else (m["bare"], m["bline"])
    return StackFrame(path=_js_path(loc), line=int(num), function=m["func"], raw=line.strip())


def _js_traces(lines: list[str], consumed: set[int]) -> Iterator[TraceHit]:
    for start, end, frames in _frame_runs(lines, consumed, "at ", _js_frame, _JS_SKIP):
        header = _header_before(lines, consumed, start, _JS_HEADER, max_back=1)
        first = header[0] if header else start
        trace = StackTrace(
            language="javascript",
            frames=frames,
            error_type=header[1]["type"] if header else None,
            error_message=(header[1]["msg"] or None) if header else None,
            raw=_raw(lines, first, end),
        )
        yield first, end, trace


# C / C++ (sanitizers, gdb) --------------------------------------------------

_C_GDB = re.compile(
    r"^\s*#\d+\s+(?:0x[0-9a-fA-F]+\s+in\s+)?(?P<func>[^\s(]+)\s*\((?P<args>[^\n]*)\)\s+"
    r"(?:at\s+(?P<loc>\S+?):(?P<line>\d+)|from\s+(?P<mod>\S+))\s*$"
)
_C_ASAN = re.compile(
    r"^\s*#\d+\s+0x[0-9a-fA-F]+\s+in\s+(?P<func>\S+)"
    r"(?:\s+(?P<loc>[^\s()]+?):(?P<line>\d+)(?::\d+)?|\s+\((?P<mod>[^()\s]+?)(?:\+0x[0-9a-fA-F]+)?\))?"
    r"\s*(?:\(BuildId: [0-9a-fA-F]+\))?\s*$"
)
_C_SKIP = re.compile(r"^\s*#\d+\s+0x[0-9a-fA-F]+\s")
_C_HEADER = re.compile(
    r"==\d+==\s*ERROR:\s*\w*Sanitizer:\s*(?P<san>[\w-]+)(?P<rest>.*)$"
    r"|Program received signal (?P<sig>SIG\w+),\s*(?P<sigmsg>.*?)\.?\s*$"
    r"|\bruntime error:\s*(?P<ubsan>.*)$"
)


def _c_frame(line: str) -> StackFrame | None:
    m = _C_GDB.match(line) or _C_ASAN.match(line)
    if m is None or not (m["loc"] or m["mod"]):
        return None
    line_no = int(m["line"]) if m["line"] else None
    return StackFrame(path=m["loc"] or m["mod"], line=line_no, function=m["func"], raw=line.strip())


def _c_error(m: re.Match[str]) -> tuple[str, str | None]:
    if m["san"]:
        return m["san"], f"{m['san']}{m['rest']}".strip()
    if m["sig"]:
        return m["sig"], m["sigmsg"] or None
    return "runtime error", m["ubsan"].strip() or None


def _c_traces(lines: list[str], consumed: set[int]) -> Iterator[TraceHit]:
    for start, end, frames in _frame_runs(lines, consumed, "#", _c_frame, _C_SKIP):
        header = _header_before(lines, consumed, start, _C_HEADER, max_back=6)
        first = header[0] if header else start
        error = _c_error(header[1]) if header else (None, None)
        trace = StackTrace(
            language="c",
            frames=frames,
            error_type=error[0],
            error_message=error[1],
            raw=_raw(lines, first, end),
        )
        yield first, end, trace


# Earlier parsers claim their lines first: the JavaScript `at path:line:col`
# shape is generic enough to also match Rust backtrace lines.
_TRACE_PARSERS: tuple[tuple[str, Callable[[list[str], set[int]], Iterator[TraceHit]]], ...] = (
    ('File "', _python_traces),
    ("panicked at", _rust_traces),
    ("goroutine ", _go_traces),
    ("at ", _java_traces),
    ("#", _c_traces),
    ("at ", _js_traces),
)


def _find_traces(text: str) -> list[StackTrace]:
    lines = [
        _QUOTE_PREFIX.sub("", ln) if ln.lstrip().startswith(">") else ln for ln in text.split("\n")
    ]
    consumed: set[int] = set()
    found: list[tuple[int, StackTrace]] = []
    for trigger, parser in _TRACE_PARSERS:
        if trigger not in text and not (parser is _python_traces and _PY_HEADER in text):
            continue
        for start, end, trace in parser(lines, consumed):
            consumed.update(range(start, end))
            found.append((start, trace))
    found.sort(key=lambda hit: hit[0])
    return [trace for _, trace in found[:MAX_TRACES]]


# ----------------------------------------------------------------- entrypoint


def extract_artifacts(text: str) -> Artifacts:
    """All checkable references in an issue or PR text, deduplicated in order of appearance.

    Only the first MAX_CHARS characters are read, so a pathological body
    cannot stall the pipeline.
    """
    text = _normalize(text)
    if not text.strip():
        return Artifacts()
    fenced = _find_fences(text)
    prose = _mask(text, ((f.start, f.end) for f in fenced))
    inline = _find_inline(prose)
    urls = _find_urls(text)
    url_spans = [(start, end) for start, end, _ in urls]
    file_refs, file_spans = _find_file_refs(_mask(text, url_spans), urls)
    source_spans = [
        (f.content_start, f.content_start + len(f.block.content))
        for f in fenced
        if f.block.language in _SOURCE_LANGUAGES
    ]
    return Artifacts(
        code_blocks=[f.block for f in fenced],
        inline_code=_unique((span.content.strip() for span in inline), MAX_INLINE_CODE),
        file_refs=file_refs,
        symbols=_find_symbols(_mask(prose, url_spans + file_spans), inline, fenced),
        stack_traces=_find_traces(text),
        error_messages=_find_errors(text),
        versions=_find_versions(_mask(text, url_spans + file_spans + source_spans)),
        cli_flags=_find_flags(inline, fenced),
        urls=_unique((url for _, _, url in urls), MAX_ITEMS),
        issue_refs=_find_issue_refs(_mask(prose, url_spans + [(s.start, s.end) for s in inline])),
        cve_ids=_find_cves(text),
    )
