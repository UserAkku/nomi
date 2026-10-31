"""Check that a stack trace quoted in a report could have come from this code.

A real traceback is a fingerprint of the code that produced it: every frame
inside the project names a file, a line, and the function that line sits in.
Invented traces, and traces pasted from another project or version, cite
files that do not exist, lines past the end of a file, or lines that live in
a different function. Frames outside the repository (standard library,
dependencies, the reporter's own scripts) say nothing about this project and
are ignored rather than held against the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from nomi.models import Artifacts, Check, CheckStatus, Evidence, Signal, StackFrame, StackTrace
from nomi.repo.symbols import defined_names, enclosing_function
from nomi.repo.workspace import Workspace
from nomi.verify.grounding import (
    at_commit,
    did_you_mean,
    file_evidence,
    line_evidence,
    project_path_strength,
)

_MAX_EVIDENCE = 8
_NAME = re.compile(r"[^\W\d][\w$]*[?!]?")


@dataclass(frozen=True)
class _FrameResult:
    frame: StackFrame
    path: str
    ok: bool
    function_checked: bool
    reason: str
    evidence: Evidence


def check_stack_traces(artifacts: Artifacts, ws: Workspace) -> list[Check]:
    """One `trace.frames` check per distinct stack trace in the report."""
    checks: list[Check] = []
    seen: set[tuple[tuple[str, int | None, str | None], ...]] = set()
    for trace in artifacts.stack_traces:
        key = tuple((f.path, f.line, f.function) for f in trace.frames)
        if not key or key in seen:
            continue
        seen.add(key)
        checks.append(_check_trace(trace, ws))
    return checks


def frame_function(name: str | None) -> str | None:
    """Comparable function name of a frame: its last component, `<module>`, or None.

    None means the name cannot be compared with the code (anonymous functions,
    closures, compiler-generated names such as `lambda$main$0` or `<init>`).
    """
    if not name or not name.strip():
        return None
    text = name.strip()
    if text == "<module>":
        return text
    if text.startswith("<") or "{" in text or "lambda$" in text:
        return None
    text = re.sub(r"^(?:async\s+|new\s+|\*)", "", text)
    text = re.sub(r"\([^()]*\)\s*$", "", text)
    if re.search(r"(?:\.|::)<[^<>]*>$", text):
        return None
    text = re.sub(r"<[^<>]*>", "", text)
    pieces = [p for p in re.split(r"\.|::|#|/", text) if p.strip("()*& ")]
    last = pieces[-1].strip("()*& ") if pieces else ""
    if not _NAME.fullmatch(last) or re.search(r"\$\d*$", last) or last == "anonymous":
        return None
    return last


def _check_trace(trace: StackTrace, ws: Workspace) -> Check:
    results: list[_FrameResult] = []
    missing: list[StackFrame] = []
    unlined = 0
    for frame in trace.frames:
        resolved = ws.resolve(frame.path)
        if resolved is None:
            if _missing_project_file(frame.path, ws):
                missing.append(frame)
            continue
        result = _check_frame(ws, frame, resolved)
        if result is None:
            unlined += 1
        else:
            results.append(result)
    good = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    if (len(bad) >= 2 and len(bad) > len(good)) or missing:
        return _contradicting(ws, good, bad, missing)
    if bad:
        return Check(
            id="trace.frames",
            title=(
                f"Stack trace partly matches the code {at_commit(ws)}: {len(bad)} of "
                f"{len(results)} frames disagree (the reporter may run a different version)"
            ),
            status=CheckStatus.WARN,
            signal=Signal.CONTRADICTS,
            strength=0.4,
            detail=_mismatch_detail(bad),
            evidence=_evidence(bad, good),
        )
    if good:
        named = any(r.function_checked for r in good)
        where = "real lines inside the named functions" if named else "real lines"
        noun = "frame points" if len(good) == 1 else "frames point"
        return Check(
            id="trace.frames",
            title=f"Stack trace matches the code {at_commit(ws)}: {len(good)} {noun} at {where}",
            status=CheckStatus.PASS,
            signal=Signal.SUPPORTS,
            strength=min(0.6, 0.25 + 0.1 * len(good)),
            evidence=_evidence([], good),
        )
    if unlined:
        return Check(
            id="trace.frames",
            title=f"Stack trace names {unlined} repository file(s) but no line numbers to check",
            status=CheckStatus.INFO,
            signal=Signal.NEUTRAL,
        )
    return Check(
        id="trace.frames",
        title="Stack trace only touches code outside this repository",
        status=CheckStatus.INFO,
        signal=Signal.NEUTRAL,
        detail="None of its frames point at a file of this repository, so it cannot be checked.",
    )


def _missing_project_file(path: str, ws: Workspace) -> bool:
    """A frame path inside the project's own directories whose file name exists nowhere."""
    return not ws.candidates(path) and project_path_strength(path, ws) == "strong"


