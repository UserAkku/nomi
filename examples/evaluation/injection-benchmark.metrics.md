# Nomi evaluation

| Metric | Value |
| --- | ---: |
| Items scored | 38 |
| Coverage (answered instead of abstaining) | 81.6% (31 of 38) |
| Accuracy (abstentions count as wrong) | 78.9% (30 of 38) |
| Selective accuracy (answered items only) | 96.8% |
| Macro F1 | 0.58 |
| Majority-class baseline accuracy | 50.0% (always invalid) |
| False-invalid rate (confirmed bugs called invalid) | 0.0% (0 of 19) |
| Errors (scored as abstentions) | 0 |
| Mean seconds per analysed item | 1.2 |

## Per class

| Class | Precision | Recall | F1 | Support |
| --- | ---: | ---: | ---: | ---: |
| invalid | 1.00 | 1.00 | 1.00 | 19 |
| cannot_reproduce | 0.00 | 0.00 | 0.00 | 0 |
| confirmed | 1.00 | 0.58 | 0.73 | 19 |

## Confusion matrix

Rows are the maintainers' labels, columns are Nomi's predictions.

| gold \ predicted | invalid | cannot_reproduce | confirmed | abstain |
| --- | ---: | ---: | ---: | ---: |
| invalid | 19 | 0 | 0 | 0 |
| cannot_reproduce | 0 | 0 | 0 | 0 |
| confirmed | 0 | 1 | 11 | 7 |

## How to read this

- **Abstention.** When the evidence does not settle a report Nomi answers `inconclusive`, shown here as `abstain`. Accuracy counts every abstention as wrong; selective accuracy scores only the answered items, so always read it next to coverage.
- **False-invalid rate** is the share of maintainer-confirmed bugs that Nomi called invalid. Dismissing a real bug is the most harmful mistake a triage assistant can make, so this should stay near zero even at the cost of coverage.
- **Majority baseline** is the accuracy of always predicting the most common label; a useful system has to beat it, not just score high.
