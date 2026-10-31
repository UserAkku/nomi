from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

import pytest

from nomi.models import (
    ChangedFile,
    Check,
    CheckStatus,
    Claim,
    ClaimType,
    EvidenceKind,
    Kind,
    Signal,
    Submission,
)
from nomi.repo.workspace import Workspace
from nomi.verify.pr import MAX_EXCERPT_LINES, DiffLine, parse_patch, run_pr_checks
from tests.conftest import SAMPLE_FILES

PR_URL = "https://github.com/acme/widget/pull/7"
REAL_BODY = "This change makes the parser reject blank URLs with a clear error message."


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def hunk(*lines: str, old: int = 1, new: int = 1, heading: str = "") -> str:
    """A well-formed hunk from '+x' / '-x' / ' x' lines."""
    old_n = sum(1 for line in lines if line[:1] in "- ")
    new_n = sum(1 for line in lines if line[:1] in "+ ")
    head = f"@@ -{old},{old_n} +{new},{new_n} @@" + (f" {heading}" if heading else "")
    return "\n".join([head, *lines]) + "\n"


def changed(
    path: str, *lines: str, status: str = "modified", patch: str | None = None, **kw
) -> ChangedFile:
    text = patch if patch is not None else (hunk(*lines) if lines else None)
    return ChangedFile(
        path=path,
        status=status,
        additions=kw.pop("additions", sum(1 for line in lines if line.startswith("+"))),
        deletions=kw.pop("deletions", sum(1 for line in lines if line.startswith("-"))),
        patch=text,
        **kw,
    )


def pr(body: str = REAL_BODY, title: str = "Reject blank URLs", files=(), **kw) -> Submission:
    return Submission(
        kind=Kind.PULL_REQUEST,
        repo="acme/widget",
        number=7,
        url=PR_URL,
        title=title,
        body=body,
        changed_files=list(files),
        **kw,
    )


def by_id(checks: list[Check], check_id: str) -> list[Check]:
    return [c for c in checks if c.id == check_id]


def one(checks: list[Check], check_id: str) -> Check:
    found = by_id(checks, check_id)
    assert len(found) == 1, f"expected one {check_id}, got {found}"
    return found[0]


def change_claim(cid: str, text: str, *entities: str) -> Claim:
    return Claim(id=cid, text=text, type=ClaimType.CHANGE, entities=list(entities))


CODE_FIX = changed(
    "src/widget/parser.py",
    " def parse_url(url: str) -> dict:",
    "-    if not url:",
    "+    if not url or not url.strip():",
    '         raise ValueError("empty url")',
)
TEST_ADD = changed(
    "tests/test_parser.py",
    "+def test_blank():",
    "+    with pytest.raises(ValueError):",
    '+        parse_url("  ")',
    status="added",
)
DOC_EDIT = changed(
    "README.md",
    "-The default timeout is 10 seconds.",
    "+The default timeout is 10 seconds and can be changed with --timeout.",
)
WHITESPACE_ONLY = changed(
    "src/widget/net.py",
    "-DEFAULT_TIMEOUT=10",
    "+DEFAULT_TIMEOUT = 10",
    "-",
    "+   ",
)


# --------------------------------------------------------------------------
# parse_patch
# --------------------------------------------------------------------------


def test_parse_patch_numbers_lines_and_skips_no_newline_marker():
    patch = "@@ -3,3 +3,4 @@ def parse_url(url):\n a\n-b\n+c\n+d\n\\ No newline at end of file\n"
    [h] = parse_patch(patch)
    assert (h.old_start, h.new_start, h.heading) == (3, 3, "def parse_url(url):")
    assert h.lines == (
        DiffLine(" ", "a", 3, 3),
        DiffLine("-", "b", 4, None),
        DiffLine("+", "c", None, 4),
        DiffLine("+", "d", None, 5),
    )


def test_parse_patch_handles_crlf_multiple_hunks_and_headers():
    patch = (
        "diff --git a/x.py b/x.py\r\nindex 1..2 100644\r\n--- a/x.py\r\n+++ b/x.py\r\n"
        "@@ -1 +1 @@\r\n-old\r\n+new\r\n@@ -10,2 +10,2 @@\r\n ctx\r\n-x = 1\r\n+x = 2\r\n"
    )
    hunks = parse_patch(patch)
    assert [h.old_start for h in hunks] == [1, 10]
    assert [(line.kind, line.text) for line in hunks[0].lines] == [("-", "old"), ("+", "new")]
    assert hunks[1].lines[-1] == DiffLine("+", "x = 2", None, 11)


def test_parse_patch_tolerates_empty_malformed_and_odd_content():
    assert parse_patch(None) == []
    assert parse_patch("") == []
    assert parse_patch("no hunks here\n+not inside a hunk\n") == []
    [h] = parse_patch("@@ garbage @@\n+added\n\n-gone\n")
    assert h.old_start is None
    assert [(line.kind, line.text, line.new_line) for line in h.lines] == [
        ("+", "added", None),
        (" ", "", None),
        ("-", "gone", None),
    ]
    # form feeds and unicode separators are content, not line breaks
    [h] = parse_patch("@@ -1 +1 @@\n-a\x0cb\n+naïve\u2028ü\n")
    assert [line.text for line in h.lines] == ["a\x0cb", "naïve\u2028ü"]


