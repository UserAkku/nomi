from __future__ import annotations

import pytest

from nomi.models import Artifacts, CheckStatus, Signal, StackFrame, StackTrace
from nomi.verify.grounding import at_commit
from nomi.verify.traces import check_stack_traces, frame_function

SITE = "/home/u/.venv/lib/python3.12/site-packages"


def frame(path: str, line: int | None, function: str | None) -> StackFrame:
    return StackFrame(path=path, line=line, function=function, raw=f"{path}:{line} {function}")


def trace(*frames: StackFrame, language: str = "python") -> Artifacts:
    return Artifacts(stack_traces=[StackTrace(language=language, frames=list(frames))])


def test_genuine_python_traceback_passes(sample_repo):
    art = trace(
        frame("repro.py", 3, "<module>"),
        frame(f"{SITE}/widget/net.py", 13, "fetch"),
        frame(f"{SITE}/widget/parser.py", 12, "parse_url"),
        frame("/usr/lib/python3.12/urllib/parse.py", 500, "urlsplit"),
    )
    (check,) = check_stack_traces(art, sample_repo)
    assert check.id == "trace.frames"
    assert (check.status, check.signal) == (CheckStatus.PASS, Signal.SUPPORTS)
    assert check.strength == pytest.approx(0.45)
    assert check.title == (
        f"Stack trace matches the code {at_commit(sample_repo)}: 2 frames point at real lines "
        "inside the named functions"
    )
    paths = [e.path for e in check.evidence]
    assert paths == ["src/widget/net.py", "src/widget/parser.py"]
    assert "12 |" in check.evidence[1].excerpt and check.evidence[1].url


def test_pass_strength_is_capped(sample_repo):
    frames = [frame("src/widget/parser.py", n, "parse_url") for n in range(7, 14)]
    (check,) = check_stack_traces(trace(*frames), sample_repo)
    assert check.status is CheckStatus.PASS and check.strength == 0.6


def test_module_level_and_unverifiable_frames(sample_repo):
    art = trace(
        frame("src/widget/net.py", 5, "<module>"),
        frame("src/widget/parser.py", 9, "<lambda>"),
        frame("src/widget/parser.py", 10, None),
        frame("src/widget/net.py", 10, "widget.net.Client.__init__"),
    )
    (check,) = check_stack_traces(art, sample_repo)
    assert check.status is CheckStatus.PASS


def test_one_wrong_function_warns(sample_repo):
    art = trace(
        frame("src/widget/net.py", 13, "fetch"),
        frame("src/widget/parser.py", 9, "parse_url"),
        frame("src/widget/parser.py", 17, "parse_url"),  # really normalize_host
    )
    (check,) = check_stack_traces(art, sample_repo)
    assert (check.status, check.signal, check.strength) == (
        CheckStatus.WARN,
        Signal.CONTRADICTS,
        0.4,
    )
    assert "is in `normalize_host`, not `parse_url`" in check.detail
    assert check.evidence[0].line_start == 17


def test_majority_inconsistent_fails(sample_repo):
    art = trace(
        frame("src/widget/net.py", 13, "fetch"),
        frame("src/widget/parser.py", 17, "parse_url"),
        frame("src/widget/parser.py", 240, "parse_url"),  # past EOF
    )
    (check,) = check_stack_traces(art, sample_repo)
    assert (check.status, check.strength) == (CheckStatus.FAIL, 0.65)
    assert "2 of 3 frames" in check.title
    assert "has 17 lines, but the frame cites line 240" in check.detail


def test_missing_project_file_fails(sample_repo):
    art = trace(
        frame(f"{SITE}/widget/net.py", 13, "fetch"),
        frame(f"{SITE}/widget/transport.py", 88, "send_request"),
    )
    (check,) = check_stack_traces(art, sample_repo)
    assert (check.status, check.signal, check.strength) == (
        CheckStatus.FAIL,
        Signal.CONTRADICTS,
        0.6,
    )
    assert "widget/transport.py" in check.title and "does not exist" in check.title


def test_ambiguous_paths_are_not_held_against_the_report(make_repo):
    ws = make_repo(
        {"a/util.py": "def f():\n    return 1\n", "b/util.py": "def g():\n    return 2\n"}
    )
    (check,) = check_stack_traces(trace(frame("util.py", 2, "f")), ws)
    assert check.status is CheckStatus.INFO and check.signal is Signal.NEUTRAL


