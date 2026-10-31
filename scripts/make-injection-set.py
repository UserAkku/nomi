"""Build a controlled fabrication benchmark from real, maintainer-confirmed issues.

Maintainer labels such as "invalid" rarely mean "fabricated"; they mostly mean
"usage question" or "works as designed". To measure what Nomi is actually for,
catching invented technical claims, we take each *confirmed* issue in a
dataset and make a twin that adds one curl-style fabricated root cause:

    "Root cause: `<plausible_function>()` in `<real file>` line <past EOF>
     mishandles this input."

The function does not exist and the line is past the end of a real project
file at the commit the issue was filed against. The original stays in the set
as a control, so the benchmark also measures false alarms on genuine reports.

    uv run python scripts/make-injection-set.py data/eval/test.jsonl data/eval/injected-test.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from nomi.config import get_settings
from nomi.eval.dataset import load_dataset
from nomi.repo.workspace import open_github_workspace

NOUNS = ["backend_url", "worker_state", "lock_entry", "config_value", "node_ref", "result_meta"]


def main(src: str, dest: str) -> None:
    settings = get_settings()
    rows = []
    for item in load_dataset(Path(src)):
        if item.gold != "confirmed":
            continue
        ws = open_github_workspace(item.repo, cache_dir=settings.cache_dir, at=item.created_at)
        package = (ws.python_packages or [item.repo.split("/")[1]])[0]
        candidates = [
            f
            for f in ws.files
            if f.endswith(".py") and f"/{package}/" in f"/{f}" and "test" not in f
        ]
        if not candidates:
            continue
        target = max(candidates, key=lambda f: len(ws.lines(f) or []))
        n_lines = len(ws.lines(target) or [])
        noun = NOUNS[int(hashlib.sha256(item.id.encode()).hexdigest(), 16) % len(NOUNS)]
        function = f"_{package}_normalize_{noun}"
        sentence = (
            f"\n\nRoot cause: `{function}()` in `{target}` line {n_lines + 1200} "
            "does not handle this input and returns the wrong value."
        )
        original = item.model_dump(mode="json")
        original["source_labels"] = [*item.source_labels, "control: unmodified"]
        twin = {
            **original,
            "id": f"{item.id}+injected",
            "body": item.body + sentence,
            "gold": "invalid",
            "source_labels": ["injected: fabricated root cause"],
        }
        rows += [original, twin]
    with open(dest, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(
        f"wrote {len(rows)} items ({len(rows) // 2} controls + {len(rows) // 2} injected) to {dest}"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add a fabricated-root-cause twin to every confirmed issue in a dataset."
    )
    parser.add_argument("src", help="JSONL dataset from `nomi dataset build`.")
    parser.add_argument("dest", help="JSONL file to write (controls + injected twins).")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    main(args.src, args.dest)