def _check_frame(ws: Workspace, frame: StackFrame, path: str) -> _FrameResult | None:
    lines = ws.lines(path)
    if frame.line is None or frame.line <= 0 or lines is None:
        return None
    line, total = frame.line, len(lines)
    if line > total:
        tail = max(1, total - 2)
        evidence = (
            line_evidence(ws, path, tail, total, description=f"end of `{path}`")
            if total
            else file_evidence(ws, path, "empty file")
        )
        reason = f"`{path}` has {total:,} lines, but the frame cites line {line:,}"
        return _FrameResult(frame, path, False, False, reason, evidence)
    expected = frame_function(frame.function)
    actual = enclosing_function(ws, path, line)
    evidence = line_evidence(
        ws, path, line, context=1, description=f"frame in `{frame.function or '?'}`"
    )
    if expected is None or actual is None:
        return _FrameResult(frame, path, True, False, "", evidence)
    ok = _same_function(expected, actual) or _defines_here(lines[line - 1], path, expected)
    reason = "" if ok else f"line {line:,} of `{path}` is in `{actual}`, not `{frame.function}`"
    return _FrameResult(frame, path, ok, True, reason, evidence)


def _same_function(expected: str, actual: str) -> bool:
    if expected == "<module>" or actual == "<module>":
        return expected == actual
    return frame_function(actual) == expected


def _defines_here(text: str, path: str, expected: str) -> bool:
    """Some runtimes report a function's own `def`/header line for its frame."""
    return expected in defined_names(text, PurePosixPath(path).suffix.lstrip("."))


def _contradicting(
    ws: Workspace, good: list[_FrameResult], bad: list[_FrameResult], missing: list[StackFrame]
) -> Check:
    lines = [f"- `{f.path}` does not exist in the repository" for f in missing]
    evidence = _evidence(bad, good)
    if missing:
        evidence += [
            file_evidence(ws, s, f"closest existing file to `{missing[0].path}`")
            for s in did_you_mean(missing[0].path, ws)
        ]
    mismatch_fail = len(bad) >= 2 and len(bad) > len(good)
    if mismatch_fail:
        title = (
            f"Stack trace does not match the code {at_commit(ws)}: {len(bad)} of "
            f"{len(bad) + len(good)} frames point at the wrong lines or functions"
        )
    else:
        extra = f" (and {len(missing) - 1} more missing files)" if len(missing) > 1 else ""
        title = (
            f"Stack trace cites `{missing[0].path}`, which does not exist in the repository "
            f"{at_commit(ws)}{extra}"
        )
    detail = "\n".join([*lines, _mismatch_detail(bad)]).strip()
    return Check(
        id="trace.frames",
        title=title,
        status=CheckStatus.FAIL,
        signal=Signal.CONTRADICTS,
        strength=0.65 if mismatch_fail else 0.6,
        detail=detail,
        evidence=evidence,
    )


def _mismatch_detail(bad: list[_FrameResult]) -> str:
    return "\n".join(f"- {r.reason}" for r in bad)


def _evidence(bad: list[_FrameResult], good: list[_FrameResult]) -> list[Evidence]:
    return [r.evidence for r in [*bad, *good]][:_MAX_EVIDENCE]
