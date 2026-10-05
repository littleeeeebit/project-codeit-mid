"use client";

import { useEffect, useRef, useState } from "react";
import { memberHeaders } from "./api";

/** What a streaming answer has written so far (service.answer_progress): unvalidated, shown as provisional. */
export type Streamed = {
  summary: string;
  summary_evidence_ids: string[];
  claims: { text: string; kind: string; doc_id: string; evidence_ids: string[] }[];
};

/**
 * Follows `/api/requests/{id}/stream` for one owned request while it runs. EventSource cannot send the
 * submitting member's header, so this reads the event stream through fetch. `onDone` fires when the server
 * reports the request finished, so the caller can fetch the validated outcome at once. A stream for a request the
 * screen has left is aborted and its late events are dropped, as usePoll drops late polls.
 */
export function useAnswerStream(owned: { request_id: string; generation_id: string; member: string } | null,
                                onDone: () => void): Streamed | null {
  const [state, setState] = useState<{ key: string; data: Streamed } | null>(null);
  const done = useRef(onDone);
  useEffect(() => {
    done.current = onDone;
  });
  const key = owned ? `${owned.request_id}/${owned.generation_id}` : null;

  useEffect(() => {
    if (!owned || !key) return;
    const abort = new AbortController();
    (async () => {
      const url = `/api/requests/${encodeURIComponent(owned.request_id)}/stream?generation_id=${encodeURIComponent(owned.generation_id)}`;
      const response = await fetch(url, { headers: memberHeaders(owned.member), signal: abort.signal });
      if (!response.ok || !response.body) return;
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done: ended } = await reader.read();
        if (ended) return;
        buffer += value;
        let end;
        while ((end = buffer.indexOf("\n\n")) >= 0) {
          const event = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          if (event.startsWith("event: done")) {
            done.current();
            return;
          }
          if (event.startsWith("data: ")) setState({ key, data: JSON.parse(event.slice(6)) as Streamed });
        }
      }
    })().catch(() => {});  // the status poll still delivers the outcome when the stream drops
    return () => abort.abort();
    // `owned` is identified by `key`; its member is fixed at submission
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return state && state.key === key ? state.data : null;
}
