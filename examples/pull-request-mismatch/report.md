<!-- nomi:report -->
### Nomi evidence report

**🟥 Likely invalid** · low confidence · score -0.75

_Automated, evidence-based triage — not a final decision; a maintainer should confirm._

Checked 6 claims: 2 supported, 2 contradicted, 2 unverified. Top finding: The description claims tests, but no test file is changed.

No commit pinned · model `qwen3:4b-instruct-2507-q4_K_M`

<details><summary>How the verdict was reached</summary>

- Contradicting evidence outweighs supporting evidence (score -0.75).
- \-0.60 pr.tests_claim: The description claims tests, but no test file is changed
- \-0.60 pr.breaking_change: The description says there is no breaking change, but public names are removed: `normalize_host`
- \+0.15 grounding.file: `src/widget/net.py` exists in the repository
- \+0.15 grounding.symbol: `Client.fetch` is defined at `src/widget/net.py:12`
- \+0.15 pr.docs_claim: The claimed documentation changes are in the diff (1 doc file(s))

</details>

#### Claims

| # | Claim | Verdict | Evidence |
| --- | --- | --- | --- |
| C1 | Fixes a crash in `Client.fetch` when the URL has trailing whitespace | ❔ unverified · low | — |
| C2 | Fixes a crash in `Client.fetch` when the URL has trailing whitespace, by normalising the host in `src/widget/net.py`. | ✅ supported · low | src/widget/net.py, src/widget/net.py#L12 |
| C3 | by normalising the host in `src/widget/net.py` | ✅ supported · low | src/widget/net.py |
| C4 | Adds unit tests for the new behaviour | ❌ contradicted · medium | diff |
| C5 | Updates the documentation | ❔ unverified · low | — |
| C6 | No breaking changes: the public API is unchanged | ❌ contradicted · medium | [src/widget/parser.py#L17](https://github.com/acme/widget/pull/42/files#diff-e931eee7d17f5acbd05a22eedf7a9594ace7f1aad5dc4751e80af6a410170837L17) |

<details><summary>Why each claim got its verdict</summary>

- **C1** (language model, advisory): No verifiable evidence for or against this claim. Model note: Model found the evidence insufficient: The code shows that `parse_url` splits a URL and returns components including scheme, host, and path. It also includes a `normalize_host` function that strips whitespace from the host. However, the claim is about trailing whitespace in the URL itself (not just the host), and there is no evidence t…
- **C2** (repository check): Only partly verified: the referenced code exists (`src/widget/net.py` exists in the repository)
- **C3** (repository check): Only partly verified: the referenced code exists (`src/widget/net.py` exists in the repository)
- **C4** (repository check): The description claims tests, but no test file is changed
- **C5** (no direct evidence): No verifiable evidence for or against this claim.
- **C6** (repository check): The description says there is no breaking change, but public names are removed: `normalize_host`

</details>

#### Suggested next steps

- Ask the reporter where `unit tests` comes from: The description claims tests, but no test file is changed
- Ask the reporter where `normalize_host` comes from: The description says there is no breaking change, but public names are removed: `normalize_host`
- Wait for the reporter's answer before deciding; a maintainer should confirm before closing or labelling anything.

<details><summary>Checks performed (7: 3 passed, 2 failed, 1 warnings, 0 skipped)</summary>

| Check | Result | Detail |
| --- | --- | --- |
| The description claims tests, but no test file is changed. `pr.tests_claim` | ❌ fail · −0.60 | The description says "Adds unit tests", but none of the 2 changed files is a test file. · diff |
| The description says there is no breaking change, but public names are removed: `normalize_host`. `pr.breaking_change` | ❌ fail · −0.60 | The description says "No breaking changes", yet the diff removes 1 public definition(s) without re-adding, aliasing or re-exporting them: `normalize_host`. · [src/widget/parser.py#L17](https://github.com/acme/widget/pull/42/files#diff-e931eee7d17f5acbd05a22eedf7a9594ace7f1aad5dc4751e80af6a410170837L17) |
| `Client.fetch` mentioned but not touched by this PR. `pr.mentioned_symbols` | ⚠️ warning | These identifiers exist in the repository but no line of the diff contains them; check that the description matches the change. · src/widget/net.py#L12 |
| `src/widget/net.py` exists in the repository `grounding.file` | ✅ pass · +0.15 | src/widget/net.py |
| `Client.fetch` is defined at `src/widget/net.py:12` `grounding.symbol` | ✅ pass · +0.15 | src/widget/net.py#L12 |
| The claimed documentation changes are in the diff (1 doc file(s)). `pr.docs_claim` | ✅ pass · +0.15 | The description says "Updates the documentation"; changed doc files: `README.md` · [README.md#L3](https://github.com/acme/widget/pull/42/files#diff-b335630551682c19a781afebcf4d07bf978fb1f8ac04c6bf87428ed5106870f5R3) |
| Files named in the description exist but are not changed by this PR: `src/widget/net.py`. `pr.mentioned_files` | ℹ️ info | src/widget/net.py#L1 |

</details>

#### Limitations & uncertainty

- Sandbox unavailable: the Docker daemon is not reachable: failed to connect to the docker API at unix://~/.docker/run/docker.sock; check if the path is correc...
- LLM judgements come from a small local model and are advisory (never high confidence on their own)

---
<sub>Generated by Nomi v0.1.0 · reproduce locally: `nomi analyze https://github.com/acme/widget/pull/42`</sub>
