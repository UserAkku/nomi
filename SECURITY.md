# Security policy

Nomi runs code from untrusted issue reports inside a sandbox and holds GitHub App credentials, so we take reports seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting (the *Security* tab → *Report a vulnerability*) and include:

- the affected version or commit,
- a minimal reproduction (input, command, observed vs expected behaviour),
- the impact you believe it has.

We aim to acknowledge reports within 72 hours. Reports are judged on verifiable evidence, the same standard Nomi applies to everyone else's.

## Scope

In scope: sandbox escapes or policy bypasses (`src/nomi/verify/sandbox.py`), webhook authentication (`src/nomi/github/app.py`), token handling, and markdown or HTML injection in generated reports.

The threat model and mitigations are in [docs/security.md](docs/security.md).
