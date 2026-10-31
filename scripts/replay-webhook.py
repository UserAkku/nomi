"""Send a correctly signed GitHub webhook to a local `nomi serve`.

Demonstrates the GitHub App flow end to end without registering an app or
exposing the laptop to the internet:

    export NOMI_WEBHOOK_SECRET=dev-secret NOMI_DRY_RUN=true
    uv run nomi serve &
    uv run python scripts/replay-webhook.py celery/celery 7203

With NOMI_DRY_RUN=true the report is written to nomi-reports/ instead of
being posted, so this is safe to point at any public repository.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
import uuid


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("number", type=int)
    parser.add_argument(
        "--pull", action="store_true", help="send a pull_request event instead of issues"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhook")
    args = parser.parse_args()

    secret = os.environ.get("NOMI_WEBHOOK_SECRET")
    if not secret:
        print("set NOMI_WEBHOOK_SECRET to the same value the server uses", file=sys.stderr)
        return 2
    event, key = ("pull_request", "pull_request") if args.pull else ("issues", "issue")
    payload = {
        "action": "opened",
        key: {"number": args.number},
        "repository": {"full_name": args.repo},
        "sender": {"login": "nomi-demo", "type": "User"},
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        args.url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": signature,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(response.status, response.read().decode())
    except urllib.error.HTTPError as exc:
        print(exc.code, exc.read().decode(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
