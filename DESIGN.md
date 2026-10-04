---
colors:
  primary: "#1F4E9A"
  on-primary: "#FFFFFF"
  text: "#191F28"
  background: "#FFFFFF"
  surface: "#F2F4F6"
  muted-text: "#5B6573"
  citation-background: "#FFF3BF"
  status-answered: { text: "#1E6B3A", background: "#E6F4EA" }
  status-clarification: { text: "#1F4E9A", background: "#E8F0FB" }
  status-insufficient-or-conflict: { text: "#8A4B00", background: "#FFF1DC" }
  status-error-or-blocked: { text: "#B3261E", background: "#FDECEA" }
  status-neutral: { text: "#4A5160", background: "#EEF0F3" }
typography:
  font: "Pretendard Variable, sans-serif"
  page-title: { size: 24px, weight: 700 }
  section-title: { size: 18px, weight: 700 }
  body: { size: 16px, weight: 400, line-height: 1.6 }
  menu: { size: 14px, weight: 500 }
  label: { size: 13px, weight: 600 }
  caption: { size: 13px, weight: 400, color: muted-text }
  table: { size: 14px }
spacing:
  page-padding-wide: "32px top, 40px sides on chat"
  page-padding-narrow: "32px top, 24px sides on chat"
  chat-sidebar-width: 360px
  section-menu-width: 220px
  review-queue-width: 260px
  input-height: "40px main fields; 32px header name"
  button-height: "44px answer submission; 28px small controls"
  table-row-min-height: 40px
rounded: 10px
components:
  status-badge: "StatusBadge with a text label; colour never carries the state alone"
  citation-chip: "24px-high native button with accessible name 근거 E<n> 원문 보기"
---

# Interface design

The initial redesign of 2026-10-02 made the Streamlit app a thin shell over `service.py`. The initial design pass ran in the fixed order below; its choices were picked by the user from concrete sketches, one question per turn, before layout code was written.

Current code checkpoint (2026-10-03): the frontend is now the three Next.js pages in `web/src/app/`, served through `api.py` over `service.py`; `app.py` and `ui.py` are absent. Streamlit was abandoned for its limited layout flexibility; this continuation polishes the existing frontend without adding another API or framework. The front matter now records the current frontend's tokens. Sections 2–5 preserve the earlier Streamlit pass; section 6 records the code checkpoint and current browser measurements. The user accepted the chat and verification UI on 2026-10-03 and requested a commit and PR after the verification pass. Dataset acceptance remains pending.

## 1. Purpose and layout

### The three screens

| Screen | Who comes here | To do | Success |
| --- | --- | --- | --- |
| 질문하기 | A consultant looking for a past RFP fact | Ask about one or two documents | Numbered claims whose citation chips open the original quote, and the original file |
| 검증 | A team verifier | Explain a retrieval or answer outcome, read evaluation and release results, handle gold and sealed status | A frozen run with its stage trace, scores with denominators, the release decision with reasons, second reviews recorded |
| 데이터셋 만들기 | A verifier building the development gold set | Draft source-bound questions with gpt-6-luna, then have a different person approve or reject each one | Pending candidates shown next to their evidence spans, decided with a note by someone other than the drafter |

### Picks

| Topic | Options offered | Picked |
| --- | --- | --- |
| (a) Navigation and page split | Top menu with the sidebar as document picker; sidebar menu with search in the page; top menu with a two-step chat; a separate 문서 찾기 page | Top menu, and on 질문하기 the sidebar is the document search and picker. The name field and budget meter sit in one row under the menu on every page. |
| (b) Chat answer | Claim list with a right evidence pane; claim cards with inline expanders; claim table with a preview below; chat bubbles with a modal | Claim list (answer 60 %) with a right evidence pane (40 %). Each claim has a number, a kind badge and `근거 E<n>` chips; a chip opens the quote, neighbouring paragraphs, location and original download in the pane. |
| (c) Verification page | Summary tiles with task tabs; a stage-flow view; a run list with detail; one long page with jump links | Four summary tiles (release decision, development set, sealed set, second reviews waiting) above four tabs: 검색 추적 (with run comparison), 평가·릴리스, 골드·봉인 검토, 원문·수집. |
| (d) Dataset generation | Three tabs with a three-step draft; review queue first with a draft modal; a source browser with a basket; plan-file upload | Tabs 초안 만들기, 검토 대기 n건, 처리 기록. Drafting is ① documents and source passages, ② type and intent per slot, ③ maximum cost, consent and generation. Valid drafts are sent to the review queue with one button; the queue shows the draft on the left and its evidence spans on the right. |
| (e) Settings and controls | A keep/remove/move-to-CLI proposal for every control, in five groups | The proposal as offered in all five groups (table below). |
| (f) Typography and density | System font 15 px normal; bundled Pretendard 15 px; system 14 px compact; system 16 px roomy | System font, 15 px body, normal density. |
| (f) Palette | Navy accent; teal accent; neutral with status colours only; the current brick red | Navy accent (section 4). |

