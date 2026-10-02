---
colors:
  primary: "#1F4E9A"
  on-primary: "#FFFFFF"
  text: "#1A1D23"
  background: "#FFFFFF"
  surface: "#F3F5F8"
  muted-text: "#5A6270"
  status-answered: { text: "#1E6B3A", background: "#E6F4EA" }
  status-clarification: { text: "#1F4E9A", background: "#E8F0FB" }
  status-insufficient-or-conflict: { text: "#8A4B00", background: "#FFF1DC" }
  status-error-or-blocked: { text: "#B3261E", background: "#FDECEA" }
  status-neutral: { text: "#4A5160", background: "#EEF0F3" }
typography:
  font: "Malgun Gothic, Apple SD Gothic Neo, Noto Sans KR, sans-serif"
  page-title: { size: 24px, weight: 700 }
  section-title: { size: 17px, weight: 700 }
  body: { size: 15px, weight: 400 }
  label: { size: 13.125px, weight: 400 }
  caption: { size: 13.125px, weight: 400, color: muted-text }
  table: { size: 13.125px }
spacing:
  page-padding-wide: "120px top, 75px sides"
  page-padding-narrow: "90px top, 15px sides"
  sidebar-width: 300px
  input-height: 33px
  button-height: 38px
  table-row-height: 28px
rounded: 8px
components:
  status-badge: "st.badge with a text label; colour never carries the state alone"
  citation-chip: "tertiary button labelled 근거 E<n>"
---

# Interface design

The redesign of 2026-10-02 turns the Streamlit app into a thin shell over `service.py`. The design pass ran in the fixed order below; every choice in section 1 was picked by the user from concrete sketches, one question per turn, before any layout code was written.

Status: in the click-through on 2026-10-02 the user rejected the rendered screens. Built from stock Streamlit widgets, titles, tables and buttons all carry the same weight, and the new pages do not stand out. The user chose to settle a new spec that replaces the frontend stack. The page split, the result states, the settings decisions and the `service.py` boundary below carry over to that stack; the Streamlit-specific measurements in sections 2 and 3 do not.

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

There is no login. Session state holds the typed name, the selected scope, the mode and the owned request `{request_id, generation_id, target}`; `target` hashes the scope, question, mode and date (`service.target_key`). Submitting creates one generation and idempotency ID; reruns reuse it and never resubmit. While the owned request is unfinished, the input is disabled. The answer pane renders a request only when `service.may_attach` holds: same request, generation and target, and not cancelled. Changing the selection or mode makes the old request history; it is cancelled only while still queued (`service.abandon_request`), and its billing continues regardless.

### Polling

Read-only fragments only: the budget row every 2 s, the owned request every 1 s while unfinished, a development evaluation or a drafting run every 2 s while it runs. None of them can submit, embed, generate, index or judge. When the watched work finishes, the fragment triggers one app rerun. A failed ledger read shows "최신 아님" and never shows numbers as live.

### Shell boundary

`ui.py` imports only `service` from the package, plus Streamlit and the standard library; `tests/test_ui_boundary.py` checks this. Every action calls one `service.py` function. Sealed questions and labels never reach the verifier or dataset pages, and the dataset page drafts only from development-split documents.

## 2. Browser measurements

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

## 3. Typography

Four levels, each with one meaning: page title 24/700 (one per page), section title 17/700 (a tab's or pane's heading), body 15/400 (answers, quotes), small 13.125/400 (form labels, captions, table cells; captions in the muted colour for locations, IDs, costs and hints). The font is the operating system's Korean UI face; nothing is bundled. On the review queue each source span is shown as the text before the quote, the quote, and the text after; the quote carries the blue background, applied line by line because an inline mark cannot cross a line break.

## 4. Colours

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

## 5. Remaining space

What the measurement pass found and changed:

- A result without evidence (free basic information, budget blocked, technical error) takes the full width; the right evidence pane only appears when there is a citation to open.
- The requirement list no longer repeats its completeness line as a caption when the summary already says it.
- The five trace stage tiles each carry one short note, so the strip lines up instead of one tile growing taller.
- The trace limitations paragraph became a table, and the run comparison prints numbers as written rather than as `2,305.0000`.

Space still left over, kept on purpose:

- On 검증 and 데이터셋 만들기 there is no sidebar, so tables run to 1248 px at 1400 px wide. They are left at full width because the evidence and claim columns use it; a reading-width cap would wrap the Korean quotes more often.
- The 120 px top padding is Streamlit's room for the top menu and is not overridden; the app sets no custom CSS.

## Accessibility and text

All labels and answers are Korean, every input has a visible label, and everything is reachable by keyboard with Streamlit's visible focus ring. Tables meant to be read use static cells (`st.table`) rather than the canvas grid. Source and model text is escaped before rendering, and no unsafe HTML is used. There are no uncalibrated confidence percentages. Rates show as `0.67 (2/3)`; an empty denominator shows "해당 없음", never 100 %. Citation precision is labelled as a lower bound when unjudged links count against it.