# --------------------------------------------------------------------------
# Scope, pr.diff, pr.description, pr.size
# --------------------------------------------------------------------------


def test_issues_get_no_pr_checks():
    issue = Submission(kind=Kind.ISSUE, repo="acme/widget", title="Added tests", body="x")
    assert run_pr_checks(issue, [], None) == []


def test_missing_diff_is_skipped_and_only_description_checks_run():
    checks = run_pr_checks(pr(body="Fix crash. Added unit tests and updated the docs."), [], None)
    diff = one(checks, "pr.diff")
    assert diff.status is CheckStatus.SKIPPED and diff.signal is Signal.NEUTRAL
    assert "diff unavailable" in diff.detail
    assert {c.id for c in checks} <= {"pr.diff", "pr.description", "pr.linked_issue"}
    assert one(checks, "pr.linked_issue").title.lower().startswith("no linked issue")


def test_large_file_without_patch_is_reported_not_guessed():
    big = changed("src/widget/generated.py", additions=5000, deletions=10)
    checks = run_pr_checks(pr(files=[CODE_FIX, big]), [], None)
    diff = one(checks, "pr.diff")
    assert diff.status is CheckStatus.INFO
    assert "1 of 2" in diff.title and "src/widget/generated.py" in diff.detail


def test_full_diff_emits_no_pr_diff_check():
    assert by_id(run_pr_checks(pr(files=[CODE_FIX]), [], None), "pr.diff") == []


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   \n\n",
        "<!-- Describe your change here. Please link the issue. -->\n## Description\n\n## Testing\n",
        "## Checklist\n- [ ] I have added tests\n- [ ] I have updated the docs\n**Description:**\n",
        "<!-- unterminated template comment that hides everything after it\nreal text here",
        "\r\n## Summary\r\n\r\nfix\r\n",
    ],
)
def test_empty_or_template_only_description_warns(body):
    check = one(run_pr_checks(pr(body=body, files=[CODE_FIX]), [], None), "pr.description")
    assert check.status is CheckStatus.WARN
    assert check.signal is Signal.MISSING_INFO and check.strength == 0.4


@pytest.mark.parametrize(
    "body",
    [
        REAL_BODY,
        "## Description\n<!-- template -->\n" + REAL_BODY,
        "```python\nparse_url('   ')  # used to return an empty host\n```",
        "Ändert den Parser, damit leere URLs sauber abgelehnt werden. 修复空网址。",
    ],
)
def test_real_description_does_not_warn(body):
    assert by_id(run_pr_checks(pr(body=body, files=[CODE_FIX]), [], None), "pr.description") == []


def test_size_fires_on_many_lines_or_many_files_only():
    small = run_pr_checks(pr(files=[CODE_FIX]), [], None)
    assert by_id(small, "pr.size") == []
    lines = changed("src/widget/big.py", additions=900, deletions=200, status="modified")
    files = [changed(f"src/widget/m{i}.py", "+x = 1", status="added") for i in range(51)]
    for subject in (pr(files=[lines]), pr(files=files)):
        check = one(run_pr_checks(subject, [], None), "pr.size")
        assert check.status is CheckStatus.INFO and check.signal is Signal.NEUTRAL
        assert check.strength == 0.0 and "review in parts" in check.title


# --------------------------------------------------------------------------
# pr.tests_claim
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Fix blank URLs. Added unit tests.",
        "Tests added.",
        "Tests have been updated to cover blank input.",
        "Rejects blank URLs, along with tests.",
        "This path is now covered by tests.",
        "I wrote a new test case for the blank URL.",
        "- [x] I have added tests that prove my fix is effective",
        "Adds regression tests for #12.",
    ],
)
def test_tests_claim_without_test_files_fails(body):
    claims = [change_claim("C1", "Added unit tests"), change_claim("C2", "Rejects blank URLs")]
    check = one(run_pr_checks(pr(body=body, files=[CODE_FIX]), claims, None), "pr.tests_claim")
    assert check.status is CheckStatus.FAIL
    assert check.signal is Signal.CONTRADICTS and check.strength == 0.6
    assert check.claim_ids == ["C1"]
    [evidence] = check.evidence
    assert evidence.kind is EvidenceKind.DIFF and "src/widget/parser.py" in evidence.excerpt


def test_tests_claim_with_test_files_passes_with_diff_evidence():
    check = one(
        run_pr_checks(pr(body="Added unit tests.", files=[CODE_FIX, TEST_ADD]), [], None),
        "pr.tests_claim",
    )
    assert check.status is CheckStatus.PASS
    assert check.signal is Signal.SUPPORTS and check.strength == 0.3
    [evidence] = check.evidence
    assert evidence.path == "tests/test_parser.py" and evidence.kind is EvidenceKind.DIFF
    assert evidence.excerpt.splitlines()[0] == "+def test_blank():"
    assert (evidence.line_start, evidence.line_end) == (1, 3)
    anchor = hashlib.sha256(b"tests/test_parser.py").hexdigest()
    assert evidence.url == f"{PR_URL}/files#diff-{anchor}R1"


