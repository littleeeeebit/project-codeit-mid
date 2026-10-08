---
scope: project
severity: preference
triggers: []
domain: ''
title: "Add a before-and-after comparison notebook that synchronizes with Python modifications"
pr: 32
merged: 2026-10-08
branch: "LittleBitAI/docs-explain-fix-comparison"
---

# Add a before-and-after comparison notebook that synchronizes with Python modifications

What. To allow team members who modify Python code to immediately compare execution results before and after, we are adding a Korean guide notebook that updates upon saving the source. By running Run All in `notebooks/before_after.ipynb`, it executes the fixed Git commit (A) and the working file snapshot including uncommitted changes (B) as separate processes with the same input, and displays code differences, intermediate outputs, evidence, and inspection results side-by-side.

Why. The file watcher updates the source and description and clears previous outputs. The baseline commit is maintained until explicitly changed, and execution-specific inputs, source archives, environment information, and JSON/HTML reports are saved in the local `.runtime/notebook-comparisons/`. Without duplicating production code, it calls the actual parser, chunker, BM25 search, evidence assembly, prompt generation, answer verification, and log masking functions. Verification: Passed 8 tests including automatic updates, removal of stale outputs, baseline pinning, reflection of added/deleted files, output escaping, and regression detection. Re-verified 3 tests related to final input validation and rendering, and confirmed the execution of all 20 cells in a new Jupyter kernel and the display of the browser report. …

Source. PR #32 · `LittleBitAI/docs-explain-fix-comparison`
