# Running Nomi as a GitHub App

The CLI (`nomi analyze <url>`) needs no setup beyond Ollama. These steps are only needed to have Nomi react to new issues and PRs automatically.

> **Policy:** never install Nomi on repositories you do not maintain. Keep `NOMI_DRY_RUN=true` (the default) until you have tested it on a repository you own. In dry-run mode reports are written to `nomi-reports/` and nothing is posted.

## 1. Register the app

Create a GitHub App (Settings → Developer settings → GitHub Apps → New) using the values in [`app.yml`](../app.yml):

| Setting | Value |
|---|---|
| Webhook URL | your public URL + `/webhook`. For local development, a [smee.io](https://smee.io) channel. |
| Webhook secret | a long random string. Put it in `NOMI_WEBHOOK_SECRET`. |
| Permissions | Metadata: read · Contents: read · Issues: read & write · Pull requests: read & write |
| Events | Issues · Issue comment · Pull request |

Download the private key (`*.pem`; git-ignored) and note the App ID.

## 2. Configure

```bash
cp .env.example .env
# NOMI_APP_ID=123456
# NOMI_APP_PRIVATE_KEY_PATH=./nomi.private-key.pem
# NOMI_WEBHOOK_SECRET=...
# NOMI_DRY_RUN=true
```

## 3. Run

```bash
ollama serve &                                   # local model server
uv run nomi doctor                               # checks model, Docker, GitHub config
uv run nomi serve --port 8000                    # webhook server
npx smee-client --url https://smee.io/<channel> --target http://127.0.0.1:8000/webhook
```

Install the app on a repository you own and open an issue. The server acknowledges the webhook immediately (HTTP 202) and analyses the issue on a background worker. It then writes the report, or with `NOMI_DRY_RUN=false` creates **one** comment and updates that same comment on later runs.

## Behaviour

| Event | Action |
|---|---|
| `issues.opened`, `issues.reopened` | analyse the issue at the current default branch |
| `pull_request.opened`, `reopened`, `synchronize`, `ready_for_review` | analyse the PR description against its diff and head commit |
| `issue_comment.created` starting with `/nomi` from an owner, member or collaborator | re-run the analysis |
| anything else, or any event sent by a bot | ignored (HTTP 202) |

Nomi never closes, labels, locks, merges or approves anything.