### Picks on the Next.js stack

Each screen was built three ways on one named axis in the running app, on live data, behind a picker; the user switched between them and picked one, and the others were deleted. References: Toss for the whole app (Korean readability), Perplexity for 질문하기, Linear for 검증 and the review queue.

| Screen | Axis | Variants built | Picked |
| --- | --- | --- | --- |
| 질문하기 answer | Where claims and their E1/E2 evidence sit | 나란히 (claims left, sticky quote pane right); 문장 속 인용 (Perplexity-style source cards above, numbers at sentence ends open a side sheet); 카드로 펼치기 (Toss-style big conclusion, one card per claim, the quote opens under its card) | 나란히: numbered claims with kind badges and `E<n>` chips in a 3:2 grid; the pane opens the first cited quote and is absent when nothing is cited. |
| 검증 | Page structure | 요약 + 탭 (four summary tiles over eight tabs); 왼쪽 메뉴 (Linear-style grouped menu with counts, one area at a time); 할 일 먼저 (the to-do inbox is the page, tools below) | 왼쪽 메뉴: groups 사람이 볼 차례 (할 일 with its count), 재현 (검색 추적, 실행 비교), 현황 (평가·릴리스 with the release badge, 데이터셋, 수집 상태), 기록 (수정 기록, 요청 기록). 할 일 joins second reviews and auto-flagged originals in one list beside its detail. |
| 데이터셋 만들기 review queue | How the queue and one draft share the screen | 세 칸 (Linear triage: queue list, draft with the decision, original spans); 한 건씩 (‹ n / 23 › with draft and spans side by side); 표에서 펼치기 (a table of all drafts whose row opens draft, spans and decision in place) | 세 칸: 260 px queue, then the draft card with the decision form under it, then the original spans with the cited part on the citation yellow. The page uses the same left menu as 검증: 검토 대기 (count), 초안 만들기, 초안 생성 기록, 처리 기록. |

### Controls decided in (e)

| Control (before) | Decision |
| --- | --- |
| Name field | Keep, in the row under the menu. It attributes requests and reviews and lets the service refuse a drafter's own approval. |
| Budget meter, cap warnings, paid-off and frozen notices | Keep, in the row under the menu |
| Budget 상세 expander (tokens, pacing, per-member spend, last reconciliation) | Move to CLI: `budget-status` |
| Build commit caption | Keep, at the foot of 검증 |
| "로그인 없이 사용합니다" caption | Remove |
| 검색어, 발주 기관 | Keep |
| Amount and closing-date ranges | Keep, folded under 상세 조건 |
| 원문 수집된 문서만 checkbox | Remove: askable documents are listed first |
| 값 없는·충돌 문서도 표시 checkbox | Remove: such documents always show, with a 조건 판단 불가 badge |
| 기준일 | Remove: answers use today's date |
| 더 보기, per-document selection (at most two) | Keep |
| Question mode (paid answer, free basic information, free requirement list; comparison) | Keep, as a segmented control above the input |
| 선택 해제, 원문 파일 받기, 요청 취소, citation buttons | Keep |
| 내 최근 요청 | Keep, as a folded list under the conversation |
| Request record JSON export | Remove from 질문하기; kept on 검증 only |
| Request ID and billing line | Keep, small, at the end of the answer |
| Trace: question source, documents, retrieval mode, run, paid generation | Keep |
| Trace: evidence unit maximum and target tokens | Remove: settings values apply; experiments go through `evaluate-retrieval` |
| Run JSON export | Keep |
| 결과 원본(JSON) expander, internal-ID code blocks, `st.json` diffs | Remove: rendered as tables |
| Run comparison, corrections, original fidelity, ingestion table, evaluation estimate and run | Keep |
| 사용량 관리 page (settlement, reconciliation, external adjustment, paid on/off, audit log) | Move to CLI: `unresolved`, `reconcile`, and the new `settle`, `adjust`, `paid`, `audit` |

