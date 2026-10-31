from __future__ import annotations

import time

import pytest

from nomi.extract.artifacts import MAX_CHARS, MAX_ITEMS, extract_artifacts
from nomi.models import Artifacts, FileRef

FENCE = "```"


def refs(text: str) -> list[tuple[str, int | None, int | None]]:
    return [(r.path, r.line_start, r.line_end) for r in extract_artifacts(text).file_refs]


# ------------------------------------------------------------------ basics


@pytest.mark.parametrize("text", ["", "   \n\n\t", None])
def test_empty_input_gives_empty_artifacts(text: str | None) -> None:
    assert extract_artifacts(text) == Artifacts()  # type: ignore[arg-type]


def test_crlf_is_normalized() -> None:
    text = f"Title\r\n\r\n{FENCE}py\r\nparse_url('')\r\n{FENCE}\r\nSee src/widget/parser.py:9\r\n"
    art = extract_artifacts(text)
    assert art.code_blocks[0].content == "parse_url('')"
    assert art.code_blocks[0].line == 3
    assert refs(text) == [("src/widget/parser.py", 9, None)]


def test_unicode_text_is_handled() -> None:
    text = "Ünïcödé 🚀 `naïve_func()` fails in src/módulo/ñandú.py line 3 — версия 2.0.1"
    art = extract_artifacts(text)
    assert "naïve_func" in art.symbols
    assert ("src/módulo/ñandú.py", 3, None) in refs(text)
    assert "2.0.1" in art.versions


def test_huge_input_is_truncated_and_fast() -> None:
    chunk = (
        "Calling `Client.fetch(url)` in src/widget/net.py:12 raises ValueError: bad on 1.2.3 "
        "see #4 https://example.com/x --flag CVE-2024-12345 snake_case_name 192.168.0.1\n"
    )
    text = chunk * (300_000 // len(chunk) + 1)
    assert len(text) > 300_000
    start = time.perf_counter()
    art = extract_artifacts(text)
    assert time.perf_counter() - start < 1.0
    assert art.file_refs[0].path == "src/widget/net.py"
    assert len(art.symbols) <= 50 and len(art.versions) <= 30


def test_pathological_single_line_is_fast() -> None:
    text = ("a" * 1000 + "." + "`" * 3 + "_" * 50 + "#" + "-" * 20) * 300
    start = time.perf_counter()
    extract_artifacts(text[:MAX_CHARS])
    assert time.perf_counter() - start < 1.0


# ------------------------------------------------------------- code blocks


def test_code_blocks_language_line_and_unclosed_fence() -> None:
    text = (
        "Intro\n"
        f"{FENCE}Python title='x'\nprint(1)\n{FENCE}\n"
        "between\n"
        "~~~\nplain\n~~~\n"
        f"{FENCE}\nunclosed\nrest"
    )
    blocks = extract_artifacts(text).code_blocks
    assert [(b.language, b.content, b.line) for b in blocks] == [
        ("python", "print(1)", 2),
        (None, "plain", 6),
        (None, "unclosed\nrest", 9),
    ]


def test_longer_closing_fence_and_nested_backticks() -> None:
    text = "````md\n```py\nx = 1\n```\n````\n"
    blocks = extract_artifacts(text).code_blocks
    assert len(blocks) == 1
    assert blocks[0].language == "md"
    assert blocks[0].content == "```py\nx = 1\n```"


def test_inline_code_outside_fences_only() -> None:
    text = f"Use `foo()` and ``a b`` and `foo()`.\n{FENCE}\n`not_inline`\n{FENCE}"
    assert extract_artifacts(text).inline_code == ["foo()", "a b"]


def test_inline_code_cap() -> None:
    text = " ".join(f"`x{i}`" for i in range(150))
    assert len(extract_artifacts(text).inline_code) == 100


# ---------------------------------------------------------------- file refs


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see src/widget/parser.py", ("src/widget/parser.py", None, None)),
        ("see ./src/widget/parser.py:9", ("src/widget/parser.py", 9, None)),
        ("see parser.py:9:5", ("parser.py", 9, None)),
        ("see parser.py:9-12", ("parser.py", 9, 12)),
        ("see parser.py#L10", ("parser.py", 10, None)),
        ("see parser.py#L10-L20", ("parser.py", 10, 20)),
        ("the bug is on line 42 of foo.c", ("foo.c", 42, None)),
        ("crash at line 42 in foo.c", ("foo.c", 42, None)),
        ("lines 10-20 of src/a.rs", ("src/a.rs", 10, 20)),
        ("foo.c line 42 is wrong", ("foo.c", 42, None)),
        ("foo.c (line 42) is wrong", ("foo.c", 42, None)),
        ('File "lib/x.py", line 7, in f', ("lib/x.py", 7, None)),
        ("open 'config/app.yaml' now", ("config/app.yaml", None, None)),
        ("in `pyproject.toml`", ("pyproject.toml", None, None)),
        (r"C:\proj\src\main.go:3", ("C:/proj/src/main.go", 3, None)),
        ("edit .github/workflows/ci.yml", (".github/workflows/ci.yml", None, None)),
    ],
)
def test_file_ref_forms(text: str, expected: tuple[str, int | None, int | None]) -> None:
    assert refs(text) == [expected]


