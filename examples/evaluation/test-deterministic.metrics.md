# Nomi evaluation

| Metric | Value |
| --- | ---: |
| Items scored | 91 |
| Coverage (answered instead of abstaining) | 65.9% (60 of 91) |
| Accuracy (abstentions count as wrong) | 30.8% (28 of 91) |
| Selective accuracy (answered items only) | 46.7% |
| Macro F1 | 0.32 |
| Majority-class baseline accuracy | 38.5% (always invalid) |
| False-invalid rate (confirmed bugs called invalid) | 0.0% (0 of 25) |
| Errors (scored as abstentions) | 0 |
| Mean seconds per analysed item | 1.4 |

## Per class

| Class | Precision | Recall | F1 | Support |
| --- | ---: | ---: | ---: | ---: |
| invalid | 0.00 | 0.00 | 0.00 | 35 |
| cannot_reproduce | 0.50 | 0.52 | 0.51 | 31 |
| confirmed | 0.43 | 0.48 | 0.45 | 25 |

## Confusion matrix

Rows are the maintainers' labels, columns are Nomi's predictions.

| gold \ predicted | invalid | cannot_reproduce | confirmed | abstain |
| --- | ---: | ---: | ---: | ---: |
| invalid | 0 | 14 | 10 | 11 |
| cannot_reproduce | 0 | 16 | 6 | 9 |
| confirmed | 0 | 2 | 12 | 11 |

## How to read this

- **Abstention.** When the evidence does not settle a report Nomi answers `inconclusive`, shown here as `abstain`. Accuracy counts every abstention as wrong; selective accuracy scores only the answered items, so always read it next to coverage.
- **False-invalid rate** is the share of maintainer-confirmed bugs that Nomi called invalid. Dismissing a real bug is the most harmful mistake a triage assistant can make, so this should stay near zero even at the cost of coverage.
- **Majority baseline** is the accuracy of always predicting the most common label; a useful system has to beat it, not just score high.
