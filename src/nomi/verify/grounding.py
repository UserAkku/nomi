"""Ground a report's concrete references in the repository it was filed against.

Fabricated reports are full of plausible but unreal specifics: a file that
never existed, a line number past the end of the file, a function nobody
wrote, a command-line flag the tool does not have. Genuine reports cite
things that are really there. Each check here turns one such reference into
a `Check` whose evidence is a real line of the pinned workspace.

Precision beats recall throughout. Only a reference that is clearly
attributed to this project and is absent everywhere becomes a strong
CONTRADICTS signal; anything that could come from a dependency, the OS or
the reporter's own machine is reported as neutral information instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import PurePosixPath

from rapidfuzz import process

from nomi.models import (
    Artifacts,
    Check,
    CheckStatus,
    Claim,
    Evidence,
    EvidenceKind,
    FileRef,
    Signal,
)
from nomi.repo.symbols import (
    find_definitions,
    find_mentions,
    is_external_symbol,
    symbol_parts,
)
from nomi.repo.workspace import GrepHit, Workspace, classify_path, normalize_path

_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_EXTERNAL_MARKERS = (
    "/usr/",
    "/opt/",
    "/library/",
    "/system/",
    "/nix/store/",
    "/.cargo/registry/",
    "/go/pkg/mod/",
    "/rustc/",
    "/proc/",
    "/dev/",
    "/etc/",
    "/var/",
    "/applications/",
    "/program files",
    "/windows/",
    "/.rustup/",
    "/.npm/",
    "/.m2/",
    "/.gradle/",
)
_PACKAGE_DIRS = ("site-packages", "dist-packages")


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_USER_FILE_STEMS = _words(
    """
    test tests main repro reproduce reproduction example examples demo script app index foo
    bar baz temp tmp sample bug issue poc exploit run setup code snippet playground debug
    untitled a b t x y z input output data config settings
    """
)
_FILE_EXTENSIONS = _words(
    """
    py pyi c h cc cpp cxx hpp hh js mjs cjs ts tsx jsx go rs java kt kts rb php md rst txt
    json yaml yml toml cfg ini sh bash zsh html htm css scss xml lock log csv sql swift cs
    vue scala lua pl r m mm conf env gradle bat ps1 in out zip gz tar png jpg jpeg gif svg
    pdf
    """
)
# CamelCase alone is not enough: "GitHub" or "macOS" in prose must not become symbols.
_CODE_SHAPED = re.compile(r"_|\.|::|#|->|\(\)$")
_LONG_FLAG = re.compile(r"^--[A-Za-z0-9][\w-]*$")
_CONFIGURE_FLAG = re.compile(r"^--(?:with|without|enable|disable)-")
_ANY_LONG_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9]+(?:-[a-z0-9]+)*(?![\w-])")
_ERROR_PREFIX = re.compile(r"^\s*[A-Za-z_][\w.$-]{0,60}\s*:\s+")
_DYNAMIC = re.compile(
    r"'[^']*'|\"[^\"]*\"|`[^`]*`|“[^”]*”|‘[^’]*’"
    r"|\b[a-z][a-z0-9+.-]*://\S+"
    r"|\S*[/\\]\S*"
    r"|\b0x[0-9a-fA-F]+\b|\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{7,}\b"
    r"|\b[\w-]+(?:\.[\w-]+)+\b"
    r"|[-+]?\b\d+(?:[.,:]\d+)*\b"
    r"|<[^>]*>|\{[^}]*\}|\[[^\]]*\]|\([^)]*\)"
)
_PATH_TOKEN = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,5}\b")
_MIN_FRAGMENT = 12
_MAX_EXCERPT_LINES = 40

_EVIDENCE_KIND = {"code": EvidenceKind.CODE, "test": EvidenceKind.TEST, "doc": EvidenceKind.DOC}


# ------------------------------------------------------------------ shared helpers


def at_commit(ws: Workspace) -> str:
    """'at `abc1234`' for titles, or a neutral phrase outside git."""
    return f"at `{ws.commit[:7]}`" if ws.commit else "in the analysed checkout"


def line_evidence(
    ws: Workspace,
    path: str,
    start: int,
    end: int | None = None,
    *,
    context: int = 0,
    description: str = "",
) -> Evidence:
    """Evidence pointing at real lines, with a numbered excerpt and a pinned permalink."""
    end = max(start, end or start)
    end = min(end, start + _MAX_EXCERPT_LINES - 1)
    return Evidence(
        kind=_EVIDENCE_KIND[classify_path(path)],
        description=description or f"{path}:{start}",
        path=path,
        line_start=start,
        line_end=end,
        excerpt=ws.excerpt(path, start, end, context=context),
        url=ws.permalink(path, start, end),
    )


def file_evidence(ws: Workspace, path: str, description: str) -> Evidence:
    return Evidence(
        kind=_EVIDENCE_KIND[classify_path(path)],
        description=description,
        path=path,
        url=ws.permalink(path),
    )


def claim_ids_for(
    claims: Iterable[Claim], needles: Iterable[str], *, word: bool = False
) -> list[str]:
    """Ids of the claims whose text, quote or entities mention any needle."""
    terms = [n for n in dict.fromkeys(n.strip() for n in needles) if n]
    if not terms:
        return []
    if word:
        rxs = [re.compile(rf"(?<![\w$]){re.escape(t)}(?![\w$])") for t in terms]

        def hit(s: str) -> bool:
            return any(rx.search(s) for rx in rxs)
    else:

        def hit(s: str) -> bool:
            return any(t in s for t in terms)

    return [c.id for c in claims if hit(c.text) or hit(c.quote) or any(hit(e) for e in c.entities)]


def _clean(raw: str) -> str:
    return raw.strip().strip("`'\"()[],;").replace("\\", "/")


def _is_absolute(raw: str) -> bool:
    text = _clean(raw)
    return text.startswith(("/", "~")) or bool(_DRIVE.match(text))


@lru_cache(maxsize=8)
def _repo_layout(ws: Workspace) -> tuple[frozenset[str], frozenset[str], tuple[str, ...]]:
    """(top-level dirs, all dirs, top-5 extensions) of the workspace."""
    dirs: set[str] = set()
    for f in ws.files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    top = frozenset(d for d in dirs if "/" not in d)
    return top, frozenset(dirs), tuple(list(ws.language_mix())[:5])


def project_path_strength(raw: str, ws: Workspace) -> str | None:
    """How strongly a path mention points INTO this project: 'strong', 'weak' or None.

    'strong': a relative path under a real top-level directory or package of
    the repository, a site-packages path of the repository's own package, or
    an absolute path whose tail walks into a real repository directory.
    'weak': a relative path that merely has one of the repository's main file
    extensions. None: external (system, other libraries, URLs) or unknown.
    """
    text = raw.strip()
    if not text or text.startswith("<") or _URL.match(text):
        return None
    parts = [p for p in normalize_path(text).split("/") if p and p != "."]
    if not parts:
        return None
    packages = set(ws.python_packages)
    for marker in _PACKAGE_DIRS:
        if marker in parts:
            rest = parts[len(parts) - parts[::-1].index(marker) :]
            return "strong" if len(rest) >= 2 and rest[0] in packages else None
    if "node_modules" in parts:
        return None
    top, dirs, extensions = _repo_layout(ws)
    if _is_absolute(text):
        lowered = "/" + _clean(text).lower().lstrip("/") + "/"
        if any(m in lowered for m in _EXTERNAL_MARKERS):
            return None
        repo_name = (ws.repo or ws.root.name).split("/")[-1].lower()
        for i, head in enumerate(parts[:-1]):
            if head in packages:
                return "strong"
            if head in top and i + 2 < len(parts) and f"{head}/{parts[i + 1]}" in dirs:
                return "strong"
            if head in top and i > 0 and parts[i - 1].lower() == repo_name:
                return "strong"
        return None
    if len(parts) >= 2 and (parts[0] in top or parts[0] in packages):
        return "strong"
    if PurePosixPath(parts[-1]).suffix.lower() in extensions:
        return "weak"
    return None


def is_external_path(raw: str, ws: Workspace) -> bool:
    """URLs, interpreter pseudo-files and paths into system or third-party trees."""
    text = raw.strip()
    if not text or text.startswith("<") or _URL.match(text):
        return True
    parts = normalize_path(text).split("/")
    third_party = "node_modules" in parts or any(m in parts for m in _PACKAGE_DIRS)
    if third_party or _is_absolute(text):
        return project_path_strength(text, ws) is None
    return False


def did_you_mean(path: str, ws: Workspace, *, limit: int = 3) -> list[str]:
    """Repository files whose path is close to `path` (rapidfuzz score >= 80)."""
    if not path or not ws.files:
        return []
    found = process.extract(path, ws.files, limit=limit * 3, score_cutoff=80)
    ranked = sorted(((-score, choice) for choice, score, _ in found))
    return [choice for _, choice in ranked[:limit]]


def _user_file_like(path: str) -> bool:
    stem = PurePosixPath(path).stem.lower()
    return stem in _USER_FILE_STEMS or stem.startswith(("my", "test_", "repro", "tmp"))


def _code_hit_order(hit: GrepHit) -> tuple[int, str, int]:
    rank = {"code": 0, "doc": 1, "test": 2}[classify_path(hit.path)]
    return rank, hit.path, hit.line


# ---------------------------------------------------------------------- file refs


def check_file_refs(artifacts: Artifacts, claims: list[Claim], ws: Workspace) -> list[Check]:
    """Every cited file must exist, and every cited line must be inside it."""
    groups: dict[str, list[FileRef]] = {}
    for ref in artifacts.file_refs:
        key = normalize_path(ref.path)
        if key:
            groups.setdefault(key, []).append(ref)
    checks: list[Check] = []
    for key, refs in groups.items():
        needles = [key, *(r.path.strip() for r in refs), *(r.raw.strip() for r in refs)]
        ids = claim_ids_for(claims, needles)
        resolved = ws.resolve(key)
        if resolved:
            checks.append(_file_found(ws, key, resolved, ids))
            checks.extend(_line_checks(ws, resolved, refs, ids))
            continue
        missing = _file_missing(ws, refs[0].path, key, ids)
        if missing is not None:
            checks.append(missing)
    return checks


def _file_found(ws: Workspace, key: str, resolved: str, ids: list[str]) -> Check:
    alias = "" if key == resolved else f" (cited as `{key}`)"
    return Check(
        id="grounding.file",
        title=f"`{resolved}` exists in the repository{alias}",
        status=CheckStatus.PASS,
        signal=Signal.SUPPORTS,
        strength=0.15,
        claim_ids=ids,
        evidence=[file_evidence(ws, resolved, "file cited by the report")],
    )


def _line_checks(ws: Workspace, resolved: str, refs: list[FileRef], ids: list[str]) -> list[Check]:
    spans = dict.fromkeys(
        (r.line_start, r.line_end) for r in refs if r.line_start is not None and r.line_start > 0
    )
    if not spans:
        return []
    lines = ws.lines(resolved)
    checks: list[Check] = []
    for start, end in spans:
        if lines is None:
            checks.append(
                Check(
                    id="grounding.line",
                    title=f"Cannot check line {start} of `{resolved}`",
                    status=CheckStatus.SKIPPED,
                    detail="The file is binary or too large to read.",
                    claim_ids=ids,
                )
            )
            continue
        checks.append(_line_check(ws, resolved, start, end, len(lines), ids))
    return checks


def _line_check(
    ws: Workspace, path: str, start: int, end: int | None, total: int, ids: list[str]
) -> Check:
    last = max(start, end or start)
    cited = f"line {start:,}" if last == start else f"lines {start:,}-{last:,}"
    if last <= total:
        return Check(
            id="grounding.line",
            title=f"The cited {cited} of `{path}` {'exists' if last == start else 'exist'} "
            f"({total:,} lines in the file)",
            status=CheckStatus.PASS,
            signal=Signal.SUPPORTS,
            strength=0.2,
            claim_ids=ids,
            evidence=[
                line_evidence(ws, path, start, last, context=2, description=f"cited {cited}")
            ],
        )
    tail = max(1, total - 2)
    evidence = (
        [line_evidence(ws, path, tail, total, description=f"end of file (line {total:,})")]
        if total
        else [file_evidence(ws, path, "the file is empty")]
    )
    return Check(
        id="grounding.line",
        title=f"`{path}` has {total:,} lines, but the report cites line {last:,}",
        status=CheckStatus.FAIL,
        signal=Signal.CONTRADICTS,
        strength=0.75,
        detail=(
            f"The cited {cited} lies past the end of the file {at_commit(ws)}. Either the "
            "line number is invented or the report refers to a very different version."
        ),
        claim_ids=ids,
        evidence=evidence,
    )


def _file_missing(ws: Workspace, raw: str, key: str, ids: list[str]) -> Check | None:
    if is_external_path(raw, ws):
        return None
    candidates = ws.candidates(key)
    if candidates:
        ranked = [c for c, _, _ in process.extract(key, candidates, limit=3)]
        listed = ", ".join(f"`{c}`" for c in ranked)
        return Check(
            id="grounding.file",
            title=f"`{key}` is ambiguous or in a different directory (repository has {listed})",
            status=CheckStatus.WARN,
            signal=Signal.NEUTRAL,
            strength=0.1,
            claim_ids=ids,
            evidence=[file_evidence(ws, c, "file with the same name") for c in ranked],
        )
    strength = project_path_strength(raw, ws)
    suggestions = did_you_mean(key, ws)
    hint = (
        (" Did you mean " + ", ".join(f"`{s}`" for s in suggestions) + "?") if suggestions else ""
    )
    evidence = [file_evidence(ws, s, "similarly named file") for s in suggestions]
    if strength == "strong":
        return Check(
            id="grounding.file",
            title=f"No file `{key}` exists in the repository {at_commit(ws)}",
            status=CheckStatus.FAIL,
            signal=Signal.CONTRADICTS,
            strength=0.7,
            detail="The path points into this project's own directories." + hint,
            claim_ids=ids,
            evidence=evidence,
        )
    if strength == "weak" and not _user_file_like(key):
        return Check(
            id="grounding.file",
            title=f"No file `{key}` exists in the repository {at_commit(ws)}",
            status=CheckStatus.WARN,
            signal=Signal.CONTRADICTS,
            strength=0.3,
            detail="It may also be a file on the reporter's machine." + hint,
            claim_ids=ids,
            evidence=evidence,
        )
    return Check(
        id="grounding.file",
        title=f"`{key}` is not part of this repository (likely the reporter's own file)",
        status=CheckStatus.INFO,
        signal=Signal.NEUTRAL,
        claim_ids=ids,
    )


# ------------------------------------------------------------------------ symbols


def _symbol_candidates(artifacts: Artifacts, claims: list[Claim], ws: Workspace) -> list[str]:
    trusted = {s.strip().strip("`") for s in [*artifacts.symbols, *artifacts.inline_code]}
    raw = list(artifacts.symbols)
    for claim in claims:
        raw.extend(
            e for e in claim.entities if e.strip().strip("`") in trusted or _CODE_SHAPED.search(e)
        )
    chosen: dict[tuple[str, ...], str] = {}
    for item in raw:
        name = re.sub(r"\([^()]*\)$", "", item.strip().strip("`'\""))
        parts = symbol_parts(name)
        if not parts or tuple(parts) in chosen:
            continue
        if len(parts) > 1 and parts[-1].lower() in _FILE_EXTENSIONS:
            continue
        if len(parts) == 1 and len(name) < 4:
            continue
        if is_external_symbol(name, ws):
            continue
        chosen[tuple(parts)] = name
    return list(chosen.values())


def check_symbols(artifacts: Artifacts, claims: list[Claim], ws: Workspace) -> list[Check]:
    """Named functions/classes must be defined (or at least used) in the repository."""
    return [
        _check_symbol(name, artifacts, claims, ws)
        for name in _symbol_candidates(artifacts, claims, ws)
    ]


def _check_symbol(name: str, artifacts: Artifacts, claims: list[Claim], ws: Workspace) -> Check:
    parts = symbol_parts(name)
    ids = claim_ids_for(claims, [name, ".".join(parts)], word=True)
    definitions = find_definitions(ws, name, max_hits=5)
    if definitions:
        first = definitions[0]
        more = f" (and {len(definitions) - 1} more)" if len(definitions) > 1 else ""
        return Check(
            id="grounding.symbol",
            title=f"`{name}` is defined at `{first.path}:{first.line}`{more}",
            status=CheckStatus.PASS,
            signal=Signal.SUPPORTS,
            strength=0.15,
            claim_ids=ids,
            evidence=[
                line_evidence(ws, d.path, d.line, description=f"{d.kind} definition")
                for d in definitions[:3]
            ],
        )
    mentions = find_mentions(ws, name, max_hits=5)
    if mentions:
        first_hit = mentions[0]
        return Check(
            id="grounding.symbol",
            title=(
                f"`{name}` is referenced (e.g. `{first_hit.path}:{first_hit.line}`) but not "
                "defined in the repository"
            ),
            status=CheckStatus.INFO,
            signal=Signal.NEUTRAL,
            strength=0.05,
            detail="It is probably defined by a dependency, generated code or a macro.",
            claim_ids=ids,
            evidence=[
                line_evidence(ws, h.path, h.line, description="reference") for h in mentions[:3]
            ],
        )
    attribution = _project_attribution(name, parts, artifacts, claims, ws)
    if attribution is not None:
        reason, path = attribution
        return Check(
            id="grounding.symbol",
            title=f"`{name}` is not defined or referenced anywhere in the repository {at_commit(ws)}",
            status=CheckStatus.FAIL,
            signal=Signal.CONTRADICTS,
            strength=0.8,
            detail=f"The report attributes it to this project: {reason}.",
            claim_ids=ids,
            evidence=[file_evidence(ws, path, f"does not contain `{parts[-1]}`")] if path else [],
        )
    return Check(
        id="grounding.symbol",
        title=f"`{name}` was not found in the repository {at_commit(ws)}; it may come from a dependency",
        status=CheckStatus.WARN,
        signal=Signal.CONTRADICTS,
        strength=0.3,
        claim_ids=ids,
    )


def _project_attribution(
    name: str, parts: list[str], artifacts: Artifacts, claims: list[Claim], ws: Workspace
) -> tuple[str, str | None] | None:
    """Why the report ties `name` to this project, plus a related real file if any."""
    if len(parts) > 1 and parts[0] in ws.python_packages:
        return f"`{parts[0]}` is this repository's own package", None
    if len(parts) > 1:
        owners = [
            d for d in find_definitions(ws, parts[-2], max_hits=3) if d.kind in ("class", "type")
        ]
        if owners:
            owner = owners[0]
            return f"`{parts[-2]}` is defined at `{owner.path}:{owner.line}`", owner.path
    for claim in claims:
        if not claim_ids_for([claim], [name, ".".join(parts)], word=True):
            continue
        for mention in _paths_in_claim(claim, artifacts):
            resolved = ws.resolve(mention)
            if resolved:
                return f"the same claim cites `{resolved}`, which exists", resolved
    for trace in artifacts.stack_traces:
        for frame in trace.frames:
            if frame.function and _last_component(frame.function) == parts[-1]:
                resolved = ws.resolve(frame.path)
                if resolved:
                    return f"a stack trace places it in `{resolved}`", resolved
    return None


def _paths_in_claim(claim: Claim, artifacts: Artifacts) -> list[str]:
    found = [e for e in claim.entities if "/" in e or _PATH_TOKEN.fullmatch(e.strip())]
    for ref in artifacts.file_refs:
        needles = [n for n in (ref.path.strip(), ref.raw.strip()) if n]
        if any(n in s for n in needles for s in (claim.text, claim.quote)):
            found.append(ref.path)
    for text in (claim.text, claim.quote):
        found.extend(_PATH_TOKEN.findall(text))
    return [p for p in dict.fromkeys(found) if p.strip()]


def _last_component(function: str) -> str:
    text = re.sub(r"\(.*\)$", "", function.strip())
    pieces = [p for p in re.split(r"\.|::|#|/", text) if p]
    return pieces[-1] if pieces else text


# -------------------------------------------------------------------- CLI flags


def check_cli_flags(artifacts: Artifacts, claims: list[Claim], ws: Workspace) -> list[Check]:
    """Cited long options must exist in the code or documentation."""
    flags = list(dict.fromkeys(f for f in map(_normalize_flag, artifacts.cli_flags) if f))
    found = [(flag, _find_flag_text(ws, flag)) for flag in flags]
    lookups = [(flag, hit, None if hit else _flag_identifier(ws, flag)) for flag, hit in found]
    needs_other = any(hit is None and ident is None for _, hit, ident in lookups)
    other = _other_long_flag(ws) if needs_other else None
    return [
        _flag_check(flag, hit, ident, other, claim_ids_for(claims, [flag]), ws)
        for flag, hit, ident in lookups
    ]


def _normalize_flag(raw: str) -> str | None:
    flag = raw.strip().strip("`'\",;:.()[]").split("=", 1)[0]
    return flag if _LONG_FLAG.match(flag) and len(flag) >= 4 else None


def _flag_check(
    flag: str,
    hit: GrepHit | None,
    ident: GrepHit | None,
    other: GrepHit | None,
    ids: list[str],
    ws: Workspace,
) -> Check:
    if hit is not None:
        return Check(
            id="grounding.flag",
            title=f"`{flag}` is defined or documented at `{hit.path}:{hit.line}`",
            status=CheckStatus.PASS,
            signal=Signal.SUPPORTS,
            strength=0.1,
            claim_ids=ids,
            evidence=[line_evidence(ws, hit.path, hit.line, description=f"`{flag}`")],
        )
    if ident is not None:
        return Check(
            id="grounding.flag",
            title=f"`{flag}` is not spelled out, but `{flag[2:].replace('-', '_')}` appears at "
            f"`{ident.path}:{ident.line}`",
            status=CheckStatus.INFO,
            signal=Signal.NEUTRAL,
            detail="Options may be generated from parameter names (typer, clap, ...).",
            claim_ids=ids,
            evidence=[line_evidence(ws, ident.path, ident.line, description="matching identifier")],
        )
    if other is None or _CONFIGURE_FLAG.match(flag):
        return Check(
            id="grounding.flag",
            title=f"`{flag}` was not found in the repository",
            status=CheckStatus.INFO,
            signal=Signal.NEUTRAL,
            detail=(
                "The repository does not spell out its command-line options (or the option is "
                "generated by the build system), so this cannot be checked."
            ),
            claim_ids=ids,
        )
    return Check(
        id="grounding.flag",
        title=f"`{flag}` does not appear anywhere in the repository {at_commit(ws)}",
        status=CheckStatus.WARN,
        signal=Signal.CONTRADICTS,
        strength=0.35,
        detail="The repository does spell out other long options, e.g. the one shown as evidence.",
        claim_ids=ids,
        evidence=[
            line_evidence(ws, other.path, other.line, description="another documented option")
        ],
    )


def _find_flag_text(ws: Workspace, flag: str) -> GrepHit | None:
    names = [flag]
    if flag.startswith("--no-") and len(flag) > 6:
        names.append("--" + flag[5:])
    for name in names:
        hits = ws.grep(rf"(?<![\w-]){re.escape(name)}(?![\w-])", fixed=False, max_hits=20)
        if hits:
            return min(hits, key=_code_hit_order)
    bare = flag[2:]
    if len(bare) >= 3:
        hits = ws.grep(rf"[\"']{re.escape(bare)}[\"']", fixed=False, max_hits=20)
        hits = [h for h in hits if classify_path(h.path) != "test"]
        if hits:
            return min(hits, key=_code_hit_order)
    return None


def _flag_identifier(ws: Workspace, flag: str) -> GrepHit | None:
    ident = flag[2:].replace("-", "_")
    if len(ident) < 3:
        return None
    hits = ws.grep(ident, fixed=True, word=True, max_hits=50)
    code = [h for h in hits if classify_path(h.path) == "code"]
    return min(code, key=_code_hit_order) if code else None


def _other_long_flag(ws: Workspace) -> GrepHit | None:
    hits = ws.grep(_ANY_LONG_FLAG.pattern, fixed=False, max_hits=200)
    for hit in sorted(hits, key=_code_hit_order):
        if classify_path(hit.path) == "test":
            continue
        for m in _ANY_LONG_FLAG.finditer(hit.text):
            before = hit.text[m.start() - 1] if m.start() else ""
            if classify_path(hit.path) == "doc" or before in "\"'`":
                return hit
    return None


# ---------------------------------------------------------------- error messages


def error_fragments(message: str) -> list[str]:
    """Literal pieces (>= 12 chars) of an error message, without its dynamic parts."""
    core = message.strip().splitlines()[0] if message.strip() else ""
    for _ in range(2):
        core = _ERROR_PREFIX.sub("", core, count=1)
    pieces = (p.strip(" \t:;,.-!?=") for p in _DYNAMIC.split(core))
    fragments = [re.sub(r"\s+", " ", p) for p in pieces if len(p) >= _MIN_FRAGMENT]
    return sorted(dict.fromkeys(fragments), key=lambda f: (-len(f), f))[:5]


def check_error_messages(artifacts: Artifacts, claims: list[Claim], ws: Workspace) -> list[Check]:
    """Error text quoted in a report should be produced somewhere in the code."""
    messages = [*artifacts.error_messages]
    messages += [t.error_message for t in artifacts.stack_traces if t.error_message]
    checks: list[Check] = []
    seen: set[str] = set()
    for message in messages:
        text = message.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        checks.append(_check_error_message(text, claims, ws))
    return checks


def _check_error_message(message: str, claims: list[Claim], ws: Workspace) -> Check:
    fragments = error_fragments(message)
    ids = claim_ids_for(claims, [message, *fragments])
    shown = message if len(message) <= 80 else message[:77] + "..."
    if not fragments:
        return Check(
            id="grounding.error_message",
            title=f"Error text `{shown}` is too short or too dynamic to look up",
            status=CheckStatus.SKIPPED,
            detail=f"No literal fragment of {_MIN_FRAGMENT}+ characters remains after removing values.",
            claim_ids=ids,
        )
    for fragment in fragments:
        hits = [
            h for h in ws.grep(fragment, fixed=True, max_hits=50) if classify_path(h.path) == "code"
        ]
        if hits:
            hit = hits[0]
            return Check(
                id="grounding.error_message",
                title=f"Error text is produced by `{hit.path}:{hit.line}`",
                status=CheckStatus.PASS,
                signal=Signal.SUPPORTS,
                strength=0.35,
                detail=f"`{fragment}` appears verbatim in the code.",
                claim_ids=ids,
                evidence=[
                    line_evidence(
                        ws, hit.path, hit.line, context=1, description="error raised here"
                    )
                ],
            )
    return Check(
        id="grounding.error_message",
        title=f"Error text `{shown}` was not found in the repository's code",
        status=CheckStatus.INFO,
        signal=Signal.NEUTRAL,
        detail="It may come from a dependency, the runtime or the operating system.",
        claim_ids=ids,
    )


# ------------------------------------------------------------------------ all


def run_grounding_checks(artifacts: Artifacts, claims: list[Claim], ws: Workspace) -> list[Check]:
    """All grounding checks in a stable order: files, symbols, flags, error text."""
    return [
        *check_file_refs(artifacts, claims, ws),
        *check_symbols(artifacts, claims, ws),
        *check_cli_flags(artifacts, claims, ws),
        *check_error_messages(artifacts, claims, ws),
    ]
