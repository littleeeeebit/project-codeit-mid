# Answer span annotation: browser walk

Walk of 2026-10-11 on a copy of the real generation run `generation-20261010T033906Z-2b3ee8` (55 development
questions, `main` against `review-variant-gen-minimal`, 110 answers), imported into an isolated data folder so the
walk's notes never reached the person's own store. Chromium through Playwright at 1440×900, driving `review/server.py`
on loopback with real mouse drags over the answer text.

| Step | Observed |
| --- | --- |
| Six spans selected and noted: base summary and next action, candidate summary and claim (question `refresh50-a-delivery`), base summary and candidate next action (`refresh50-a-performance`, in the one-answer view) | six highlighted spans; the store holds six `note` records with code-point offsets, the exact text, commit and author `local-user` |
| One note reopened by clicking its mark and edited | an `edit` record appended; the first records unchanged; the box shows the new text and "고침" |
| One answer marked 검토함 · 문제 없음 in the one-answer view | toolbar `검토한 답변 5 / 110` (four noted answers plus one marked) |
| ← → in the one-answer view | 1 → 3 → 2 → … ; each move brings the question back into view |
| Reload | six spans, `5 / 110` |
| Export zip, import into an empty second server through 실행 폴더 가져오기 | six spans, `5 / 110` |
| Per-question note and a decision (none chosen) | `<run-id>.md` and `.json` written; the note is in the decision JSON |

Screenshots: [new note box](note-new.jpg), [comparison with notes](compare-notes.jpg), [an opened, edited
note](note-open.jpg), [one-answer view with evidence beside it](reading-view.jpg), [after reload](after-reload.jpg),
[after export and import](after-import.jpg).