@pytest.mark.parametrize(
    "body",
    [
        REAL_BODY,
        "I didn't add tests because the parser has no test harness yet.",
        "No tests added.",
        "Tests will be added in a follow-up PR.",
        "Should I add tests for this?",
        "I added tests later in #13.",
        "<!-- Did you add tests? -->\n" + REAL_BODY,
        "- [ ] I have added tests that prove my fix is effective\n" + REAL_BODY,
        "Fixed the flaky behaviour; updated tests workflow to run on 3.12.",
        "> Added unit tests\n(quoting the issue) " + REAL_BODY,
        "```\n$ pytest\n3 tests added, 3 passed\n```\n" + REAL_BODY,
        "Fix an issue with tests failing on Windows.",
    ],
)
def test_tests_claim_not_detected(body):
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX]), [], None)
    assert by_id(checks, "pr.tests_claim") == []


# --------------------------------------------------------------------------
# pr.docs_claim
# --------------------------------------------------------------------------


def test_docs_claim_without_doc_files_fails():
    claims = [change_claim("C1", "Updated the documentation"), change_claim("C2", "Uses docker")]
    check = one(
        run_pr_checks(pr(body="Updated the documentation.", files=[CODE_FIX]), claims, None),
        "pr.docs_claim",
    )
    assert check.status is CheckStatus.FAIL
    assert check.signal is Signal.CONTRADICTS and check.strength == 0.6
    assert check.claim_ids == ["C1"]


@pytest.mark.parametrize(
    "doc",
    [
        DOC_EDIT,
        changed("docs/usage.md", "+More text."),
        changed("changelog.d/42.bugfix", "+Reject blank URLs.", status="added"),
        changed("CHANGELOG", "+* Reject blank URLs"),
    ],
)
def test_docs_claim_with_doc_files_passes(doc):
    body = "Reject blank URLs. README and changelog updated."
    check = one(run_pr_checks(pr(body=body, files=[CODE_FIX, doc]), [], None), "pr.docs_claim")
    assert check.status is CheckStatus.PASS
    assert check.signal is Signal.SUPPORTS and check.strength == 0.15
    assert check.evidence[0].path == doc.path


def test_docs_claim_backed_only_by_whitespace_warns():
    claims = [change_claim("C1", "Updated the README")]
    reflow = changed(
        "README.md", "-The default timeout is 10 seconds.", "+The default  timeout is 10 seconds.  "
    )
    body = "Reject blank URLs. README updated."
    check = one(
        run_pr_checks(pr(body=body, files=[CODE_FIX, reflow]), claims, None), "pr.docs_claim"
    )
    assert check.status is CheckStatus.WARN
    assert check.signal is Signal.CONTRADICTS and check.strength == 0.45
    assert "documentation claim backed only by whitespace changes" in check.title.lower()
    assert check.claim_ids == ["C1"] and check.evidence[0].path == "README.md"
    mixed = run_pr_checks(pr(body=body, files=[CODE_FIX, reflow, DOC_EDIT]), [], None)
    passed = one(mixed, "pr.docs_claim")
    assert passed.status is CheckStatus.PASS and "1 doc file(s)" in passed.title


@pytest.mark.parametrize(
    "body",
    [
        REAL_BODY,
        "Fix the docs build on Read the Docs.",
        "Updated docstrings of parse_url.",
        "Docs will be updated in a separate PR.",
        "I have not updated the documentation.",
        "- [x] My change requires a change to the documentation.",
    ],
)
def test_docs_claim_not_detected(body):
    assert by_id(run_pr_checks(pr(body=body, files=[CODE_FIX]), [], None), "pr.docs_claim") == []


def test_checked_template_box_counts_as_docs_claim():
    body = "- [x] I have made corresponding changes to the documentation\n" + REAL_BODY
    check = one(run_pr_checks(pr(body=body, files=[CODE_FIX]), [], None), "pr.docs_claim")
    assert check.status is CheckStatus.FAIL


# --------------------------------------------------------------------------
# pr.trivial_change
# --------------------------------------------------------------------------


def test_fix_claim_with_whitespace_only_diff_warns():
    claims = [change_claim("C1", "Fixes a crash in the client")]
    subject = pr(title="Fix crash in client", body="This fixes the crash.", files=[WHITESPACE_ONLY])
    check = one(run_pr_checks(subject, claims, None), "pr.trivial_change")
    assert check.status is CheckStatus.WARN
    assert check.signal is Signal.CONTRADICTS and check.strength == 0.45
    assert "whitespace/docs" in check.title and check.claim_ids == ["C1"]
    [evidence] = check.evidence
    assert evidence.path == "src/widget/net.py" and "+DEFAULT_TIMEOUT = 10" in evidence.excerpt


def test_fix_claim_with_docs_only_diff_warns():
    subject = pr(title="Update README.md", body="Fixed a memory leak.", files=[DOC_EDIT])
    check = one(run_pr_checks(subject, [], None), "pr.trivial_change")
    assert check.status is CheckStatus.WARN and "documentation" in check.detail


def test_whitespace_in_code_plus_doc_edits_still_warns():
    subject = pr(body="Improves performance of fetch.", files=[WHITESPACE_ONLY, DOC_EDIT])
    assert one(run_pr_checks(subject, [], None), "pr.trivial_change").status is CheckStatus.WARN


