from __future__ import annotations

import pytest

from nomi.models import (
    Artifacts,
    CheckStatus,
    Claim,
    EvidenceKind,
    FileRef,
    Signal,
    StackFrame,
    StackTrace,
)
from nomi.verify.grounding import (
    at_commit,
    check_cli_flags,
    check_error_messages,
    check_file_refs,
    check_symbols,
    claim_ids_for,
    did_you_mean,
    error_fragments,
    is_external_path,
    project_path_strength,
    run_grounding_checks,
)

EASY_C = "".join(f"/* line {i} */\n" for i in range(1, 1235))
CURL_FILES = {
    "lib/easy.c": EASY_C.replace("/* line 40 */", "CURLcode curl_easy_perform(CURL *data)"),
    "lib/vtls/openssl.c": "static int ossl_init(void)\n{\n  return 0;\n}\n",
    "lib/url.c": "int Curl_parse_url(void)\n{\n  return 0;\n}\n",
    "include/curl/curl.h": "CURLcode curl_easy_perform(CURL *data);\n",
    "src/tool_getparam.c": 'static const struct LongShort aliases[] = {\n  {"max-time", ARG_STRG},\n};\n',
    "docs/cmdline-opts/max-time.md": "Long: max-time\n\nUse `--max-time <secs>` to limit.\n",
}


def ref(path: str, line: int | None = None, end: int | None = None) -> FileRef:
    raw = path if line is None else f"{path}:{line}"
    return FileRef(path=path, line_start=line, line_end=end, raw=raw)


def by_id(checks, check_id):
    return [c for c in checks if c.id == check_id]


@pytest.fixture
def curl_repo(make_repo):
    return make_repo(CURL_FILES, repo="curl/curl", name="curl")


# ----------------------------------------------------------------- file refs


def test_existing_file_and_line(sample_repo):
    claims = [Claim(id="C1", text="parse_url in src/widget/parser.py:9 raises", entities=[])]
    art = Artifacts(file_refs=[ref("src/widget/parser.py", 9)])
    file_check, line_check = check_file_refs(art, claims, sample_repo)
    assert (file_check.id, file_check.status, file_check.signal) == (
        "grounding.file",
        CheckStatus.PASS,
        Signal.SUPPORTS,
    )
    assert file_check.strength == 0.15 and file_check.claim_ids == ["C1"]
    assert line_check.id == "grounding.line" and line_check.status is CheckStatus.PASS
    assert line_check.strength == 0.2
    ev = line_check.evidence[0]
    assert ev.kind is EvidenceKind.CODE and ev.line_start == 9
    assert '9 |         raise ValueError("empty url")' in ev.excerpt
    assert "7 |" in ev.excerpt and "11 |" in ev.excerpt  # +/- 2 lines of context
    assert ev.url.endswith("/src/widget/parser.py#L9") and sample_repo.commit in ev.url


def test_line_beyond_eof_is_contradiction(curl_repo):
    art = Artifacts(file_refs=[ref("lib/easy.c", 3412)])
    claims = [Claim(id="C1", text="overflow at lib/easy.c:3412", quote="lib/easy.c:3412")]
    _, line_check = check_file_refs(art, claims, curl_repo)
    assert line_check.status is CheckStatus.FAIL and line_check.signal is Signal.CONTRADICTS
    assert line_check.strength == 0.75
    assert line_check.title == "`lib/easy.c` has 1,234 lines, but the report cites line 3,412"
    assert line_check.claim_ids == ["C1"]
    ev = line_check.evidence[0]
    assert ev.line_end == 1234 and "1234 | /* line 1234 */" in ev.excerpt


def test_line_range_partly_beyond_eof(sample_repo):
    art = Artifacts(file_refs=[ref("src/widget/net.py", 10, 99)])
    _, line_check = check_file_refs(art, [], sample_repo)
    assert line_check.status is CheckStatus.FAIL and "cites line 99" in line_check.title


def test_duplicate_refs_are_grouped(sample_repo):
    art = Artifacts(
        file_refs=[
            ref("src/widget/net.py", 12),
            ref("./src/widget/net.py", 12),
            ref("src/widget/net.py", 13),
            ref("src/widget/net.py:0"),
        ]
    )
    checks = check_file_refs(art, [], sample_repo)
    assert [c.id for c in checks] == ["grounding.file", "grounding.line", "grounding.line"]


def test_fabricated_project_path(sample_repo):
    art = Artifacts(file_refs=[ref("src/widget/parsers.py", 3)])
    (check,) = check_file_refs(art, [], sample_repo)
    assert check.status is CheckStatus.FAIL and check.signal is Signal.CONTRADICTS
    assert check.strength == 0.7
    assert (
        check.title
        == f"No file `src/widget/parsers.py` exists in the repository {at_commit(sample_repo)}"
    )
    assert "src/widget/parser.py" in check.detail
    assert check.evidence and check.evidence[0].path == "src/widget/parser.py"


