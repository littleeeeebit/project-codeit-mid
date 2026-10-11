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

Found and fixed during the walk: the run list showed a host dash for a traffic run. Pick reasons repeated raw codes that the meta line already shows in Korean. Squares outside the sample were too faint against white (now 3:1). The map had no way to list only the groups the sample missed. Taxonomy spans named "실제 요청" twice.
