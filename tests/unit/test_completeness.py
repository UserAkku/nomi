from __future__ import annotations

import time

import pytest

from nomi.extract.artifacts import extract_artifacts
from nomi.extract.completeness import WEIGHTS, assess_completeness
from nomi.models import Artifacts, Completeness, Kind, Section, Submission

FENCE = "```"

GITHUB_TEMPLATE = """**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior:
1. Go to '...'
2. Click on '....'
3. Scroll down to '....'
4. See error

**Expected behavior**
A clear and concise description of what you expected to happen.

**Screenshots**
If applicable, add screenshots to help explain your problem.

**Desktop (please complete the following information):**
 - OS: [e.g. iOS]
 - Browser [e.g. chrome, safari]
 - Version [e.g. 22]

**Additional context**
Add any other context about the problem here.
"""

ISSUE_FORM_TEMPLATE = """### Steps to reproduce

_No response_

### Expected behavior

<!-- Tell us what should happen -->

### Actual behavior

N/A

### Version

-

### Environment

- [ ]

### Logs

```
```
"""

COMPLETE_REPORT = f"""Parsing an empty URL crashes the client.

## Steps to reproduce
1. Install widget 1.1.0 with `pip install widget==1.1.0`
2. Run the snippet below

{FENCE}python
from widget.net import Client
Client().fetch("")
{FENCE}

## Expected behavior
`fetch("")` should raise a clear `ValueError` before any request is made.

## Actual behavior
It crashes with a traceback instead:

{FENCE}
Traceback (most recent call last):
  File "repro.py", line 2, in <module>
    Client().fetch("")
  File "src/widget/net.py", line 13, in fetch
    parts = parse_url(url)
ValueError: empty url
{FENCE}

## Environment
- OS: Ubuntu 22.04
- Python version: 3.12.1
"""


def issue(body: str, title: str = "Bug report") -> Submission:
    return Submission(kind=Kind.ISSUE, repo="acme/widget", title=title, body=body)


def assess(body: str, title: str = "Bug report") -> Completeness:
    sub = issue(body, title)
    return assess_completeness(sub, extract_artifacts(sub.text))


def test_weights_sum_to_one() -> None:
    assert set(WEIGHTS) == set(Section)
    assert round(sum(WEIGHTS.values()), 6) == 1.0


def test_pull_requests_are_not_scored() -> None:
    pr = Submission(kind=Kind.PULL_REQUEST, repo="acme/widget", title="Fix", body="")
    assert assess_completeness(pr, Artifacts()) == Completeness(present=[], missing=[], score=1.0)


def test_complete_report_scores_high() -> None:
    result = assess(COMPLETE_REPORT, title="Client.fetch crashes on empty URL")
    assert result.score >= 0.8
    assert result.present == list(Section)
    assert result.missing == []


@pytest.mark.parametrize("template", [GITHUB_TEMPLATE, ISSUE_FORM_TEMPLATE])
def test_template_only_body_scores_low(template: str) -> None:
    result = assess(template, title="Bug")
    assert result.score <= 0.1
    assert Section.STEPS_TO_REPRODUCE in result.missing
    assert Section.EXPECTED_BEHAVIOR in result.missing


def test_empty_issue_misses_everything() -> None:
    result = assess("", title="Help")
    assert result.present == []
    assert result.missing == list(Section)
    assert result.score == 0.0


def test_missing_is_in_enum_order_and_score_is_rounded() -> None:
    result = assess("## Expected behavior\nIt returns 3.\n\n## Version\n2.0\n", title="x")
    assert result.present == [
        Section.EXPECTED_BEHAVIOR,
        Section.ACTUAL_BEHAVIOR,
        Section.VERSION,
    ]
    assert result.missing == [s for s in Section if s not in result.present]
    assert result.score == 0.35


def test_html_comments_are_ignored() -> None:
    body = "## Steps to reproduce\n<!-- 1. run this\n2. run that -->\n\n## Logs\n<!-- paste -->\n"
    result = assess(body, title="Odd")
    assert result.present == []


def test_unclosed_html_comment_hides_rest() -> None:
    result = assess("Hello\n<!-- Expected: it should work\nrun `x`", title="Hi")
    assert result.present == []


def test_labelled_lines_with_values() -> None:
    body = "OS: macOS 14\nVersion: 2.3.0\nExpected result: 4\nActual result: 5\n"
    result = assess(body, title="x")
    assert result.present == [
        Section.EXPECTED_BEHAVIOR,
        Section.ACTUAL_BEHAVIOR,
        Section.VERSION,
        Section.ENVIRONMENT,
    ]


def test_labelled_lines_with_placeholder_values_do_not_count() -> None:
    result = assess("OS: e.g. Windows\nVersion: N/A\nObserved: ...\n", title="x")
    assert result.present == []


def test_heading_with_code_block_content_counts() -> None:
    body = f"### How to reproduce\n{FENCE}\nwidget fetch\n{FENCE}\n"
    assert Section.STEPS_TO_REPRODUCE in assess(body, title="x").present


