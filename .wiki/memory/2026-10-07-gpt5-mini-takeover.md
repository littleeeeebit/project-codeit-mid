---
kind: memory
repo: project-codeit-mid
focus: next
title: "GPT-5-mini embedding comparison takeover: approved run stopped on unknown billing"
---

# GPT-5-mini embedding comparison takeover

Continue the task branch `gpt5-mini-answer-embedding-comparison` and spec revision 1. Claude exhausted its quota after starting the approved final comparison. The implementation commits are `9827f21` and `ef4f16b`; no task PR existed at takeover. The final comparison is incomplete, so do not report completion or publish a final result.

## Approval and run state

The user explicitly approved estimate `12af3585624f` on 2026-10-07 at 09:48:23 UTC: 660 GPT-5-mini answers, K1 plus 11 hybrid runs over the same 55 development questions, maximum $6.475916, on the owner's personal key. The approved budget change moved $2.50 from `embedding` to `gold_eval`, leaving the shared $20 cap unchanged. Claude applied the audited change and started six workers on codeit at 09:49 UTC.

Run `E-b51da1d8a276` stopped at 10:04 UTC with `unknown_billing`. It completed 46 of 660 answers, settling $0.243718. One attempt retains a $0.010889 reservation:

| Field | Recorded value |
| --- | --- |
| Attempt | `b78f39ec-cc51-4ca4-bb5f-6a944bb986f9` |
| Request | `c5693fa4-8272-4033-a3da-2b2ed1480cb4` |
| Dispatched | `2026-10-07T10:01:27.197339+00:00` (19:01:27 KST) |
| Model and purpose | `gpt-5-mini`, `gold_eval`, member `evaluation-job` |
| Error | `APITimeoutError: Request timed out.` |
| Maximum input/output | 11,553 estimated input tokens; 4,000 maximum output tokens |

Claude's liveness monitor used a pattern that could match its own remote command, so its liveness output is unreliable. At takeover no answer process remained, and `bidmate` was still inactive. Codex restarted it on 2026-10-07 at 10:48 UTC; `systemctl is-active bidmate` returned `active`, and `/` returned HTTP 200. The restart clears members' in-memory API keys; they must enter them again.

The partial table and run configuration, scores and log were copied to the owner's `.runtime/` without any keys. Canonical records remain on codeit under `/srv/bidmate/app/.runtime/runs/E-b51da1d8a276/` and `/srv/bidmate/app/.runtime/compare/tables/answer-embedding.{json,md}`. The table contains K1 and all 11 requested hybrid rows, depth 50, units 10, fusion `keyword_first:60:1.0:6`; jina and embeddinggemma are research-only. No row completed, so no winner can be concluded.

## Verified implementation

Claude's full-suite log `.runtime/full-suite-gpt5mini-2.log` records 510 passing tests, three skipped. Codex independently reran `tests.test_characterization`, `tests.test_drafting` and `tests.test_evaluation`: 126 tests passed. `npm run lint`, `npm run typecheck` and `git diff --check` passed. The auto-memory file `C:/Users/dasdk/.claude/projects/C--Users-dasdk-PycharmProjects-project-codeit-mid/memory/rfp-assistant-budget-and-model.md` already states the revised model/key policy.

Both hosts' loaded configuration resolves to GPT-5-mini. codeit loads `/srv/bidmate/app/docs/history/postgresql-migration/config.example.json`, which omits `generation_model`; the owner host's example and `.runtime/config.postgresql-live.private.json` also omit it. codeit's request timeout is 60 seconds. Historical evaluation configs naming Luna are historical records and must stay unchanged.

The browser check uses the existing scratch script `C:/Users/dasdk/AppData/Local/Temp/bidmate-check/settings_check.py`, Playwright, an isolated fixture database, the fake provider and fake hub. The current comparison menu is named `검색 구성 비교`; the task calls that same screen `실험 비교`. The script was corrected to use the current menu label and asserts the two model choices, selected-model key-check payload, 12 table rows and research-only labels. `.runtime/codex-takeover-browser-fixed.log` records a passing browser check, including switching from embeddinggemma to jina with no browser errors.

That interaction exposed a row-detail cleanup bug: the scrolling effect returned `scrollIntoView`'s result, which React then tried to call as a cleanup when the row changed. The browser stack points at React's `destroy()` invocation. The effect now uses a block and returns nothing; the same browser interaction passes. The other two `scrollIntoView` callers already discard its return value.

## Resume procedure and blocker

Billing evidence is missing for the timeout. [Runbook section 5](../../docs/operations/runbook.md#5-budget-recovery) requires dated provider token usage for `settle`, or a dated closed provider interval total and project scope for `reconcile`. It explicitly says to keep attempts unknown when provider evidence is unavailable. Do not invent token usage, settle the reservation as actual usage, erase the attempt or bypass dispatch admission.

1. Obtain the owner's dated provider usage/export or interval evidence covering the personal-key call at 19:01:27 KST. The server-environment CLI key recorded no billing project, so interval reconciliation needs the actual project scope and `unscoped_attempts: "include"`.
2. Resolve the attempt through the existing owner CLI `settle` or `reconcile`. Ledger-only commands run beside the serving service with `sudo sh /srv/bidmate/app/tools/infra/bidmate-cli.sh ...`; the repository script is not executable on codeit.
3. Re-plan the same 12 retrieval runs to price only unfinished rows. Preserve the original approval and $6.475916 total ceiling; seek a new spending decision only if the new work exceeds that approved scope or ceiling. Do not repeat already completed answers. The unknown answer can receive a new, recorded attempt only after its earlier attempt is settled or reconciled.
4. Stop the service for the owner gateway, run with the personal key supplied through stdin to the process environment, then restart the service even if the run stops again. Never store the key in a remote file.
5. Verify the completed table and shared ledger, finish browser evidence, then commit/push and publish the task PR with the final table and run ID. Stop after PR publication; wiki-agent owns any explicitly requested Review Loop.

No spec requirement was dropped or revised at takeover. The final comparison, final table and PR body remain outstanding.
