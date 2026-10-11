# Similar-case proposals and second-look flags: browser walk

Walk of 2026-10-11 on a copy of the real generation run `generation-20261010T033906Z-2b3ee8` (55 development
questions, `main` against `review-variant-gen-minimal`, 110 answers). It ran in an isolated data folder, so nothing
reached the person's own store. Everything went in through `review/server.py`'s own API, the CLI and the screens:

- six "reviewed, no issues" marks, two of them on answers that show internal evidence notation;
- two notes on real spans: `[ E7 ]` and `(doc: 6a6f639c`;
- one type grouped from those notes with `error-analysis export` / `propose` (내부 근거 표기 노출: internal
  evidence notation exposure).

Chromium, driven by Playwright at 1440×900, then walked the screens with real clicks. The coding agent (this
session) read the exported bundle itself and wrote the suggestions. No model or paid call was made.

| Step | Observed |
| --- | --- |
| 수락 on 실패 유형 | `수락된 유형 1개 · v1` |
| 비슷한 사례 찾기 프롬프트 복사 | the clipboard held the shown prompt: "Read …\suggest-exports\suggest-…\prompt.md and follow it: find other answers in …\answers.jsonl showing the failure type “내부 근거 표기 노출” defined in …\type.json, then submit them with `python -m rfp_assistant.cli error-analysis suggest --file …\suggestions.json`". The bundle held all 110 stored answers, reviewed or not, and the type's two approved spans |
| Agent search | a scan of every field of `answers.jsonl` found evidence numbers and document IDs in 15 answers; nine spans were written up, including `TER-002`, a requirement code taken from the RFP |
| `suggest` with an unknown type id | exit 1: `unknown failure type 'ffffffffffff': suggest only for an accepted type`; nothing stored |
| `suggest` with a paraphrased span (`[E11, E13]` for `[ E11, E13 ]`) | exit 1: `“[E11, E13]” is not verbatim in its stored answer field` |
| `suggest` with the nine spans | exit 1: `“TER-002” occurs 2 times in its field; give start`. The agent added `start` and the next run stored 9 suggestions |
| AI 제안 9개 검토 | the queue showed each span highlighted inside its stored answer field, the question, candidate and answer part, and the agent's one-line reason. Above the queue: "AI 제안은 모든 사례를 찾지 못합니다. 승인한 사례는 사람이 찾은 사례와 따로 세고, 검토한 답변 대비 비율에 넣지 않습니다." (AI suggestions do not find every case; approved ones are counted apart from cases the person found and are left out of the rate over reviewed answers.) |
| 승인 ×8 | each approval left one fewer waiting: `AI 제안 1개 남음 · 승인 8 · 거절 0` |
| 거절 with a reason | `TER-002` rejected with "일반 문장 속 숫자이지 내부 근거 표기가 아님" (a plain number in the sentence, not internal notation); it shows under "거절한 제안 1개 · 다음에 에이전트에게 함께 보냅니다" (rejected suggestions, sent to the agent next time) |
| Type counts | `사람이 찾은 답변 2개 · AI 제안 승인 7개 · 메모 10개`. Eight approvals made seven extra answers, because `[ E11, E13 ]` lies in the same answer as the person's own `[ E7 ]` note. Each approved note shows "AI 제안 · local-user 승인" (AI suggestion, approved by local-user) |
| Copying the prompt again | the new bundle's `type.json` lists 10 approved spans (2 `human`, 8 `ai_suggested`) and the rejected `TER-002` with its question, candidate, the agent's reason and `why_rejected` |
| 진행 상황 | `1` type, `8` reviewed answers, `6` 기준 추가 후 다시 볼 답변 (answers to look at again since the type was added). The depth row reads `2 / 8 답변 (사람이 찾음) · AI 제안 승인 +7 · 다시 볼 답변 6`; the run is linked for the second look |
| Generation run, 다시 볼 답변 6 | six answers showed "기준 추가 후 다시 보기 (내부 근거 표기 노출)" with a 다시 봤음 · 여전히 문제 없음 button (looked again, still no issues). Two approved AI spans showed as dashed blue marks, `(고려대: E1, E7)` and `(doc: e6fa61a5`, both in answers the person had marked fine before the type existed |
| Candidate summaries | main: `내부 근거 표기 노출 1/6 · AI 제안 승인 +6`; the candidate: `1/2 · AI 제안 승인 +1` |
| 다시 봤음 on one answer | 5 flags left. On 진행 상황 the count reads `5`, and the type still "first appeared in the 7th reviewed answer": a second look does not move the answer in the review order |
| Page errors | none |

Screenshots: [copied prompt](prompt.jpg), [queue](queue.jpg), [reject reason](reject-reason.jpg),
[after deciding](decided.jpg), [progress](progress.jpg), [second-look flag](rereview-flag.jpg),
[candidate counts and filter](candidate-summary.jpg), [progress after one second look](progress-after.jpg). The step
log is in [walk-log.json](walk-log.json).