def test_github_blob_url_becomes_file_ref() -> None:
    url = "https://github.com/acme/widget/blob/0a1b2c/src/widget/net.py#L10-L20"
    art = extract_artifacts(f"See {url}.")
    assert art.file_refs == [FileRef(path="src/widget/net.py", line_start=10, line_end=20, raw=url)]
    assert art.urls == [url]


@pytest.mark.parametrize(
    "text",
    [
        "Python 3.12 and 1.2.3 are versions",
        "see https://example.com/docs/setup.py for details",
        "uses Node.js and Vue.js",
        "meet at 10 a.m. tomorrow",
        "call response.json() to decode",
        "glob *.py matches",
        "file 3.12.md is odd",
        "",
    ],
)
def test_file_ref_negatives(text: str) -> None:
    assert refs(text) == []


def test_file_refs_dedupe_and_keep_order() -> None:
    text = "b.py then a.py then b.py again, b.py:3"
    assert refs(text) == [("b.py", None, None), ("a.py", None, None), ("b.py", 3, None)]


def test_file_ref_raw_is_matched_text() -> None:
    art = extract_artifacts("the bug is on line 42 of foo.c here")
    assert art.file_refs[0].raw == "on line 42 of foo.c"


# ------------------------------------------------------------------ symbols


def test_symbols_from_prose_and_inline_code() -> None:
    text = (
        "Calling `parse_url` via Client.fetch() hits normalize_host and DEFAULT_TIMEOUT; "
        "the HttpClient and getValue() fail. Also `fetch`."
    )
    assert extract_artifacts(text).symbols == [
        "parse_url",
        "Client.fetch",
        "normalize_host",
        "DEFAULT_TIMEOUT",
        "HttpClient",
        "getValue",
        "fetch",
    ]


def test_symbol_negatives() -> None:
    text = (
        "The file(s) in src/widget/parser.py, see e.g. https://foo.com/some_path and "
        "example.com; use --dry_run on macOS and GitHub with JavaScript. Version 1.2.3. "
        "This is _emphasis_ and plain words."
    )
    assert extract_artifacts(text).symbols == []


def test_symbols_from_fenced_blocks_definitions_and_calls() -> None:
    text = (
        f"{FENCE}python\n"
        "def helper(x):\n"
        "    return parse_url(x)\n"
        "class Thing:\n"
        "    pass\n"
        "if (x):\n"
        "    print(len(x))\n"
        f"{FENCE}\n"
        f"{FENCE}go\nfunc (c *Client) Fetch(u string) {{}}\n{FENCE}"
    )
    assert extract_artifacts(text).symbols == ["helper", "parse_url", "Thing", "Fetch"]


