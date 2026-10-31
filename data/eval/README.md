# Evaluation data

Closed GitHub issues whose maintainer labels serve as reference answers. The
issue *text* is stored so evaluation runs offline and is reproducible. Labels
are kept only in `gold` / `source_labels`, which Nomi never sees.

| File | Items | invalid | cannot_reproduce | confirmed |
|---|---:|---:|---:|---:|
| `dev.jsonl` | 37 | 12 | 14 | 11 |
| `test.jsonl` | 91 | 35 | 31 | 25 |
| `test-small.jsonl` (stratified subset of test, for slow model runs) | 24 | 8 | 8 | 8 |

## How it was collected (2026-11-01)

GitHub search API, newest first, `is:issue is:closed`, at most 12 issues per label:

| Repository | Label → gold |
|---|---|
| celery/celery | `Status: Confirmed ✔` → confirmed; `Status: Cannot Reproduce ✘`, `Status: Works For Me` → cannot_reproduce; `Status: Invalid ✘`, `Status: Not a Bug` → invalid |
| python-poetry/poetry | `status/confirmed` → confirmed; `status/needs-reproduction` (closed) → cannot_reproduce; `status/invalid` → invalid |
| pylint-dev/pylint | `Bug :beetle:` (closed) → confirmed; `Cannot reproduce 🤷` → cannot_reproduce; `Invalid` → invalid |

Issues carrying labels from more than one class were dropped (3 of 131). The
split is deterministic: `sha256(id) % 10 < 3` → dev, otherwise test.

## Leakage rules

- Nomi receives only the title, body and creation time (`EvalItem.to_submission`),
  never labels, comments or close state.
- The repository is checked out at the last default-branch commit **before the
  issue was filed**, so a bug fixed later does not look fabricated.

## Caveats

Maintainer labels are noisy reference signals, not ground truth. "Invalid" often
means "usage question" or "belongs elsewhere", and "cannot reproduce" often means
"environment-specific" or "the reporter went silent". Treat agreement with these
labels as a proxy. Pylint's `Bug :beetle:` label marks a triaged bug, which is
weaker evidence of confirmation than an explicit `confirmed` label.
