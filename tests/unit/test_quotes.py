from __future__ import annotations

from nomi.models import Artifacts, CheckStatus, CodeBlock, Signal
from nomi.verify.quotes import check_quoted_code, distinctive_lines

GENUINE_PARSE_URL = '''def parse_url(url: str) -> dict:
    """Split a URL into its parts. Raises ValueError on empty input."""
    if not url:
        raise ValueError("empty url")
    parts = urlsplit(url)
    if not parts.scheme:
        raise ValueError(f"missing scheme in {url!r}")
    return {"scheme": parts.scheme, "host": parts.hostname, "path": parts.path}
'''

FABRICATED_PARSE_URL = """def parse_url(url: str) -> dict:
    buffer = bytearray(256)
    buffer[: len(url)] = url.encode()
    hostname = buffer.decode().split("://", 1)[1]
    return eval_hostname_unsafely(hostname)
"""

CURL_FILES = {
    "lib/http.c": (
        '#include "curl_setup.h"\n'
        "\n"
        "CURLcode Curl_http_header(struct Curl_easy *data, const char *hd)\n"
        "{\n"
        "  size_t hdlen = strlen(hd);\n"
        "  if(hdlen > MAX_HTTP_HEADER_SIZE)\n"
        "    return CURLE_TOO_LARGE;\n"
        "  return Curl_dyn_addn(&data->state.headerb, hd, hdlen);\n"
        "}\n"
    ),
    "lib/curl_setup.h": "#define MAX_HTTP_HEADER_SIZE (300*1024)\n",
    "include/curl/curl.h": "CURLcode curl_easy_perform(CURL *curl);\n",
    "src/tool_main.c": "int main(int argc, char **argv)\n{\n  return run_tool(argc, argv);\n}\n",
}


def block(content: str, language: str | None = "python") -> Artifacts:
    return Artifacts(code_blocks=[CodeBlock(language=language, content=content, line=3)])


def test_genuine_quote_passes(sample_repo):
    (check,) = check_quoted_code(block(GENUINE_PARSE_URL), sample_repo)
    assert check.id == "quote.code"
    assert (check.status, check.signal, check.strength) == (
        CheckStatus.PASS,
        Signal.SUPPORTS,
        0.3,
    )
    ev = check.evidence[0]
    assert ev.path == "src/widget/parser.py" and ev.line_start == 6 and ev.line_end == 13
    assert check.title.startswith("Quoted code matches `src/widget/parser.py:6-13`")


def test_genuine_quote_with_crlf_and_indentation(sample_repo):
    content = "\r\n".join("    " + line for line in GENUINE_PARSE_URL.splitlines())
    (check,) = check_quoted_code(block(content, None), sample_repo)
    assert check.status is CheckStatus.PASS


def test_fabricated_body_of_real_function_fails(sample_repo):
    (check,) = check_quoted_code(block(FABRICATED_PARSE_URL), sample_repo)
    assert (check.status, check.signal, check.strength) == (
        CheckStatus.FAIL,
        Signal.CONTRADICTS,
        0.6,
    )
    assert check.title == (
        "Code block quotes `parse_url` but the body does not match the repository "
        "(1/5 lines found): possibly fabricated or from another version"
    )
    real = check.evidence[0]
    assert real.path == "src/widget/parser.py" and real.line_start == 6
    assert "def parse_url" in real.excerpt


def test_fabricated_c_function(make_repo):
    ws = make_repo(CURL_FILES, repo="curl/curl")
    fake = (
        "CURLcode Curl_http_header(struct Curl_easy *data, const char *hd)\n"
        "{\n"
        "  char header_buffer[256];\n"
        "  strcpy(header_buffer, hd);  /* no bounds check */\n"
        "  return process_header_unchecked(data, header_buffer);\n"
        "}\n"
    )
    (check,) = check_quoted_code(block(fake, "c"), ws)
    assert check.status is CheckStatus.FAIL and "`Curl_http_header`" in check.title


def test_reporters_own_script_is_ignored(sample_repo, make_repo):
    script = (
        "from widget.net import Client\n"
        "\n"
        "def fetch_everything(urls):\n"
        "    client = Client(retries=0)\n"
        "    return [client.fetch(u, timeout=1) for u in urls]\n"
    )
    assert check_quoted_code(block(script), sample_repo) == []
    ws = make_repo(CURL_FILES, repo="curl/curl", name="curl")
    poc = (
        "#include <curl/curl.h>\n"
        "int main(void) {\n"
        "  CURL *curl = curl_easy_init();\n"
        '  curl_easy_setopt(curl, CURLOPT_URL, "http://example.com");\n'
        "  return curl_easy_perform(curl);\n"
        "}\n"
    )
    assert check_quoted_code(block(poc, "c"), ws) == []


def test_partial_match_is_neutral_warning(sample_repo):
    content = (
        "def parse_url(url: str) -> dict:\n"
        "    if not url:\n"
        '        raise ValueError("empty url")\n'
        "    parts = urlsplit_with_cache(url, cache=GLOBAL_CACHE)\n"
        '    log.debug("parsed %s into %r", url, parts)\n'
        "    return normalize_all_parts(parts)\n"
    )
    (check,) = check_quoted_code(block(content), sample_repo)
    assert (check.status, check.signal, check.strength) == (
        CheckStatus.WARN,
        Signal.NEUTRAL,
        0.15,
    )


def test_blocks_that_are_not_quotes_are_skipped(sample_repo):
    skipped = [
        block("$ widget fetch http://x --timeout 10\nerror: boom\nexit 1\n", "bash"),
        block('Traceback (most recent call last):\n  File "x.py", line 1\nValueError: x\n'),
        block("def parse_url(url):\n    pass\n", "python"),  # fewer than 3 real lines
        block("{\n}\n;\n", "c"),
        block("", None),
    ]
    for art in skipped:
        assert check_quoted_code(art, sample_repo) == []


def test_duplicate_blocks_are_checked_once(sample_repo):
    art = Artifacts(
        code_blocks=[
            CodeBlock(language="python", content=GENUINE_PARSE_URL),
            CodeBlock(language="py", content=GENUINE_PARSE_URL),
        ]
    )
    assert len(check_quoted_code(art, sample_repo)) == 1


def test_distinctive_lines_filtering():
    code = (
        "import os\n"
        "from widget.parser import parse_url\n"
        "# a long comment line that should be ignored\n"
        "// another long comment line\n"
        "#define BUFFER_SIZE 4096\n"
        "    result = compute_something(alpha, beta)\n"
        "}\n"
        "short = 1\n"
        "    result = compute_something(alpha, beta)\n"
    )
    assert distinctive_lines(code, "python") == ["result = compute_something(alpha, beta)"]
    assert distinctive_lines(code, "c")[0] == "#define BUFFER_SIZE 4096"
    many = "\n".join(f"value_{i} = compute_value({i})" for i in range(40))
    assert len(distinctive_lines(many)) == 15


def test_unicode_and_huge_block(sample_repo):
    huge = "\n".join(f"résultat_{i} = calcule_quelque_chose({i})" for i in range(5000))
    assert check_quoted_code(block(huge), sample_repo) == []
