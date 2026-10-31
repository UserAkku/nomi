# Evaluation results

Every number here comes from a real run of `nomi eval` on this laptop (MacBook Pro M3, 8 GB). Each folder pairs the metrics (`*.metrics.md`) with per-issue predictions (`*.predictions.jsonl`). Nomi only ever saw each issue's title, body and creation time. The repository was checked out at the default branch's last first-parent commit **before the issue was filed**.

## 1. Fabrication benchmark: does Nomi catch invented claims?

`injection-benchmark.*` (built by [`scripts/make-injection-set.py`](../../scripts/make-injection-set.py)). Each of 19 maintainer-**confirmed** test issues appears twice:
- unchanged, as a control;
- with one added curl-style root cause: a plausible but non-existent function, and a line number past the end of a real project file at the issue's commit.

| | Result |
|---|---|
| Fabricated twins flagged **likely invalid** | **19 / 19** |
| Genuine controls flagged invalid | **0 / 19** |
| Selective accuracy (answered items) | 96.8 % |

Deterministic mode, about 1 s per issue.

## 2. Real maintainer labels (held-out test split)

`test-deterministic.*`: 91 closed issues from celery, poetry and pylint, labelled *invalid* (35), *cannot reproduce* (31) or *confirmed* (25).

| Metric | Nomi (deterministic) | Majority baseline |
|---|---:|---:|
| False-invalid rate (confirmed bugs called invalid) | **0 %** (0 / 25) | 0 % |
| Coverage (answered instead of abstaining) | 69.2 % | 100 % |
| Selective accuracy (answered items) | 47.6 % | 38.5 % |
| Accuracy (abstentions count as wrong) | 33.0 % | 38.5 % |
| Macro F1 | 0.34 | 0.19 |

## 3. Local model vs. deterministic, same 24-issue subset

`test-small-model.*` and `test-small-deterministic.*` cover a stratified subset with 8 issues per class.

| | Deterministic | + Qwen3 4B (local) |
|---|---:|---:|
| Selective accuracy | 50.0 % | 46.7 % |
| Accuracy | 29.2 % | 29.2 % |
| False-invalid rate | 0 % | 0 % |
| Time per issue | about 1 s | about 166 s |

The model run used the code at commit `85abb1f`, before the adversarial-review hardening. The deterministic runs use the final code.

## How to read this

- **What Nomi is built for works.** Invented functions, files and line numbers are caught with no false alarms on genuine reports (benchmark 1), and across 128 real issues Nomi **never called a confirmed bug invalid**.
- **Maintainer labels are not truth labels.** In these repositories *invalid* almost always means "usage question" or "works as designed", not "fabricated". Those reports describe code that really exists, so no code-grounded check can call them false, and Nomi's recall on *invalid* stays near zero. Spotting "documented behaviour" is the next step (see the README roadmap).
- **The local model did not improve label agreement** on this subset, and we report that rather than hide it. Its value is in breaking a report into individual claims and giving line-cited explanations. Verdict-changing evidence comes from deterministic checks by design, so a 4B model's mistakes can't produce a false "invalid".
- Abstention is a feature: Nomi answers *inconclusive* instead of guessing, so read accuracy next to coverage.

Reproduce:

```bash
uv run nomi eval data/eval/test.jsonl --no-llm --out eval-runs/test
uv run python scripts/make-injection-set.py data/eval/test.jsonl data/eval/injected-test.jsonl
uv run nomi eval data/eval/injected-test.jsonl --no-llm --out eval-runs/injected
uv run nomi eval data/eval/test-small.jsonl --max-temp 85 --cooldown 20 --out eval-runs/model   # slow, uses the local model
```
