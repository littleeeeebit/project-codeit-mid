"use client";

// Who comes here: the person who wrote notes on answers. What for: turn those notes into named failure types they
// agree with. The coding agent groups the notes from an exported bundle (this app never calls a model); each type it
// proposes stands on the person's own notes, and the person accepts, renames, merges, splits or rejects it here.
// For an accepted type the agent can also search every stored answer for more cases; the person approves or rejects
// each suggestion in the type's queue, and approved ones are counted apart from the cases the person found.

import { useCallback, useEffect, useState } from "react";
import type { Exported, FailureType, SuggestExported, Suggestion, SuggestionQueue, Taxonomy, TaxNote } from "@/lib/api";
import { getJson, postJson, when } from "@/lib/api";
import { Badge } from "./ui";

type Decide = (body: Record<string, unknown>) => Promise<void>;
const keyOf = (t: FailureType) => (t.ref ?? t.id)!;
const NOT_ALL = "AI 제안은 모든 사례를 찾지 못합니다. 승인한 사례는 사람이 찾은 사례와 따로 세고, 검토한 답변 대비 비율에 넣지 않습니다.";
/** Which part of the answer a span lies in, as the answer shows it. */
const part = (field: string) => {
  const [kind, i] = field.split(".");
  return ({ summary: "요약", next_action: "다음 조치", claim: `주장 ${Number(i) + 1}`, conflict: "근거 충돌 값",
            missing: "빠진 항목" } as Record<string, string>)[kind] ?? field;
};
const OP: Record<string, string> = { accept: "수락", rename: "이름 바꿈", merge: "합침", split: "나눔", reject: "버림" };

export function TaxonomyView() {
  const [state, setState] = useState<Taxonomy | null>(null);
  const [error, setError] = useState("");
  const [picked, setPicked] = useState<string[]>([]);

  const load = useCallback(() => {
    getJson<Taxonomy>("/api/taxonomy").then(setState).catch((e: Error) => setError(`유형을 읽지 못했습니다: ${e.message}`));
  }, []);
  useEffect(load, [load]);

  const decide: Decide = async (body) => {
    setError("");
    try {
      setState(await postJson<Taxonomy>("/api/taxonomy/decide", JSON.stringify(body)));
      setPicked([]);
    } catch (e) {
      setError(`저장하지 못했습니다: ${(e as Error).message}`);
      throw e;
    }
  };
  const pick = (k: string, on: boolean) => setPicked((p) => (on ? [...p, k] : p.filter((x) => x !== k)));

  if (!state) return error ? <p role="alert" className="text-bad">{error}</p> : <p className="text-muted">불러오는 중…</p>;
  const all = [...state.pending, ...state.accepted];
  return (
    <div className="max-w-[1080px] pb-28">
      <h1 className="text-[24px] font-bold">실패 유형</h1>
      <p className="mt-1 max-w-[760px] text-[14px] text-muted">
        답변에 단 메모를 코딩 에이전트가 묶어 제안하면, 유형마다 받아들이거나 이름을 고치거나 합치고 나누고 버립니다.
        에이전트는 메모를 묶기만 하고, 메모 없는 유형은 제안할 수 없습니다. 수락한 유형은 에이전트에게 저장된 모든 답변에서
        비슷한 사례를 찾게 하고, 제안을 하나씩 승인하거나 거절합니다.
      </p>
      <p className="mt-4 text-[18px] font-semibold tabular-nums">
        수락된 유형 {state.accepted.length}개 <span className="text-muted">· v{state.version}</span>
        {state.pending.length > 0 && <span className="text-warn"> · 결정 대기 {state.pending.length}개</span>}
      </p>
      <ExportPrompt unassigned={state.unassigned} />
      {error && <p role="alert" className="mt-4 rounded-[10px] bg-bad-bg px-4 py-3 text-[14px] text-bad">{error}</p>}

      {state.pending.length > 0 && (
        <section aria-labelledby="pending-title" className="mt-10">
          <h2 id="pending-title" className="text-[18px] font-bold">결정 대기</h2>
          <p className="text-[14px] text-muted">에이전트가 제안한 묶음입니다. 아래 인용은 모두 내가 단 메모입니다.</p>
          <ol className="divide-y divide-line">
            {state.pending.map((t) => (
              <TypeRow key={keyOf(t)} t={t} pending picked={picked.includes(keyOf(t))} onPick={pick} decide={decide} />
            ))}
          </ol>
        </section>
      )}

      <section aria-labelledby="accepted-title" className="mt-10">
        <h2 id="accepted-title" className="text-[18px] font-bold">수락된 유형</h2>
        {state.accepted.length === 0 ? (
          <p className="mt-2 text-[15px] text-muted">아직 없습니다. 제안된 유형을 수락하면 여기에 쌓이고, 바뀔 때마다 버전이 올라갑니다.</p>
        ) : (
          <ol className="divide-y divide-line">
            {state.accepted.map((t) => (
              <TypeRow key={keyOf(t)} t={t} pending={false} picked={picked.includes(keyOf(t))} onPick={pick} decide={decide}
                       reload={load} />
            ))}
          </ol>
        )}
      </section>

      {state.history.length > 0 && (
        <details className="mt-10">
          <summary className="cursor-pointer text-[15px] font-semibold">결정 기록 {state.history.length}건</summary>
          <ol className="mt-2 text-[14px]">
            {state.history.map((h, i) => (
              <li key={i} className="py-1 tabular-nums">
                <span className="text-muted">{when(h.at)} · v{h.version} · {h.author}</span>{" "}
                <b>{OP[h.op] ?? h.op}</b> {h.names.join(", ")}{h.reason && <span className="text-muted"> — {h.reason}</span>}
              </li>
            ))}
          </ol>
        </details>
      )}

      {picked.length >= 2 && (
        <MergeBar key={picked.join()} types={all.filter((t) => picked.includes(keyOf(t)))}
                  onMerge={(name, definition) => decide({ op: "merge", refs: picked, name, definition })}
                  onCancel={() => setPicked([])} />
      )}
    </div>
  );
}