Nothing in `settings.py` became unused: the evidence limits still drive retrieval.

### Ask scope control (2026-10-04)

The owner found that selecting one or two documents before asking proves nothing about finding the right passage, so 질문하기 gained a scope switch above the document list:

```
[ 선택한 문서 | 전체 문서 ]
선택한 문서: document search, at most two picks; modes 근거 기반 답변 / 기본 정보 / 요구사항 목록, or the comparison modes for two picks
전체 문서:   no picks; one mode, 전체 문서에서 답변; a hint to name the project or agency in the question
```

- 전체 문서 posts `mode: "corpus"` with an empty scope. The service retrieves over every active chunk (98 sources): keyword BM25 over the whole index, fused with exact pgvector search by the measured `keyword_first` setting, and the evidence is routed to the best-supported documents before generation.
- A question with no lexical hit returns no evidence and the 근거 부족 state; dense search alone never supplies arbitrary passages.
- Picking a document switches the scope back to 선택한 문서. Changing the scope releases the owned request exactly like changing the selection.
- Single-document and two-document comparison behaviour is unchanged. A corpus request with a scope, or a scoped request without one, is refused with 400/422.

### Result states

Every answer opens with a status badge carrying its text label, and each state has its own body.

| State | Badge | Body |
| --- | --- | --- |
| Loading (queued or running) | 대기 중 / 처리 중, neutral | What is happening, the reserved maximum once known, and 요청 취소 |
| Complete | 답변, green | Conclusion, then the claim list with citation chips |
| Insufficient evidence | 근거 부족, amber | Conclusion, the table of unconfirmed fields with reasons, the next action as a callout |
| Clarification | 확인 필요, blue | The question back to the user as a callout, then any claims |
| Conflict | 근거 충돌, amber | A table of the competing values with their citations, side by side |
| Budget blocked | 사용 한도로 차단, red | Why, and that search and originals stay free; no claims |
| Technical error | 기술 오류, red | An error block with the reason code; never rendered as a domain answer |
| Cancelled, interrupted, not ingested | neutral | One line saying so |

### Request ownership

There is no login. The typed name lives in browser storage; React state holds the selected scope, mode and owned request `{request_id, generation_id, target}`. `target` hashes the scope, question, mode and date (`service.target_key`). Submitting creates one generation and idempotency ID; read-only polling reuses it and never resubmits. While the owned request is unfinished, the input is disabled. The answer pane renders a request only when `service.may_attach` holds: same request, generation and target, and not cancelled. Changing the selection or mode makes the old request history; it is cancelled only while still queued (`service.abandon_request`), and its billing continues regardless.

### Polling

Read-only client polling: the budget row every 2 s, the owned request every 1 s while unfinished, and a development evaluation or drafting run every 2 s while it runs. None of these polls can submit, embed, generate, index or judge. Finished results update the current component. A failed ledger read shows "최신 아님" and never shows numbers as live.

### Shell boundary

`api.py` imports only `service` from the package, plus FastAPI, Pydantic and the standard library; `tests/test_api.py` checks this boundary. The pages call the existing API routes, each backed by a service function. Sealed questions and labels never reach the verifier or dataset pages, and the dataset page drafts only from development-split documents.

## 2. Browser measurements (historical Streamlit pass)

Measured on 2026-10-02 in headless Chromium (Playwright 1.63) against this branch served on live `.runtime` data, at 1400 px and 400 px wide, after each rerun had settled. No step raised a Streamlit exception and no page scrolled sideways at either width.

### Widths and spacing

