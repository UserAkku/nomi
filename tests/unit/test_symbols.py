from __future__ import annotations

from nomi.repo.symbols import (
    Definition,
    defined_names,
    enclosing_function,
    find_definitions,
    find_mentions,
    is_external_symbol,
    language_family,
    language_of,
    symbol_parts,
)
from nomi.repo.workspace import Workspace

C_FILES = {
    "lib/easy.c": (
        "#include <stdio.h>\n"
        "\n"
        "static int helper(int x);\n"
        "\n"
        "CURLcode curl_easy_perform(struct Curl_easy *data)\n"
        "{\n"
        "  int x = helper(1);\n"
        "  if(x) {\n"
        "    return 1;\n"
        "  }\n"
        "  return 0;\n"
        "}\n"
        "\n"
        "static int\n"
        "helper(int x)\n"
        "{\n"
        "  return x + 1;\n"
        "}\n"
        "\n"
        "#define MAX_LEN 256\n"
        "\n"
        "struct Curl_easy {\n"
        "  int a;\n"
        "};\n"
        "typedef struct Curl_easy Curl_handle;\n"
    ),
    "include/curl/curl.h": "CURLcode curl_easy_perform(struct Curl_easy *data);\n",
}

POLYGLOT = {
    "src/app.js": (
        "export async function load(url) {\n"
        "  return fetch(url);\n"
        "}\n"
        "const parse = (s) => {\n"
        "  return s;\n"
        "};\n"
        "class Store {\n"
        "  get(key) {\n"
        "    if (key) {\n"
        "      return 1;\n"
        "    }\n"
        "  }\n"
        "}\n"
    ),
    "main.go": (
        "package main\n"
        "\n"
        "func (c *Client) Fetch(url string) error {\n"
        "\treturn nil\n"
        "}\n"
        "\n"
        "type Client struct {\n"
        "\tx int\n"
        "}\n"
    ),
    "src/lib.rs": "pub struct Parser {}\n\npub(crate) async fn parse_all(x: u8) -> u8 {\n    x\n}\n",
    "src/Main.java": (
        "public class Engine {\n"
        "    public static String runTask(String name) {\n"
        "        return name;\n"
        "    }\n"
        "}\n"
    ),
    "lib/tool.rb": "module Tools\n  class Runner\n    def self.launch_now\n    end\n  end\nend\n",
    "web/index.php": "<?php\nfunction render_page($x) {\n  return $x;\n}\n",
    "README.md": "def not_code(): documented only\n",
}


def test_symbol_parts():
    assert symbol_parts("widget.parser.parse_url") == ["widget", "parser", "parse_url"]
    assert symbol_parts("ns::Client::fetch") == ["ns", "Client", "fetch"]
    assert symbol_parts("Client#fetch") == ["Client", "fetch"]
    assert symbol_parts("`parse_url()`") == ["parse_url"]
    assert symbol_parts("valid?") == ["valid?"]
    for bad in ("", "  ", "not a name", "a.*b", "1abc", "x" * 500, "foo(bar"):
        assert symbol_parts(bad) == []


def test_language_helpers():
    assert language_of("src/a.PY") == "python"
    assert language_of("lib/x.h") == "c"
    assert language_of("README") is None
    assert language_family("Python3") == "python"
    assert language_family("c++ title=x") == "c"
    assert language_family("kt") == "jvm"
    assert language_family("") is None
    assert language_family(None) is None


def test_python_definitions(sample_repo):
    assert find_definitions(sample_repo, "parse_url") == [
        Definition("src/widget/parser.py", 6, "function", "def parse_url(url: str) -> dict:")
    ]
    (cls,) = find_definitions(sample_repo, "Client")
    assert (cls.path, cls.line, cls.kind) == ("src/widget/net.py", 8, "class")
    (const,) = find_definitions(sample_repo, "DEFAULT_TIMEOUT")
    assert const.kind == "variable" and const.line == 5
    (method,) = find_definitions(sample_repo, "Client.fetch")
    assert (method.path, method.line) == ("src/widget/net.py", 12)
    (qualified,) = find_definitions(sample_repo, "widget.parser.parse_url")
    assert qualified.line == 6
    (module,) = find_definitions(sample_repo, "widget.parser")
    assert (module.path, module.kind) == ("src/widget/parser.py", "module")


