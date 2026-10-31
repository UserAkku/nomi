# Examples

Every folder holds an **input**, and the **actual output** Nomi produced for it: `report.md` is the GitHub comment and `report.json` is machine-readable. Regenerate any of them with the command shown.

| Example | What it shows |
|---|---|
| [`fabricated-security-report/`](fabricated-security-report/) | An AI-slop-style security report against a tiny C library bundled in `repo/` (runs offline and in CI). It cites a function that does not exist, a line past the end of the file and a non-existent source file. |
| [`curl-style-fabricated-report/`](curl-style-fabricated-report/) | The same pattern against the **real curl repository**, mixing real symbols (`CURLOPT_FOLLOWLOCATION`) with invented ones. Each claim gets its own verdict. |
| [`pull-request-mismatch/`](pull-request-mismatch/) | A PR whose description claims tests, docs and "no breaking changes", while the diff deletes a public function and only re-spaces the README. |
| [`real-issues/`](real-issues/) | Real closed issues from the evaluation set, analysed as they were when filed. |
| [`evaluation/`](evaluation/) | Metrics and per-issue predictions on the labelled evaluation set. |

The constructed reports are written by us, modelled on publicly described patterns (curl's AI-slop write-ups, the Hacktoberfest 2020 spam wave). They are not copies of real reports.
