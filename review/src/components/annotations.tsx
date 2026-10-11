"use client";

// Span notes on generated answers: select any part of a summary, claim, next action, conflict value or missing
// field and write what is wrong with it. The server keeps every note in an append-only store outside the run folder
// and checks the offsets against the stored answer, so the selection is sent as code-point offsets into the field.

import { createContext, Fragment, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { Annotations, SpanNote } from "@/lib/api";
import { getJson, postJson, when } from "@/lib/api";

type Pop =
  | { kind: "new"; q: string; c: string; field: string; start: number; end: number; text: string; x: number; y: number }
  | { kind: "notes"; ids: string[]; x: number; y: number };

type Ctx = {
  version: number;
  notes: SpanNote[];
  reviewedMarks: Set<string>;
  again: Map<string, string[]>;
  sampled: Map<string, { reason: string | null; author: string }>;
  send: (body: Record<string, unknown>) => Promise<void>;
  show: (p: Pop) => void;
};

const AnnotationContext = createContext<Ctx | null>(null);
const pair = (q: string, c: string) => `${q}\u0000${c}`;
const codePoints = (s: string) => Array.from(s).length;
/** A note the person wrote themselves, as opposed to an AI suggestion they approved. */
export const human = (n: SpanNote) => !n.origin;

function useAnnotations() {
  const ctx = useContext(AnnotationContext);
  if (!ctx) throw new Error("AnnotationProvider is missing");
  return ctx;
}

/** Below the given viewport rectangle, in page coordinates, kept inside the window. */
function place(rect: DOMRect) {
  return { x: Math.max(8, Math.min(rect.left, window.innerWidth - 400)) + window.scrollX, y: rect.bottom + window.scrollY + 6 };
}

export function AnnotationProvider({ run, children }: { run: string; children: React.ReactNode }) {
  const [state, setState] = useState<Annotations>({ version: -1, notes: [], reviewed: [] });
  const [pop, setPop] = useState<Pop | null>(null);
  const [error, setError] = useState("");
  // Every response is a whole snapshot; one that arrives after a newer one (the first read landing after a save)
  // is dropped instead of undoing what is already stored.
  const take = useCallback((next: Annotations) => setState((s) => (next.version >= s.version ? next : s)), []);

  useEffect(() => {
    let live = true;
    getJson<Annotations>(`/api/runs/${run}/annotations`).then((a) => live && take(a))
      .catch((e: Error) => live && setError(`메모를 읽지 못했습니다: ${e.message}`));
    return () => { live = false; };
  }, [run, take]);

  const send = useCallback(async (body: Record<string, unknown>) => {
    take(await postJson<Annotations>(`/api/runs/${run}/annotations`, JSON.stringify(body)));
  }, [run, take]);

  // A finished selection inside one answer field opens the note box under it. A selection that runs past the field
  // (a triple click, a drag into the next line) is cut at the field's edge.
  useEffect(() => {
    function selected(e: Event) {
      if ((e.target as Element | null)?.closest?.("[data-note-box]")) return;
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) return;
      const range = sel.getRangeAt(0).cloneRange();
      const host = (n: Node) => (n.nodeType === 1 ? (n as Element) : n.parentElement)?.closest<HTMLElement>("[data-anno]");
      const a = host(range.startContainer), b = host(range.endContainer);
      const field = a ?? b;
      if (!field) return;
      if (a !== field) range.setStart(field, 0);
      if (b !== field) range.setEnd(field, field.childNodes.length);
      const text = range.toString();
      if (!text.trim()) return;
      const before = document.createRange();
      before.selectNodeContents(field);
      before.setEnd(range.startContainer, range.startOffset);
      const start = codePoints(before.toString());
      const { q, c, f } = field.dataset;
      setPop({ kind: "new", q: q!, c: c!, field: f!, start, end: start + codePoints(text), text, ...place(range.getBoundingClientRect()) });
    }
    document.addEventListener("mouseup", selected);
    document.addEventListener("keyup", selected);
    return () => {
      document.removeEventListener("mouseup", selected);
      document.removeEventListener("keyup", selected);
    };
  }, []);

  const ctx = useMemo<Ctx>(() => ({
    version: state.version,
    notes: state.notes,
    reviewedMarks: new Set(state.reviewed.map((r) => pair(r.question, r.candidate))),
    again: new Map((state.rereview ?? []).map((r) => [pair(r.question, r.candidate), r.types])),
    sampled: new Map((state.sampled ?? []).map((s) => [s.question, s])),
    send,
    show: setPop,
  }), [state, send]);

  return (
    <AnnotationContext.Provider value={ctx}>
      {error && <p role="alert" className="mb-4 rounded-[10px] bg-bad-bg px-4 py-3 text-[14px] text-bad">{error}</p>}
      {children}
      {pop && <NoteBox key={JSON.stringify(pop)} pop={pop} onClose={() => setPop(null)} />}
    </AnnotationContext.Provider>
  );
}