def test_missing_and_invalid_names(sample_repo):
    assert find_definitions(sample_repo, "parse_urls") == []
    assert find_definitions(sample_repo, "") == []
    assert find_definitions(sample_repo, "url.*(") == []
    assert find_definitions(sample_repo, "parse_url", max_hits=0) == []
    assert find_mentions(sample_repo, "no such thing here") == []


def test_qualifier_prefers_named_class(make_repo):
    ws = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "class Alpha:\n    def fetch(self):\n        return 1\n",
            "pkg/b.py": "class Beta:\n    def fetch(self):\n        return 2\n",
        }
    )
    assert [d.path for d in find_definitions(ws, "fetch")] == ["pkg/a.py", "pkg/b.py"]
    assert [d.path for d in find_definitions(ws, "Beta.fetch")] == ["pkg/b.py"]
    assert [d.path for d in find_definitions(ws, "pkg.b.fetch")] == ["pkg/b.py"]
    # unknown qualifier: fall back to every definition of the bare name
    assert len(find_definitions(ws, "Gamma.fetch")) == 2


def test_c_definitions_skip_prototypes(make_repo):
    ws = make_repo(C_FILES)
    (perform,) = find_definitions(ws, "curl_easy_perform")
    assert (perform.path, perform.line, perform.kind) == ("lib/easy.c", 5, "function")
    (helper,) = find_definitions(ws, "helper")
    assert helper.line == 15
    assert find_definitions(ws, "MAX_LEN")[0].kind == "macro"
    assert find_definitions(ws, "Curl_easy")[0].line == 22
    assert find_definitions(ws, "Curl_handle")[0].kind == "type"


LANGUAGE_CASES = [
    ("load", "src/app.js", "function"),
    ("parse", "src/app.js", "function"),
    ("Store", "src/app.js", "class"),
    ("Store.get", "src/app.js", "method"),
    ("Client.Fetch", "main.go", "method"),
    ("Client", "main.go", "type"),
    ("Parser", "src/lib.rs", "type"),
    ("parse_all", "src/lib.rs", "function"),
    ("Engine", "src/Main.java", "class"),
    ("Engine.runTask", "src/Main.java", "method"),
    ("Runner", "lib/tool.rb", "class"),
    ("launch_now", "lib/tool.rb", "function"),
    ("render_page", "web/index.php", "function"),
]


def test_other_languages(make_repo):
    ws = make_repo(POLYGLOT)
    for name, path, kind in LANGUAGE_CASES:
        defs = find_definitions(ws, name)
        assert defs and (defs[0].path, defs[0].kind) == (path, kind), name


def test_docs_are_not_definitions(make_repo):
    ws = make_repo(POLYGLOT)
    assert find_definitions(ws, "not_code") == []
    assert [h.path for h in find_mentions(ws, "not_code")] == ["README.md"]


def test_mentions_are_word_bounded(make_repo):
    ws = make_repo({"a.py": "parse_url_v2()\nparse_url()\nx = my_parse_url\n"})
    assert [(h.path, h.line) for h in find_mentions(ws, "widget.parse_url")] == [("a.py", 2)]


def test_python_enclosing_function(sample_repo):
    path = "src/widget/parser.py"
    assert enclosing_function(sample_repo, path, 1) == "<module>"
    assert enclosing_function(sample_repo, path, 9) == "parse_url"
    assert enclosing_function(sample_repo, path, 12) == "parse_url"
    assert enclosing_function(sample_repo, path, 17) == "normalize_host"
    assert enclosing_function(sample_repo, "src/widget/net.py", 10) == "__init__"
    assert enclosing_function(sample_repo, "src/widget/net.py", 13) == "fetch"
    # resolves paths the way reports write them
    assert enclosing_function(sample_repo, "/venv/site-packages/widget/net.py", 13) == "fetch"
    assert enclosing_function(sample_repo, path, 0) is None
    assert enclosing_function(sample_repo, path, 999) is None
    assert enclosing_function(sample_repo, "src/widget/missing.py", 3) is None


