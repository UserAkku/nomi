# Contributing to Nomi

Thanks for helping. Nomi is built like a small open-source project, and this page is the whole process.

## Branches

```
feature/fix branch ──PR──▶ dev ──release PR──▶ main
```

- **`main`** holds released, CI-green code only. Nothing is pushed to it directly. It changes only through a release pull request from `dev`.
- **`dev`** is the integration branch. Every change lands here first, through a pull request.
- **Work branches** are cut from `dev` and named by intent: `feat/…`, `fix/…`, `docs/…`, `chore/…`.

## Pull requests

1. Branch from `dev`, keep the change focused, and open a PR **into `dev`**.
2. Fill in every section of the [PR template](.github/pull_request_template.md): what the PR does, **the models the product uses**, **the AI coding assistants that helped**, and how you tested it.
3. [CI](.github/workflows/ci.yml) must pass: ruff, the tests on Python 3.11–3.13 (including the real Docker sandbox tests), and the end-to-end smoke test.
4. Merge with a merge commit, so the individual commits stay visible.

## Commit messages

An imperative subject line, a short body explaining *why*, then these two lines:

```
Add tests for URL parser

Coding agent: Claude Code
Models used in project: Qwen3 4B Instruct 2507 (qwen3:4b-instruct-2507-q4_K_M via Ollama)
```

## Local setup

```bash
uv sync
uv run pytest                 # no network or model needed; Docker tests skip without Docker
uv run ruff check . && uv run ruff format --check .
ollama pull qwen3:4b-instruct-2507-q4_K_M   # only for model-backed runs
```

## Ground rules

- **Evidence, not opinions.** Anything shown to a maintainer as evidence must be re-checkable: a path and line range at a pinned commit, a command and its output, or a diff hunk. Model output is never evidence on its own.
- **Precision before recall for "contradicted".** Wrongly calling a real bug report fabricated is the worst error Nomi can make. When unsure, say *unverified*.
- **Untrusted input stays untrusted.** Never execute reporter code outside the sandbox, and never pass secrets into it.
- **No secrets in the repo.** Use `.env` (git-ignored). `.env.example` lists every variable.
- **Never point Nomi at projects you don't maintain** with posting enabled. Keep `NOMI_DRY_RUN=true`.

Report security problems privately as described in [SECURITY.md](SECURITY.md).
