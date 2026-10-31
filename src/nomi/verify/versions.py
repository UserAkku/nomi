"""Check the versions a report mentions against the project's real releases.

Reporters state the version they run. A version that matches a release tag
is mild support, and newer releases at report time hint the bug may already
be fixed. A version newer than every release that existed when the report
was filed, in a release series the project actually ships, is a classic tell
of an invented report. Versions of other software in the same text (Python
3.12, Ubuntu 22.04, gcc 13.2) must never count against a report, so each
version is attributed from the words around it before it is judged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from nomi.models import Artifacts, Check, CheckStatus, Evidence, EvidenceKind, Signal
from nomi.repo.workspace import Workspace, classify_path

_SUFFIX = r"(?:[-.]?(?:rc|alpha|beta|dev|pre|post|snapshot|a|b)[.\-_]?\d*)?"
_TAG = re.compile(rf"(?:^|[-_/v])(\d+(?:[._]\d+)+)({_SUFFIX})$", re.I)
_MENTION = re.compile(rf"(?<![\w.])[vV]?(\d+(?:\.\d+)+)({_SUFFIX})(?![\w]|\.\d)", re.I)
_WORD = re.compile(r"[A-Za-z][\w+-]*")
_VERSION_WORD = re.compile(r"\bversions?\b", re.I)
_OF_FOR = re.compile(r"^\s*(?:of|for)\s+([A-Za-z][\w+-]*)", re.I)
_PAD = 6
_WINDOW = 40
# `kombu:5.0.2`, `kombu==5.0.2`, `py: 3.8.5` and `billiard 3.6.3` name their package
# right before the number (as in `celery report` / `pip freeze` output). The spaced form
# also keeps the word before it, so `celery multi v5.0.1` stays the project's version.
_KEYED_OWNER = re.compile(r"([A-Za-z][\w.+-]*) ?(?:==|[:=]) ?$")
_SPACED_OWNER = re.compile(r"(?:([A-Za-z][\w.+-]*) )?([A-Za-z][\w.+-]*) $")


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_OTHER_PRODUCTS = _words(
    """
    python py cpython pypy node nodejs npm yarn pnpm deno bun ubuntu debian fedora centos rhel
    alpine arch macos osx mac ios ipados android windows win linux kernel gcc clang llvm msvc
    java jdk jre openjdk go golang rust rustc cargo ruby rails php perl pip setuptools poetry
    uv conda docker kubernetes k8s chrome chromium firefox safari edge openssl libressl glibc
    musl cmake make git bash zsh xcode vscode django flask numpy pandas react vue angular
    typescript tsc electron nginx apache postgres postgresql mysql sqlite redis mongodb cuda
    torch pytorch tensorflow curl libcurl wget sonoma ventura monterey
    """
)


# Words that sit right before a version number without naming a package.
_NOT_OWNERS = _words(
    """
    a an the this that it its is was are be been being on in at to of for from with and or
    but by as since after before until till into than then about around near between only
    even still also just now upgrade upgrading upgraded update updating updated downgrade
    downgrading downgraded install installing installed using use used uses running run runs
    version versions ver release released releases tag tagged build branch latest current
    newest old older new newer tested testing see fixed fix fixes broken breaks introduced
    v
    """
)


@dataclass(frozen=True)
class Version:
    """A release number: numeric core plus an optional pre-release suffix."""

    numbers: tuple[int, ...]
    suffix: str = ""

    @property
    def key(self) -> tuple[tuple[int, ...], int, str]:
        padded = (self.numbers + (0,) * _PAD)[:_PAD]
        return padded, 0 if self.suffix else 1, self.suffix

    @property
    def major(self) -> int:
        return self.numbers[0]

    def __str__(self) -> str:
        core = ".".join(map(str, self.numbers))
        return f"{core}-{self.suffix}" if self.suffix else core


def _version(core: str, suffix: str) -> Version | None:
    try:
        numbers = tuple(int(p) for p in re.split(r"[._]", core))
    except ValueError:
        return None
    return Version(numbers, re.sub(r"[^a-z0-9]", "", suffix.lower()))


def normalize_tag(tag: str) -> Version | None:
    """`v1.1.0` -> 1.1.0, `curl-8_4_0` -> 8.4.0, `release/2.0-rc1` -> 2.0-rc1; else None."""
    m = _TAG.search(tag.strip())
    return _version(m.group(1), m.group(2)) if m else None


def parse_version(text: str) -> Version | None:
    """First dotted version number in a mention such as `v1.2.3` or `widget 1.2`."""
    m = _MENTION.search(text or "")
    return _version(m.group(1), m.group(2)) if m else None


def check_versions(
    artifacts: Artifacts,
    ws: Workspace,
    *,
    text: str = "",
    project_name: str | None = None,
) -> list[Check]:
    """`version.release` checks for every version the report mentions."""
    releases = _releases(ws)
    if not releases:
        return []
    project = (project_name or (ws.repo or ws.root.name).split("/")[-1]).strip().lower()
    own = {project, *(p.lower() for p in ws.python_packages)} - {""}
    newest_tag, newest = releases[0]
    majors = {v.major for _, v in releases}
    checks: list[Check] = []
    matched: list[Version] = []
    for raw, version in _mentions(artifacts):
        if _other_package_pin(raw, version, text, project, own):
            continue
        attribution = _attribution(raw, version, text, project)
        tag = _matching_tag(version, releases)
        if tag is not None:
            if attribution != "other":
                checks.append(_real_release(ws, version, tag))
                matched.append(version)
        elif (
            attribution in ("project", "version_word")
            and not version.suffix
            and version.major in majors
            and version.key > newest.key
            and not _in_repository(ws, version)
        ):
            checks.append(_invented_release(ws, version, newest_tag, newest))
    if matched:
        newer = _newer_releases(releases, max(matched, key=lambda v: v.key))
        if newer:
            checks.append(_newer_info(newer))
    return checks


def _releases(ws: Workspace) -> list[tuple[str, Version]]:
    """Version-like tags, newest first (ties broken by tag name for determinism)."""
    parsed = [(tag, v) for tag in ws.tags if (v := normalize_tag(tag)) is not None]
    return sorted(parsed, key=lambda tv: (tv[1].key, tv[0]), reverse=True)


def _mentions(artifacts: Artifacts) -> list[tuple[str, Version]]:
    found: dict[tuple[tuple[int, ...], int, str], tuple[str, Version]] = {}
    for raw in artifacts.versions:
        version = parse_version(raw)
        if version is not None:
            found.setdefault(version.key, (raw, version))
    return list(found.values())


def _matching_tag(version: Version, releases: list[tuple[str, Version]]) -> str | None:
    for tag, release in releases:
        if release.key == version.key:
            return tag
    return None


def _is_project(word: str, project: str) -> bool:
    return bool(project) and word in (project, f"lib{project}", f"py{project}", f"{project}-cli")


def _word_list(text: str) -> list[tuple[int, str]]:
    return [(m.start(), re.sub(r"[\d.+-]+$", "", m.group().lower())) for m in _WORD.finditer(text)]


def _attribution(raw: str, version: Version, text: str, project: str) -> str:
    """'project', 'other', 'version_word' or 'none': which software a version belongs to."""
    core = ".".join(map(str, version.numbers))
    words = [w for _, w in _word_list(raw.replace(core, " "))]
    if any(_is_project(w, project) for w in words):
        return "project"
    if any(w in _OTHER_PRODUCTS for w in words):
        return "other"
    verdicts = {_window_attribution(text, m, project) for m in _occurrences(core, text)}
    for verdict in ("project", "other", "version_word"):
        if verdict in verdicts:
            return verdict
    return "none"


def _immediate_owner(prefix: str) -> tuple[str, ...]:
    """Package names right before a version (`kombu:`, `kombu==`, `celery multi `), or ()."""
    keyed = _KEYED_OWNER.search(prefix)
    spaced = None if keyed else _SPACED_OWNER.search(prefix)
    names = [keyed.group(1)] if keyed else [spaced.group(2), spaced.group(1)] if spaced else []
    clean = tuple(n.lower().rstrip(".+-") for n in names if n)
    return () if not clean or not clean[0] or clean[0] in _NOT_OWNERS else clean


def _other_package_pin(raw: str, version: Version, text: str, project: str, own: set[str]) -> bool:
    """True when the version is pinned to a named package that is not this project."""
    owners = []
    m = _MENTION.search(raw)
    if m:
        owners.append(_immediate_owner(raw[: m.start()]))
    core = ".".join(map(str, version.numbers))
    for occ in _occurrences(core, text):
        owners.append(_immediate_owner(text[max(0, occ.start() - _WINDOW) : occ.start()]))
    named = [o for o in owners if o]
    return bool(named) and not any(
        _is_project(n, project) or n in own or n.replace("-", "_") in own
        for names in named
        for n in names
    )


def _occurrences(core: str, text: str) -> list[re.Match[str]]:
    if not text:
        return []
    rx = re.compile(rf"(?<![\w.])[vV]?{re.escape(core)}(?!\d|\.\d)")
    return list(rx.finditer(text))


def _window_attribution(text: str, match: re.Match[str], project: str) -> str:
    before = text[max(0, match.start() - _WINDOW) : match.start()]
    after = _OF_FOR.match(text[match.end() : match.end() + 25])
    if after:
        owner = after.group(1).lower()
        if _is_project(owner, project):
            return "project"
        if owner in _OTHER_PRODUCTS:
            return "other"
    words = _word_list(before)
    last_project = max((p for p, w in words if _is_project(w, project)), default=-1)
    last_other = max(
        (p for p, w in words if w in _OTHER_PRODUCTS and not _is_project(w, project)), default=-1
    )
    if last_project > last_other:
        return "project"
    if last_other >= 0:
        return "other"
    if _VERSION_WORD.search(before) or match.group().lower().startswith("v"):
        return "version_word"
    return "none"


def _in_repository(ws: Workspace, version: Version) -> bool:
    """An unreleased number already in the tree (pyproject, changelog) is a dev build."""
    core = re.escape(".".join(map(str, version.numbers)))
    hits = ws.grep(rf"(?<![\w.]){core}(?![\w]|\.\d)", fixed=False, max_hits=20)
    return any(classify_path(h.path) != "test" for h in hits)


def _newer_releases(releases: list[tuple[str, Version]], version: Version) -> list[Version]:
    newer = {str(v): v for _, v in releases if not v.suffix and v.key > version.key}
    return sorted(newer.values(), key=lambda v: v.key, reverse=True)


def _tag_evidence(ws: Workspace, tag: str) -> Evidence:
    url = f"https://github.com/{ws.repo}/releases/tag/{quote(tag)}" if ws.repo else None
    return Evidence(kind=EvidenceKind.HISTORY, description=f"release tag `{tag}`", url=url)


def _real_release(ws: Workspace, version: Version, tag: str) -> Check:
    return Check(
        id="version.release",
        title=f"{version} is a real release (tag {tag})",
        status=CheckStatus.PASS,
        signal=Signal.SUPPORTS,
        strength=0.1,
        evidence=[_tag_evidence(ws, tag)],
    )


def _newer_info(newer: list[Version]) -> Check:
    count = len(newer)
    noun = "release" if count == 1 else "releases"
    return Check(
        id="version.release",
        title=(
            f"{count} newer {noun} existed when this was reported (latest {newer[0]}); "
            "it may already be fixed"
        ),
        status=CheckStatus.INFO,
        signal=Signal.NEUTRAL,
    )


def _invented_release(ws: Workspace, version: Version, newest_tag: str, newest: Version) -> Check:
    return Check(
        id="version.release",
        title=f"Version {version} does not exist (newest release at that commit: {newest})",
        status=CheckStatus.WARN,
        signal=Signal.CONTRADICTS,
        strength=0.3,
        detail=(
            "No tag matches it, it is newer than every release reachable from the analysed "
            "commit, and it does not appear anywhere in the source tree."
        ),
        evidence=[_tag_evidence(ws, newest_tag)],
    )
