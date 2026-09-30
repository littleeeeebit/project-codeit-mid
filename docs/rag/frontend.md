# Consultant and verification interfaces

The consultant comes here to find an RFP and obtain a source-backed answer that helps decide the next consulting action. Success is a relevant project or supported requirement answer whose original evidence can be inspected. The team verifier comes here to explain a pipeline failure or compare a frozen experiment. Success is a reproducible trace, a reviewed label, and a recorded quality/cost comparison.

These purposes require separate entry points and access controls. They can share one Streamlit application package and the same pipeline; they do not need separate frontend frameworks. This is a concept and acceptance plan, not a browser-tested screen or a chosen color palette.

## Consultant workflow

Begin with one work screen: project search, useful filters, and a compact shared budget status. Show results in a readable list with project title, actual contracting institution, recorded amount, publication/closing date, and ingestion state. Historical corpus status and an explicit as-of date must be visible; the current files cannot support a default “open now” recommendation.

Selecting a project establishes document scope. Show the selected title and version next to the question box. An answer appears with a short conclusion, supported facts, conditions/unknowns, and clickable evidence. A source panel should open the cited excerpt plus nearby context and offer the original file. It should not require users to understand chunk IDs, embedding models, or RRF scores.

| User action | Behavior |
| --- | --- |
| Search or change a filter | Local retrieval and metadata only; no generation call |
| Ask about one selected RFP | Grounded answer within that document/version |
| Select several RFPs to compare | Explicit comparison mode with evidence/missing fields per document |
| Open a citation | PDF page/region, or HWP section/table/element plus original download |
| Ask an ambiguous requirement-code question | Choose a document rather than silently mixing them |
| Reach the paid-use cap | Explain the limit and retain free search/source browsing |

Keep the search results and answer beside their evidence on wide screens; stack them in reading order on narrow screens. Long Korean titles must wrap rather than conceal the distinction between similar projects. Format KRW with grouping and preserve “unknown” versus zero. Do not show a model-derived confidence percentage without calibration.

The shared budget indicator should show the money percentage, estimated amount used, pending reservations, and current request cost when settled. Its expandable detail can show input/output/embedding tokens, per-member totals, daily pacing, and reconciliation time. A spend number must not be mislabeled as actual remaining provider credit.

## Verification workflow

The verification entry point is for authorized team members, with experiment-changing and budget-admin actions further restricted. Pick a question or reviewed dataset row and a versioned configuration. The default button runs retrieval only. Generation and LLM judging are separate actions showing their estimate before dispatch; a comparison should not silently create multiple paid calls.

Display the evidence path end to end: ingestion status and parser warnings, source elements, chunks, tokenization, filters, BM25 and dense ranks, RRF score, reranker score, selected/expanded context, exact final input budget, answer claims, and measured usage. Individual component scores belong here, not on the consultant's main work screen.

The experiment table compares frozen run IDs with per-type recall/coverage, answer correctness, latency, and dollars. Provide failure categories, human review/correction fields, and exports. Hide sealed test questions and labels from ordinary tuning access; restrict the one final evaluation job separately. A navigation link being hidden is not authorization.

## State and request ownership

Capture the authenticated member/session identity and selected document scope when a request begins. Attach a request generation ID. If the user changes project, scope, mode, or question while an old response is arriving, drop that response from the current screen unless its generation ID still matches. Clear document-specific inputs and answers when changing scope; a failed request must not leave an old answer labeled as the new result.

Budget settlement belongs to the server and must continue even when the browser leaves. Permission to render a late answer and responsibility to record its billed usage are separate. Recheck authorization for source access and writes; never accept a client filename/path as unrestricted server file access. Escape source HTML and model text rather than treating them as executable UI markup.

Use explicit loading, completion, insufficient-evidence, clarification, conflict, budget-blocked, and technical-error states. During structured-output streaming, any provisional text is unverified; mark completion only after the server validates the final object and citation targets. Duplicate submission must not start a second paid request unnoticed.

## Reusable UI primitives

[Streamlit navigation](https://docs.streamlit.io/develop/api-reference/navigation/st.navigation) can organize separate pages. [Fragments](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment) support timed partial reruns, suitable for a small shared budget widget. A polling fragment must read the ledger only: rerunning it cannot invoke generation. Validate polling behavior during a long answer, including updates from other sessions; use a server event channel later if measurements show polling is insufficient.

Start with labeled inputs, keyboard navigation, visible focus, readable contrast, text labels alongside warning colors, and status updates that do not steal focus. Use Korean UI labels and answers. No chart or debug panel should displace the consultant's current task merely to fill whitespace.

When actual frontend implementation starts, follow the design pass in order: purpose and layout, browser-measured widths/spacing/button behavior, typography, user-selected accessible colors, then useful remaining space. No `DESIGN.md` values or visual measurements are claimed in this research turn.

## Acceptance scenarios

Test consultant and verifier roles separately, repeated project names, missing budgets/dates, both converter failures, citations for HWP and PDF, cross-document comparison, a question changed mid-request, network interruption, duplicate submission, six active sessions, and the operational cap. Verify that a stale answer never attaches to a new scope, paid generation cannot be triggered by refresh/polling, and ordinary users cannot execute verification or budget-admin operations.
