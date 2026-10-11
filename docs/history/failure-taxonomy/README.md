# Failure taxonomy from answer notes: browser walk

Walk of 2026-10-11 on a copy of the real generation run `generation-20261010T033906Z-2b3ee8` (55 development
questions, `main` against `review-variant-gen-minimal`, 110 answers), in an isolated data folder so the walk's notes
and types never reached the person's own store. Eleven notes on real answer spans and ten "reviewed, no issues"
marks went in through `review/server.py`'s own API.

The notes were exported with `python -m rfp_assistant.cli error-analysis export`. The coding agent grouped them by
following the bundle's `prompt.md`, and the grouping was submitted with `error-analysis propose`. A first proposal
held a type with no note behind it ("근거 없는 추측"). It was refused with exit 1 and nothing was stored. The
second proposal had seven types covering all eleven notes, and it was stored. Then Chromium, driven by Playwright
at 1440×900, walked the screens with real clicks. No model or paid call was made.

| Step | Observed |
| --- | --- |
| Open 실패 유형 | 7 proposed types waiting; 묶기 프롬프트 복사 disabled, since every note is in a proposal |
| Quoted spans behind a type | 내부 근거 표기 노출 shows “[ E7 ]”, “(doc: 6a6f639c”, “(고려대: E1, E7)” with each note, its question, candidate and author |
| 수락 | `수락된 유형 1개 · v1 · 결정 대기 6개` |
| 이름 고쳐 수락 | 기준 바꿔 적기 accepted as 기준 금액 바꿔치기 with a new definition |
| Two checkboxes, then 합쳐 수락 | 세부 항목 뭉뚱그림 + 모호한 표현 became 흐릿한 요구사항, carrying both quoted spans |
| 나누기 | 읽기 어려운 형식 became 지나치게 긴 문장 (2 notes) and 특수 문자 섞임 (1 note) |
| 버리기 with a reason, then 수락 | 질문 회피 rejected; 처리 실패 문구 노출 accepted; no proposal left |
| Accepted taxonomy and 결정 기록 | `수락된 유형 6개 · v5`. Five changes made five versions; the rejection of a proposal kept the version. `history.jsonl` holds six decisions and `versions/` holds `v1`–`v5` |
| 묶기 프롬프트 복사 | the rejected type's note was free again: a new bundle was written and the clipboard held "Read …\prompt.md and follow it: group the 1 answer notes …" |
| Reload | `수락된 유형 6개 · v5` |
| 진행 상황, last 20 | breadth 6, 21 reviewed answers, 5 types first seen in the last 20, 5 types under three cases (marked), depth bars with `n / 21 답변` |
| 진행 상황, last 10 | "최근 10개에서 새 유형이 나오지 않았습니다": the last ten reviewed answers were the fine marks |
| Generation run summary | main: `실패 유형 v5 · 검토한 답변 12개 중` 지나치게 긴 문장 2/12, 내부 근거 표기 노출 1/12, 특수 문자 섞임 1/12, 처리 실패 문구 노출 1/12. The candidate: 내부 근거 표기 노출 2/9, 흐릿한 요구사항 2/9, 기준 금액 바꿔치기 1/9 |
| Page errors | none |

Screenshots: [proposed types](pending.jpg), [rename while accepting](rename.jpg), [merge bar](merge.jpg),
[split form](split.jpg), [accepted taxonomy with history](accepted.jpg), [copied prompt](prompt.jpg),
[progress, last 20](progress.jpg), [progress, last 10](progress-10.jpg),
[per-candidate counts](candidate-summary.jpg). The step log is in [walk-log.json](walk-log.json).