def test_unreadable_file_line_is_skipped(make_repo):
    ws = make_repo({"assets/blob.bin": "a\0b"})
    _, line_check = check_file_refs(Artifacts(file_refs=[ref("assets/blob.bin", 3)]), [], ws)
    assert line_check.status is CheckStatus.SKIPPED and line_check.signal is Signal.NEUTRAL


def test_ambiguous_or_moved_file(make_repo):
    ws = make_repo({"a/util.py": "x = 1\n", "b/util.py": "y = 2\n"})
    (check,) = check_file_refs(Artifacts(file_refs=[ref("util.py")]), [], ws)
    assert check.status is CheckStatus.WARN and check.signal is Signal.NEUTRAL
    assert check.strength == 0.1
    assert {e.path for e in check.evidence} == {"a/util.py", "b/util.py"}


EXTERNAL_PATHS = [
    "/usr/lib/python3.12/json/decoder.py",
    "/home/u/.venv/lib/python3.12/site-packages/requests/api.py",
    "node_modules/lodash/index.js",
    "C:\\Users\\me\\app\\main.py",
    "https://example.com/src/widget/x.py",
    "<frozen importlib._bootstrap>",
    "~/scratch/notes.py",
]


def test_external_paths_are_skipped(sample_repo):
    for path in EXTERNAL_PATHS:
        assert is_external_path(path, sample_repo), path
        assert check_file_refs(Artifacts(file_refs=[ref(path)]), [], sample_repo) == [], path


def test_site_packages_path_into_the_project(sample_repo):
    path = "/home/u/.venv/lib/python3.12/site-packages/widget/fabricated.py"
    assert not is_external_path(path, sample_repo)
    assert project_path_strength(path, sample_repo) == "strong"
    (check,) = check_file_refs(Artifacts(file_refs=[ref(path, 4)]), [], sample_repo)
    assert check.status is CheckStatus.FAIL


def test_user_files_are_not_accused(sample_repo):
    checks = check_file_refs(
        Artifacts(file_refs=[ref("repro.py"), ref("notes.cfg"), ref("helpers.py")]), [], sample_repo
    )
    statuses = {c.title.split("`")[1]: (c.status, c.signal) for c in checks}
    assert statuses["repro.py"] == (CheckStatus.INFO, Signal.NEUTRAL)
    assert statuses["notes.cfg"] == (CheckStatus.INFO, Signal.NEUTRAL)
    # a bare project-language file name is only weak evidence
    assert statuses["helpers.py"] == (CheckStatus.WARN, Signal.CONTRADICTS)
    assert next(c for c in checks if "helpers.py" in c.title).strength == 0.3


def test_project_path_strength(curl_repo):
    assert project_path_strength("lib/fake.c", curl_repo) == "strong"
    assert project_path_strength("/home/me/curl/lib/fake.c", curl_repo) == "strong"
    assert project_path_strength("/home/me/build/lib/vtls/fake.c", curl_repo) == "strong"
    assert project_path_strength("/home/me/other/lib/fake.c", curl_repo) is None
    assert project_path_strength("fake.c", curl_repo) == "weak"
    assert project_path_strength("", curl_repo) is None


def test_did_you_mean(curl_repo):
    assert did_you_mean("lib/vtls/openssl.cc", curl_repo)[0] == "lib/vtls/openssl.c"
    assert did_you_mean("", curl_repo) == []


# ------------------------------------------------------------------- symbols


def test_defined_symbol_passes(sample_repo):
    claims = [Claim(id="C1", text="Client.fetch ignores the timeout", entities=["Client.fetch"])]
    (check,) = check_symbols(Artifacts(symbols=["parse_url()"]), claims, sample_repo)[:1]
    assert check.status is CheckStatus.PASS and check.strength == 0.15
    assert check.evidence[0].path == "src/widget/parser.py" and check.evidence[0].line_start == 6
    checks = check_symbols(Artifacts(), claims, sample_repo)
    assert len(checks) == 1 and checks[0].claim_ids == ["C1"]
    assert checks[0].title.startswith("`Client.fetch` is defined at `src/widget/net.py:12`")


def test_mentioned_only_symbol_is_neutral(make_repo):
    ws = make_repo({"app.py": "from thirdparty import make_session\nmake_session()\n"})
    (check,) = check_symbols(Artifacts(symbols=["make_session"]), [], ws)
    assert check.status is CheckStatus.INFO and check.signal is Signal.NEUTRAL
    assert check.strength == 0.05


