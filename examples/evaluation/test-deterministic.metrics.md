# Nomi evaluation

| Metric | Value |
| --- | ---: |
| Items scored | 91 |
| Coverage (answered instead of abstaining) | 69.2% (63 of 91) |
| Accuracy (abstentions count as wrong) | 33.0% (30 of 91) |
| Selective accuracy (answered items only) | 47.6% |
| Macro F1 | 0.34 |
| Majority-class baseline accuracy | 38.5% (always invalid) |
| False-invalid rate (confirmed bugs called invalid) | 0.0% (0 of 25) |
| Errors (scored as abstentions) | 0 |
| Mean seconds per analysed item | 0.8 |

## Per class

| Class | Precision | Recall | F1 | Support |
| --- | ---: | ---: | ---: | ---: |
| invalid | 0.00 | 0.00 | 0.00 | 35 |
| cannot_reproduce | 0.50 | 0.52 | 0.51 | 31 |
| confirmed | 0.45 | 0.56 | 0.50 | 25 |

## Confusion matrix

Rows are the maintainers' labels, columns are Nomi's predictions.

| gold \ predicted | invalid | cannot_reproduce | confirmed | abstain |
| --- | ---: | ---: | ---: | ---: |
| invalid | 0 | 14 | 11 | 10 |
| cannot_reproduce | 0 | 16 | 6 | 9 |
| confirmed | 0 | 2 | 14 | 9 |

## How to read this

- **Abstention.** When the evidence does not settle a report Nomi answers `inconclusive`, shown here as `abstain`. Accuracy counts every abstention as wrong; selective accuracy scores only the answered items, so always read it next to coverage.
- **False-invalid rate** is the share of maintainer-confirmed bugs that Nomi called invalid. Dismissing a real bug is the most harmful mistake a triage assistant can make, so this should stay near zero even at the cost of coverage.
- **Majority baseline** is the accuracy of always predicting the most common label; a useful system has to beat it, not just score high.