def test_trace_outside_repository_is_info(sample_repo):
    art = trace(
        frame("/usr/lib/python3.12/json/decoder.py", 337, "decode"),
        frame("<frozen importlib._bootstrap>", 1, "_call_with_frames_removed"),
        frame("my_script.py", 4, "<module>"),
    )
    (check,) = check_stack_traces(art, sample_repo)
    assert check.status is CheckStatus.INFO and check.signal is Signal.NEUTRAL
    assert check.title == "Stack trace only touches code outside this repository"


def test_frames_without_lines_are_info(sample_repo):
    (check,) = check_stack_traces(trace(frame("src/widget/net.py", None, "fetch")), sample_repo)
    assert check.status is CheckStatus.INFO and "no line numbers" in check.title


def test_c_trace_against_c_repo(make_repo):
    ws = make_repo(
        {
            "lib/easy.c": (
                "#include <stdio.h>\n"
                "\n"
                "static int parse_header(const char *h)\n"
                "{\n"
                "  return h[0];\n"
                "}\n"
                "\n"
                "int curl_easy_perform(void *data)\n"
                "{\n"
                "  return parse_header(data);\n"
                "}\n"
            )
        },
        repo="curl/curl",
    )
    genuine = trace(
        frame("lib/easy.c", 5, "parse_header"),
        frame("/home/u/src/curl/lib/easy.c", 10, "curl_easy_perform"),
        language="c",
    )
    (ok,) = check_stack_traces(genuine, ws)
    assert ok.status is CheckStatus.PASS
    fabricated = trace(
        frame("lib/easy.c", 5, "Curl_header_overflow"),
        frame("lib/easy.c", 3412, "curl_easy_perform"),
        frame("lib/http_chunks.c", 77, "Curl_httpchunk_read"),
        language="c",
    )
    (bad,) = check_stack_traces(fabricated, ws)
    assert bad.status is CheckStatus.FAIL and bad.signal is Signal.CONTRADICTS
    assert bad.strength == 0.65


def test_duplicate_and_empty_traces(sample_repo):
    one = StackTrace(language="python", frames=[frame("src/widget/net.py", 13, "fetch")])
    art = Artifacts(stack_traces=[one, one.model_copy(), StackTrace(language="python")])
    assert len(check_stack_traces(art, sample_repo)) == 1
    assert check_stack_traces(Artifacts(), sample_repo) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("parse_url", "parse_url"),
        ("widget.net.Client.fetch", "fetch"),
        ("ns::Client::fetch(int)", "fetch"),
        ("main.(*Client).Fetch(0xc000010000)", "Fetch"),
        ("async Object.load", "load"),
        ("std::vector<int>::push_back", "push_back"),
        ("<module>", "<module>"),
        ("<lambda>", None),
        ("<anonymous>", None),
        ("lambda$main$0", None),
        ("access$000", None),
        ("{{closure}}", None),
        ("", None),
        (None, None),
        ("Object.<anonymous>", None),
    ],
)
def test_frame_function(raw, expected):
    assert frame_function(raw) == expected


# ---------------------------------------------- regression: false accusations


def test_bare_file_name_frames_never_fail(make_repo):
    # B3: `manage.py` in a traceback is usually the reporter's own script
    ws = make_repo({"example/manage.py": "import sys\n\n\ndef main():\n    return sys.argv\n"})
    art = trace(frame("manage.py", 22, "<module>"), frame("manage.py", 99, "main"))
    (check,) = check_stack_traces(art, ws)
    assert (check.status, check.signal, check.strength) == (CheckStatus.WARN, Signal.NEUTRAL, 0.1)
    assert "reporter's own files" in check.title


def test_installed_frames_from_another_release_only_warn(sample_repo):
    # B6: site-packages frames are checked against the default branch, not the user's release
    art = trace(
        frame("repro.py", 3, "<module>"),
        frame(f"{SITE}/widget/parser.py", 17, "parse_url"),  # normalize_host in this version
        frame(f"{SITE}/widget/parser.py", 240, "parse_url"),  # past EOF in this version
    )
    (check,) = check_stack_traces(art, sample_repo)
    assert (check.status, check.signal, check.strength) == (
        CheckStatus.WARN,
        Signal.CONTRADICTS,
        0.3,
    )
    assert "different release" in check.title


def test_reporter_paths_are_not_missing_project_files(sample_repo):
    # B1/B2: the reporter's own tests/ dir and home-directory checkout are not this project
    art = trace(
        frame("tests/test_downloads.py", 15, "test_download"),
        frame("/home/me/app/src/widget/helpers.py", 40, "get"),
    )
    (check,) = check_stack_traces(art, sample_repo)
    assert check.status is CheckStatus.INFO and check.signal is Signal.NEUTRAL