/** One answer field, with every noted span marked; clicking a mark shows its notes. data-* lets the selection
 *  handler find the field, and the element holds nothing but the field's text so DOM offsets are text offsets. */
export function Annotatable({ q, c, field, text, className = "" }: {
  q: string; c: string; field: string; text: string; className?: string;
}) {
  const { notes, show } = useAnnotations();
  const mine = notes.filter((n) => n.question === q && n.candidate === c && n.field === field);
  const chars = Array.from(text);
  const cuts = [...new Set([0, chars.length, ...mine.flatMap((n) => [n.start, n.end])])].sort((x, y) => x - y);
  return (
    <span data-anno="" data-q={q} data-c={c} data-f={field} className={className}>
      {cuts.slice(0, -1).map((s, i) => {
        const e = cuts[i + 1];
        const piece = chars.slice(s, e).join("");
        const over = mine.filter((n) => n.start <= s && n.end >= e);
        if (!over.length) return <Fragment key={s}>{piece}</Fragment>;
        // An approved AI suggestion is marked apart (dashed, blue) from what the person found themselves.
        const look = over.some(human) ? "bg-bad-bg decoration-bad" : "bg-primary-bg decoration-primary decoration-dashed";
        return (
          <mark key={s} title={over.map((n) => (human(n) ? n.note : `AI 제안 · ${n.note}`)).join("\n")}
                onClick={(ev) => {
                  if (!window.getSelection()?.isCollapsed) return;  // the end of a new selection, not a click on a mark
                  show({ kind: "notes", ids: over.map((n) => n.id), ...place(ev.currentTarget.getBoundingClientRect()) });
                }}
                className={`cursor-pointer rounded-sm text-inherit underline decoration-2 underline-offset-4 ${look}`}>
            {piece}
          </mark>
        );
      })}
    </span>
  );
}

