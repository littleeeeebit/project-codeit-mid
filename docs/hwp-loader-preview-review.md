# PR draft: HWP/HWPX read-only preview loader

Adds `storage.hwp_loader.load_hwp_hwpx(Path)` using `HwpHwpxLoader`, dependencies, and adapter tests. **This API is not connected to production ingestion. The production HWP XML walker and PDF parser remain unchanged.** No data migration or preprocessing is performed.

## Behavior and limitations
The loader returns grouped body, tables and notes rather than native interleaved paragraph/table order. Preview records preserve original text and loader metadata; table cell spans, pages, coordinates and native heading context are not invented. Preview records are not a drop-in replacement for structured table ingestion.

Invalid extension/missing file/dependency import errors propagate. Loader execution errors return `hwp_loader_failed`; malformed results or conversion errors return `hwp_loader_result_invalid` with no partial output. Empty/whitespace-only output returns `hwp_empty_output`. Metadata must be a mapping with string keys; an empty mapping is retained.

## Validation
- Adapter unit tests: 9/9 passed, including 11 malformed-result subcases.
- `git diff --check`: passed.
- Real HWP comparison artifacts remain separately preserved. Nong-eochon sample: legacy XML conversion produced invalid/empty output; loader extracted 176 elements including 174 tables. Robot-industry sample: legacy 466 elements / 79 tables / 51,787 characters; loader 80 elements / 79 tables / 96,333 characters. Both returned zero replacement characters in the latter sample. Increased text volume is not proof of correctness: body/table duplication and ordering remain concerns. Comparison isolated the existing XML walker; legacy binary equation-script injection was not exercised.
- **Actual HWPX document tests were NOT executed**: no real HWPX sample was found in the inspected project/source paths. Mock HWPX tests do not establish real-document fidelity.
- **DB integration tests were NOT executed**: the local test Python lacks psycopg; no production DB or ingestion job was used.

## Dependency lock
README describes `requirements-lock.txt` as the full installed-package snapshot. Added 15 missing loader/transitive pins from the existing macOS rfp-rag environment and checked 58 active dependency edges against locked versions. Existing pins were retained. This is an incremental addition, not a full refreeze. Full Windows/CUDA environment installation and cross-platform resolution were NOT tested.

GitHub push and PR creation are pending user authorization. Existing user changes are excluded from this commit.