@pytest.mark.parametrize(
    ("title", "body", "files"),
    [
        ("Fix crash on blank URL", REAL_BODY, [CODE_FIX]),
        ("Fix typo in README", "Fixes a spelling mistake.", [DOC_EDIT]),
        ("docs: fix wrong default", "The README said 5 seconds.", [DOC_EDIT]),
        ("Update README", "Fixes #12", [DOC_EDIT]),
        ("Update README", "Closes https://github.com/acme/widget/issues/12", [DOC_EDIT]),
        ("Reformat net.py", "No functional change, just formatting.", [WHITESPACE_ONLY]),
        ("Tidy", "This is not a bug fix.", [WHITESPACE_ONLY]),
    ],
)
def test_trivial_change_not_flagged(title, body, files):
    checks = run_pr_checks(pr(title=title, body=body, files=files), [], None)
    assert by_id(checks, "pr.trivial_change") == []


@pytest.mark.parametrize(
    "code",
    [
        # Python indentation is semantic: dedenting a return is a real fix
        changed("src/widget/net.py", "-            return b''", "+        return b''"),
        changed("Makefile", "-    pytest", "+\tpytest"),
        # whitespace inside a string literal changes behaviour
        changed("src/widget/cli.py", '-    print("a  b")', '+    print("a b")'),
        # a pure rename without a patch is a change we cannot see into
        changed("src/widget/client.py", status="renamed", previous_path="src/widget/net.py"),
        # GitHub omitted the patch
        changed("src/widget/net.py", additions=3, deletions=3),
        changed("src/widget/new.py", "+", status="added"),
    ],
)
def test_semantic_or_invisible_changes_are_not_whitespace_only(code):
    subject = pr(title="Fix crash", body="Fixes the crash in fetch.", files=[code])
    assert by_id(run_pr_checks(subject, [], None), "pr.trivial_change") == []


# --------------------------------------------------------------------------
# pr.breaking_change
# --------------------------------------------------------------------------

REMOVE_NORMALIZE = changed(
    "src/widget/parser.py",
    patch=hunk(
        " ",
        "-def normalize_host(host: str) -> str:",
        "-    return host.strip().lower()",
        old=15,
        new=15,
    ),
)


@pytest.mark.parametrize(
    "body",
    [
        "Cleanup. No breaking changes.",
        "This is a non-breaking change.",
        "Fully backwards compatible.",
        "Breaking change: none",
        "- [x] Bug fix (non-breaking change which fixes an issue)",
    ],
)
def test_removed_public_function_contradicts_non_breaking_claim(body):
    claims = [change_claim("C1", "This is backwards compatible"), change_claim("C2", "x")]
    check = one(
        run_pr_checks(pr(body=body, files=[REMOVE_NORMALIZE]), claims, None), "pr.breaking_change"
    )
    assert check.status is CheckStatus.FAIL
    assert check.signal is Signal.CONTRADICTS and check.strength == 0.6
    assert "`normalize_host`" in check.title and check.claim_ids == ["C1"]
    [evidence] = check.evidence
    assert evidence.excerpt == "-def normalize_host(host: str) -> str:"
    assert (evidence.line_start, evidence.line_end) == (16, 16)
    assert evidence.url.endswith("L16")


def test_python_methods_count_but_nested_and_private_definitions_do_not():
    patch = hunk(
        "-class Client:",
        "-    def fetch(self, url):",
        "-        def _inner():",
        "-            pass",
        "-        def helper():",
        "-            pass",
        "-    def _private(self):",
        "-        pass",
        "-class _Hidden:",
        "-    def visible_name(self):",
        "-        pass",
    )
    subject = pr(body="No breaking changes.", files=[changed("src/widget/net.py", patch=patch)])
    check = one(run_pr_checks(subject, [], None), "pr.breaking_change")
    assert "`Client`, `Client.fetch`" in check.title
    for name in ("helper", "_inner", "_private", "_Hidden", "visible_name"):
        assert name not in check.title


def test_method_owner_found_from_hunk_heading():
    patch = hunk(
        "     retries = 3",
        "-    def fetch(self, url):",
        "-        return b''",
        heading="class Client:",
    )
    subject = pr(body="No breaking changes.", files=[changed("src/widget/net.py", patch=patch)])
    assert "`Client.fetch`" in one(run_pr_checks(subject, [], None), "pr.breaking_change").title


@pytest.mark.parametrize(
    "files",
    [
        # signature change: the name is re-added
        [
            changed(
                "src/widget/parser.py",
                "-def normalize_host(host: str) -> str:",
                "+def normalize_host(host: str, *, idna: bool = False) -> str:",
            )
        ],
        # moved to another module and re-exported
        [
            REMOVE_NORMALIZE,
            changed("src/widget/hosts.py", "+def normalize_host(host):", status="added"),
        ],
        [REMOVE_NORMALIZE, changed("src/widget/__init__.py", "+from .hosts import normalize_host")],
        # private helpers, tests, examples and private modules are not public API
        [changed("src/widget/parser.py", "-def _strip(host):")],
        [changed("tests/test_parser.py", "-def test_empty():", status="removed")],
        [changed("examples/demo.py", "-def run():")],
        [changed("src/widget/_compat.py", "-def shim():")],
        [changed("src/lib.rs", "-pub(crate) fn helper() {}")],
        [changed("pkg/client.go", "-func helper() {}")],
    ],
)
def test_non_breaking_claim_holds(files):
    subject = pr(body="No breaking changes.", files=files)
    assert by_id(run_pr_checks(subject, [], None), "pr.breaking_change") == []