def test_format_and_path_patterns_are_not_symbols() -> None:
    text = (
        'Run `celery multi start w1 --pidfile="tmp/%n.pid" --logfile=/var/log/%n%I.log`; '
        "also --pidfile=worker.pid and build_dir/out. But proc.pid and app.conf are attributes."
    )
    assert extract_artifacts(text).symbols == ["proc.pid", "app.conf"]


@pytest.mark.parametrize(
    "text",
    [
        "Start it with `celery -A FahrplanDatenGarten worker` and it hangs.",
        "Start it with `celery --app=FahrplanDatenGarten worker` and it hangs.",
        "Start it with `celery --app FahrplanDatenGarten worker` and it hangs.",
        f"{FENCE}\ncelery -A FahrplanDatenGarten worker -l info\n"
        f"celery --app=make_app() worker\n{FENCE}",
    ],
)
def test_app_option_value_is_not_a_symbol(text: str) -> None:
    assert extract_artifacts(text).symbols == []


def test_app_option_skips_only_its_value() -> None:
    text = "Run `celery -A FahrplanDatenGarten worker` then call `Celery.send_task()`."
    assert extract_artifacts(text).symbols == ["Celery.send_task"]


def test_external_tool_names_are_not_symbols() -> None:
    text = (
        "Found with ASan, UBSan, TSan, MSan and LSan; AddressSanitizer and "
        "UndefinedBehaviorSanitizer agree. `valgrind` and `gdb` too, plus lldb, strace, perf.\n"
        f"{FENCE}\nvalgrind(run_parser())\nperf(record)\n{FENCE}"
    )
    assert extract_artifacts(text).symbols == ["run_parser"]


def test_symbol_caps() -> None:
    prose = " ".join(f"name_{i}" for i in range(80))
    block = "\n".join(f"call_{i}()" for i in range(40))
    art = extract_artifacts(f"{prose}\n{FENCE}\n{block}\n{FENCE}")
    assert len(art.symbols) == 50
    only_block = extract_artifacts(f"{FENCE}\n{block}\n{FENCE}")
    assert len(only_block.symbols) == 20


# ------------------------------------------------------------- stack traces


def test_python_traceback() -> None:
    text = (
        "It fails:\n"
        f"{FENCE}\n"
        "Traceback (most recent call last):\n"
        '  File "/app/main.py", line 3, in <module>\n'
        '    parse_url("")\n'
        '  File "/venv/lib/python3.12/site-packages/widget/parser.py", line 9, in parse_url\n'
        '    raise ValueError("empty url")\n'
        "ValueError: empty url\n"
        f"{FENCE}\n"
    )
    [trace] = extract_artifacts(text).stack_traces
    assert trace.language == "python"
    assert [(f.path, f.line, f.function) for f in trace.frames] == [
        ("/app/main.py", 3, "<module>"),
        ("/venv/lib/python3.12/site-packages/widget/parser.py", 9, "parse_url"),
    ]
    assert (trace.error_type, trace.error_message) == ("ValueError", "empty url")
    assert trace.raw.startswith("Traceback") and trace.raw.endswith("ValueError: empty url")


def test_python_chained_and_dotted_exception() -> None:
    text = (
        "Traceback (most recent call last):\n"
        '  File "a.py", line 1, in f\n'
        "KeyError: 'x'\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "b.py", line 2, in g\n'
        "requests.exceptions.HTTPError: 404\n"
    )
    traces = extract_artifacts(text).stack_traces
    assert [(t.error_type, t.error_message) for t in traces] == [
        ("KeyError", "'x'"),
        ("requests.exceptions.HTTPError", "404"),
    ]


