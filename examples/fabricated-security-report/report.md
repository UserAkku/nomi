<!-- nomi:report -->
### Nomi evidence report

**🟥 Likely invalid** · high confidence · score -1.75

_Automated, evidence-based triage — not a final decision; a maintainer should confirm._

Checked 6 claims: 3 contradicted, 2 unverified, 1 needs info. Top finding: `mh_easy_setopt_url_safe` is not defined or referenced anywhere in the repository in the analysed checkout.

No commit pinned · model `qwen3:4b-instruct-2507-q4_K_M`

<details><summary>How the verdict was reached</summary>

- Strong contradicting evidence: 2 check(s) contradict the report (strongest 0.80, total 2.50).
- \-0.80 grounding.symbol: `mh_easy_setopt_url_safe` is not defined or referenced anywhere in the repository in the analysed checkout
- \-0.75 grounding.line: `lib/easy.c` has 62 lines, but the report cites line 412
- \-0.65 trace.frames: Stack trace does not match the code in the analysed checkout: 2 of 2 frames point at the wrong lines or functions
- \-0.30 grounding.file: No file `lib/vtls/openssl_ext.c` exists in the repository in the analysed checkout
- \+0.15 grounding.file: `lib/easy.c` exists in the repository

</details>

#### Claims

| # | Claim | Verdict | Evidence |
| --- | --- | --- | --- |
| C1 | heap buffer overflow in `mh_easy_setopt_url_safe()` lets a remote attacker execute arbitrary code by supplying a long URL | ❌ contradicted · high | lib/easy.c |
| C2 | `lib/easy.c` line 412, the function `mh_easy_setopt_url_safe()` copies the user-supplied URL into a fixed 256-byte buffer with `strcpy()` without checking its length | ❌ contradicted · high | lib/easy.c |
| C3 | Because the URL can come from an HTTP redirect, a malicious server controls it, which makes this remotely exploitable (CWE-122). | 📝 needs info · low | — |
| C4 | The same unchecked copy also exists in `lib/vtls/openssl_ext.c`, which is called from `mh_easy_perform()` | ❔ unverified · low | — |
| C5 | Remote code execution in every application that uses minihttp 2.4.1. | ❔ unverified · low | — |
| C6 | Replace `strcpy()` with `strncpy()` in `mh_easy_setopt_url_safe()` | ❌ contradicted · high | lib/easy.c |

<details><summary>Why each claim got its verdict</summary>

- **C1** (repository check): `mh_easy_setopt_url_safe` is not defined or referenced anywhere in the repository in the analysed checkout
- **C2** (repository check): `mh_easy_setopt_url_safe` is not defined or referenced anywhere in the repository in the analysed checkout
- **C3** (no direct evidence): The report does not state the version or environment needed to check this claim.
- **C4** (language model, advisory): No verifiable evidence for or against this claim. Model note: Model found the evidence insufficient: The claim references a copy of code existing in `lib/vtls/openssl_ext.c` that is called from `mh_easy_perform()`. However, none of the provided evidence includes the file `lib/vtls/openssl_ext.c` or any code related to it. The evidence only covers `minihttp.h`, `easy.c`, `url.c`, `README.md`, and…
- **C5** (language model, advisory): No verifiable evidence for or against this claim. Model note: Unverified: model gave no verifiable citation for its 'contradicts' answer (1 discarded: quote not found in the cited evidence or unknown evidence id). Model rationale: The code in E3 shows that mh_url_parse rejects URLs that are longer than MH_MAX_URL or missing '://', and returns MH_URL_MALFORMAT in such cases. This means that malfor…
- **C6** (repository check): `mh_easy_setopt_url_safe` is not defined or referenced anywhere in the repository in the analysed checkout

</details>

#### Missing information

- What you expected to happen vs. what happened
- Your environment (OS, runtime version)
- More detail to verify claim C3: “Because the URL can come from an HTTP redirect, a malicious server controls it, which makes this remotely exploitable (CWE-122).”

#### Suggested next steps

- Ask the reporter where `mh_easy_setopt_url_safe` comes from: `mh_easy_setopt_url_safe` is not defined or referenced anywhere in the repository in the analysed checkout
- Ask the reporter where `lib/easy.c` comes from: `lib/easy.c` has 62 lines, but the report cites line 412
- Ask the reporter to clarify: Stack trace does not match the code in the analysed checkout: 2 of 2 frames point at the wrong lines or functions
- Wait for the reporter's answer before deciding; a maintainer should confirm before closing or labelling anything.

<details><summary>Checks performed (12: 5 passed, 3 failed, 1 warning, 2 info, 1 skipped)</summary>

| Check | Result | Detail |
| --- | --- | --- |
| `lib/easy.c` has 62 lines, but the report cites line 412 `grounding.line` | ❌ fail · −0.75 | The cited line 412 lies past the end of the file in the analysed checkout. Either the line number is invented or the report refers to a very different version. · lib/easy.c#L60-L62 |
| `mh_easy_setopt_url_safe` is not defined or referenced anywhere in the repository in the analysed checkout `grounding.symbol` | ❌ fail · −0.80 | The report attributes it to this project: a stack trace places it in `lib/easy.c`. · lib/easy.c |
| Stack trace does not match the code in the analysed checkout: 2 of 2 frames point at the wrong lines or functions `trace.frames` | ❌ fail · −0.65 | - `lib/easy.c` has 62 lines, but the frame cites line 412 - `lib/easy.c` has 62 lines, but the frame cites line 530 · lib/easy.c#L60-L62 |
| No file `lib/vtls/openssl_ext.c` exists in the repository in the analysed checkout `grounding.file` | ⚠️ warning · −0.30 | It may also be a file on the reporter's machine. |
| `lib/easy.c` exists in the repository `grounding.file` | ✅ pass · +0.15 | lib/easy.c |
| `include/minihttp.h` exists in the repository (cited as `minihttp.h`) `grounding.file` | ✅ pass · +0.15 | include/minihttp.h |
| `mh_easy_perform` is defined at `lib/easy.c:43` `grounding.symbol` | ✅ pass · +0.15 | lib/easy.c#L43 |
| `main` is defined at `tests/test_url.c:7` `grounding.symbol` | ✅ pass · +0.15 | tests/test_url.c#L7 |
| `mh_easy_init` is defined at `lib/easy.c:11` `grounding.symbol` | ✅ pass · +0.15 | lib/easy.c#L11 |
| Dynamic reproduction not run `repro.sandbox` | ⏭️ skipped | No Docker sandbox for this run (disabled or Docker unavailable) |
| Error text `AddressSanitizer: heap-buffer-overflow on address 0x602000000110` was not found in the repository's code `grounding.error_message` | ℹ️ info | It may come from a dependency, the runtime or the operating system. |
| Error text `heap-buffer-overflow on address 0x602000000110` was not found in the repository's code `grounding.error_message` | ℹ️ info | It may come from a dependency, the runtime or the operating system. |

</details>

#### Limitations & uncertainty

- Sandbox unavailable: the Docker daemon is not reachable: failed to connect to the docker API at unix://~/.docker/run/docker.sock; check if the path is correc...
- Dynamic reproduction not run: skipped (No Docker sandbox for this run (disabled or Docker unavailable))
- LLM judgements come from a small local model and are advisory (never high confidence on their own)

---
<sub>Generated by Nomi v0.1.0 · reproduce locally: `nomi analyze https://github.com/example/minihttp/issues/101`</sub>