@pytest.mark.parametrize(
    ("path", "line", "name"),
    [
        ("src/index.ts", "-export function parseUrl(url: string) {", "parseUrl"),
        ("src/index.js", "-export const DEFAULT_TIMEOUT = 10;", "DEFAULT_TIMEOUT"),
        ("src/index.ts", "-export default class Client {", "Client"),
        ("pkg/client.go", "-func (c *Client) Fetch(url string) error {", "Fetch"),
        ("pkg/client.go", "-func Parse(url string) (*URL, error) {", "Parse"),
        ("src/lib.rs", "-pub fn parse_url(url: &str) -> Url {", "parse_url"),
        ("src/lib.rs", "-    pub async fn fetch(&self) {", "fetch"),
    ],
)
def test_removed_public_names_in_other_languages(path, line, name):
    subject = pr(body="Backward compatible.", files=[changed(path, line)])
    assert f"`{name}`" in one(run_pr_checks(subject, [], None), "pr.breaking_change").title


@pytest.mark.parametrize(
    "body",
    [
        REAL_BODY,
        "This is a breaking change.",
        "Not sure if this is non-breaking.",
        "Is it backwards compatible?",
    ],
)
def test_breaking_check_needs_an_affirmed_claim(body):
    subject = pr(body=body, files=[REMOVE_NORMALIZE])
    assert by_id(run_pr_checks(subject, [], None), "pr.breaking_change") == []


# --------------------------------------------------------------------------
# pr.linked_issue
# --------------------------------------------------------------------------


def test_linked_issues_are_listed():
    body = (
        "Fixes #12\nAlso closes https://github.com/acme/widget/issues/34 and "
        "resolves other/repo#5. Fixes #12 again.\n<!-- Fixes #99 -->"
    )
    claims = [change_claim("C1", "Fixes #12"), change_claim("C2", "Fixes #120")]
    check = one(run_pr_checks(pr(body=body, files=[CODE_FIX]), claims, None), "pr.linked_issue")
    assert check.status is CheckStatus.INFO and check.signal is Signal.NEUTRAL
    assert check.title == "Links issue(s) #12, #34, other/repo#5."
    assert check.claim_ids == ["C1"]


def test_fix_without_linked_issue_is_noted_and_plain_change_is_not():
    fix = run_pr_checks(pr(title="Fix crash on blank URL", files=[CODE_FIX]), [], None)
    assert one(fix, "pr.linked_issue").title.lower().startswith("no linked issue")
    plain = run_pr_checks(pr(title="Reject blank URLs", files=[CODE_FIX]), [], None)
    assert by_id(plain, "pr.linked_issue") == []


# --------------------------------------------------------------------------
# pr.mentioned_files
# --------------------------------------------------------------------------


def test_changed_file_mentions_pass(sample_repo: Workspace):
    body = "Touches `parser.py` (see src/widget/parser.py:7) and tests/test_parser.py."
    claims = [change_claim("C1", "Changes parser.py"), change_claim("C2", "Unrelated")]
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX, TEST_ADD]), claims, sample_repo)
    check = one(checks, "pr.mentioned_files")
    assert check.status is CheckStatus.PASS
    assert check.signal is Signal.SUPPORTS and check.strength == 0.15
    assert "`parser.py`" in check.title and "`tests/test_parser.py`" in check.title
    assert check.claim_ids == ["C1"]
    assert [e.path for e in check.evidence] == [
        "src/widget/parser.py",
        "src/widget/parser.py",
        "tests/test_parser.py",
    ]


def test_mention_of_a_nonexistent_file_fails(sample_repo: Workspace):
    body = REAL_BODY + " The retry logic lives in src/widget/retry_policy.py."
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX]), [], sample_repo)
    check = one(checks, "pr.mentioned_files")
    assert check.status is CheckStatus.FAIL
    assert check.signal is Signal.CONTRADICTS and check.strength == 0.5
    assert check.title == (
        "The description mentions `src/widget/retry_policy.py`, which is neither changed nor "
        "present in the repository."
    )


def test_existing_but_unchanged_file_warns_with_permalink(sample_repo: Workspace):
    body = REAL_BODY + " By normalising the host in src/widget/net.py."
    claims = [
        Claim(id="C1", text="Normalises the host in src/widget/net.py", type=ClaimType.BEHAVIOR),
        change_claim("C2", "Touches the parser", "net.py"),
        change_claim("C3", "Rejects blank URLs"),
    ]
    check = one(
        run_pr_checks(pr(body=body, files=[CODE_FIX]), claims, sample_repo), "pr.mentioned_files"
    )
    assert check.status is CheckStatus.WARN
    assert check.signal is Signal.CONTRADICTS and check.strength == 0.25
    assert check.title == "`src/widget/net.py` is named as changed but is not touched by this PR."
    assert check.claim_ids == ["C1", "C2"]
    [evidence] = check.evidence
    assert evidence.kind is EvidenceKind.CODE and evidence.path == "src/widget/net.py"
    assert evidence.excerpt == '1 | """Tiny HTTP client."""'
    assert evidence.url == sample_repo.permalink("src/widget/net.py", 1)