def test_javascript_trace() -> None:
    text = (
        "TypeError: Cannot read properties of undefined (reading 'bar')\n"
        "    at Object.<anonymous> (/app/index.js:3:7)\n"
        "    at Module._compile (node:internal/modules/cjs/loader:1105:14)\n"
        "    at async Promise.all (index 0)\n"
        "    at /app/lib/util.js:10:2\n"
    )
    [trace] = extract_artifacts(text).stack_traces
    assert trace.language == "javascript"
    assert trace.error_type == "TypeError"
    assert trace.error_message == "Cannot read properties of undefined (reading 'bar')"
    assert [(f.path, f.line, f.function) for f in trace.frames] == [
        ("/app/index.js", 3, "Object.<anonymous>"),
        ("node:internal/modules/cjs/loader", 1105, "Module._compile"),
        ("/app/lib/util.js", 10, None),
    ]


def test_java_trace_with_caused_by() -> None:
    text = (
        'Exception in thread "main" java.lang.IllegalStateException: boom\n'
        "\tat com.acme.Widget.run(Widget.java:42)\n"
        "\tat com.acme.Main.main(Main.java:7)\n"
        "Caused by: java.io.IOException: disk\n"
        "\tat com.acme.io.Disk.read(Disk.java:9)\n"
        "\t... 2 more\n"
    )
    [trace] = extract_artifacts(text).stack_traces
    assert trace.language == "java"
    assert (trace.error_type, trace.error_message) == ("java.lang.IllegalStateException", "boom")
    assert [(f.path, f.line) for f in trace.frames] == [
        ("com/acme/Widget.java", 42),
        ("com/acme/Main.java", 7),
        ("com/acme/io/Disk.java", 9),
    ]
    assert trace.frames[0].function == "com.acme.Widget.run"


def test_go_panic() -> None:
    text = (
        "panic: runtime error: index out of range [5] with length 3\n"
        "\n"
        "goroutine 1 [running]:\n"
        "main.(*T).get(0xc000010000, 0x5)\n"
        "\t/tmp/prog/main.go:8 +0x1d\n"
        "main.main()\n"
        "\t/tmp/prog/main.go:13 +0x25\n"
        "exit status 2\n"
    )
    [trace] = extract_artifacts(text).stack_traces
    assert trace.language == "go"
    assert trace.error_type == "panic"
    assert trace.error_message == "runtime error: index out of range [5] with length 3"
    assert [(f.path, f.line, f.function) for f in trace.frames] == [
        ("/tmp/prog/main.go", 8, "main.(*T).get"),
        ("/tmp/prog/main.go", 13, "main.main"),
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "thread 'main' panicked at src/main.rs:10:5:\nindex out of bounds\n",
            "index out of bounds",
        ),
        (
            "thread 'main' panicked at 'called `Option::unwrap()` on a `None` value', src/main.rs:10:5\n"
            "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\n",
            "called `Option::unwrap()` on a `None` value",
        ),
    ],
)
def test_rust_panic_formats(text: str, message: str) -> None:
    [trace] = extract_artifacts(text).stack_traces
    assert trace.language == "rust"
    assert (trace.error_type, trace.error_message) == ("panic", message)
    assert [(f.path, f.line) for f in trace.frames] == [("src/main.rs", 10)]


def test_rust_backtrace_frames_are_not_javascript() -> None:
    text = (
        "thread 'main' panicked at src/main.rs:4:5:\n"
        "boom\n"
        "stack backtrace:\n"
        "   0: rust_begin_unwind\n"
        "             at /rustc/abc/library/std/src/panicking.rs:595:5\n"
        "   1: demo::main\n"
        "             at ./src/main.rs:4:5\n"
    )
    [trace] = extract_artifacts(text).stack_traces
    assert trace.language == "rust"
    assert [(f.path, f.line, f.function) for f in trace.frames][1:] == [
        ("/rustc/abc/library/std/src/panicking.rs", 595, "rust_begin_unwind"),
        ("./src/main.rs", 4, "demo::main"),
    ]