def test_hash_comment_in_code_is_not_a_heading() -> None:
    body = f"### Expected behaviour\n{FENCE}python\n# Paste your code here\n{FENCE}\n"
    result = assess(body, title="x")
    assert Section.EXPECTED_BEHAVIOR not in result.present
    assert Section.CODE_SAMPLE not in result.present


def test_artifacts_drive_sections() -> None:
    body = (
        "Seen on Windows with Python 3.11 and widget 1.1.0.\n"
        f"{FENCE}python\nparse_url('')\n{FENCE}\n"
        "Error: boom\n"
    )
    result = assess(body, title="x")
    for section in (Section.CODE_SAMPLE, Section.LOGS, Section.VERSION, Section.ENVIRONMENT):
        assert section in result.present


def test_log_block_is_logs_not_code_sample() -> None:
    body = f"{FENCE}\n2024-01-01 12:00:00 ERROR failed\n2024-01-01 12:00:01 INFO retry\n{FENCE}\n"
    result = assess(body, title="x")
    assert Section.LOGS in result.present
    assert Section.CODE_SAMPLE not in result.present


@pytest.mark.parametrize(
    "body",
    [
        "1. open the app\n2. press the button\n",
        "- install the package\n- call fetch\n",
        "Run `widget fetch` to see it.",
        f"{FENCE}\n$ widget fetch\n{FENCE}",
        f"{FENCE}\n>>> parse_url('')\n{FENCE}",
    ],
)
def test_step_detection(body: str) -> None:
    assert Section.STEPS_TO_REPRODUCE in assess(body, title="x").present


def test_plain_bullets_and_checklists_are_not_steps() -> None:
    body = "- [x] I searched existing issues\n- [x] I read the docs\n- apples\n- pears\n"
    assert Section.STEPS_TO_REPRODUCE not in assess(body, title="x").present


@pytest.mark.parametrize(
    ("body", "section"),
    [
        ("It should return a dict.", Section.EXPECTED_BEHAVIOR),
        ("It returns None instead.", Section.ACTUAL_BEHAVIOR),
        ("The parser crashes.", Section.ACTUAL_BEHAVIOR),
        ("but got 3", Section.ACTUAL_BEHAVIOR),
        ("Tested with Node v18 inside Docker.", Section.ENVIRONMENT),
        ("Built with Go 1.21.", Section.ENVIRONMENT),
        ("Running on Java 17.", Section.ENVIRONMENT),
    ],
)
def test_keyword_detection(body: str, section: Section) -> None:
    assert section in assess(body, title="x").present


def test_crlf_and_unicode_body() -> None:
    body = "## Steps to reproduce\r\n1. Öffne die App 🚀\r\n2. Klicke\r\n\r\n## Expected behavior\r\nNichts\r\n"
    result = assess(body, title="Fehler")
    assert Section.STEPS_TO_REPRODUCE in result.present
    assert Section.EXPECTED_BEHAVIOR in result.present


def test_huge_body_is_bounded() -> None:
    result = assess("lorem ipsum " * 50_000, title="x")
    assert 0.0 <= result.score <= 1.0


# ---------------------------------------- regex backtracking (review group D)

BLANKS = " " * 50_000


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("#" + BLANKS + "x", id="D1-heading-blank-run"),
        pytest.param("# a" + BLANKS + "b", id="D1-heading-label-then-blanks"),
        pytest.param("**a**" + BLANKS + ":" + BLANKS + "b", id="bold-line"),
        pytest.param(BLANKS + "b", id="placeholder-empty-bullet"),
        pytest.param("- [ ]" + BLANKS + "b", id="placeholder-checkbox"),
        pytest.param("1." + BLANKS + "b", id="placeholder-numbered"),
        pytest.param("- " + BLANKS + "b", id="placeholder-bullet"),
        pytest.param("[eg " * 50_000, id="example-bracket"),
        pytest.param("a" + "\n" * 200_000 + "b", id="multiline-blank-lines"),
        pytest.param("a" + " \n" * 100_000 + "b", id="multiline-blank-run"),
    ],
)
def test_adversarial_body_is_assessed_in_linear_time(body: str) -> None:
    start = time.perf_counter()
    assess_completeness(issue(body, title="x"), Artifacts())
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    "heading",
    [
        "## Logs ##",
        "## Logs ##" + " " * 600,
        "## Logs " + "x" * 600,
        "**Logs** :   ",
        "__Logs:__",
    ],
)
def test_heading_variants_still_open_their_section(heading: str) -> None:
    body = f"{heading}\nsomething went wrong here\n"
    assert assess_completeness(issue(body, title="x"), Artifacts()).present == [Section.LOGS]


def test_template_placeholders_still_blank_out() -> None:
    body = "## Logs\n- [ ]   ...\n1.  N/A\n// please paste the output here\n[e.g. a log]\n"
    assert assess_completeness(issue(body, title="x"), Artifacts()).present == []
