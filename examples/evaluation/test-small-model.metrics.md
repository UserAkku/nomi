# Nomi evaluation

| Metric | Value |
| --- | ---: |
| Items scored | 24 |
| Coverage (answered instead of abstaining) | 62.5% (15 of 24) |
| Accuracy (abstentions count as wrong) | 33.3% (8 of 24) |
| Selective accuracy (answered items only) | 53.3% |
| Macro F1 | 0.34 |
| Majority-class baseline accuracy | 33.3% (always invalid) |
| False-invalid rate (confirmed bugs called invalid) | 0.0% (0 of 8) |
| Errors (scored as abstentions) | 0 |
| Mean seconds per analysed item | 33.3 |

## Per class

| Class | Precision | Recall | F1 | Support |
| --- | ---: | ---: | ---: | ---: |
| invalid | 0.00 | 0.00 | 0.00 | 8 |
| cannot_reproduce | 0.60 | 0.38 | 0.46 | 8 |
| confirmed | 0.50 | 0.62 | 0.56 | 8 |

## Confusion matrix

Rows are the maintainers' labels, columns are Nomi's predictions.

| gold \ predicted | invalid | cannot_reproduce | confirmed | abstain |
| --- | ---: | ---: | ---: | ---: |
| invalid | 0 | 2 | 4 | 2 |
| cannot_reproduce | 0 | 3 | 1 | 4 |
| confirmed | 0 | 0 | 5 | 3 |

## How to read this

- **Abstention.** When the evidence does not settle a report Nomi answers `inconclusive`, shown here as `abstain`. Accuracy counts every abstention as wrong; selective accuracy scores only the answered items, so always read it next to coverage.
- **False-invalid rate** is the share of maintainer-confirmed bugs that Nomi called invalid. Dismissing a real bug is the most harmful mistake a triage assistant can make, so this should stay near zero even at the cost of coverage.
- **Majority baseline** is the accuracy of always predicting the most common label; a useful system has to beat it, not just score high.