def test_c_asan_trace() -> None:
    text = (
        "=================================================================\n"
        "==4242==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000014 at pc 0x1\n"
        "READ of size 4 at 0x602000000014 thread T0\n"
        "    #0 0x4f5a2b in parse_header /src/lib/header.c:123:4\n"
        "    #1 0x4f6b11 in main /src/main.c:10:3\n"
        "    #2 0x7f00 in __libc_start_main (/lib/x86_64-linux-gnu/libc.so.6+0x21b96)\n"
    )
    [trace] = extract_artifacts(text).stack_traces
    assert trace.language == "c"
    assert trace.error_type == "heap-buffer-overflow"
    assert trace.error_message.startswith("heap-buffer-overflow on address")
    assert [(f.path, f.line, f.function) for f in trace.frames] == [
        ("/src/lib/header.c", 123, "parse_header"),
        ("/src/main.c", 10, "main"),
        ("/lib/x86_64-linux-gnu/libc.so.6", None, "__libc_start_main"),
    ]


def test_gdb_backtrace() -> None:
    text = (
        "Program received signal SIGSEGV, Segmentation fault.\n"
        "#0  0x00007ffff7a42428 in __GI_raise (sig=sig@entry=6) at ../sysdeps/raise.c:54\n"
        '#1  copy_name (dst=0x0, src=0x4006f4 "x") at src/name.c:17\n'
    )
    [trace] = extract_artifacts(text).stack_traces
    assert (trace.language, trace.error_type, trace.error_message) == (
        "c",
        "SIGSEGV",
        "Segmentation fault",
    )
    assert [(f.path, f.line, f.function) for f in trace.frames] == [
        ("../sysdeps/raise.c", 54, "__GI_raise"),
        ("src/name.c", 17, "copy_name"),
    ]


def test_quoted_trace_and_no_false_traces() -> None:
    quoted = '> Traceback (most recent call last):\n>   File "a.py", line 1, in f\n> OSError: x\n'
    [trace] = extract_artifacts(quoted).stack_traces
    assert trace.error_type == "OSError"
    prose = "We met at 5 and #1 is the issue; look at the file (line 3) at noon"
    assert extract_artifacts(prose).stack_traces == []


# ------------------------------------------------------------ error messages


def test_error_messages() -> None:
    text = (
        "Calling it gives `ValueError: empty url` every time. More text.\n"
        "error: could not compile `demo`\n"
        "fatal: not a git repository\n"
        "panic: runtime error: nil map\n"
        "Segmentation fault (core dumped)\n"
        "==1==ERROR: AddressSanitizer: heap-use-after-free on address 0x1\n"
        "src/a.c:3:5: error: expected ';'\n"
        "Error: ENOENT: no such file\n"
        "except ValueError: pass\n"
        "signal(SIGSEGV, handler);\n"
    )
    assert extract_artifacts(text).error_messages == [
        "ValueError: empty url",
        "error: could not compile",
        "fatal: not a git repository",
        "panic: runtime error: nil map",
        "Segmentation fault (core dumped)",
        "AddressSanitizer: heap-use-after-free on address 0x1",
        "src/a.c:3:5: error: expected ';'",
        "Error: ENOENT: no such file",
    ]


def test_error_message_length_is_capped() -> None:
    text = "RuntimeError: " + "x" * 1000
    [message] = extract_artifacts(text).error_messages
    assert len(message) == 300


# ------------------------------------------------------------------ versions


def test_versions_forms() -> None:
    text = (
        "Python 3.12, widget v1.1.0, numpy==1.26.4, 3.12.0a1, 2.0rc1, 1.2.dev3, 1.0.0-beta.1, "
        "torch 2.1.0+cu118 and again 3.12"
    )
    assert extract_artifacts(text).versions == [
        "3.12",
        "v1.1.0",
        "1.26.4",
        "3.12.0a1",
        "2.0rc1",
        "1.2.dev3",
        "1.0.0-beta.1",
        "2.1.0+cu118",
    ]


