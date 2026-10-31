<!-- nomi:report -->
### Nomi evidence report

**🟥 Likely invalid** · high confidence · score -2.35

_Automated, evidence-based triage — not a final decision; a maintainer should confirm._

Checked 6 claims: 2 contradicted, 4 unverified. Top finding: `curl_easy_setopt_safe` is not defined or referenced anywhere in the repository at `a133b27`.

Analysed commit [`a133b27`](https://github.com/curl/curl/commit/a133b27d0779fd6f74127139231efa456fdd2b44) · model `qwen3:4b-instruct-2507-q4_K_M`

<details><summary>How the verdict was reached</summary>

- Strong contradicting evidence: 2 check(s) contradict the report (strongest 0.80, total 2.60).
- \-0.80 grounding.symbol: `curl_easy_setopt_safe` is not defined or referenced anywhere in the repository at `a133b27`
- \-0.75 grounding.line: `lib/easy.c` has 1,480 lines, but the report cites line 3,412
- \-0.65 trace.frames: Stack trace does not match the code at `a133b27`: 2 of 2 frames point at the wrong lines or functions
- \-0.30 grounding.file: No file `lib/transfer_safe.c` exists in the repository at `a133b27`
- \-0.30 grounding.symbol: `Curl_follow_redirect_copy` was not found in the repository at `a133b27`; it may come from a dependency

</details>

#### Claims

| # | Claim | Verdict | Evidence |
| --- | --- | --- | --- |
| C1 | stack-based buffer overflow in `curl_easy_setopt_safe()` allows remote code execution when libcurl follows a redirect to a long URL. | ❌ contradicted · high | [lib/easy.c](https://github.com/curl/curl/blob/a133b27d0779fd6f74127139231efa456fdd2b44/lib/easy.c) |
| C2 | `lib/easy.c` line 3412, `curl_easy_setopt_safe()` copies the redirect URL into a fixed 1024-byte stack buffer with `strcpy()` without validating its length | ❌ contradicted · high | [lib/easy.c](https://github.com/curl/curl/blob/a133b27d0779fd6f74127139231efa456fdd2b44/lib/easy.c) |
| C3 | vulnerable helper `Curl_follow_redirect_copy()` in `lib/transfer_safe.c` is also affected | ❔ unverified · low | — |
| C4 | Run a server that answers with `Location: http://` followed by 5000 `A` characters | ❔ unverified · low | — |
| C5 | 2. `curl -L http://attacker.example/` | ❔ unverified · low | — |
| C6 | Remote code execution in every application using libcurl 8.4.0 with redirects enabled. | ❔ unverified · low | — |

<details><summary>Why each claim got its verdict</summary>

- **C1** (repository check): `curl_easy_setopt_safe` is not defined or referenced anywhere in the repository at `a133b27`
- **C2** (repository check): `curl_easy_setopt_safe` is not defined or referenced anywhere in the repository at `a133b27`
- **C3** (language model, advisory): No verifiable evidence for or against this claim. Model note: Model found the evidence insufficient: The evidence provided does not mention or reference the `Curl_follow_redirect_copy()` function in `lib/transfer_safe.c`, nor does it confirm or deny that this specific helper is affected by a vulnerability. The documentation describes the structure of vulnerability entries and how advisories are g…
- **C4** (language model, advisory): No verifiable evidence for or against this claim. Model note: Model found the evidence insufficient: The claim asks for a server that responds with a 'Location:' header followed by 5000 'A' characters. None of the provided test cases include a Location header with a body of 5000 repeated 'A' characters. All test cases show Location headers with paths or URLs (e.g., 'http​://anotherone.com/%TESTNUM…
- **C5** (no direct evidence): No verifiable evidence for or against this claim.
- **C6** (no direct evidence): No verifiable evidence for or against this claim.

</details>

#### Missing information

- What you expected to happen vs. what happened
- Your environment (OS, runtime version)
- A minimal code sample or command that reproduces it

#### Suggested next steps

- Ask the reporter where `curl_easy_setopt_safe` comes from: `curl_easy_setopt_safe` is not defined or referenced anywhere in the repository at `a133b27` (checked at `a133b27`)
- Ask the reporter where `lib/easy.c` comes from: `lib/easy.c` has 1,480 lines, but the report cites line 3,412 (checked at `a133b27`)
- Ask the reporter where `a133b27` comes from: Stack trace does not match the code at `a133b27`: 2 of 2 frames point at the wrong lines or functions (checked at `a133b27`)
- Wait for the reporter's answer before deciding; a maintainer should confirm before closing or labelling anything.

<details><summary>Checks performed (13: 2 passed, 3 failed, 2 warnings, 1 skipped)</summary>

| Check | Result | Detail |
| --- | --- | --- |
| `lib/easy.c` has 1,480 lines, but the report cites line 3,412 `grounding.line` | ❌ fail · −0.75 | The cited line 3,412 lies past the end of the file at `a133b27`. Either the line number is invented or the report refers to a very different version. · [lib/easy.c#L1478-L1480](https://github.com/curl/curl/blob/a133b27d0779fd6f74127139231efa456fdd2b44/lib/easy.c#L1478-L1480) |
| `curl_easy_setopt_safe` is not defined or referenced anywhere in the repository at `a133b27` `grounding.symbol` | ❌ fail · −0.80 | The report attributes it to this project: a stack trace places it in `lib/easy.c`. · [lib/easy.c](https://github.com/curl/curl/blob/a133b27d0779fd6f74127139231efa456fdd2b44/lib/easy.c) |
| Stack trace does not match the code at `a133b27`: 2 of 2 frames point at the wrong lines or functions `trace.frames` | ❌ fail · −0.65 | - `lib/easy.c` has 1,480 lines, but the frame cites line 3,412 - `lib/transfer.c` has 914 lines, but the frame cites line 1,501 · [lib/easy.c#L1478-L1480](https://github.com/curl/curl/blob/a133b27d0779fd6f74127139231efa456fdd2b44/lib/easy.c#L1478-L1480) |
| No file `lib/transfer_safe.c` exists in the repository at `a133b27` `grounding.file` | ⚠️ warning · −0.30 | It may also be a file on the reporter's machine. Did you mean `lib/transfer.c`? · [lib/transfer.c](https://github.com/curl/curl/blob/a133b27d0779fd6f74127139231efa456fdd2b44/lib/transfer.c) |
| `Curl_follow_redirect_copy` was not found in the repository at `a133b27`; it may come from a dependency `grounding.symbol` | ⚠️ warning · −0.30 | — |
| `lib/easy.c` exists in the repository `grounding.file` | ✅ pass · +0.15 | [lib/easy.c](https://github.com/curl/curl/blob/a133b27d0779fd6f74127139231efa456fdd2b44/lib/easy.c) |
| 8.4.0 is a real release (tag curl-8_4_0) `version.release` | ✅ pass · +0.10 | [history](https://github.com/curl/curl/releases/tag/curl-8_4_0) |
| Dynamic reproduction not run `repro.sandbox` | ⏭️ skipped | No Docker sandbox for this run (disabled or Docker unavailable) |
| `CURLOPT_FOLLOWLOCATION` is referenced (e.g. `.github/labeler.yml:270`) but not defined in the repository `grounding.symbol` | ℹ️ info | It is probably defined by a dependency, generated code or a macro. · [.github/labeler.yml#L270](https://github.com/curl/curl/blob/a133b27d0779fd6f74127139231efa456fdd2b44/.github/labeler.yml#L270) |
| `ASan` is referenced (e.g. `tests/unit/unit1397.c:117`) but not defined in the repository `grounding.symbol` | ℹ️ info | It is probably defined by a dependency, generated code or a macro. · [tests/unit/unit1397.c#L117](https://github.com/curl/curl/blob/a133b27d0779fd6f74127139231efa456fdd2b44/tests/unit/unit1397.c#L117) |
| Error text `AddressSanitizer: stack-buffer-overflow on address 0x7ffd5c3a1e40` was not found in the repository's code `grounding.error_message` | ℹ️ info | It may come from a dependency, the runtime or the operating system. |
| Error text `stack-buffer-overflow on address 0x7ffd5c3a1e40` was not found in the repository's code `grounding.error_message` | ℹ️ info | It may come from a dependency, the runtime or the operating system. |
| 25 newer releases existed when this was reported (latest 8.23.0); it may already be fixed `version.release` | ℹ️ info | — |

</details>

#### Limitations & uncertainty

- Sandbox unavailable: the Docker daemon is not reachable: failed to connect to the docker API at unix://~/.docker/run/docker.sock; check if the path is correc...
- Dynamic reproduction not run: skipped (No Docker sandbox for this run (disabled or Docker unavailable))
- LLM judgements come from a small local model and are advisory (never high confidence on their own)
- Evidence is pinned to commit a133b27d0779fd6f74127139231efa456fdd2b44

---
<sub>Generated by Nomi v0.1.0 · reproduce locally: `nomi analyze curl/curl`</sub>