| Measure | 1400 px | 400 px |
| --- | --- | --- |
| Main block | x 300, 1100 wide beside the 300 px sidebar on 질문하기; x 0, 1400 wide on 검증 and 데이터셋 만들기 | x 0, 400 wide; the sidebar folds away |
| Page padding | 120 top, 75 each side | 90 top, 15 each side |
| Sidebar inputs | 211 wide, 33 high | in the folded sidebar |
| Trace question input | 1248 wide | full width |
| Review-queue selectbox | 1218 wide, 36 high | 338 wide |
| Table rows | 28 high; the review meta table splits 214 / 504 | 110 / 258 |
| Summary tiles on 검증 | four in one row | stacked, one per row |

### Typography as rendered

The Korean UI face resolves to Malgun Gothic on every element. Page titles render at 24 px / 700 and section titles at 17 px / 700 in the main area and, after the `[theme.sidebar]` heading scale was added, in the sidebar too (it rendered 18.75 px before). Answer and quote text is 15 px. Streamlit draws widget labels, captions and table cells at 13.125 px with a 21 px line; this is the theme's small size for a 15 px base, so the token table above records it rather than the 13 and 14 px first planned.

### Buttons and interaction

Buttons are 38 px high with 15 px text; icon buttons (clear, close) are 26 px square. Each step below is the time from the click until Streamlit reported the rerun finished:

| Step | Time |
| --- | --- |
| Document search | 2.3 s |
| Pick a document, switch question mode, free basic information, free requirement list | 1.4–1.5 s each |
| Trace: type the question, pick a document, 검색만 실행 (무료) | 1.4–1.7 s each |
| Switch a 검증 tab | 1.4 s; 골드·봉인 검토 2.2 s |
| Switch a 데이터셋 만들기 tab, pick a draft document, find a passage | 1.3–1.7 s |

An open multiselect dropdown swallowed the next click, so the first press of 검색만 실행 did nothing. The trace inputs now sit in one `st.form`: changing them no longer reruns the page, and the submit button acts on the first click. Paid buttons stay disabled until their consent box is ticked, and a running request disables its input until it finishes or is cancelled.

### Keyboard and contrast

`tools/verify.py accessibility` passed on this branch: the pages are usable by keyboard alone (21 Tab stops while asking a question and opening a citation; all 19 stops that are inputs, buttons or links draw a visible focus ring), the lowest contrast among 73 sampled texts is 5.72:1, and at 390 px nothing scrolls sideways.

## 3. Typography (historical Streamlit pass)

Four levels, each with one meaning: page title 24/700 (one per page), section title 17/700 (a tab's or pane's heading), body 15/400 (answers, quotes), small 13.125/400 (form labels, captions, table cells; captions in the muted colour for locations, IDs, costs and hints). The font is the operating system's Korean UI face; nothing is bundled. On the review queue each source span is shown as the text before the quote, the quote, and the text after; the quote carries the blue background, applied line by line because an inline mark cannot cross a line break.

## 4. Colours (original user choice)

Picked by the user from four candidates, all checked against WCAG AA.

| Role | Foreground | Background | Contrast |
| --- | --- | --- | --- |
| Primary button | #FFFFFF | #1F4E9A | 8.0 |
| Body text | #1A1D23 | #FFFFFF | 16.9 |
| Text on surface | #1A1D23 | #F3F5F8 | 15.5 |
| Caption | #5A6270 | #FFFFFF | 6.1 |
| 답변 | #1E6B3A | #E6F4EA | 5.7 |
| 확인 필요 | #1F4E9A | #E8F0FB | 7.0 |
| 근거 부족, 근거 충돌 | #8A4B00 | #FFF1DC | 6.1 |
| 기술 오류, 사용 한도로 차단 | #B3261E | #FDECEA | 5.7 |
| 취소됨, 미상 | #4A5160 | #EEF0F3 | 7.0 |

Red appears only for errors and blocks, so a red element always means a problem. States are always spelled out next to the colour.

## 5. Remaining space (historical Streamlit pass)

What the measurement pass found and changed:

- A result without evidence (free basic information, budget blocked, technical error) takes the full width; the right evidence pane only appears when there is a citation to open.
- The requirement list no longer repeats its completeness line as a caption when the summary already says it.
- The five trace stage tiles each carry one short note, so the strip lines up instead of one tile growing taller.
- The trace limitations paragraph became a table, and the run comparison prints numbers as written rather than as `2,305.0000`.

Space still left over, kept on purpose:

- On 검증 and 데이터셋 만들기 there is no sidebar, so tables run to 1248 px at 1400 px wide. They are left at full width because the evidence and claim columns use it; a reading-width cap would wrap the Korean quotes more often.
- The 120 px top padding is Streamlit's room for the top menu and is not overridden; the app sets no custom CSS.

## Accessibility and text (historical Streamlit pass)

All labels and answers are Korean, every input has a visible label, and everything is reachable by keyboard with Streamlit's visible focus ring. Tables meant to be read use static cells (`st.table`) rather than the canvas grid. Source and model text is escaped before rendering, and no unsafe HTML is used. There are no uncalibrated confidence percentages. Rates show as `0.67 (2/3)`; an empty denominator shows "해당 없음", never 100 %. Citation precision is labelled as a lower bound when unjudged links count against it.

## 6. Current code checkpoint and typography pass

This checkpoint comes from the source and working diff on 2026-10-03. Source declarations establish what is implemented; they do not establish browser behaviour or the user's acceptance of the rendered screens.

### Implemented paths

| Area | Code evidence | Current implementation |
| --- | --- | --- |
| Pages | `web/src/app/page.tsx`, `web/src/app/verify/page.tsx`, `web/src/app/dataset/page.tsx` | Separate chat, verification and dataset pages, with the shared header in `app-header.tsx`. |
| Chat | `ask/answer-view.tsx`, `ask/answer-parts.tsx` | Numbered claims with citation chips; a 3:2 answer/evidence grid; a sticky evidence pane appears only when evidence exists. |
| Verification | `verify/verify-page.tsx`, `side-menu.tsx` | A grouped left menu for the review inbox, retrieval trace, comparison, evaluation/release, datasets, ingestion, corrections and exports. |
| Dataset generation | `dataset/draft.tsx`, `dataset/review.tsx`, `api.py` | Source selection, slot validation, free estimate, consented generation, submit, review and decision call API routes backed by service functions; the review grid is a 260 px queue beside the draft/decision and original spans. |
| Budget and source boundary | `service.draft_documents`, `service._draft_plan`, `service.start_drafting`, `drafting._generate` | Sources must belong to development families; the service passes the explicit `dev` split; `gpt-6-luna` calls reserve their maximum cost through the budget gateway. |
| Review boundary | `service.gold_decide`, `gold.decide`, `gold.candidate`, `gold.queue` | Both decisions require a note; approval is refused for the generation requester and the recorded drafter; sealed candidates are excluded from the page's queue and candidate calls. |
| Existing checks | `tests/test_api.py`, `tests/test_shell.py` | All 14 tests passed during this continuation, covering the API/service import boundary, schema parity, source filtering, drafting billing and independent approval. |

### Where the working code stopped

At the start of this continuation, the four inherited uncommitted edits were limited to two small-control font sizes and two disclosure click targets; they are included in this typography pass:

| File | Working diff |
| --- | --- |
| `web/src/components/ui/button.tsx` | Small button text changes from `0.8rem` to `13px`. |
| `web/src/components/ui/toggle.tsx` | Small toggle text changes from `0.8rem` to `13px`. |
| `web/src/components/ask/document-search.tsx` | The detailed-filter disclosure gains `min-h-6` (24 px). |
| `web/src/components/ask/ask-page.tsx` | The request-history disclosure gains `min-h-6` (24 px). |

The code loads Pretendard locally in `web/src/app/layout.tsx`. At the checkpoint, chat claims declared `15px` with `leading-7`, and dataset evidence declared `text-sm` (14 px) with `leading-7`. Shared table cells already used `text-sm` and `py-2.5`. The pass now uses native `text-base` for claims and original quotes, with its line-height set to 1.6, and `text-lg` for section headings. Source selection shows full passages rather than clipping them to three lines; the existing scroll container bounds the list. Form text is 16 px below the `sm` breakpoint and 14 px above it. Small-text utilities use 13 px. The chat empty-state instruction no longer says “on the left” because its document list is above it on narrow screens.

### Selected typography and density

The user selected “Reading 16, tables 14” on 2026-10-03 from three concrete alternatives: reading 16/tables 14, balanced 15, and roomy 16 throughout. The following roles are now implemented; the choice was not inferred from the code checkpoint.

