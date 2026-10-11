# Maintenance parse time limit, cancel and progress: root cause and browser walk

## Why the Oct 10 run hung

On codeit, maintenance run `M-20261010-233122` re-parsed 59 of 98 originals in 94 seconds. Then one thread of the
serving process sat at 100% CPU for more than seven hours. A `py-spy dump --locals` of the live process on
2026-10-11 showed what was running:

```text
Thread "rfp-maintenance-M-20261010-233122" (active+gil)
    _extract_hyperlink_texts_from_para (hwp_hwpx_parser/hwp5.py:897)   i: 14, code: 3
    ...
    load_hwp_hwpx (rfp_assistant/storage/hwp_loader.py:41)
    _parse_source (rfp_assistant/corpus/ingestion.py:854)               reason: "hwp_converter_missing"
        src: {"source_hash": "4098c9b30df8fcdd…", "format": "hwp"}
    ingest_source → ingest → step_ingest → maintenance.run
```

The original is `케빈랩 주식회사_평택시 강소형 스마트시티 AI 기반의 영상감시 시스템 .hwp`, the suspected file. Two faults
combined:

1. The service found no HWP converter. `default_converter()` looked for `Scripts/hwp5proc.exe` (a Windows conda
   layout) and then on `PATH`. The systemd unit's `PATH` has no venv, so `/srv/bidmate/venv/bin/hwp5proc` was never
   found. The converter is part of every HWP's input key, so the missing converter changed the key of every HWP, and
   every one was re-parsed through the fallback HWP loader.
2. The loader, `hwp_hwpx_parser` 1.0.0, loops forever on this original. In `_extract_hyperlink_texts_from_para`, a
   control code `0x03` within 6 bytes of the end of the record advances nothing, so `i` stays at 14.

Locally, the loader alone on this file hangs at the same line (stopped by `faulthandler` after 20 s). The normal path
(pyhwp with the converter found) parses it in 4.16 s, in line with its earlier 3.46 s. `default_converter()` now
looks beside the interpreter first (`Scripts/hwp5proc.exe`, `hwp5proc.exe`, `hwp5proc`), then on `PATH`.
`tests.test_maintenance.ConverterTest` returns `None` on the old code and the venv path on the new.

Left on codeit: the 59 re-parsed HWP sources there point at loader extractions. Serving still searches the activated
index's extractions. Once the fixed code runs, the next maintenance run sees the converter in the input key again and
re-parses them with pyhwp. Identical output reuses the earlier extraction ids. The stuck run itself waits for a
`bidmate` restart at a time the owner chooses.

## Browser walk

2026-10-11, Chromium through Playwright at 1440×1000, the API and the built screens on one uvicorn worker. The
corpus was the fixture corpus, its keyword index activated, plus 12 real HWP originals added to the manifest:
11 others and the 케빈랩 one, 15 unique originals in all. The local converter was used and the provider was fake.
A copy of the full local corpus was not used, because its `pg_dump` alone ran past 10 minutes. Steps are in
[walk-log.json](walk-log.json).

| Run | Observed |
| --- | --- |
| `parse_timeout_seconds: 1` | The 12 new HWPs each hit the limit; the fixture HWP read in time. The heading reads 끝났지만 검증되지 않음, followed by one Korean sentence ("시간 제한 안에 읽지 못한 원문 12개가 이전 추출로 남았습니다") and a notice listing the 12 filenames. Ingest reads "원문 16개 추출 모두 그대로 … · 1초 안에 못 읽은 12개는 이전 추출 유지". Fidelity, keyword, embedding, regression and report all ran. API status `unverified`, `timed_out` = the 12 names ([screen](timed-out-unverified.jpg)). |
| Default limit, 취소 | Backup and restore check done, then "실행 중 0:06 경과 · 4 / 15 읽는 중 · (사)벤처기업협회_… .hwp · 이 파일 0:00". Four seconds later: "0:10 경과 … 이 파일 0:04" ([1](progress-1.jpg), [2](progress-2.jpg)). The page showed the cancelled state 2.4 s after 취소 was pressed: heading 매니페스트·수집에서 취소됨, then "verify-verifier-…이(가) 2026-10-11 08:13에 취소했습니다. 이미 쓴 결과는 그대로 남습니다." Ingest reads "3 / 15까지 읽고 멈춤 · … 읽던 중 취소", and later steps read 대기 ([screen](cancelled.jpg)). API status `cancelled`, `stopped_at` `ingest`, `cancelled_by` and `cancelled_at` set. The report `.md` was written, and 유지보수 실행 was enabled again. |
| Cancel with no live run | `POST /api/verify/maintenance/cancel` answered 400 "실행 중인 유지보수가 없습니다." |

The first walk also found a bug, fixed before the runs above: a new original that timed out was also listed as
changed, because `before` is read before the manifest import.