def test_without_workspace_unchanged_mentions_are_unverifiable():
    body = REAL_BODY + " See src/widget/retry_policy.py."
    check = one(run_pr_checks(pr(body=body, files=[CODE_FIX]), [], None), "pr.mentioned_files")
    assert check.status is CheckStatus.INFO and "no repository checkout" in check.detail


@pytest.mark.parametrize(
    "mention",
    [
        "https://github.com/acme/widget/blob/main/src/widget/ghost.py",
        "/usr/lib/python3.12/site-packages/requests/api.py",
        "venv/lib/site-packages/foo/bar.py",
        "C:\\Users\\me\\proj\\ghost.py",
        "~/notes/ghost.md",
        "Node.js",
        "React.js",
        "version 1.2.3",
        "e.g. this",
    ],
)
def test_non_repository_paths_are_ignored(sample_repo: Workspace, mention):
    body = f"{REAL_BODY} Context: {mention}."
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX]), [], sample_repo)
    assert [c for c in by_id(checks, "pr.mentioned_files") if c.status is CheckStatus.FAIL] == []


def test_file_referenced_by_repository_text_is_not_fabricated(make_repo: Callable[..., Workspace]):
    ws = make_repo({**SAMPLE_FILES, ".gitignore": "secrets.local.json\n"})
    body = REAL_BODY + " Put your token in secrets.local.json."
    check = one(run_pr_checks(pr(body=body, files=[CODE_FIX]), [], ws), "pr.mentioned_files")
    assert check.status is CheckStatus.INFO


def test_renamed_file_matches_its_previous_path(sample_repo: Workspace):
    rename = changed("src/widget/client.py", status="renamed", previous_path="src/widget/net.py")
    body = REAL_BODY + " Renames src/widget/net.py."
    check = one(run_pr_checks(pr(body=body, files=[rename]), [], sample_repo), "pr.mentioned_files")
    assert check.status is CheckStatus.PASS
    assert check.evidence[0].path == "src/widget/client.py"


def test_unicode_and_case_insensitive_mentions(make_repo: Callable[..., Workspace]):
    ws = make_repo({**SAMPLE_FILES, "docs/übersicht.md": "# Übersicht\n"})
    body = REAL_BODY + " See docs/übersicht.md and readme.md."
    checks = run_pr_checks(pr(body=body, files=[DOC_EDIT]), [], ws)
    statuses = {c.status for c in by_id(checks, "pr.mentioned_files")}
    assert statuses == {CheckStatus.PASS, CheckStatus.WARN}


# --------------------------------------------------------------------------
# pr.mentioned_symbols
# --------------------------------------------------------------------------


def test_symbols_in_the_diff_pass(sample_repo: Workspace):
    body = REAL_BODY + " `parse_url()` now strips input; see `ValueError`."
    claims = [change_claim("C1", "parse_url strips input", "parse_url")]
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX]), claims, sample_repo)
    check = one(checks, "pr.mentioned_symbols")
    assert check.status is CheckStatus.PASS
    assert check.signal is Signal.SUPPORTS and check.strength == 0.2
    assert check.claim_ids == ["C1"]
    first = check.evidence[0]
    assert first.kind is EvidenceKind.DIFF and first.path == "src/widget/parser.py"
    assert first.excerpt == " def parse_url(url: str) -> dict:"


def test_symbol_in_repository_but_untouched_warns(sample_repo: Workspace):
    body = REAL_BODY + " `Client.fetch` callers benefit too."
    claims = [
        Claim(id="C1", text="Client.fetch callers benefit", type=ClaimType.BEHAVIOR),
        change_claim("C2", "Rejects blank URLs"),
    ]
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX]), claims, sample_repo)
    check = one(checks, "pr.mentioned_symbols")
    assert check.status is CheckStatus.WARN
    assert check.signal is Signal.CONTRADICTS and check.strength == 0.2
    assert check.claim_ids == ["C1"]
    assert "mentioned but not touched by this PR" in check.title
    [evidence] = check.evidence
    assert evidence.path == "src/widget/net.py" and "def fetch" in evidence.excerpt
    assert evidence.url and "#L12" in evidence.url


def test_fabricated_symbols_fail_but_prose_words_do_not(sample_repo: Workspace):
    body = (
        REAL_BODY
        + " It also wires `RetryPolicyManager` into `apply_backoff()` via `widget.retry`. "
        "Tested on `Ubuntu` with `pytest`, `None` and `HTTP` handling unchanged."
    )
    claims = [change_claim("C1", "Adds RetryPolicyManager", "RetryPolicyManager")]
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX]), claims, sample_repo)
    [check] = by_id(checks, "pr.mentioned_symbols")
    assert check.status is CheckStatus.FAIL
    assert check.signal is Signal.CONTRADICTS and check.strength == 0.5
    assert "`RetryPolicyManager`, `apply_backoff`, `widget.retry`" in check.title
    for word in ("Ubuntu", "pytest", "None", "HTTP"):
        assert f"`{word}`" not in check.title
    assert check.claim_ids == ["C1"]