| Role | Selected target |
| --- | --- |
| Font | Existing Pretendard |
| Page title | 24 px |
| Section title | 18 px |
| Claims and original quotes | 16 px, unitless line-height 1.6 |
| Tables and menus | 14 px |
| Metadata | 13 px |
| Single-line table rows | At least 40 px; wrapped content may grow |

### Browser measurements and behaviour

The old tab at `http://127.0.0.1:8510/` showed a connection error. Measurements instead used an isolated preview at `http://127.0.0.1:8765/`, with the existing phase-4 fixture corpus, two pending candidates and `FakeTransport`; no live API calls were made. Chrome was measured at 1400 × 1000 and 390 × 844, and its temporary viewport override was reset afterwards.

| Rendered measure | Observed value |
| --- | --- |
| Page title on all three pages | 24 px |
| Verification and dataset section headings | 18 px |
| Chat claims, chat evidence and pending-candidate spans | 16 px / 25.6 px line-height |
| Drafting source picker | 16 px / 25.6 px line-height; full passages wrap without a line clamp and fit at 390 px |
| Review-queue metadata | 13 px / 19.5 px line-height |
| Verification table cells | 14 px; sampled single-line rows 44.25–44.75 px high |
| Citation chip | 32 × 24 px; 13 px text |
| Detailed filters, request history and evidence-context disclosures | At least 24 px high |
| Chat main padding at the desktop width | 32 px top/bottom, 40 px sides |
| Review queue at the desktop width | 260 px |
| Mobile name, search, institution, review-note and drafting inputs/select | 16 px |
| Page-level horizontal overflow | None on any of the three pages at either width; the expanded chat history answer also fits at 390 px |

Button behaviour was observed for document selection, fixture answer submission, citation selection, context expansion, request history, verification/dataset menu navigation and drafting document selection. The answer textarea and submit button disabled while the fake request ran and re-enabled on completion. Keyboard activation opened the citation and expanded its surrounding paragraphs; 17 sampled input/button/link focus stops showed visible rings. A screen-reader walkthrough and a complete audit of every button were not performed.

Rendered leaf-text contrast was checked by reading computed foreground and ancestor background colours, converting sRGB/OKLab values and compositing alpha. Samples: chat 41 texts (minimum 5.58:1), verification 25 (5.15:1), dataset review 71 (5.15:1). No sampled pair fell below 4.5:1; these samples are not a complete accessibility certification. No new palette was selected; the navy accent and existing status colours remain.

The whitespace pass keeps the established answer/evidence split, left menus and review queue. No filler panels or additional controls were added. Validation includes the production build, ESLint, TypeScript, the 14 API/shell tests and `git diff --check`. A user click-through of the finished three pages remains required before a completion report.

### Real runtime connection and acceptance boundary

The user rejected the fixture preview as acceptance evidence on 2026-10-03 and supplied the original checkout at `C:\Users\dasdk\PycharmProjects\project-codeit-mid`. The fixture server was stopped. After the user authorized writes to the shared runtime, this branch's API was started on `http://127.0.0.1:8765` with `RFP_SOURCE_DIR` pointing to that checkout's `원본 데이터` and `RFP_DATA_DIR` to its `.runtime`. No repository, corpus, API key file or budget DB was copied. The normal application resource factory selects the default OpenAI provider; the fixture script and its explicit `FakeTransport` are not used.

The connected runtime has schema 6 and active keyword index `62bea0c9c27ad3e7`. The API returned 100 documents and 53 development documents for drafting. Browser checks after reload showed the actual verification inbox and the 23-candidate review queue, including original evidence spans. The shared ledger reported paid generation enabled, $4.388678 available and no pending reservations at connection time; this is the application ledger, not the provider account balance.

The actual 한영대학 RFP was found and selected in chat. An ECR-001 question was prepared without pressing the paid submit button. The pending candidate's source spans identify ASP.NET and MSSQL, providing a concrete reference for checking the resulting answer. This continuation has not yet dispatched a live model call, verified that answer, or received the user's three-page acceptance. The earlier fixture measurements establish only rendering and interaction observations.

For future local tasks, the user's workspace preference is a branch in the original checkout rather than a repository copy or a separately created worktree. The current task's existing worktree remains the location of its code changes.

