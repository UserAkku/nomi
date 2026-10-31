# Adversarial review: findings and fixes

Before release, four independent reviewers each examined Nomi through one lens. Every finding came with a reproduced failure scenario, and every one was fixed with a regression test that fails on the old code ([PR #4](https://github.com/UserAkku/nomi/pull/4), plus follow-ups in the release-polish PR).

## Security

1. **High**: Workspace.read_text follows tracked symlinks: a PR can make Nomi read host files, or block forever on /dev/zero or a FIFO (`src/nomi/repo/workspace.py`)
2. **Medium**: find_report_comment falls back to a human-authored comment that contains the marker, so Nomi edits a stranger's comment instead of posting its own (`src/nomi/github/publish.py`)
3. **Medium**: Reproduction check is forgeable: an exception the snippet raises or prints itself counts as 'Reproduced' (+0.9 SUPPORTS), even with exit code 0 (`src/nomi/verify/sandbox.py`)
4. **Medium**: Unbounded job queue with no per-issue/PR coalescing lets any stranger starve the single triage worker (`src/nomi/github/app.py`)
5. **Low**: _plain does not neutralize autolinks or cross-repo references, so attacker text becomes clickable links and backlinks in the bot's comment (`src/nomi/report.py`)

## False accusations

6. **Critical**: Reporter's own relative paths under generic top-level dirs (tests/, src/, lib/) are treated as 'strong' project paths, so a missing file is a hard 0.7 FAIL (`src/nomi/verify/grounding.py`)
7. **Critical**: Absolute paths in the reporter's home dir (/Users/x/app/src/components/...) count as 'strong' project paths when a common dir pair such as src/components also exists in the repo (`src/nomi/verify/grounding.py`)
8. **High**: Bare file names from the reporter's own script (manage.py, app.py, main.py, App.java) resolve to a unique same-named repo file and are line-checked: hard 0.75 FAIL (`src/nomi/repo/workspace.py`)
9. **High**: GitHub blob links to OTHER repositories become file refs of this repo, so an upstream link is reported as a fabricated file (`src/nomi/extract/artifacts.py`)
10. **High**: Reporter's own helper function gets a 0.8 'not defined' FAIL just because the same sentence cites a real repo file (`src/nomi/verify/grounding.py`)
11. **Medium**: Line refs are checked against the default branch even when the report names an existing release tag; one site-packages frame of that release gives likely_invalid (`src/nomi/verify/grounding.py`)
12. **Medium**: The reporter's own subclass that overrides a repo method is flagged as a 'possibly fabricated' quote (FAIL 0.6) (`src/nomi/verify/quotes.py`)

## Robustness

13. **Critical**: _MD_HEADING regex has cubic backtracking: one short heading line can stall analysis for minutes (`src/nomi/extract/completeness.py`)
14. **High**: _FENCE_OPEN lazy info plus [ \t]*$ backtracks quadratically on the first extraction stage (`src/nomi/extract/artifacts.py`)
15. **High**: Stack-trace regexes ending in lazy .*?/.+? followed by \s*$ are quadratic on lines with long whitespace runs (`src/nomi/extract/artifacts.py`)
16. **High**: Sentence splitting is O(n^2) on repeated abbreviations, and claims run on the untruncated body (`src/nomi/extract/claims.py`)
17. **High**: Workspace.lines uses str.splitlines, so line numbers drift from git/traceback/GitHub numbering and real traces are reported as contradicting (`src/nomi/repo/workspace.py`)
18. **Medium**: _RUST_PANIC greedy msg group backtracks over the rest of the line from every 'thread ...' start (`src/nomi/extract/artifacts.py`)
19. **Low**: _find_issue_refs rescans to the line start for every '#N' match, which is O(n^2) on a long single line (`src/nomi/extract/artifacts.py`)

## Evaluation validity

20. **High**: Time travel can pick a commit that was not on the default branch when the issue was filed (no --first-parent) (`src/nomi/repo/workspace.py`)
21. **High**: Metrics score every row in predictions.jsonl, not the dataset/limit being evaluated; resume never checks dataset, gold or config (`src/nomi/eval/runner.py`)
22. **Medium**: A failed repository checkout during eval becomes a scored, non-error prediction that resume never retries (`src/nomi/pipeline.py`)
23. **Low**: README points to evaluation results that do not exist (`README.md`)
