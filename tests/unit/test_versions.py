from __future__ import annotations

import pytest

from nomi.models import Artifacts, CheckStatus, EvidenceKind, Signal
from nomi.verify.versions import Version, check_versions, normalize_tag, parse_version


def versions(*items: str) -> Artifacts:
    return Artifacts(versions=list(items))


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v1.1.0", "1.1.0"),
        ("1.2", "1.2"),
        ("curl-8_4_0", "8.4.0"),
        ("release/2.0-rc1", "2.0-rc1"),
        ("widget-v3.10.2", "3.10.2"),
        ("openssl-3.0.0-alpha1", "3.0.0-alpha1"),
        ("py3-1.2", "1.2"),
        ("latest", None),
        ("v3", None),
        ("", None),
    ],
)
def test_normalize_tag(tag, expected):
    result = normalize_tag(tag)
    assert (str(result) if result else None) == expected


def test_version_ordering():
    assert parse_version("v1.10.0").key > parse_version("1.9.9").key
    assert parse_version("2.0-rc1").key < parse_version("2.0").key
    assert parse_version("1.1").key == parse_version("1.1.0").key
    assert parse_version("no version here") is None
    assert Version((1, 2)).major == 1


def test_real_release_passes_and_newer_ones_are_reported(sample_repo):
    checks = check_versions(versions("1.0.0"), sample_repo, text="Since widget 1.0.0 fetch hangs.")
    real, newer = checks
    assert real.id == newer.id == "version.release"
    assert (real.status, real.signal, real.strength) == (CheckStatus.PASS, Signal.SUPPORTS, 0.1)
    assert real.title == "1.0.0 is a real release (tag v1.0.0)"
    assert real.evidence[0].kind is EvidenceKind.HISTORY
    assert real.evidence[0].url == "https://github.com/acme/widget/releases/tag/v1.0.0"
    assert (newer.status, newer.signal) == (CheckStatus.INFO, Signal.NEUTRAL)
    assert newer.title == (
        "1 newer release existed when this was reported (latest 1.1.0); it may already be fixed"
    )


def test_latest_release_has_no_newer_info(sample_repo):
    (check,) = check_versions(versions("v1.1.0", "1.1"), sample_repo)
    assert check.status is CheckStatus.PASS and "tag v1.1.0" in check.title


def test_invented_version_warns(sample_repo):
    text = "I'm running widget 1.4.2 on Python 3.12 and Ubuntu 22.04."
    (check,) = check_versions(versions("1.4.2", "3.12", "22.04"), sample_repo, text=text)
    assert (check.status, check.signal, check.strength) == (
        CheckStatus.WARN,
        Signal.CONTRADICTS,
        0.3,
    )
    assert check.title == "Version 1.4.2 does not exist (newest release at that commit: 1.1.0)"


def test_version_word_also_attributes(sample_repo):
    (check,) = check_versions(
        versions("1.9.0"), sample_repo, text="After upgrading to version 1.9.0"
    )
    assert check.status is CheckStatus.WARN


AMBIGUOUS = [
    ("1.4.2", "the bug appeared around 1.4.2"),  # nothing says whose version it is
    ("1.4.2", "Python version 1.4.2 crashes"),  # another product is named
    ("1.4.2", "version 1.4.2 of numpy breaks it"),
    ("Node 1.4.2", ""),
    ("2.0.0", "widget 2.0.0 breaks"),  # no 2.x release series exists
    ("1.0.5", "widget 1.0.5 breaks"),  # older than the newest release: maybe a branch
    ("1.5.0rc1", "widget 1.5.0rc1 breaks"),  # pre-releases are not judged
    ("1.4", ""),
]


def test_ambiguous_versions_are_ignored(sample_repo):
    for item, text in AMBIGUOUS:
        assert check_versions(versions(item), sample_repo, text=text) == [], (item, text)


def test_other_software_versions_never_flagged(make_repo):
    ws = make_repo({"x.py": "x = 1\n"}, tags=("v3.10.0", "v3.11.0", "v22.3.0"))
    text = "Python 3.12 on Ubuntu 22.04 with python3.13.1 and node v22.4.0"
    checks = check_versions(versions("Python 3.12", "22.04", "3.13.1", "v22.4.0"), ws, text=text)
    assert checks == []


def test_unreleased_version_in_tree_is_not_invented(make_repo):
    ws = make_repo({"pyproject.toml": 'version = "1.2.0"\n'}, tags=("v1.0.0", "v1.1.0"))
    assert check_versions(versions("1.2.0"), ws, text="widget 1.2.0 from main") == []


def test_curl_style_tags_and_project_name(make_repo):
    ws = make_repo({"lib/easy.c": "int x;\n"}, repo="curl/curl", tags=("curl-8_3_0", "curl-8_4_0"))
    text = "Tested with curl 8.4.0 and libcurl 8.9.1"
    checks = check_versions(versions("8.4.0", "8.9.1"), ws, text=text)
    assert [c.status for c in checks] == [CheckStatus.PASS, CheckStatus.WARN]
    assert checks[0].title == "8.4.0 is a real release (tag curl-8_4_0)"
    custom = check_versions(versions("8.9.1"), ws, text="tool 8.9.1", project_name="Tool")
    assert [c.status for c in custom] == [CheckStatus.WARN]


def test_no_tags_or_no_versions(make_repo, sample_repo):
    ws = make_repo({"a.py": "x = 1\n"}, name="untagged")
    assert check_versions(versions("1.0.0"), ws, text="widget 1.0.0") == []
    assert check_versions(Artifacts(), sample_repo) == []
    assert check_versions(versions("", "not a version", "é" * 1000), sample_repo, text="\x00") == []


def test_versions_pinned_to_other_packages_are_skipped(make_repo):
    ws = make_repo({"celery/__init__.py": ""}, repo="celery/celery", tags=("v5.0.0",))
    text = "software -> celery:5.0.0 (singularity) kombu:5.0.2 py:3.8.5 billiard:3.6.3"
    found = versions("5.0.0", "5.0.2", "3.8.5", "3.6.3")
    checks = check_versions(found, ws, text=text, project_name="celery")
    assert [c.title for c in checks if c.status is CheckStatus.WARN] == []
    assert [c.title for c in checks] == ["5.0.0 is a real release (tag v5.0.0)"]
    for pinned in ("kombu==5.0.2", "kombu=5.0.2", "billiard 5.0.2", "Kombu v5.0.2"):
        assert check_versions(versions("5.0.2"), ws, text=f"celery 5.0.0, {pinned}") == []


def test_versions_pinned_to_the_project_or_its_packages_are_still_judged(make_repo):
    ws = make_repo({"src/celerylib/__init__.py": ""}, repo="celery/celery", tags=("v5.0.0",))
    for text in (
        "celery:5.0.2",
        "Celery==5.0.2",
        "celery worker from celerylib 5.0.2",
        "upgrading to version 5.0.2",
        "celery multi v5.0.2 (singularity)",
    ):
        (check,) = check_versions(versions("5.0.2"), ws, text=text, project_name="Celery")
        assert check.status is CheckStatus.WARN, text