def test_version_negatives() -> None:
    text = (
        "host 192.168.0.1 on 2024.01.15 or 15.01.2024 at foo.py:10:5 in setup.py; "
        "took 2.5 seconds using 1.5 GB, 99.9% up; python3.12/site-packages; "
        "https://example.com/v1.2.3/x; 1,234.56"
    )
    assert extract_artifacts(text).versions == []


def test_versions_skip_source_code_blocks_and_cap() -> None:
    text = f"{FENCE}python\nx = 3.14\n{FENCE}\n{FENCE}\n$ tool --version\ntool 4.5.6\n{FENCE}"
    assert extract_artifacts(text).versions == ["4.5.6"]
    many = " ".join(f"1.{i}" for i in range(50))
    assert len(extract_artifacts(many).versions) == 30


# --------------------------------------------------------------------- flags


def test_flags_only_inside_code() -> None:
    text = (
        "In prose --verbose and -x are ignored -- really.\n"
        "Run `widget fetch URL --timeout=10 -v`.\n"
        f"{FENCE}bash\n$ widget --retries 3 -rf\n{FENCE}\n"
        f"{FENCE}python\ny = -x\nparser.add_argument('--dry-run')\n{FENCE}\n"
    )
    assert extract_artifacts(text).cli_flags == ["--timeout", "-v", "--retries", "-rf", "--dry-run"]


# -------------------------------------------------------- urls, refs, CVEs


def test_urls_are_trimmed() -> None:
    text = "See [docs](https://example.com/a_(b)) and <http://x.org/y>, https://z.io/q?a=1."
    assert extract_artifacts(text).urls == [
        "https://example.com/a_(b)",
        "http://x.org/y",
        "https://z.io/q?a=1",
    ]


def test_issue_refs() -> None:
    text = (
        "#12 at line start, also (#34) and see #56.\n"
        "## Heading #78\n"
        "# Title\n"
        "https://github.com/o/r/issues/9#90 and `#11` and owner/repo#13 and #14abc\n"
        f"{FENCE}\n#15\n{FENCE}\n"
    )
    assert extract_artifacts(text).issue_refs == [12, 34, 56]


def test_cve_ids() -> None:
    text = "Fixes CVE-2021-44228 and cve-2024-123456; not CVE-21-1 (see CVE-2021-44228)."
    assert extract_artifacts(text).cve_ids == ["CVE-2021-44228", "CVE-2024-123456"]


def test_adversarial_text_does_not_crash() -> None:
    text = "```\n``` ``` `\n\\\\\\ ((((( #### ----- ::: \x00 \ufeff " + "](" * 500 + "\n~~~"
    assert isinstance(extract_artifacts(text), Artifacts)


# ---------------------------------------- regex backtracking (review group D)

