# Nomi evaluation

| Metric | Value |
| --- | ---: |
| Items scored | 37 |
| Coverage (answered instead of abstaining) | 64.9% (24 of 37) |
| Accuracy (abstentions count as wrong) | 29.7% (11 of 37) |
| Selective accuracy (answered items only) | 45.8% |
| Macro F1 | 0.30 |
| Majority-class baseline accuracy | 37.8% (always cannot_reproduce) |
| False-invalid rate (confirmed bugs called invalid) | 0.0% (0 of 11) |
| Errors (scored as abstentions) | 0 |
| Mean seconds per analysed item | 1.1 |

## Per class

| Class | Precision | Recall | F1 | Support |
| --- | ---: | ---: | ---: | ---: |
| invalid | 0.00 | 0.00 | 0.00 | 12 |
| cannot_reproduce | 0.46 | 0.43 | 0.44 | 14 |
| confirmed | 0.45 | 0.45 | 0.45 | 11 |

## Confusion matrix

Rows are the maintainers' labels, columns are Nomi's predictions.

| gold \ predicted | invalid | cannot_reproduce | confirmed | abstain |
| --- | ---: | ---: | ---: | ---: |
| invalid | 0 | 5 | 3 | 4 |
| cannot_reproduce | 0 | 6 | 3 | 5 |
| confirmed | 0 | 2 | 5 | 4 |

## How to read this

- **Abstention.** When the evidence does not settle a report Nomi answers `inconclusive`, shown here as `abstain`. Accuracy counts every abstention as wrong; selective accuracy scores only the answered items, so always read it next to coverage.
- **False-invalid rate** is the share of maintainer-confirmed bugs that Nomi called invalid. Dismissing a real bug is the most harmful mistake a triage assistant can make, so this should stay near zero even at the cost of coverage.
- **Majority baseline** is the accuracy of always predicting the most common label; a useful system has to beat it, not just score high.
