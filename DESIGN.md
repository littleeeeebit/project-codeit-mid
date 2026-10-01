# Interface design (phase 3)

Written before the phase 3 layout changes, as the [phase 3 plan](docs/plan/end-to-end/3-workflows-and-operations.md) requires. It records purpose, reading order and state ownership. It does not record measured widths, spacing or colors. Real-browser observations live in `.runtime/releases/phase-3/report.md`, and only for what was actually checked.

## Purpose first

| Screen | Who comes here | What success looks like |
| --- | --- | --- |
| 컨설턴트 | A consultant looking for a past RFP fact | A relevant project, an answer whose claims open the original evidence, and the original file |
| 검증 | A team verifier explaining a failure or comparing configurations | A frozen run ID with the stage trace, a comparison of two runs, a correction with quoted evidence |
| 질문 검토 | A verifier who did not draft the dataset questions | Each candidate approved or rejected against the original |
| 사용량 관리 | The owner (open to every visitor: there is no login) | Unknown billing resolved with evidence, provider intervals reconciled, every action audited under the typed name with a reason |

Each page has one primary task. Provenance, token details and per-member spend sit in expanders. Retrieval scores, chunk IDs and model names stay on the verifier page.

## Consultant reading order

On a wide screen the order is:

1. A compact strip with the historical-corpus notice, the as-of date and the shared allowance (in the sidebar).
2. Search and filters, then a readable result list.
3. The selected project(s) with the question form.
4. The answer next to its evidence panel.

Streamlit columns stack on narrow screens, so the same order holds there.

- **Results** are bordered cards, not a grid. A long Korean title wraps in full, so similar projects stay distinguishable. Each card carries the recorded institution, an explicit conflict warning, a KRW amount (unknown stays distinct from 0원), the publication and closing dates, and the source review state. Nothing on a card says a bid is open now.
- **Selection** uses a checkbox per card, with at most two. One selected project allows a grounded question (paid), a basic-information view (free) or a requirement list (free). Two allow a balanced comparison (paid) or a basic-information comparison (free). The selected titles, source versions (hash prefix) and as-of date sit next to the question.
- **Answer.** A short conclusion comes first, then source facts and labelled inferences, unknown/conflicting data and the next verification action. A technical failure appears in its own error block, never as a domain answer. Each claim's evidence buttons open the evidence panel: the exact quote, neighbouring elements with the cited ones in bold, the location (PDF physical page, or HWP section/table without invented page numbers), source review warnings, and the original download.
- **Cost of this request.** The panel shows the reserved maximum while the request runs, the settled cost after it, and an unknown pending cost after an interruption.

## Request ownership

There is no login. Session state holds the typed visitor name, the selected scope, mode, as-of date and the owned request. The owned request is recorded as `{request_id, generation_id, target}`, where `target` hashes the scope, question, mode and date. Submitting creates one generation/idempotency ID; reruns reuse it and never resubmit. While the owned request is unfinished, the submit button is disabled.

The answer panel renders a request only when `ui.may_attach` holds: the same request, generation and target, and not cancelled. Changing the selection, mode or date changes the target. The old request then becomes history, and it is cancelled only if it is still queued. Its server-side billing continues regardless. "내 최근 요청" reopens a past request with its frozen scope labelled as history, never as the current answer.

## Polling

Two fragments only read: the allowance strip every 2 s and the owned request every 1 s while it is unfinished. Neither can submit, embed, generate, index or judge. When the request finishes, the fragment triggers one app rerun, which stops polling and re-enables the form. A failed ledger read shows "최신 아님" and never shows numbers as live.

## Accessibility and text

All labels and answers are in Korean, with labelled inputs. States are spelled out in text next to any color: 대기 중, 처리 중, 근거 부족, 확인 필요, 근거 충돌, 사용 한도로 차단, 기술 오류, 취소됨, 중단됨. Source and model text is escaped before rendering, and no unsafe HTML is used. There are no uncalibrated confidence percentages.