BLANKS = " " * 50_000


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("```x" + " " * 80_000 + "y", id="D2-fence-info"),
        pytest.param('File "a.py", line 1, in ' + BLANKS + "x", id="D3-python-frame"),
        pytest.param(
            "thread 'main' panicked at src/main.rs:1:1:\nboom\nstack backtrace:\n"
            f"   0: f{BLANKS}x\n",
            id="D3-rust-backtrace-frame",
        ),
        pytest.param(
            f"panic: a{BLANKS}x\n\ngoroutine 1 [running]:\nmain.f()\n\tmain.go:1\n",
            id="D3-go-panic",
        ),
        pytest.param(f"java.lang.Foo: a{BLANKS}x\n\tat a.B.c(B.java:1)\n", id="D3-java-header"),
        pytest.param(f"TypeError: a{BLANKS}x\n    at f (a.js:1:1)\n", id="D3-js-header"),
        pytest.param(f"    at a{BLANKS}x", id="D3-js-frame"),
        pytest.param(
            f"Program received signal SIGSEGV, a{BLANKS}x\n#0  0x1 in f () at a.c:1\n",
            id="D3-c-header",
        ),
        pytest.param(f"#0 0x1 in f{BLANKS}x", id="D3-c-asan-frame"),
        pytest.param("thread 'x' panicked at '" * 8333, id="D4-rust-panic"),
        pytest.param(" #1" * 66_666, id="D5-issue-refs"),
        pytest.param(
            f"goroutine 1 [running]:\nmain.f{BLANKS}in goroutine x\n\tmain.go:1\n",
            id="go-in-goroutine-suffix",
        ),
        pytest.param("A" * 200_000, id="camel-case-check"),
        pytest.param("http://a(" + ")" * 200_000, id="url-closing-brackets"),
        pytest.param("http://a" + "." * 200_000, id="url-trailing-dots"),
    ],
)
def test_adversarial_input_is_extracted_in_linear_time(text: str) -> None:
    start = time.perf_counter()
    extract_artifacts(text)
    assert time.perf_counter() - start < 1.0


def test_fence_info_is_stripped_and_capped() -> None:
    text = (
        f"{FENCE}   python   \nx = 1\n{FENCE}\n{FENCE}{' ' * 300}rust\nfn main() {{}}\n{FENCE}\n"
        f"{FENCE}{'x' * 300}\ny\n{FENCE}\n"
    )
    languages = [b.language for b in extract_artifacts(text).code_blocks]
    assert languages == ["python", "rust", "x" * 200]


def test_trace_lines_with_trailing_blanks_still_parse() -> None:
    text = (
        "Traceback (most recent call last):   \n"
        '  File "src/widget/net.py", line 13, in fetch   \t\n'
        "    parts = parse_url(url)\n"
        "ValueError: empty url   \n\n"
        "TypeError: bad input   \n"
        "    at Object.fetch  (src/net.js:10:5)   \n\n"
        "Program received signal SIGSEGV, Segmentation fault.  \n"
        "#0  0x1 in parse (s=0x0) at src/parse.c:12  \n"
    )
    py, js, c = extract_artifacts(text).stack_traces
    assert (py.frames[0].function, py.error_message) == ("fetch", "empty url")
    assert (js.frames[0].function, js.frames[0].path, js.error_message) == (
        "Object.fetch",
        "src/net.js",
        "bad input",
    )
    assert (c.error_type, c.error_message) == ("SIGSEGV", "Segmentation fault")


def test_go_created_by_frame_drops_goroutine_suffix() -> None:
    text = (
        "goroutine 7 [running]:\n"
        "main.worker(0xc000010000)\n\t/app/main.go:12 +0x1d\n"
        "created by main.main in goroutine 1\n\t/app/main.go:30 +0x5a\n"
    )
    (trace,) = extract_artifacts(text).stack_traces
    assert [f.function for f in trace.frames] == ["main.worker", "main.main"]


def test_issue_refs_skip_headings_dedupe_and_cap() -> None:
    text = "# Heading #5\nSee #12 and #12, then #7\n## #9\n#8\n"
    assert extract_artifacts(text).issue_refs == [12, 7, 8]
    many = " ".join(f"#{n}" for n in range(1, MAX_ITEMS + 50))
    assert extract_artifacts(many).issue_refs == list(range(1, MAX_ITEMS + 1))


def test_camel_case_check_is_unchanged() -> None:
    symbols = extract_artifacts("HTTPServer and XMLHttpRequest, not ABC or NASA.").symbols
    assert symbols == ["HTTPServer", "XMLHttpRequest"]


def test_url_trimming_keeps_balanced_brackets() -> None:
    text = "see (http://a.com/x_(y))). and [docs](https://b.io/c). or (https://e.com/a]])"
    urls = extract_artifacts(text).urls
    assert urls == ["http://a.com/x_(y)", "https://b.io/c", "https://e.com/a"]
