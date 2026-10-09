---
scope: project
severity: preference
triggers: []
domain: ''
title: "Parse HWP files that pyhwp cannot read using HwpHwpxLoader and determine with a fidelity run."
pr: 34
merged: 2026-10-09
branch: "hwp-recovery-native-loader"
---

# Parse HWP files that pyhwp cannot read using HwpHwpxLoader and determine with a fidelity run.

What. If pyhwp fails, `ingest` does not read the text layer of the Hancom printout, but parses the original HWP with `load_hwp_hwpx` (HwpHwpxLoader). - The location format of the element is `hwp_loader`, and the parser fingerprint (adapter version and package version) is also kept separately. A `pyhwp_failed` warning containing the reason for pyhwp's failure remains in the source. …

Why. | Document | Element | Judgment | Difference | Extracted/Printed Character Count | Ratio of 6-grams in printout present in extraction | |---|---|---|---|---|---| | AFSIS Cambodia | 7 | auto_flagged | 84 | 73,262 / 73,200 | 98.7% | | MILE | 10 | auto_flagged | 51 | 40,808 / 42,893 | 96.0% | Both documents are active with `parsed`. The reason for the high mismatch rate (26–30%) appears to be that the comparator cannot align by page because the body consists of only one element. If compared ignoring order, 96–99% of the printout content is included. Comparison rules and re-indexing are outside the scope of this task. …

Source. PR #34 · `hwp-recovery-native-loader`