function NoteBox({ pop, onClose }: { pop: Pop; onClose: () => void }) {
  const { notes, send } = useAnnotations();
  const box = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const shown = pop.kind === "notes" ? notes.filter((n) => pop.ids.includes(n.id)) : [];

  useEffect(() => {
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) onClose(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [onClose]);

  useEffect(() => { if (pop.kind === "notes" && shown.length === 0) onClose(); }, [pop.kind, shown.length, onClose]);

  async function act(body: Record<string, unknown>, after: () => void) {
    setBusy(true);
    setError("");
    try {
      await send(body);
      after();
    } catch (e) {
      setError(`저장하지 못했습니다: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }
  const save = () => {
    if (pop.kind === "new") {
      act({ op: "note", question: pop.q, candidate: pop.c, field: pop.field, start: pop.start, end: pop.end,
            text: pop.text, note: draft }, () => { window.getSelection()?.removeAllRanges(); onClose(); });
    } else if (editing) {
      act({ op: "edit", target: editing, note: draft }, () => setEditing(null));
    }
  };
  const onKey = (e: React.KeyboardEvent) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && draft.trim()) save(); };
  const textarea = (
    <textarea value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKey} rows={3} autoFocus
              placeholder="무엇이 틀렸는지 내 말로" aria-label="선택한 부분에 대한 메모"
              className="mt-2 block w-full rounded-[10px] border border-line px-3 py-2 text-[15px] focus:border-primary" />
  );
  const buttons = (label: string) => (
    <div className="mt-2 flex items-center gap-2">
      <button type="button" disabled={busy || !draft.trim()} onClick={save}
              className="h-9 rounded-[10px] bg-primary px-4 text-[14px] font-semibold text-white disabled:bg-surface disabled:text-muted">
        {label}
      </button>
      <span className="text-[13px] text-muted">Ctrl+Enter 저장 · Esc 닫기</span>
    </div>
  );

  return (
    <div ref={box} data-note-box="" role="dialog" aria-label={pop.kind === "new" ? "새 메모" : "메모"}
         style={{ left: pop.x, top: pop.y }}
         className="absolute z-30 w-[380px] rounded-[10px] border border-line bg-white p-3 shadow-lg">
      {pop.kind === "new" ? (
        <>
          <blockquote className="line-clamp-3 border-l-[3px] border-bad pl-2 text-[14px] text-muted">{pop.text}</blockquote>
          {textarea}
          {buttons("메모 저장")}
        </>
      ) : (
        <ul className="flex flex-col divide-y divide-line">
          {shown.map((n) => (
            <li key={n.id} className="py-2 first:pt-0 last:pb-0">
              <blockquote className="line-clamp-2 border-l-[3px] border-bad pl-2 text-[13px] text-muted">{n.text}</blockquote>
              {editing === n.id ? <>{textarea}{buttons("고친 메모 저장")}</> : (
                <>
                  <p className="mt-1 whitespace-pre-line break-words text-[15px]">{n.note}</p>
                  <div className="mt-1 flex items-center gap-3 text-[13px] text-muted">
                    <span>
                      {!human(n) && <b className="text-primary">AI 제안 · {n.approved_by} 승인 · </b>}
                      {n.edited_by ?? n.author} · {when(n.edited_at ?? n.at)}{n.edited_at && " 고침"}
                    </span>
                    <button type="button" className="ml-auto font-medium text-primary hover:underline"
                            onClick={() => { setEditing(n.id); setDraft(n.note); }}>고치기</button>
                    <button type="button" disabled={busy} className="font-medium text-bad hover:underline"
                            onClick={() => window.confirm("이 메모를 지울까요? 기록에는 지운 사실이 남습니다.")
                              && act({ op: "delete", target: n.id }, () => undefined)}>지우기</button>
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {error && <p role="alert" className="mt-2 text-[13px] text-bad">{error}</p>}
    </div>
  );
}

/** The note store's record count as last seen: it grows with every note, edit, deletion or reviewed mark, so
 *  anything derived from the store on the server is read again when it changes. */
export function useAnnotationVersion() {
  return useAnnotations().version;
}

/** Reviewed: marked '검토함 · 문제 없음', or carrying at least one note the person wrote. An approved AI suggestion
 *  does not make an answer reviewed: the person read that span, not the answer. */
export function useReview() {
  const { notes, reviewedMarks, again, send } = useAnnotations();
  return useMemo(() => {
    const noted = new Map<string, number>();
    const suggested = new Map<string, number>();
    for (const n of notes) {
      const into = human(n) ? noted : suggested;
      into.set(pair(n.question, n.candidate), (into.get(pair(n.question, n.candidate)) ?? 0) + 1);
    }
    return {
      noteCount: (q: string, c: string) => noted.get(pair(q, c)) ?? 0,
      suggestedCount: (q: string, c: string) => suggested.get(pair(q, c)) ?? 0,
      marked: (q: string, c: string) => reviewedMarks.has(pair(q, c)),
      reviewed: (q: string, c: string) => reviewedMarks.has(pair(q, c)) || noted.has(pair(q, c)),
      /** Types accepted after this answer was marked '문제 없음': it was read without them. */
      again: (q: string, c: string) => again.get(pair(q, c)) ?? [],
      mark: (q: string, c: string, value: boolean) => send({ op: "reviewed", question: q, candidate: c, value }),
    };
  }, [notes, reviewedMarks, again, send]);
}

/** Traffic runs: requests a person added to the drawn sample, and adding or taking one back. */
export function useSample() {
  const { sampled, send } = useAnnotations();
  return useMemo(() => ({
    added: sampled,
    set: (q: string, value: boolean, reason = "") => send({ op: "sample", question: q, value, reason }),
  }), [sampled, send]);
}

export function ReviewedMark({ q, c }: { q: string; c: string }) {
  const review = useReview();
  const [error, setError] = useState("");
  const n = review.noteCount(q, c);
  const ai = review.suggestedCount(q, c);
  const again = review.again(q, c);
  const save = (value: boolean) => review.mark(q, c, value).then(() => setError(""),
    (err: Error) => setError(`저장하지 못했습니다: ${err.message}`));
  const suggestedLine = ai > 0 && <p className="mt-1 text-[13px] font-semibold text-primary">AI 제안 승인 {ai}개</p>;
  if (n > 0) return <><p className="mt-3 text-[14px] font-semibold text-bad">메모 {n}개 · 검토함</p>{suggestedLine}</>;
  return (
    <div className="mt-3">
      {again.length > 0 && (
        <div className="mb-2 rounded-[10px] bg-warn-bg px-3 py-2">
          <p className="text-[14px] font-semibold text-warn">기준 추가 후 다시 보기 ({again.join(", ")})</p>
          <p className="text-[13px] text-warn">이 답변을 &apos;문제 없음&apos;으로 표시한 뒤에 생긴 유형입니다. 그 기준으로 다시 읽고,
            문제가 있으면 메모를 다세요.</p>
          <button type="button" onClick={() => save(true)}
                  className="mt-1.5 h-8 rounded-[10px] bg-white px-3 text-[13px] font-semibold text-ink shadow-sm">
            다시 봤음 · 여전히 문제 없음
          </button>
        </div>
      )}
      <label className="inline-flex cursor-pointer items-center gap-2 text-[14px] font-medium">
        <input type="checkbox" checked={review.marked(q, c)} className="size-4 accent-[var(--color-ok)]"
               onChange={(e) => save(e.target.checked)} />
        <span className={review.marked(q, c) ? "text-ok" : ""}>검토함 · 문제 없음</span>
      </label>
      {suggestedLine}
      {error && <p role="alert" className="text-[13px] text-bad">{error}</p>}
    </div>
  );
}
