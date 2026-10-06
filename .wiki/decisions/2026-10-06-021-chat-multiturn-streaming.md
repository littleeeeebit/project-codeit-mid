---
scope: project
severity: contract
triggers: ["대화\\s*(모델|프롬프트|응답|생성)", "응답\\s*(정책|수리|스키마)", "페르소나", "말투", "gemini", "openai"]
domain: dialogue
title: "Conversational transition for asking questions: rewriting follow-up questions, streaming answers, and evidence numbering by sentence"
pr: 21
merged: 2026-10-06
branch: "chat-multiturn-streaming"
---

# Conversational transition for asking questions: rewriting follow-up questions, streaming answers, and evidence numbering by sentence

What. Asking questions operates as a conversation on top of the enabled search settings. Search settings and active execution have not been changed.

Why. Follow-up questions: Include `previous_request_id` in the request. Check if the previous conversation is from the same person, if it has ended, and if the scope is the same. Then, create an independent search query based on the conversation history with a single paid call. This call is reserved and settled in the existing gateway (step `query_rewrite`). This query is used for search and answer generation. It is displayed in the trace (`rewrite-question`, `standalone_question` of the root output) and in "Query used for search" on the screen. - Streaming: Receive the answer generation call as a stream. `GET /api/requests/{id}/stream` (SSE) sends partial answers that are currently being executed. …

Source. PR #21 · `chat-multiturn-streaming`