def test_symbols_are_not_failed_when_patches_are_missing(sample_repo: Workspace):
    big = changed("src/widget/generated.py", additions=4000, deletions=0, status="added")
    body = REAL_BODY + " Adds `RetryPolicyManager`."
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX, big]), [], sample_repo)
    [check] = by_id(checks, "pr.mentioned_symbols")
    assert check.status is CheckStatus.INFO and "omitted the patch" in check.detail


def test_symbols_without_workspace_are_unverifiable():
    body = REAL_BODY + " Adds `RetryPolicyManager`."
    [check] = by_id(
        run_pr_checks(pr(body=body, files=[CODE_FIX]), [], None), "pr.mentioned_symbols"
    )
    assert check.status is CheckStatus.INFO and "no repository checkout" in check.detail


def test_symbol_matching_only_a_changed_path_passes(sample_repo: Workspace):
    body = REAL_BODY + " Only `widget.parser` changes."
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX]), [], sample_repo)
    check = one(checks, "pr.mentioned_symbols")
    assert check.status is CheckStatus.PASS
    [evidence] = check.evidence
    assert evidence.path == "src/widget/parser.py" and evidence.excerpt is None


def test_plain_backticked_words_are_only_used_as_support(sample_repo: Workspace):
    body = REAL_BODY + " Run `pytest` and `widget` as usual; `url` handling changed."
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX]), [], sample_repo)
    check = one(checks, "pr.mentioned_symbols")
    assert check.status is CheckStatus.PASS and check.title.endswith("`url`.")


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


def test_crlf_body_and_patch(sample_repo: Workspace):
    patch = hunk(" def parse_url(url):", "-    if not url:", "+    if not url.strip():").replace(
        "\n", "\r\n"
    )
    body = "Fixes #3.\r\nAdded unit tests.\r\nTouches `parse_url`.\r\n"
    files = [changed("src/widget/parser.py", patch=patch, additions=1, deletions=1)]
    checks = run_pr_checks(pr(body=body, files=files), [], sample_repo)
    assert one(checks, "pr.tests_claim").status is CheckStatus.FAIL
    assert one(checks, "pr.mentioned_symbols").evidence[0].excerpt == " def parse_url(url):"
    assert one(checks, "pr.linked_issue").title == "Links issue(s) #3."


def test_evidence_excerpts_are_capped():
    lines = [f"+line {i}" for i in range(40)]
    subject = pr(body="Added tests.", files=[changed("tests/test_big.py", *lines, status="added")])
    [evidence] = one(run_pr_checks(subject, [], None), "pr.tests_claim").evidence
    assert len(evidence.excerpt.splitlines()) == MAX_EXCERPT_LINES


def test_huge_diff_and_description_are_fast(sample_repo: Workspace):
    files = [
        changed(
            f"src/widget/mod{i}.py",
            *[f"+value_{i}_{j} = compute({j})  # ünïcode ✓" for j in range(2000)],
            status="added",
        )
        for i in range(60)
    ]
    body = (
        "Fixes #1. Added tests. Updated docs. No breaking changes. "
        + " ".join(f"`symbol_{k}` src/widget/mod{k}.py" for k in range(200))
        + " added tests," * 5000
        + "<!--" * 20000
    )
    start = time.perf_counter()
    checks = run_pr_checks(pr(body=body, files=files), [], sample_repo)
    assert time.perf_counter() - start < 2
    assert one(checks, "pr.size").status is CheckStatus.INFO


@pytest.mark.parametrize(
    "body",
    [
        "a" * 200_000,
        "`" * 50_000,
        "src/" * 30_000 + "x.py",
        "fix " * 30_000,
        "\x00\x01\u202e`parse_url`\u202c",
        "[x](" * 10_000,
    ],
)
def test_adversarial_descriptions_do_not_crash(sample_repo: Workspace, body):
    start = time.perf_counter()
    checks = run_pr_checks(pr(body=body, files=[CODE_FIX]), [], sample_repo)
    assert time.perf_counter() - start < 3
    assert all(isinstance(c, Check) for c in checks)


def test_output_is_deterministic(sample_repo: Workspace):
    body = (
        "Fix crash. Added unit tests. Updated README. No breaking changes. Fixes #4.\n"
        "Touches `parse_url`, `Client.fetch`, `GhostThing_x` and src/widget/ghost.py."
    )
    subject = pr(body=body, files=[CODE_FIX, DOC_EDIT, REMOVE_NORMALIZE])
    first = [c.model_dump() for c in run_pr_checks(subject, [], sample_repo)]
    second = [c.model_dump() for c in run_pr_checks(subject, [], sample_repo)]
    assert first == second
    assert [c["id"] for c in first] == [
        "pr.tests_claim",
        "pr.docs_claim",
        "pr.breaking_change",
        "pr.mentioned_files",
        "pr.mentioned_symbols",
        "pr.mentioned_symbols",
        "pr.mentioned_symbols",
        "pr.linked_issue",
    ]