/** Writes the bundle of notes in no type yet and copies the sentence that sends the coding agent to it. */
function ExportPrompt({ unassigned }: { unassigned: number }) {
  const [out, setOut] = useState<Exported | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  async function run() {
    setError("");
    setCopied(false);
    try {
      const made = await postJson<Exported>("/api/taxonomy/export", "{}");
      setOut(made);
      await navigator.clipboard.writeText(made.prompt).then(() => setCopied(true), () => undefined);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="mt-4">
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={run} disabled={unassigned === 0}
                className="h-10 rounded-[10px] bg-primary px-4 text-[14px] font-semibold text-white disabled:bg-surface disabled:text-muted">
          묶기 프롬프트 복사
        </button>
        <span className="text-[14px] text-muted">
          {unassigned > 0 ? `어느 유형에도 없는 메모 ${unassigned}개를 묶음 파일로 쓰고, 에이전트에게 줄 문장을 복사합니다`
            : "묶을 메모가 없습니다. 답변에 메모를 달면 여기서 묶을 수 있습니다"}
        </span>
      </div>
      {out && (
        <div className="mt-3 max-w-[860px] rounded-[10px] bg-ok-bg px-4 py-3">
          <div className="text-[15px] font-semibold text-ok">
            {copied ? "복사했습니다" : "아래 문장을 복사하세요"} · 메모 {out.notes}개
          </div>
          <p className="mt-1 select-all break-words text-[14px]">{out.prompt}</p>
          <p className="mt-1 text-[13px] text-muted">에이전트가 제안을 넣으면 이 화면을 새로 고치세요.</p>
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-[14px] text-bad">{error}</p>}
    </div>
  );
}

function TypeRow({ t, pending, picked, onPick, decide, reload }: {
  t: FailureType; pending: boolean; picked: boolean; onPick: (k: string, on: boolean) => void; decide: Decide;
  reload?: () => void;
}) {
  const [mode, setMode] = useState<"" | "edit" | "split">("");
  const [queue, setQueue] = useState(false);
  const ref = keyOf(t);
  const quiet = "h-9 rounded-[10px] px-3 text-[14px] font-semibold hover:bg-surface";
  return (
    <li className="flex gap-4 py-7">
      <input type="checkbox" checked={picked} onChange={(e) => onPick(ref, e.target.checked)}
             aria-label={`${t.name} 합치기에 넣기`} title="두 개 이상 고르면 합칠 수 있습니다" className="mt-2 size-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h3 className="text-[20px] font-bold">{t.name}</h3>
          <span className="text-[14px] tabular-nums text-muted">
            사람이 찾은 답변 <b className="text-ink">{t.answers}</b>개
            {t.ai_answers > 0 && <> · <span className="text-primary">AI 제안 승인 <b>{t.ai_answers}</b>개</span></>}
            {" "}· 메모 {t.notes.length}개{t.deleted > 0 && ` · 지워진 메모 ${t.deleted}개`}
          </span>
          {!pending && t.answers < 3 && <Badge tone="warn">사례 3개 미만</Badge>}
        </div>
        <p className="mt-1 max-w-[860px] text-[16px]">{t.definition}</p>

        {mode === "edit" && (
          <NameForm name={t.name} definition={t.definition} label={pending ? "고친 이름으로 수락" : "이름 바꾸기"}
                    onCancel={() => setMode("")}
                    onSubmit={(name, definition) => decide({ op: pending ? "accept" : "rename", ref, name, definition })} />
        )}
        {mode === "split" && <SplitForm t={t} onCancel={() => setMode("")}
                                        onSubmit={(parts) => decide({ op: "split", ref, parts })} />}

        {mode !== "split" && <Spans notes={t.notes} />}

        {mode === "" && (
          <div className="mt-3 flex flex-wrap items-center gap-1">
            {pending && (
              <button type="button" onClick={() => decide({ op: "accept", ref }).catch(() => undefined)}
                      className="h-9 rounded-[10px] bg-primary px-4 text-[14px] font-semibold text-white">수락</button>
            )}
            <button type="button" className={quiet} onClick={() => setMode("edit")}>{pending ? "이름 고쳐 수락" : "이름 바꾸기"}</button>
            {t.notes.length > 1 && <button type="button" className={quiet} onClick={() => setMode("split")}>나누기</button>}
            <button type="button" className={`${quiet} text-bad`} onClick={() => {
              const reason = window.prompt(pending ? "이 제안을 버리는 이유 (비워도 됩니다)" : "이 유형을 버리는 이유 (비워도 됩니다). 메모는 다시 묶을 수 있게 남습니다.");
              if (reason !== null) decide({ op: "reject", ref, reason }).catch(() => undefined);
            }}>버리기</button>
            {!pending && (t.suggested ?? 0) > 0 && (
              <button type="button" aria-expanded={queue} onClick={() => setQueue((q) => !q)}
                      className="h-9 rounded-[10px] bg-primary-bg px-3 text-[14px] font-semibold text-primary">
                AI 제안 {t.suggested}개 검토
              </button>
            )}
          </div>
        )}
        {mode === "" && !pending && <SuggestPrompt id={ref} />}
        {queue && !pending && <Queue id={ref} onChanged={() => reload?.()} />}
      </div>
    </li>
  );
}

/** Writes one type's search bundle (definition, approved and rejected examples, every stored answer) and copies the
 *  sentence that sends the coding agent to it. */
function SuggestPrompt({ id }: { id: string }) {
  const [out, setOut] = useState<SuggestExported | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  async function run() {
    setError("");
    setCopied(false);
    try {
      const made = await postJson<SuggestExported>("/api/taxonomy/suggest-export", JSON.stringify({ type: id }));
      setOut(made);
      await navigator.clipboard.writeText(made.prompt).then(() => setCopied(true), () => undefined);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="mt-3 max-w-[860px]">
      <button type="button" onClick={run} className="text-[14px] font-semibold text-primary hover:underline">
        비슷한 사례 찾기 프롬프트 복사
      </button>
      {out && (
        <div className="mt-2 rounded-[10px] bg-ok-bg px-4 py-3">
          <div className="text-[15px] font-semibold text-ok">
            {copied ? "복사했습니다" : "아래 문장을 복사하세요"} · 저장된 답변 {out.answers}개를 찾게 합니다
          </div>
          <p className="mt-1 select-all break-words text-[14px]">{out.prompt}</p>
          <p className="mt-1 text-[13px] text-muted">에이전트가 제안을 넣으면 이 화면을 새로 고치세요. {NOT_ALL}</p>
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-[14px] text-bad">{error}</p>}
    </div>
  );
}

/** The type's waiting suggestions, each highlighted in its stored answer with the agent's reason. Approving files
 *  the span as a note under the type; rejecting keeps it, with the reason, for the agent's next search. */
function Queue({ id, onChanged }: { id: string; onChanged: () => void }) {
  const [q, setQ] = useState<SuggestionQueue | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    getJson<SuggestionQueue>(`/api/taxonomy/suggestions/${id}`).then(setQ).catch((e: Error) => setError(e.message));
  }, [id]);
  async function decide(body: Record<string, unknown>) {
    setError("");
    try {
      setQ(await postJson<SuggestionQueue>("/api/taxonomy/suggestions/decide", JSON.stringify(body)));
      onChanged();
    } catch (e) {
      setError(`저장하지 못했습니다: ${(e as Error).message}`);
    }
  }
  if (!q) return error ? <p role="alert" className="mt-3 text-bad">{error}</p> : <p className="mt-3 text-muted">불러오는 중…</p>;
  return (
    <section aria-label={`${q.type.name} AI 제안`} className="mt-4 max-w-[900px] rounded-[10px] border border-line px-5 py-4">
      <h4 className="text-[16px] font-bold">AI 제안 {q.pending.length}개 남음
        <span className="ml-2 text-[14px] font-normal text-muted">승인 {q.approved} · 거절 {q.rejected.length}</span>
      </h4>
      <p className="text-[13px] text-muted">{NOT_ALL}</p>
      {error && <p role="alert" className="mt-2 text-[14px] text-bad">{error}</p>}
      {q.pending.length === 0 && <p className="mt-3 text-[15px] text-muted">남은 제안이 없습니다.</p>}
      <ol className="mt-2 divide-y divide-line">
        {q.pending.map((s) => <SuggestionItem key={s.id} s={s} decide={decide} />)}
      </ol>
      {q.rejected.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-[14px] font-semibold">거절한 제안 {q.rejected.length}개 · 다음에 에이전트에게 함께 보냅니다</summary>
          <ul className="mt-1 text-[14px]">
            {q.rejected.map((s) => (
              <li key={s.id} className="py-1">“{s.text}”<span className="text-muted"> — {s.reason_rejected ?? "이유 없음"} · {s.decided_by}</span></li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function SuggestionItem({ s, decide }: { s: Suggestion; decide: (body: Record<string, unknown>) => Promise<void> }) {
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const chars = Array.from(s.field_text ?? "");  // offsets count code points, as Python does
  return (
    <li className="py-4">
      <p className="text-[13px] text-muted">{s.question_text ?? s.question} · {s.candidate_label} · {part(s.field)}</p>
      <p className="mt-1 whitespace-pre-line break-words text-[16px] leading-relaxed">
        {s.field_text != null ? (
          <>{chars.slice(0, s.start).join("")}
            <mark className="rounded-sm bg-primary-bg px-0.5 text-inherit underline decoration-primary decoration-dashed decoration-2 underline-offset-4">
              {chars.slice(s.start, s.end).join("")}
            </mark>{chars.slice(s.end).join("")}</>
        ) : <mark className="bg-primary-bg">{s.text}</mark>}
      </p>
      <p className="mt-1 text-[15px]"><span className="font-semibold text-primary">AI</span> {s.reason}</p>
      {rejecting ? (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <input value={reason} onChange={(e) => setReason(e.target.value)} autoFocus placeholder="거절 이유 (비워도 됩니다)"
                 aria-label="거절 이유" className="h-9 w-[420px] max-w-full rounded-[10px] border border-line px-3 text-[14px] focus:border-primary" />
          <button type="button" onClick={() => decide({ op: "reject", suggestion: s.id, reason })}
                  className="h-9 rounded-[10px] bg-bad px-4 text-[14px] font-semibold text-white">거절</button>
          <button type="button" onClick={() => setRejecting(false)} className="h-9 rounded-[10px] px-3 text-[14px] font-semibold hover:bg-surface">취소</button>
        </div>
      ) : (
        <div className="mt-2 flex items-center gap-1">
          <button type="button" onClick={() => decide({ op: "approve", suggestion: s.id })}
                  className="h-9 rounded-[10px] bg-primary px-4 text-[14px] font-semibold text-white">승인</button>
          <button type="button" onClick={() => setRejecting(true)}
                  className="h-9 rounded-[10px] px-3 text-[14px] font-semibold text-bad hover:bg-surface">거절…</button>
        </div>
      )}
    </li>
  );
}

/** The quoted spans behind a type: what the person selected in the answer and what they wrote about it. */
function Spans({ notes }: { notes: TaxNote[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? notes : notes.slice(0, 4);
  return (
    <ul className="mt-3 flex max-w-[860px] flex-col gap-3">
      {shown.map((n) => (
        <li key={n.id}>
          <blockquote className={`border-l-[3px] pl-3 text-[15px] ${n.origin ? "border-primary" : "border-bad"}`}>“{n.text}”</blockquote>
          <p className="pl-3.5 text-[15px] font-medium">{n.note}</p>
          <p className="pl-3.5 text-[13px] text-muted">
            {n.origin && <b className="text-primary">AI 제안 · {n.approved_by} 승인 · </b>}
            {n.question_text ?? n.question} · {n.candidate_label} · {n.edited_by ?? n.author}
          </p>
        </li>
      ))}
      {notes.length > shown.length && (
        <li><button type="button" onClick={() => setAll(true)} className="text-[14px] font-medium text-primary hover:underline">
          메모 {notes.length - shown.length}개 더 보기
        </button></li>
      )}
    </ul>
  );
}

function Fields({ name, definition, setName, setDefinition }: {
  name: string; definition: string; setName: (v: string) => void; setDefinition: (v: string) => void;
}) {
  const input = "mt-1 block w-full rounded-[10px] border border-line px-3 py-2 text-[15px] focus:border-primary";
  return (
    <div className="grid gap-2 sm:grid-cols-[minmax(180px,1fr)_2fr]">
      <label className="block text-[13px] text-muted">이름 (한국어, 한 줄)
        <input value={name} onChange={(e) => setName(e.target.value)} className={`${input} text-ink`} />
      </label>
      <label className="block text-[13px] text-muted">정의 (한 문장)
        <input value={definition} onChange={(e) => setDefinition(e.target.value)} className={`${input} text-ink`} />
      </label>
    </div>
  );
}

function NameForm({ name: n0, definition: d0, label, onSubmit, onCancel }: {
  name: string; definition: string; label: string; onSubmit: (n: string, d: string) => Promise<void>; onCancel: () => void;
}) {
  const [name, setName] = useState(n0);
  const [definition, setDefinition] = useState(d0);
  return (
    <div className="mt-3 max-w-[860px]">
      <Fields name={name} definition={definition} setName={setName} setDefinition={setDefinition} />
      <div className="mt-2 flex gap-2">
        <button type="button" disabled={!name.trim() || !definition.trim()}
                onClick={() => onSubmit(name, definition).catch(() => undefined)}
                className="h-9 rounded-[10px] bg-primary px-4 text-[14px] font-semibold text-white disabled:bg-surface disabled:text-muted">{label}</button>
        <button type="button" onClick={onCancel} className="h-9 rounded-[10px] px-3 text-[14px] font-semibold hover:bg-surface">취소</button>
      </div>
    </div>
  );
}

/** Each note goes to exactly one part; every part needs a name, a definition and at least one note. */
function SplitForm({ t, onSubmit, onCancel }: {
  t: FailureType; onSubmit: (parts: { name: string; definition: string; notes: string[] }[]) => Promise<void>; onCancel: () => void;
}) {
  const [parts, setParts] = useState([{ name: t.name, definition: t.definition }, { name: "", definition: "" }]);
  const [where, setWhere] = useState<Record<string, number>>(Object.fromEntries(t.notes.map((n) => [n.id, 0])));
  const built = parts.map((p, i) => ({ ...p, notes: t.notes.filter((n) => where[n.id] === i).map((n) => n.id) }));
  const ready = built.every((p) => p.name.trim() && p.definition.trim() && p.notes.length > 0);
  const set = (i: number, patch: Partial<{ name: string; definition: string }>) =>
    setParts((ps) => ps.map((p, j) => (j === i ? { ...p, ...patch } : p)));
  return (
    <div className="mt-4 max-w-[960px]">
      <ol className="flex flex-col gap-3">
        {parts.map((p, i) => (
          <li key={i}>
            <div className="text-[14px] font-semibold">부분 {i + 1} · 메모 {built[i].notes.length}개</div>
            <Fields name={p.name} definition={p.definition} setName={(v) => set(i, { name: v })}
                    setDefinition={(v) => set(i, { definition: v })} />
          </li>
        ))}
      </ol>
      <button type="button" onClick={() => setParts((ps) => [...ps, { name: "", definition: "" }])}
              className="mt-2 text-[14px] font-medium text-primary hover:underline">부분 추가</button>
      <ul className="mt-4 divide-y divide-line">
        {t.notes.map((n) => (
          <li key={n.id} className="flex items-start gap-4 py-2">
            <select value={where[n.id]} onChange={(e) => setWhere((w) => ({ ...w, [n.id]: Number(e.target.value) }))}
                    aria-label={`“${n.text}” 메모를 넣을 부분`}
                    className="h-9 shrink-0 rounded-[10px] border border-line bg-white px-2 text-[14px]">
              {parts.map((p, i) => <option key={i} value={i}>부분 {i + 1}{p.name && ` · ${p.name}`}</option>)}
            </select>
            <div className="min-w-0">
              <blockquote className="border-l-[3px] border-bad pl-3 text-[15px]">“{n.text}”</blockquote>
              <p className="pl-3.5 text-[14px]">{n.note}</p>
            </div>
          </li>
        ))}
      </ul>
      <div className="mt-3 flex items-center gap-2">
        <button type="button" disabled={!ready} onClick={() => onSubmit(built).catch(() => undefined)}
                className="h-9 rounded-[10px] bg-primary px-4 text-[14px] font-semibold text-white disabled:bg-surface disabled:text-muted">
          {parts.length}개로 나눠 수락
        </button>
        <button type="button" onClick={onCancel} className="h-9 rounded-[10px] px-3 text-[14px] font-semibold hover:bg-surface">취소</button>
        {!ready && <span className="text-[13px] text-muted">부분마다 이름, 정의, 메모 하나 이상이 필요합니다</span>}
      </div>
    </div>
  );
}

function MergeBar({ types, onMerge, onCancel }: {
  types: FailureType[]; onMerge: (name: string, definition: string) => Promise<void>; onCancel: () => void;
}) {
  const [name, setName] = useState(types[0]?.name ?? "");
  const [definition, setDefinition] = useState(types[0]?.definition ?? "");
  return (
    <div role="region" aria-label="유형 합치기" className="fixed bottom-0 left-[280px] right-0 z-20 border-t border-line bg-white/95 px-10 py-4 shadow-[0_-4px_16px_rgba(0,0,0,0.06)] backdrop-blur">
      <div className="max-w-[1080px]">
        <div className="text-[15px] font-semibold">{types.map((t) => t.name).join(" + ")} → 하나로 합치기</div>
        <div className="mt-2"><Fields name={name} definition={definition} setName={setName} setDefinition={setDefinition} /></div>
        <div className="mt-2 flex gap-2">
          <button type="button" disabled={!name.trim() || !definition.trim()}
                  onClick={() => onMerge(name, definition).catch(() => undefined)}
                  className="h-9 rounded-[10px] bg-primary px-4 text-[14px] font-semibold text-white disabled:bg-surface disabled:text-muted">
            {types.length}개 합쳐 수락
          </button>
          <button type="button" onClick={onCancel} className="h-9 rounded-[10px] px-3 text-[14px] font-semibold hover:bg-surface">선택 풀기</button>
        </div>
      </div>
    </div>
  );
}
