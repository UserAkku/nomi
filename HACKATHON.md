# HACKATHON.md

## Team name
akku

## Team members
- Akhilesh Kumar

## Problem statement
**Problem 02: Evidence-Based Open Source Issue and Pull Request Triage**
(Artificial Intelligence | Developer Tools | Open Source Quality)

Maintainers lose hours to bug reports, security reports and pull requests whose technical claims are wrong, unreproducible or invented: functions that don't exist, line numbers past the end of the file, "fixes" whose diff only touches whitespace. Generating a plausible report is now cheap, but verifying one is still expensive. curl closed its bug bounty in January 2026 after the share of genuine reports fell below 5%.

We are **not** trying to detect whether a human or an AI wrote a report. We check whether **what it says is true**.

## What we are building: Nomi
Nomi is a GitHub App and CLI. For each incoming issue or PR it produces an evidence report for maintainers:

1. **Extract claims.** The report is split into small, checkable technical claims. A local model does the splitting, and every quote it returns is re-checked against the original text.
2. **Verify each claim against the repository, using deterministic checks first.**
   - Do the cited files, line numbers, functions, CLI flags and error messages exist at the commit the reporter was looking at?
   - Do stack-trace frames land inside the functions they name?
   - Does quoted code match the real code?
   - Does a PR description match its diff (tests claimed vs tests changed, "no breaking changes" vs removed public functions)?
3. **Reproduce when it is safe.** Python reproduction snippets run in a locked-down Docker sandbox: no network, read-only, non-root, CPU/memory/time limits, and no secrets.
4. **Judge with citations.** The model judges whether retrieved code supports or contradicts a claim. It must cite exact lines, and every citation is verified mechanically before it is shown.
5. **Report with uncertainty.** Each claim is marked Supported, Contradicted, Unverified or Needs info, with evidence pinned to a commit permalink. The overall verdict is Likely valid, Likely invalid, Needs more information or Inconclusive (an explicit abstention). Nomi never closes, labels or rejects anything on its own.

## Models we use and why
- **Qwen3 4B Instruct 2507**, `qwen3:4b-instruct-2507-q4_K_M` via Ollama, running locally on the laptop (Apache-2.0, open weights).
  - *What it does in Nomi:* (1) turns an issue/PR into atomic claims as schema-constrained JSON; (2) decides whether retrieved code supports or contradicts a claim, and cites the exact lines.
  - *Why this model:* it has the strongest published instruction-following and structured-output scores among models that fit an **8 GB MacBook Pro M3**: IFEval 83.4, BFCL-v3 61.9, MMLU-Pro 69.6 and LiveCodeBench v6 35.1, all non-thinking results.
    - The Q4_K_M build is 2.5 GB, which leaves room for macOS, the Docker sandbox and an 8K-token KV cache.
    - It has no "thinking" mode, so JSON output is fast and clean.
    - It has a 256K native context and an Apache-2.0 license.
    - Measured on our M3: about 25 tokens/s generation, and 4.1 GB resident with the 8K context, fully on the GPU. A full model-backed analysis takes about 1–3 minutes uncached. Answers are cached, so re-runs are fast.
    - Sustained back-to-back inference heated the laptop to about 99 °C, so `nomi eval --max-temp 85 --cooldown 20` paces long runs by chip temperature. That kept it between 60 and 85 °C without moving to a weaker model.
  - *What it is not trusted with:* producing evidence. In our first benchmark the model rewrote "line 3412" as "line 3416" inside a quote it was told to copy verbatim. So every quote and citation the model returns is matched mechanically against the issue text and the repository, and anything that doesn't match is discarded.
- **No other AI model is used.** Considered and rejected for this 8 GB machine:
  - Gemma 4 E2B/E4B: 4.3–9.6 GB downloads.
  - Qwen3.5 4B: 3.3–4 GB, and it carries a vision encoder we don't need.
  - Qwen2.5-Coder 7B: 4.7 GB.
  - Embedding models: for code identifiers, lexical BM25 plus symbol search was more precise and costs no memory.

## Non-AI tools that provide the evidence
git (pinned-commit checkouts, `git grep`, tags), BM25 retrieval over code and docs, multi-language symbol and stack-trace parsers, a unified-diff analyser, and a Docker sandbox. The model reads and cites. These tools establish what is true.

## Results (measured, see `examples/evaluation/`)
- **Fabrication benchmark:** 19 maintainer-confirmed issues, each with a twin carrying a curl-style invented root cause (a non-existent function plus a line past the end of a real file). Nomi flagged **19/19 twins** and **0/19 originals**. This is a controlled check with one injection template.
- **Real maintainer labels:** 91 held-out closed issues from celery, poetry and pylint. **False-invalid rate 0%**: no confirmed bug was ever called invalid. Selective accuracy was 46.7% against a 38.5% majority baseline, and Nomi abstained on 34% instead of guessing.
- **Model contribution, measured honestly:** on a 24-issue subset the local model was slightly better than deterministic checks alone (8 vs 7 correct; selective accuracy 53.3% vs 50.0%), too small a sample to claim a real gain. Its role is breaking a report into claims and giving line-cited explanations; verdicts rest on deterministic evidence by design.

## How we built it
- After the two initial setup commits, every change went through a pull request into `dev`, and `dev` is released to `main` by pull request. CI (lint, about 1,100 tests on Python 3.11–3.13 including real Docker sandbox tests, and an end-to-end smoke test) passed on every merge.
- An adversarial review by four independent reviewers (security, false accusations, robustness, evaluation validity) found 23 reproducible defects. All were fixed, 22 with regression tests (`docs/review-findings.md`, PR #4). A final judge-eye verification of a fresh clone led to one more polish PR.

## AI coding assistant
Claude Code (Claude Opus 5.5) helped write this project. Every authored commit and every PR names it and the model used, as the hackathon rules require, and the release merge commits do too.

## Repository
https://github.com/UserAkku/nomi (Apache-2.0)
