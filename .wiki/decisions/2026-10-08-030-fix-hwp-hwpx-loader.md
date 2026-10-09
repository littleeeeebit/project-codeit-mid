---
scope: project
severity: preference
triggers: []
domain: ''
title: "Add read-only HWP/HWPX preview loader and dependency pins"
pr: 30
merged: 2026-10-08
branch: "fix/hwp-hwpx-loader"
---

# Add read-only HWP/HWPX preview loader and dependency pins

What. We have added `HwpHwpxLoader`-based `storage.hwp_loader.load_hwp_hwpx(Path)` API, dependencies, and tests to compare documents for which existing HWP XML conversion fails.

Why. This API is not connected to the existing production ingestion. The existing HWP XML parser and PDF parser are maintained as is. No data migration or reprocessing is performed. - Added a read-only preview API that preserves the original text and loader metadata. - The loader returns tables and footnotes appended after the full body text. It does not guarantee the placement order of actual paragraphs and tables. - It does not arbitrarily generate table cell/merge structures, page numbers, original coordinates, and heading context. It cannot directly replace existing structured table ingestion. - Invalid extensions, missing files, and dependency import errors are passed to the caller. …

Source. PR #30 · `fix/hwp-hwpx-loader`
