# Traffic sample review: browser walk

Walk of 2026-10-11 on a real window of codeit's serving database, read through the SSH tunnel (`ssh -N -L 55439:127.0.0.1:55432 bidmate`, `CODEIT_DATABASE_DSN` from `.runtime/codeit-tunnel.env`):

```powershell
python -m rfp_assistant.cli stage-review run traffic --since 2026-10-02 --sample 30 --source-dsn-env CODEIT_DATABASE_DSN
```

The run `traffic-20261011T053115Z-c8939c` froze 242 finished 질문하기 requests (2026-10-02 to 2026-10-10: 693 completed and 168 failed requests in the table, of which these 242 have a 질문하기 key; evaluation, stage-review and verifier requests left out). They fell into 46 groups, and 30 were drawn. The session ran with `default_transaction_read_only=on`. Twenty distinct member IDs exist in that database, and none of them appears in any byte of the run folder (21 MB with the gzip decompressed) or in its exported zip. The DSN's password does not appear either. The folder was copied into an isolated data folder together with a copy of the generation run `generation-20261010T033906Z-2b3ee8` and its earlier walk notes, so the walk's notes never reached the person's own store. Chromium through Playwright at 1440×900 drove `review/server.py` on loopback with real mouse drags.

| Step | Observed |
| --- | --- |
| Open the run | The banner leads: "이 표본은 추측입니다. 모든 경우를 덮지 않습니다." The banner reads 46 groups, 30 picked, 16 groups the sample never reached, and states the rule and the seed. Metrics: 읽은 표본 0/30, 표본이 닿은 묶음 30/46, 242 requests. |
| Sample list | Each pick shows its group in Korean (mode · outcome · validation), the documents, and its reason, e.g. "묶음 대표: 이 묶음 4건 중 하나를 시드 1357774497로 무작위로 골랐습니다." Validation-failure groups come first. |
| A refused answer | `evidence_scope_mismatch` on a two-document comparison. The banner reads "이 답변은 사용자에게 보이지 않습니다 · 아래는 트레이스에 남은 거부된 답변". The refused summary and claims came back from the trace's raw output. A drag over a claim took a note. The reading view is the generation run's own, with ← → navigation. |
| Mark reviewed | `검토함 · 문제 없음` on the next answer. A note on its summary followed. |
| Map, groups outside the sample | `표본 밖 묶음 16` lists only the groups the sample missed, each with one square per request. Opening one and choosing 표본에 추가 raised the sample to 31 and filtered that group out. In the sample list it reads "직접 추가 · local-user · 지도에서 직접 추가". |
| Largest group | Corpus questions answered with validation passed: 101 requests, one drawn. Clicking its 41st square read that answer and took a note. The square turned red and the drawn one stays outlined. |
| Grouping | `실패 유형 › 묶기` exported 9 notes: 3 traffic notes and 6 gold notes. A proposal putting the 3 traffic notes and 2 gold notes in one type passed `error-analysis propose`. 수락 accepted it as v1. Each quoted span names its source, "실제 요청" or "골드". |
| Progress | 사람이 검토한 답변 8 (골드 5 · 실제 요청 3). The type stands on 5 answers, split 골드 2 · 실제 요청 3. |
| Back in the run | `실패 유형 v1 · 사람이 찾은 답변 / 검토한 답변 3개 · 근거와 범위가 어긋난 답 3/3` |

Screens: [sample](sample-top.jpg), [refused answer with a note](reading-refused-noted.jpg), [map outside the sample, one added](map-uncovered-added.jpg), [map marks](map-marks.jpg), [taxonomy with sources](taxonomy-sources.jpg), [progress by source](progress-by-source.jpg).

Review round 1, rechecked on the same window (`traffic-20261011T055429Z-0546cb`, 242 requests, 46 groups, 30 drawn, in an isolated data folder):

- Member IDs are now also looked for behind JSON escapes, at any depth, both when replaced and at the exit gate. None of the 20 member IDs appears in the folder, plainly or after decoding.
- A refused answer that broke its schema no longer stops the run. A field of the wrong type is dropped, and output with nothing readable is shown as it came. 18 of 27 validation failures show the refused answer, as before.
- Two first runs at once now share one pseudonym key: the key is made under the review write lock and published whole.
- A group reads 검증 통과 only when an answer call settled and no failure was recorded. Six requests (two 기본 정보 and four 요구사항 목록 answers) had no answer call and read 검증 안 함 ([map](map-not-validated.jpg)). Every 근거 부족 answer in this window had a validated answer call. Runs frozen before this change read 검증 실패 기록 없음.

Review round 2, rechecked on the same window (`traffic-20261011T060612Z-941072`, 242 requests, 46 groups, 30 drawn):

- Member IDs are no longer found by decoding escapes, which could only decode a whitelist of characters and rewrote whole strings. Each ID is now matched in every JSON spelling of each of its characters: the character itself, its `\u` escape in either hex case (a surrogate pair above U+FFFF), or its short escape, behind any number of backslashes. Only the matched span is replaced, so the frozen text around it stays as it was. The exit gate uses the same matcher. No member ID appears in the folder in any spelling (the 20 real names are all plain ASCII; the tests use one with a quote, a backslash, `!` and an emoji).
- Refused output too deeply nested for the decoder falls back to its raw text instead of stopping the run.
- An answer call counts as validated in any ledger state after it was sent, including `unknown` when the provider returned no usage. The counts on this window are unchanged: 199 passed, 6 not validated.

Review round 3, rechecked on the same window (`traffic-20261011T061909Z-bf9e08`, 242 requests, 46 groups, 30 drawn):

- Matching every spelling of a member ID kept missing one, such as an escape whose own backslash is spelled as an escape. `write_out` now both cleans and checks, and is the only way into the folder:
  - Detection decodes every JSON escape, level after level, until none are left, which is everything a reader can recover however often they decode.
  - A string that names a member is cleaned value by value if it is JSON text, so it stays valid JSON. Other text has the ID replaced where it is spelled plainly or JSON-escaped.
  - A string where an ID still hides behind an escape is withheld whole, as `[구성원 ID가 인코딩된 채 들어 있어 가렸습니다]`.
  - Strings that name no member are written exactly as they were.
- An unpaired surrogate, which no UTF-8 file can hold, is written as U+FFFD instead of stopping the run.
- The view is built from the cleaned record, so it shows what the frozen input holds.
- On this window no member ID is left after decoding every escape, and no string was withheld.

Found and fixed during the walk: the run list showed a host dash for a traffic run. Pick reasons repeated raw codes that the meta line already shows in Korean. Squares outside the sample were too faint against white (now 3:1). The map had no way to list only the groups the sample missed. Taxonomy spans named "실제 요청" twice.