def test_unknown_symbol_may_be_dependency(sample_repo):
    (check,) = check_symbols(Artifacts(symbols=["retry_with_backoff"]), [], sample_repo)
    assert check.status is CheckStatus.WARN and check.signal is Signal.CONTRADICTS
    assert check.strength == 0.3


def test_project_attributed_symbol_fails(curl_repo):
    claims = [
        Claim(
            id="C1",
            text="Curl_easy_overflow_check in lib/easy.c copies the header with strcpy",
            entities=["Curl_easy_overflow_check", "lib/easy.c", "strcpy"],
        ),
        Claim(id="C2", text="Unrelated claim"),
    ]
    checks = check_symbols(Artifacts(symbols=["Curl_easy_overflow_check"]), claims, curl_repo)
    (check,) = checks  # strcpy is a C library function and is skipped
    assert check.status is CheckStatus.FAIL and check.signal is Signal.CONTRADICTS
    assert check.strength == 0.8 and check.claim_ids == ["C1"]
    assert check.title == (
        "`Curl_easy_overflow_check` is not defined or referenced anywhere in the repository "
        f"{at_commit(curl_repo)}"
    )
    assert check.evidence[0].path == "lib/easy.c"


def test_symbol_attributed_by_package_or_owner_or_trace(sample_repo):
    trace = StackTrace(
        language="python",
        frames=[StackFrame(path="src/widget/net.py", line=13, function="fetch_all_pages", raw="")],
    )
    art = Artifacts(
        symbols=["widget.parser.parse_uri", "Client.fetch_many", "fetch_all_pages"],
        stack_traces=[trace],
    )
    checks = check_symbols(art, [], sample_repo)
    assert [(c.status, c.strength) for c in checks] == [(CheckStatus.FAIL, 0.8)] * 3


def test_symbol_candidates_are_filtered(sample_repo):
    claims = [
        Claim(
            id="C1",
            text="prose",
            entities=[
                "timeout",
                "src/widget/parser.py",
                "--timeout",
                "1.2.3",
                "README.md",
                "os.path",
            ],
        )
    ]
    art = Artifacts(symbols=["len", "np.array", "abc", "x", "ValueError"])
    assert check_symbols(art, claims, sample_repo) == []


def test_inline_code_makes_plain_word_a_symbol(sample_repo):
    claims = [Claim(id="C1", text="see `normalize`", entities=["normalize"])]
    (check,) = check_symbols(Artifacts(inline_code=["normalize"]), claims, sample_repo)
    assert check.status is CheckStatus.WARN  # not a real name in the repo


# --------------------------------------------------------------------- flags


def test_documented_flag_passes(sample_repo):
    claims = [Claim(id="C1", text="--retries is ignored")]
    (check,) = check_cli_flags(Artifacts(cli_flags=["--retries=5"]), claims, sample_repo)
    assert check.id == "grounding.flag" and check.status is CheckStatus.PASS
    assert check.evidence[0].path == "src/widget/cli.py" and check.claim_ids == ["C1"]


def test_flag_defined_without_dashes(curl_repo):
    (check,) = check_cli_flags(Artifacts(cli_flags=["--max-time"]), [], curl_repo)
    assert check.status is CheckStatus.PASS


def test_unknown_flag_in_cli_project_warns(sample_repo):
    checks = check_cli_flags(
        Artifacts(cli_flags=["--insecure-mode", "-v", "--x", "--with-ssl"]), [], sample_repo
    )
    assert [c.status for c in checks] == [CheckStatus.WARN, CheckStatus.INFO]
    assert checks[0].signal is Signal.CONTRADICTS and checks[0].strength == 0.35
    assert checks[0].evidence


def test_unknown_flag_without_cli_is_info(make_repo):
    ws = make_repo({"lib.py": "def f():\n    count = 1\n    return count\n"})
    (check,) = check_cli_flags(Artifacts(cli_flags=["--fast"]), [], ws)
    assert check.status is CheckStatus.INFO and check.signal is Signal.NEUTRAL


def test_flag_matching_parameter_name_is_info(make_repo):
    ws = make_repo(
        {
            "cli.py": "def main(dry_run: bool = False):\n    return dry_run\n",
            "README.md": "Use `--verbose`.\n",
        }
    )
    (check,) = check_cli_flags(Artifacts(cli_flags=["--dry-run"]), [], ws)
    assert check.status is CheckStatus.INFO and "dry_run" in check.title


# ------------------------------------------------------------ error messages


