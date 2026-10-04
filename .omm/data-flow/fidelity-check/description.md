`cli fidelity run` → `fidelity.verify_source`:
1. `print_hwp` launches the Hancom viewer with `/p`. The Windows default printer must be 'Microsoft Print to PDF'. The function finds the Save dialog through Win32 (`FindWindowW`, `WM_SETTEXT`) and waits for a complete PDF at `.runtime/reviews/printed/<hash16>.pdf`.
2. `compare` checks both directions with 6-character shingles and requires exact runs of 3+ digits.
3. The verdict `auto_verified` or `auto_flagged` is stored in `fidelity_checks` with per-element, cell and page findings.

On the verify screen, `GET /api/verify/fidelity` lists HWP sources, `/pages/{page}` renders a print page to PNG via PyMuPDF, and `POST .../confirm` → `service.confirm_fidelity` records `sample_checked` through `ingestion.record_review`. Per the README, a human review status is never overwritten by an automatic one.