def test_only_change_claims_are_linked():
    claims = [
        Claim(id="C1", text="Added tests", type=ClaimType.BEHAVIOR),
        change_claim("C2", "Testing was extended"),
        change_claim("C3", "Contest entries are now sorted"),
    ]
    check = one(
        run_pr_checks(pr(body="Added tests.", files=[CODE_FIX]), claims, None), "pr.tests_claim"
    )
    assert check.claim_ids == ["C2"]


def test_evidence_urls_fall_back_to_repo_and_number():
    subject = pr(body="Added tests.", files=[TEST_ADD]).model_copy(update={"url": None})
    [evidence] = one(run_pr_checks(subject, [], None), "pr.tests_claim").evidence
    assert evidence.url.startswith(f"{PR_URL}/files#diff-")
    anonymous = subject.model_copy(update={"number": None})
    [evidence] = one(run_pr_checks(anonymous, [], None), "pr.tests_claim").evidence
    assert evidence.url is None


# --------------------------------------------------------------------------
# Precision guards for the tests and docs claims
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "test_file",
    [
        changed("app/tests.py", "+def test_blank(): ..."),
        changed("conftest.py", "+import pytest"),
        changed("spec/parser_spec.rb", "+it 'rejects blank urls' do"),
        changed("src/test/java/acme/ParserTest.java", "+@Test void blank() {}"),
        changed("Parser/ParserTests.cs", "+[Fact] public void Blank() {}"),
        changed("tests/fixtures/blank.json", "+{}"),
    ],
)
def test_other_test_conventions_count_as_test_files(test_file):
    checks = run_pr_checks(pr(body="Added tests.", files=[CODE_FIX, test_file]), [], None)
    assert one(checks, "pr.tests_claim").status is CheckStatus.PASS


@pytest.mark.parametrize(
    "source",
    [
        changed(
            "src/lib.rs", "+#[cfg(test)]", "+mod tests {", "+    #[test]", "+    fn blank() {}"
        ),
        changed("src/widget/parser.py", '+    """', "+    >>> parse_url('http://x')['host']"),
        changed("pkg/parse.go", "+func TestBlank(t *testing.T) {"),
        changed("src/parse.ts", "+describe('parseUrl', () => {"),
    ],
)
def test_inline_tests_in_source_files_support_the_claim(source):
    check = one(
        run_pr_checks(pr(body="Added unit tests.", files=[source]), [], None), "pr.tests_claim"
    )
    assert check.status is CheckStatus.PASS and check.signal is Signal.SUPPORTS
    assert check.evidence[0].path == source.path


def test_generic_docs_claim_with_docstring_changes_is_not_a_contradiction():
    docstring = changed(
        "src/widget/parser.py",
        " def parse_url(url: str) -> dict:",
        '-    """Split a URL into its parts."""',
        '+    """Split a URL into its parts. Blank URLs raise ValueError."""',
    )
    generic = run_pr_checks(pr(body="Updated the documentation.", files=[docstring]), [], None)
    check = one(generic, "pr.docs_claim")
    assert check.status is CheckStatus.INFO and check.signal is Signal.NEUTRAL
    assert check.evidence[0].excerpt.startswith('+    """Split')
    named = run_pr_checks(pr(body="Updated the README.", files=[docstring]), [], None)
    assert one(named, "pr.docs_claim").status is CheckStatus.FAIL


def test_quoted_release_notes_are_not_the_authors_claims():
    body = (
        "Bumps [httpx](https://github.com/encode/httpx) from 0.27.0 to 0.28.1.\n"
        "<details><summary>Release notes</summary>\n<blockquote>\n"
        "<li>Fix crash. Added unit tests. No breaking changes. Updated the docs.</li>\n"
        "<li>See `GhostThing_x` in src/httpx/ghost.py</li>\n"
        "</BLOCKQUOTE>\n</details>\n"
    )
    docs_dep = changed("docs/requirements.txt", "-httpx==0.27.0", "+httpx==0.28.1")
    assert run_pr_checks(pr(title="Bump httpx", body=body, files=[docs_dep]), [], None) == []


def test_adjectives_between_verb_and_tests_still_count():
    body = "- Added comprehensive unit tests\n- Wrote a couple of failing tests first"
    check = one(run_pr_checks(pr(body=body, files=[CODE_FIX]), [], None), "pr.tests_claim")
    assert check.status is CheckStatus.FAIL and '"Added comprehensive unit tests"' in check.detail


def test_mentioned_binary_or_empty_files_cite_no_lines(make_repo: Callable[..., Workspace]):
    ws = make_repo({**SAMPLE_FILES, "src/widget/py.typed": "", "assets/logo.png": "\x00PNG"})
    body = REAL_BODY + " Ships assets/logo.png and keeps src/widget/__init__.py as is."
    files = [changed("src/widget/empty.py", "+x = 1", status="added")]
    ws_empty = make_repo({"src/widget/__init__.py": "", "assets/logo.png": "\x00PNG"}, name="other")
    for workspace in (ws, ws_empty):
        check = one(run_pr_checks(pr(body=body, files=files), [], workspace), "pr.mentioned_files")
        assert check.status is CheckStatus.WARN
        for evidence in check.evidence:
            lines = workspace.lines(evidence.path)
            assert lines and 1 <= evidence.line_start <= len(lines)