def test_error_fragments():
    assert error_fragments("ValueError: missing scheme in 'example.com'") == ["missing scheme in"]
    frags = error_fragments("curl: (6) Could not resolve host: example.invalid")
    assert "Could not resolve host" in frags
    assert error_fragments("KeyError: 'x'") == []
    assert error_fragments("") == []
    assert error_fragments("Failed to connect to 10.0.0.1 port 443 after 0x1f ms") == [
        "Failed to connect to"
    ]


def test_error_message_found_in_code(sample_repo):
    claims = [Claim(id="C1", text="It raises ValueError: missing scheme in 'example.com'")]
    art = Artifacts(error_messages=["ValueError: missing scheme in 'example.com'"])
    (check,) = check_error_messages(art, claims, sample_repo)
    assert check.status is CheckStatus.PASS and check.strength == 0.35
    assert check.title == "Error text is produced by `src/widget/parser.py:12`"
    assert check.claim_ids == ["C1"]


def test_error_message_not_found_is_neutral(sample_repo):
    trace = StackTrace(language="python", error_message="Connection reset by peer while reading")
    art = Artifacts(error_messages=["boom"], stack_traces=[trace])
    skipped, info = check_error_messages(art, [], sample_repo)
    assert skipped.status is CheckStatus.SKIPPED
    assert info.status is CheckStatus.INFO and info.strength == 0.0
    assert info.signal is Signal.NEUTRAL


def test_error_message_only_in_tests_does_not_count(make_repo):
    ws = make_repo({"tests/test_x.py": "assert msg == 'the frobnicator exploded badly'\n"})
    art = Artifacts(error_messages=["RuntimeError: the frobnicator exploded badly"])
    (check,) = check_error_messages(art, [], ws)
    assert check.status is CheckStatus.INFO


# ---------------------------------------------------------------- end to end


def test_fabricated_curl_style_report(curl_repo):
    claims = [
        Claim(
            id="C1",
            text="Curl_parse_header_overflow in lib/easy.c:3412 uses strcpy on attacker data",
            quote="lib/easy.c:3412",
            entities=["Curl_parse_header_overflow", "lib/easy.c"],
        )
    ]
    art = Artifacts(
        file_refs=[ref("lib/easy.c", 3412), ref("lib/vtls/sslstream.c")],
        symbols=["Curl_parse_header_overflow"],
        cli_flags=["--max-time"],
    )
    checks = run_grounding_checks(art, claims, curl_repo)
    contradictions = [c for c in checks if c.signal is Signal.CONTRADICTS]
    assert {c.id for c in contradictions} == {
        "grounding.line",
        "grounding.symbol",
        "grounding.file",
    }
    assert all(c.status is CheckStatus.FAIL for c in contradictions)
    assert by_id(checks, "grounding.flag")[0].status is CheckStatus.PASS


def test_genuine_report_has_no_contradictions(sample_repo):
    claims = [
        Claim(
            id="C1",
            text="parse_url in src/widget/parser.py raises ValueError: missing scheme in 'x.org'",
            entities=["parse_url", "src/widget/parser.py"],
        )
    ]
    art = Artifacts(
        file_refs=[ref("src/widget/parser.py", 12)],
        symbols=["parse_url", "urlsplit"],
        cli_flags=["--timeout"],
        error_messages=["ValueError: missing scheme in 'x.org'"],
    )
    checks = run_grounding_checks(art, claims, sample_repo)
    assert checks and all(c.signal is not Signal.CONTRADICTS for c in checks)
    assert all(c.status is CheckStatus.PASS for c in checks if c.id != "grounding.symbol")


def test_empty_and_adversarial_input(sample_repo):
    assert run_grounding_checks(Artifacts(), [], sample_repo) == []
    weird = Artifacts(
        file_refs=[ref("   "), ref("../../../../etc/passwd"), ref("a" * 5000 + ".py")],
        symbols=["$(rm -rf /)", "'; DROP TABLE", "ünïcødé_name"],
        cli_flags=["--", "---", "--$(x)"],
        error_messages=["\x00\x01", "é" * 10000],
    )
    checks = run_grounding_checks(weird, [], sample_repo)
    assert all(c.status is not CheckStatus.FAIL for c in checks)


def test_claim_ids_for():
    claims = [
        Claim(id="C1", text="calls parse_url twice"),
        Claim(id="C2", text="x", quote="parse_url_v2"),
        Claim(id="C3", text="y", entities=["widget.parse_url"]),
    ]
    assert claim_ids_for(claims, ["parse_url"]) == ["C1", "C2", "C3"]
    assert claim_ids_for(claims, ["parse_url"], word=True) == ["C1", "C3"]
    assert claim_ids_for(claims, ["", "  "]) == []
