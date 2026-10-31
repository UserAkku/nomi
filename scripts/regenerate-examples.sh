#!/usr/bin/env bash
# Regenerate every example output in examples/ with the real local model.
# Needs: `ollama serve` with qwen3:4b-instruct-2507-q4_K_M pulled, network access
# for the curl example, and optionally Docker for sandbox reproduction.
set -euo pipefail
cd "$(dirname "$0")/.."

run() {  # run <example-dir> [extra nomi args...]
  local dir="examples/$1"; shift
  echo "==> $dir"
  uv run nomi analyze --input-file "$dir/input.json" "$@" --format both --out "$dir"
}

run fabricated-security-report --repo-path examples/fabricated-security-report/repo
run pull-request-mismatch --repo-path examples/pull-request-mismatch/repo
run curl-style-fabricated-report   # clones curl/curl at the report's date