def test_python_enclosing_ignores_strings_and_continuations(make_repo):
    ws = make_repo(
        {
            "m.py": (
                "def outer():\n"
                "    sql = '''\n"
                "SELECT 1\n"
                "'''\n"
                "    value = call(\n"
                "1,\n"
                "    )\n"
                "    return value\n"
                "class Box:\n"
                "    size = 3\n"
            ),
            "crlf.py": "def first():\r\n    return 1\r\n\r\ndef second():\r\n    return 2\r\n",
            "broken.py": "def f(:\n    return (\n",
        }
    )
    assert enclosing_function(ws, "m.py", 3) == "outer"
    assert enclosing_function(ws, "m.py", 6) == "outer"
    assert enclosing_function(ws, "m.py", 8) == "outer"
    assert enclosing_function(ws, "m.py", 10) == "Box"
    assert enclosing_function(ws, "crlf.py", 5) == "second"
    assert enclosing_function(ws, "broken.py", 2) == "f"


def test_brace_enclosing_function(make_repo):
    ws = make_repo({**C_FILES, **POLYGLOT, "notes.txt": "a\nb\n"})
    assert enclosing_function(ws, "lib/easy.c", 7) == "curl_easy_perform"
    assert enclosing_function(ws, "lib/easy.c", 12) == "curl_easy_perform"
    assert enclosing_function(ws, "lib/easy.c", 17) == "helper"
    assert enclosing_function(ws, "lib/easy.c", 20) is None  # after the closing brace
    assert enclosing_function(ws, "src/app.js", 2) == "load"
    assert enclosing_function(ws, "src/app.js", 10) == "get"
    assert enclosing_function(ws, "main.go", 4) == "Fetch"
    assert enclosing_function(ws, "src/Main.java", 3) == "runTask"
    assert enclosing_function(ws, "notes.txt", 1) is None


def test_is_external_symbol(sample_repo, make_repo):
    for name in ("len", "ValueError", "os.path.join", "json.loads", "strcpy", "console.log", "np"):
        assert is_external_symbol(name, sample_repo), name
    for name in ("parse_url", "Client.fetch", "widget.parser.parse_url", "", "not a name"):
        assert not is_external_symbol(name, sample_repo), name
    shadow = make_repo({"json/__init__.py": "def loads(s):\n    return s\n"}, name="shadow")
    assert not is_external_symbol("json.loads", shadow)


def test_defined_names():
    assert defined_names("def foo(x):\n    return 1\nclass Bar:\n    pass\n", "python") == [
        "foo",
        "Bar",
    ]
    # calls in a snippet of unknown language are not definitions
    assert defined_names("parse(url)\nfoo = 1\n", None) == []
    assert defined_names("int curl_easy_perform(CURL *d) {\n  return 0;\n}\n", "c") == [
        "curl_easy_perform"
    ]
    assert defined_names("    def indented(self):\r\n        pass\r\n") == ["indented"]
    assert defined_names("", "python") == []


def test_unicode_and_huge_files(make_repo):
    body = "".join(f"def f{i}():\n    return {i}\n" for i in range(5000))
    ws = make_repo({"big.py": body, "uni.py": "def grüße():\n    return 'ü'\n"})
    assert find_definitions(ws, "f4999")[0].line == 9999
    assert enclosing_function(ws, "big.py", 10000) == "f4999"
    assert find_definitions(ws, "grüße")[0].path == "uni.py"


def test_works_without_git(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "core.py").write_text("class Engine:\n    def start(self):\n        pass\n")
    ws = Workspace(tmp_path)
    assert [d.line for d in find_definitions(ws, "Engine.start")] == [2]
    assert enclosing_function(ws, "pkg/core.py", 3) == "start"
