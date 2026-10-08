# PR draft: HWP/HWPX read-only preview loader

Adds `storage.hwp_loader.load_hwp_hwpx(Path)` using `HwpHwpxLoader`, dependencies, and adapter tests. **This API is not connected to production ingestion. The production HWP XML walker and PDF parser remain unchanged.** No data migration or preprocessing is performed.

## Behavior and limitations
The loader returns grouped body, tables and notes rather than native interleaved paragraph/table order. Preview records preserve original text and loader metadata; table cell spans, pages, coordinates and native heading context are not invented. Preview records are not a drop-in replacement for structured table ingestion.

Invalid extension/missing file/dependency import errors propagate. Loader execution errors return `hwp_loader_failed`; malformed results or conversion errors return `hwp_loader_result_invalid` with no partial output. Empty/whitespace-only output returns `hwp_empty_output`. Metadata must be a mapping with string keys; an empty mapping is retained.

## Validation
Code head under test: `194fa37` (`git rev-parse HEAD` = `194fa377f59187963607609c8d62e0013027c8a1`). Later commits on this branch change only this document; `git diff 194fa37 HEAD -- src tests requirements.txt requirements-lock.txt pyproject.toml` is empty.

- Adapter unit tests at `194fa37`, in the README's tested environment (conda `rfp-assistant`, Python 3.12, Windows): `PYTHONPATH=src python -m unittest tests.test_hwp_loader -v` → `Ran 10 tests` / `OK`, including 11 malformed-result subcases and missing-parser dependency propagation. Before the loader was installed there, the same command failed with 18 errors (`No module named 'langchain_hwp_hwpx'`).
- Full suite at `194fa37` in the same environment without a PostgreSQL server: 445 tests, 303 errors all `the tests need PostgreSQL`, 2 failures (`test_release.CheckCommandTest.test_unknown_phase_is_refused`, `test_verification.EvidenceTest.test_a_flow_prints_exactly_one_local_evidence_block_last`). `main` fails the same two (plus one more) in the same environment, so neither comes from this change.
- `git diff --check main...HEAD`: no output.
- Real HWP comparison (recorded before `194fa37` in the macOS rfp-rag environment; NOT re-run at this head — `194fa37` only adds a parser import check ahead of loading, so loader output is unchanged). Nong-eochon sample: legacy XML conversion produced invalid/empty output; loader extracted 176 elements including 174 tables. Robot-industry sample: legacy 466 elements / 79 tables / 51,787 characters; loader 80 elements / 79 tables / 96,333 characters. Both returned zero replacement characters in the latter sample. Increased text volume is not proof of correctness: body/table duplication and ordering remain concerns. Comparison isolated the existing XML walker; legacy binary equation-script injection was not exercised.
- **Actual HWPX document tests were NOT executed**: no real HWPX sample was found in the inspected project/source paths. Mock HWPX tests do not establish real-document fidelity.
- **DB integration tests were NOT executed**: no PostgreSQL server was running for this worktree; no production DB or ingestion job was used.

## Dependency lock
README describes `requirements-lock.txt` as the full installed-package snapshot. Added 15 missing loader/transitive pins from the existing macOS rfp-rag environment and checked 58 active dependency edges against locked versions there. Existing pins were retained. This is an incremental addition, not a full refreeze. On Windows `rfp-assistant`, `pip install --dry-run -c requirements-lock.txt langchain-hwp-hwpx-loader==0.1.1` resolved to exactly those 15 pins with no change to installed packages; after installing them, `pip check` reported `No broken requirements found`. A clean Windows/CUDA install from scratch was NOT tested.

PR #30 is open against main. The branch is pushed; this follow-up fixes a reproduced dependency-import classification defect. Existing user changes are excluded. The PR must not be merged as part of this task.