## 7. Readability correction with the real corpus

### Purpose and layout

The user found the page split useful but the rendered screens difficult to read. Inspection of the actual comparison answer confirmed that unselected document cards faded after two selections, repeated badges competed with claims, and an OCR excerpt filled a narrow pane with yellow highlighting. This pass keeps the selected page split, 3:2 answer/evidence layout and three-column review queue. It gives the summary a labeled surface, separates numbered claims into rows, and puts the exact quotation before optional surrounding paragraphs. No answer text is rewritten.

### Browser measurements and button behaviour

Chrome checks used the existing shared corpus and the user's completed comparison request `67575d73`, including its E5 OCR evidence. They did not use fixture answers or dispatch another model call. Checks covered the actual verification inbox and the 23-item dataset queue at 1400 × 1000 and 390 × 844; the viewport override was reset afterwards.

| Measure or interaction | Observed result |
| --- | --- |
| Document titles after selecting two documents | 16 px / 25.6 px; disabled-card container opacity 1; selection limit still applies |
| Answer claims | 16 px / 25.6 px, separated rows and numbered markers |
| Citation controls | 14 px text, 32 px height |
| Table headers and sampled single-line row | 14 px headers; 45 px row height |
| Verification source names and extraction passages | 16 px; source names no longer clipped to one line |
| Verification section menu | 44 px rows with a visible selected edge |
| Pending questions and review evidence | 16 px; questions no longer clipped to two lines |
| Long E5 quotation on mobile | About 307 px preview versus 742 px full content; visible expand control |
| Keyboard quotation expansion | Enter expands the quotation; the complete source text remains identical |
| Surrounding paragraphs and citation switching | Context expands in source order and resets to folded when the citation changes |
| Horizontal page overflow | None on chat, verification or dataset review at either tested width |

### Typography and colours

The selected reading size remains 16 px, tables and menus 14 px, section headings 18 px, and page headings 24 px. Document, verification and queue titles now use the reading size rather than 14 px. Metadata remains 13 px. Repeated fact classifications use plain text; inference warnings keep their badge. The existing navy palette remains selected. Chat quotations use a navy edge on a white surface instead of highlighting whole paragraphs; dataset review still highlights the specific cited spans.

### Remaining space and validation

Spacing separates the summary, claim rows, quotation and optional context; surrounding paragraphs no longer fill the evidence pane before the citation. No filler panels were added. The production build, ESLint, TypeScript and `git diff --check` passed; edited files were checked as UTF-8 without BOM. These browser observations establish the implemented readability changes, not the user's acceptance. The corrected three pages still require the user's confirmation before the overall completion report.

## 8. Verification workflow and visual review

### Purpose and layout

A verifier comes here to locate a problem, compare it with its source, and see the saved outcome. The user accepted chat and requested repeated visual inspection and repair of verification, specifically automatic revision history and readable task and retrieval views.

The running screen showed a manual correction form requiring an evidence ID, element ID, copied quotation and JSON proposal. Its service only appended a proposal; it did not apply a correction. The actual source and gold review actions already saved their own records, which the screen ignored. The form is removed. History reads those saved actions, including legacy decisions without a newer review-log entry, and refreshes every five seconds. Prior proposals remain visible as unapplied. This adds a read-only service function and API route; it changes no review decision, retrieval, generation, budget or release policy. Sealed questions and labels are excluded.

The selected desktop menu remains. Tasks now have search and review-kind filters, a bounded queue, a compact list of mismatch locations and one selected excerpt. Printed-page previews remain available. Trace inputs open only when requested; saved runs identify their otherwise identical questions with a short run ID. The five trace stages select separate structured panels. Document scope is explicit, warnings use a table, and technical identities and exports are folded. Expanded evidence preserves the exact quotation.

### Browser measurements and button behaviour

Chrome inspection used the shared original runtime at `http://127.0.0.1:8765`, at 1400 × 1000 and 390 × 844. Each layout repair was rebuilt and inspected again. The second pass corrected a CSS display rule that defeated excerpt clamping. Mobile inspection then replaced the cramped menu grid with a labeled native selector. The final pass corrected a misleading human-review count and untranslated diagnostic codes. Screenshots were captured at the actual viewport scale rather than judging a downscaled browser surface.

