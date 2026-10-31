# Real closed issues, analysed as they were filed

Three issues from the held-out evaluation set, chosen to show one correct verdict, one abstention and one miss. Each folder has `input.json` (exactly what Nomi saw: title, body and creation time, never labels or comments) and the actual `report.md` / `report.json` from the local model (`qwen3:4b-instruct-2507-q4_K_M`). celery was checked out at the default branch's last commit before each issue was filed.

| Issue | Maintainer label | Nomi | What happened |
|---|---|---|---|
| [celery#2126](https://github.com/celery/celery/issues/2126) | cannot reproduce | 🟨 needs more information | ✅ Agrees. The report lacks a runnable reproduction and environment details, and Nomi asks for exactly those. |
| [celery#6426](https://github.com/celery/celery/issues/6426) | confirmed | ⬜ inconclusive | ➖ Abstained. The referenced code exists, but nothing proves or disproves the behaviour, so Nomi says so instead of guessing. |
| [celery#6921](https://github.com/celery/celery/issues/6921) | invalid | 🟩 likely valid | ❌ Miss. The reported error and code paths are real; maintainers closed it as an environment/usage problem. A code-grounded check cannot tell "real code, wrong usage" from a bug. This is the main roadmap item. |

Regenerate one with `uv run nomi analyze --input-file examples/real-issues/<dir>/input.json`.