| Measurement or action | Observation |
| --- | --- |
| Desktop task and trace queue | 300 px; selected rows use the existing navy edge |
| Mobile verification navigation | 140 px high, with a 44 px native selector |
| Mobile history filters | 44 px high, 16 px input text |
| Evidence previews | Two lines, about 51.2 px high at 16 px / 25.6 px |
| Task search | Filtering for the actual 한영대학교 RFP reduced 58 flagged sources to one matching document |
| Finding selection | Enter changed the selected finding; the focus ring remained visible |
| Printed source | The selected original page loaded as a 910 × 1287 image |
| Trace execution | One free keyword search for the actual 한영대학교 RFP saved run `vr-455f1a7fb037` and opened its six evidence units |
| Stage switching | All five panels opened; paid-generation consent survived stage changes and was cleared after checking, without submitting |
| Evidence expansion | Keyboard activation opened the full quotation; its text equalled the unclipped source text |
| History | Existing source reviews appeared without a second write; kind and text filters worked; the screen has no submit action |
| Other verification areas | Comparison, evaluation, dataset state, ingestion and request records were inspected on actual runtime data |
| Mobile overflow | None at page level in any of the eight verification areas; wide tables scroll within their own container |

No source was marked reviewed and no gold decision was made during these checks. There were no paid model or evaluation calls. The free trace is the only new application action. Automated checks use isolated test databases to prove successful saves appear once in history, legacy decisions are included without duplicates, and sealed questions are excluded; those checks are not evidence of answer quality.

### Typography and selected colours

The existing user-selected navy palette and status colours remain. Titles and exact quotations use the selected reading size, 16 px; tables and menus use 14 px; metadata uses 13 px. Repeated warning pills are removed from the task queue. Human review completion counts only human-reviewed statuses; an automatic flag is not presented as completed human review. Technical warning strings are replaced with Korean labels, while exact source content and recorded reviewer notes remain intact.

### Remaining space and validation

Only the selected finding or trace stage fills the detail area. Empty evaluation and release states remain empty rather than gaining invented metrics. Read failures show errors instead of appearing as empty results or indefinite loading. Production build, TypeScript, focused ESLint, the ten API tests and `git diff --check` pass. The user accepted the verification UI on 2026-10-03 and requested a commit and PR. Dataset acceptance remains required before the overall completion report.

## 9. Publication recovery

On 2026-10-03 the user asked the assistant to retrieve the wiki-agent's work from its other folder, commit it and open the PR. The 14 existing commits through `5144ae1` were brought from `frontend-redesign-shell` into the Orca worktree on `LittleBitAI/fix-wiki-agent-worktree` by a fast-forward merge. The original checkout and source branch were preserved.

This request transfers publication from the wiki task server to the assistant and supersedes the server-only publication restriction in the handoff specification. Chat and verification acceptance remain recorded; dataset acceptance remains pending and must be disclosed in the PR. Opening the PR does not establish overall acceptance or authorize merging.

## 10. Request lifecycle repairs after independent review

The existing GPT-5.6-Sol cell reviewed PR #10 in this worktree and found that drafting could dispatch a second paid batch after shutdown released the gateway lock. Application resources now track drafting threads, stop new admission during shutdown, wait within the existing shutdown deadline, recover outstanding reservations conservatively, and guard each batch and its dispatch marker. Interrupted work retains that status instead of reporting completion. Isolated tests hold the gateway lock as a replacement owner while the old response returns; they also stop between reservation and dispatch and during the final batch.

Chat now blocks a second submission while its initial POST is pending. Polling, cancellation and abandonment use the member captured at submission. Navigation releases screen ownership, including an initial POST whose ownership record arrives after navigation; editing a completed question detaches the previous answer while retaining it in request history. Cancellation errors are displayed rather than discarded. Five browser regressions use temporary fixture data and a fake provider. They do not establish actual-corpus answer quality or dataset acceptance.

The standalone TypeScript command now generates Next.js route types before checking them, so it works without an earlier production build. The fresh type check, ESLint and production build pass. Chat and verification acceptance remain recorded; dataset acceptance remains pending. The review verdict and final broad checks will be recorded in the PR after the repair round is reviewed.
