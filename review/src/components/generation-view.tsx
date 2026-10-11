"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import type { Answer, Candidate, Claim, Evidence, GenerationQuestion, GenerationView as View } from "@/lib/api";
import { usd } from "@/lib/api";
import { FACT_STATE, FIELD, label, TYPE, VALIDATION } from "@/lib/labels";
import { Badge, LedgerLine, Metric, Metrics, NotedList, Segmented, Summaries, columns, useShown } from "./ui";
import { NoteField } from "./note-field";
import { Annotatable, AnnotationProvider, ReviewedMark, useReview } from "./annotations";

const OUTCOME: Record<string, string> = {
  answered: "답변", insufficient_evidence: "근거 부족", clarification_required: "되묻기",
  conflicting_evidence: "근거 충돌", technical_error: "답변 거부됨", ingestion_unavailable: "문서 없음",
  budget_blocked: "예산 거절",
};
const VERDICT: Record<string, [string, string]> = {
  correct: ["정답", "text-ok"], wrong_value: ["값 틀림", "text-bad"], contested: ["상충", "text-warn"],
  incomplete_qualifier: ["조건 빠짐", "text-warn"], missing: ["누락", "text-bad"], needs_review: ["검토 필요", "text-muted"],
};

type Notes = { notes: Record<string, string>; setNote: (key: string, note: string) => void };

export function GenerationView(props: Notes & { view: View; decide: React.ReactNode }) {
  return (
    <AnnotationProvider run={props.view.run_id}>
      <Answers {...props} />
    </AnnotationProvider>
  );
}

/** Every answer of the run, (question, candidate), in reading order. */
const answersOf = (questions: GenerationQuestion[], candidates: Candidate[]) =>
  questions.flatMap((q) => candidates.filter((c) => q.candidates[c.id]).map((c) => ({ q, c })));

function Answers({ view, notes, setNote, decide }: Notes & { view: View; decide: React.ReactNode }) {
  const [filter, setFilter] = useState<"differs" | "all">("differs");
  const [mode, setMode] = useState<"compare" | "read">("compare");
  const [at, setAt] = useState(0);
  const { shown: cands, all, picker } = useShown(view.candidates, 2);  // answers are long: two columns at most
  const review = useReview();
  const differing = view.questions.filter((q) => q.differs).length;
  const shown = filter === "all" ? view.questions : view.questions.filter((q) => q.differs);
  const reading = answersOf(shown, view.candidates);
  const everyAnswer = answersOf(view.questions, view.candidates);
  const done = everyAnswer.filter(({ q, c }) => review.reviewed(q.key, c.id)).length;
  const read = (q: GenerationQuestion, c: Candidate) => {
    const i = reading.findIndex((r) => r.q.key === q.key && r.c.id === c.id);
    if (i >= 0) { setAt(i); setMode("read"); }
  };
  return (
    <>
      <Summaries candidates={view.candidates} summary={view.summary} note={<>
        모든 후보가 gpt-5-mini로 같은 개발 질문 {view.dataset.rows}개에 한 번씩 답했습니다
        {view.dataset.skipped > 0 && ` (검토되지 않았거나 근거가 맞지 않는 ${view.dataset.skipped}개 제외)`}. 검색은 서빙 중인
        설정({view.activation.run_id ?? "기본"} · {view.activation.mode})으로 같고, 후보의 코드와 review-variant.json만
        다릅니다. 비용은 공유 원장의 정산 금액입니다.
      </>}>
        {(s, c) => <Summary s={s} c={c} reviewed={everyAnswer.filter((r) => r.c.id === c.id)} />}
      </Summaries>

      <section aria-label="질문별 답변" className="mt-10">
        <div className="sticky top-0 z-10 -mx-2 flex flex-wrap items-center gap-3 bg-white/95 px-2 py-3 backdrop-blur">
          <h2 className="mr-2 text-[18px] font-bold">질문별 답변</h2>
          <Segmented value={mode} onChange={setMode} options={[["compare", "나란히 비교"], ["read", "한 답변씩 읽기"]]} />
          <Segmented value={filter} onChange={(f) => { setFilter(f); setAt(0); }} options={[
            ["differs", `결과가 다른 질문 ${differing}`], ["all", `전체 ${view.questions.length}`],
          ]} />
          {mode === "compare" && picker}
          <span className="text-[14px] tabular-nums text-muted" title="'검토함 · 문제 없음'으로 표시했거나 메모를 단 답변">
            검토한 답변 <span className="font-bold text-ink">{done}</span> / {everyAnswer.length}
          </span>
          {decide}
        </div>
        {shown.length === 0 && (
          <p className="py-10 text-center text-muted">결과가 다른 질문이 없습니다. &apos;전체&apos;에서 답변을 봅니다.</p>
        )}
        {mode === "read" && reading.length > 0 ? (
          <ReadingView key={`${filter}`} items={reading} at={Math.min(at, reading.length - 1)} setAt={setAt}
                       notes={notes} setNote={setNote} />
        ) : (
          <NotedList items={shown} keyOf={(q) => q.key} notes={notes} setNote={setNote} row={(q, note, set) => (
            <QuestionRow q={q} candidates={cands} all={all} note={note} setNote={set} read={read} />
          )} />
        )}
      </section>
    </>
  );
}

function Summary({ s, c, reviewed }: { s: View["summary"][string]; c: Candidate; reviewed: { q: GenerationQuestion }[] }) {
  const failures = Object.entries(s.validation_failures);
  const review = useReview();
  const done = reviewed.filter(({ q }) => review.reviewed(q.key, c.id)).length;
  const noted = reviewed.filter(({ q }) => review.noteCount(q.key, c.id) > 0).length;
  return (
    <>
      <Metrics>
        <Metric lead value={`${s.required_correct}/${s.required}`} label="필수 주장 정답" />
        <Metric value={`${s.passed}/${s.passage_rows ?? s.rows}`} label="질문 통과" />
        {(s.metadata_rows ?? 0) > 0 && (
          <Metric value={`${s.metadata_correct}/${s.metadata_rows}`} label="메타데이터 질문 정답" />
        )}
        <Metric value={`${s.validation_failed}/${s.answered}`} label="검증에서 거부된 답변"
                tone={s.validation_failed ? "text-bad" : ""} />
      </Metrics>
      {failures.length > 0 && (
        <ul className="mt-2 text-[13px] leading-snug text-bad">
          {failures.map(([code, n]) => <li key={code} title={code}>{label(VALIDATION, code)} {n}</li>)}
        </ul>
      )}
      {/* Support is decided only by a verbatim quote or a review, so the unjudged rest is shown, not hidden. */}
      <p className="mt-3 text-[14px] tabular-nums">
        주장 {s.claims}개: 원문 그대로 인용 {s.claims_supported} · <span className={s.claims_rejected ? "text-bad" : ""}>근거 아님 {s.claims_rejected}</span>
        {" "}· 사람 확인 필요 {s.claims - s.claims_supported - s.claims_rejected}
      </p>
      <p className="text-[14px] tabular-nums">열리지 않는 인용 {s.links_invalid}/{s.links}</p>
      <p className="text-[14px] tabular-nums">
        사람이 읽은 답변 {done}/{reviewed.length} · <span className={noted ? "font-semibold text-bad" : ""}>메모 달린 답변 {noted}</span>
      </p>
      {s.technical_failures > 0 && (
        <p className="mt-1 text-[13px] text-warn">처리 실패(제공자 오류 등) {s.technical_failures}/{s.answered}</p>
      )}
      {s.not_done > 0 && <p className="mt-1 text-[13px] text-warn">답하지 못한 질문 {s.not_done}개</p>}
      <p className="mt-2 text-[13px] tabular-nums text-muted">
        {usd(s.cost_micro_usd)} · 유료 답변 {s.paid_answers}개 · 지연 p50 {sec(s.latency_ms.p50)} · p95 {sec(s.latency_ms.p95)}
        {" "}({s.latency_ms.n}/{s.answered}개 측정)
      </p>
      <LedgerLine ledger={s.ledger ?? c.ledger} />
    </>
  );
}

const sec = (ms: number | null | undefined) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)}초`);

function QuestionHead({ q, size = "text-[17px]" }: { q: GenerationQuestion; size?: string }) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
        <Badge tone="info">{q.mode === "compare" ? "두 문서 비교" : "단일 문서"}</Badge>
        <span title={q.type}>{label(TYPE, q.type)}</span>
        <span>기대: {OUTCOME[q.expected_status] ?? q.expected_status}</span>
      </div>
      <h3 className={`mt-2 max-w-[960px] font-semibold leading-snug ${size}`} title={`질문 ID ${q.id}`}>{q.question}</h3>
      <p className="mt-1 text-[13px] text-muted">{q.docs.join(" · ")}</p>
    </>
  );
}

function QuestionRow({ q, candidates, all, note, setNote, read }: {
  q: GenerationQuestion; candidates: Candidate[]; all: boolean; note: string; setNote: (v: string) => void;
  read: (q: GenerationQuestion, c: Candidate) => void;
}) {
  return (
    <li className="py-8">
      <QuestionHead q={q} />
      {q.required.length > 0 && <Required q={q} candidates={candidates} />}
      <div className="mt-5 overflow-x-auto">
        <div className="grid gap-6" style={columns(candidates.length, all ? 340 : 280)}>
          {candidates.map((c) => <AnswerColumn key={c.id} q={q} c={c} read={() => read(q, c)} />)}
        </div>
      </div>
      <div className="mt-3"><NoteField value={note} onChange={setNote} /></div>
    </li>
  );
}

/** One answer at a time: the question and its required claims on top, the answer large on the left and the
 *  evidence it opens beside it. ← → (or j k) move between answers. */
function ReadingView({ items, at, setAt, notes, setNote }: Notes & {
  items: { q: GenerationQuestion; c: Candidate }[]; at: number; setAt: (i: number) => void;
}) {
  const { q, c } = items[at];
  const a = q.candidates[c.id];
  const [open, setOpen] = useState<string | null>(null);
  const top = useRef<HTMLElement>(null);
  useEffect(() => {  // each answer starts with its question in view, under the sticky toolbar
    setOpen(null);
    top.current?.scrollIntoView({ block: "start" });
  }, [q.key, c.id]);
  useEffect(() => {
    function key(e: KeyboardEvent) {
      const t = e.target as HTMLElement;
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || t.closest("input, textarea, select, [data-note-box]")) return;
      const step = e.key === "ArrowRight" || e.key === "j" ? 1 : e.key === "ArrowLeft" || e.key === "k" ? -1 : 0;
      if (!step || !items[at + step]) return;
      e.preventDefault();
      window.getSelection()?.removeAllRanges();
      setAt(at + step);
    }
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [at, items, setAt]);
  const nav = (step: number, text: string) => (
    <button type="button" disabled={!items[at + step]} onClick={() => setAt(at + step)}
            className="h-9 rounded-[10px] bg-surface px-3 text-[14px] font-semibold disabled:text-muted">{text}</button>
  );
  return (
    <article ref={top} aria-label="한 답변씩 읽기" className="scroll-mt-16 pt-4">
      <div className="flex flex-wrap items-center gap-3">
        {nav(-1, "← 이전")}
        <span className="text-[14px] tabular-nums">답변 <b>{at + 1}</b> / {items.length}</span>
        {nav(1, "다음 →")}
        <span className="text-[13px] text-muted">← → 키로 이동 · 답변에서 글자를 고르면 메모</span>
      </div>
      <div className="mt-6"><QuestionHead q={q} size="text-[20px]" /></div>
      {q.required.length > 0 && <Required q={q} candidates={[c]} />}
      <div className="mt-6 grid grid-cols-[minmax(0,3fr)_minmax(320px,2fr)] gap-8">
        <div className="min-w-0">
          {a && <AnswerBody q={q} c={c} a={a} open={open} setOpen={setOpen} large />}
          <ReviewedMark q={q.key} c={c.id} />
        </div>
        <aside aria-label="열린 근거" className="sticky top-20 self-start">
          {open && a?.evidence?.[open]
            ? <EvidencePanel id={open} e={a.evidence[open]} onClose={() => setOpen(null)} tall />
            : <p className="rounded-[10px] bg-surface px-3 py-6 text-center text-[14px] text-muted">인용 번호를 누르면 근거가 여기 열립니다</p>}
        </aside>
      </div>
      <div className="mt-6"><NoteField value={notes[q.key] ?? ""} onChange={(v) => setNote(q.key, v)} /></div>
    </article>
  );
}

/** Each required claim of the gold beside the verdict every shown candidate's answer got on it. */
function Required({ q, candidates }: { q: GenerationQuestion; candidates: Candidate[] }) {
  const verdict = (c: Candidate, claimId: string) =>
    q.candidates[c.id]?.required?.find((r) => r.claim_id === claimId)?.verdict;
  return (
    <table className="mt-4 text-[14px]">
      <caption className="sr-only">필수 주장별 판정</caption>
      <thead>
        <tr className="text-left text-[13px] text-muted">
          <th className="pb-1 pr-4 font-semibold">필수 주장 <span className="font-normal">· ★ 틀리면 치명</span></th>
          {candidates.map((c) => (
            <th key={c.id} className="pb-1 pr-3 font-semibold"><span className="block w-[8.5rem] truncate" title={c.label}>{c.label}</span></th>
          ))}
        </tr>
      </thead>
      <tbody>
        {q.required.map((r, i) => (
          <tr key={r.claim_id} className="border-t border-line align-baseline">
            <td className="max-w-[640px] py-1.5 pr-6 leading-relaxed" title={`주장 ID ${r.claim_id}`}>
              <span className="mr-1.5 font-semibold tabular-nums">{i + 1}</span>
              {r.critical && <span className="mr-1 text-warn" title="틀리면 치명 오류">★</span>}{r.text}
            </td>
            {candidates.map((c) => {
              // No verdict reads as a dash; a verdict code with no label yet shows as itself rather than vanishing.
              const v = verdict(c, r.claim_id);
              const [text, tone] = (v && VERDICT[v]) || [v || "—", "text-muted"];
              return <td key={c.id} className={`whitespace-nowrap py-1.5 pr-3 font-semibold ${tone}`} title={v}>{text}</td>;
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function AnswerColumn({ q, c, read }: { q: GenerationQuestion; c: Candidate; read: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const a = q.candidates[c.id];
  if (!a) return <p className="self-start text-[14px] text-muted">{c.label}: 결과 없음</p>;
  return (
    <div className="min-w-0">
      <AnswerBody q={q} c={c} a={a} open={open} setOpen={setOpen} read={read} />
      <ReviewedMark q={q.key} c={c.id} />
    </div>
  );
}

/** One answer. Its summary, claims, conflict values, next action and missing fields take span notes. In the
 *  comparison the opened evidence shows inside the column; the reading view (`large`) shows it beside the answer. */
function AnswerBody({ q, c, a, open, setOpen, large = false, read }: {
  q: GenerationQuestion; c: Candidate; a: Answer; open: string | null; setOpen: (id: string | null) => void;
  large?: boolean; read?: () => void;
}) {
  const at = (field: string, text: string, className = "") =>
    <Annotatable q={q.key} c={c.id} field={field} text={text} className={className} />;
  const body = large ? "text-[17px]" : "text-[14px]";
  if (a.error) {
    return (
      <>
        <ColumnHead c={c} a={a} read={read} />
        <p className="mt-2 rounded-[10px] bg-bad-bg px-3 py-2 text-[14px] text-bad">{a.error}{a.detail && ` · ${a.detail}`}</p>
      </>
    );
  }
  return (
    <>
      <ColumnHead c={c} a={a} read={read} />
      {a.validation && (
        <p className="mt-2 rounded-md bg-bad-bg px-2 py-1 text-[14px] text-bad" title={a.validation_detail ?? a.validation}>
          <span className="font-semibold">검증 실패: {label(VALIDATION, a.validation)}</span> · 이 답변은 사용자에게 보이지 않습니다
        </p>
      )}
      {a.failure && (
        <p className="mt-2 break-words rounded-md bg-warn-bg px-2 py-1 text-[14px] text-warn">
          처리 실패 (답변 검증과 무관) · {a.failure}
        </p>
      )}
      {a.summary && at("summary", a.summary, `mt-2 block whitespace-pre-line break-words leading-relaxed ${large ? "text-[19px]" : "text-[15px]"}`)}
      {(a.claims ?? []).length > 0 && (
        <ol className="mt-3 flex flex-col gap-2">
          {a.claims!.map((claim, i) => (
            <ClaimItem key={i} claim={claim} evidence={a.evidence ?? {}} open={open} setOpen={setOpen} size={body}
                       text={at(`claim.${i}`, claim.text, "break-words")} />
          ))}
        </ol>
      )}
      {(a.conflicts ?? []).map((conflict, ci) => (
        <div key={conflict.field} className="mt-3">
          <p className="text-[13px] font-semibold text-warn">근거 충돌 · {conflict.field}</p>
          <ul className="mt-1 flex flex-col gap-1">
            {conflict.alternatives.map((alt, i) => (
              <li key={i} className={`border-l-[3px] border-warn pl-2 leading-relaxed ${body}`}>
                <span className="text-muted">{alt.doc}</span> {at(`conflict.${ci}.${i}`, alt.value, "break-words font-semibold")}
                <Cites ids={alt.evidence_ids} evidence={a.evidence ?? {}} open={open} setOpen={setOpen} />
              </li>
            ))}
          </ul>
        </div>
      ))}
      {!large && open && a.evidence?.[open] && <EvidencePanel id={open} e={a.evidence[open]} onClose={() => setOpen(null)} />}
      {a.next_action && (
        <p className={`mt-2 ${body}`}><span className="mr-1 font-semibold text-muted">다음 조치</span>{at("next_action", a.next_action)}</p>
      )}
      {(a.facts ?? []).length > 0 && (
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 text-[14px]">
          {a.facts!.map((f) => (
            <Fragment key={`${f.doc_id} ${f.field}`}>
              <dt className="text-muted" title={f.field}>{label(FIELD, f.field)}</dt>
              <dd className="break-words">{label(FACT_STATE, f.state)}{f.value != null && ` · ${String(f.value)}`}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      {(a.missing ?? []).length > 0 && (
        <div className="mt-2 text-[13px] text-muted">
          <span className="font-semibold">빠진 항목</span>
          <ul>
            {a.missing!.map((m, i) => (
              <li key={i}>
                {label(FIELD, m.field) !== m.field && <span>{label(FIELD, m.field)} </span>}
                {at(`missing.${i}`, m.field, "break-words")}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}

function ColumnHead({ c, a, read }: { c: Candidate; a: Answer; read?: () => void }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line pb-1.5">
      <span className="flex w-full items-baseline gap-2">
        <span className="truncate text-[13px] font-semibold text-muted" title={c.ref}>{c.label}</span>
        {read && (
          <button type="button" onClick={read} className="ml-auto shrink-0 text-[13px] font-medium text-primary hover:underline">
            크게 읽기
          </button>
        )}
      </span>
      {a.outcome && (a.metadata ? (
        <span className={`text-[18px] font-bold ${a.metadata_correct ? "text-ok" : "text-bad"}`}>
          {OUTCOME[a.outcome] ?? a.outcome} · 메타데이터 {a.metadata_correct ? "정답" : "오답"}
        </span>
      ) : (
        <span className={`text-[18px] font-bold ${a.passed ? "text-ok" : a.status_ok ? "" : "text-bad"}`}>
          {OUTCOME[a.outcome] ?? a.outcome}{a.passed ? " · 통과" : ""}
        </span>
      ))}
      <span className="ml-auto whitespace-nowrap text-[13px] tabular-nums text-muted">
        {usd(a.cost_micro_usd)} · {a.replayed ? <span title="저장된 답변을 다시 읽어 지연을 재지 못했습니다">지연 미측정</span> : sec(a.latency_ms)}
        {a.trace_url && (
          <> · <a href={a.trace_url} target="_blank" rel="noreferrer" className="font-medium text-primary hover:underline">트레이스</a></>
        )}
      </span>
    </div>
  );
}

function ClaimItem({ claim, text, size, evidence, open, setOpen }: {
  claim: Claim; text: React.ReactNode; size: string; evidence: Record<string, Evidence>; open: string | null;
  setOpen: (id: string | null) => void;
}) {
  const valid = Object.fromEntries(claim.links.map((l) => [l.evidence_id, l.valid]));
  const mark = claim.supported === true ? "border-ok" : claim.supported === false ? "border-bad" : "border-line";
  return (
    <li className={`border-l-[3px] pl-2 leading-relaxed ${size} ${mark}`}>
      {text}
      <Cites ids={claim.evidence_ids} valid={valid} evidence={evidence} open={open} setOpen={setOpen} />
      <span className="ml-2 text-[12px] text-muted">
        {claim.supported === true ? "원문 그대로 인용" : claim.supported === false ? "근거 아님" : "사람 확인 필요"}
      </span>
    </li>
  );
}

/** Citation buttons that open their evidence below the answer; one marked invalid did not open on the server. */
function Cites({ ids, valid = {}, evidence, open, setOpen }: {
  ids: string[]; valid?: Record<string, boolean>; evidence: Record<string, Evidence>;
  open: string | null; setOpen: (id: string | null) => void;
}) {
  return ids.map((id) => (
    <Fragment key={id}>
      {" "}
      <button type="button" onClick={() => setOpen(open === id ? null : id)} aria-expanded={open === id}
              disabled={!evidence[id]}
              className={`rounded px-1 text-[12px] font-semibold tabular-nums ${valid[id] === false ? "bg-bad-bg text-bad" : "bg-primary-bg text-primary"} ${open === id ? "ring-2 ring-primary" : ""}`}>
        {id}
      </button>
    </Fragment>
  ));
}

function EvidencePanel({ id, e, onClose, tall = false }: { id: string; e: Evidence; onClose: () => void; tall?: boolean }) {
  const text = e.text ?? "";
  const at = e.quote ? text.indexOf(e.quote) : -1;
  return (
    <div className={`rounded-[10px] bg-surface px-3 py-2 ${tall ? "" : "mt-3"}`}>
      <div className="flex items-center gap-2 text-[13px] text-muted">
        <span className="font-semibold text-ink">{id}</span>
        <span className="truncate">{[e.doc, e.section].filter(Boolean).join(" · ")}</span>
        <button type="button" onClick={onClose} className="ml-auto shrink-0 font-medium text-primary hover:underline">닫기</button>
      </div>
      <p className={`mt-1 overflow-y-auto whitespace-pre-line break-words leading-relaxed ${tall ? "max-h-[70vh] text-[15px]" : "max-h-[320px] text-[14px]"}`}>
        {at >= 0 ? (
          <>{text.slice(0, at)}<mark className="bg-cite">{e.quote}</mark>{text.slice(at + (e.quote?.length ?? 0))}</>
        ) : (
          <>{e.quote && <mark className="block bg-cite">{e.quote}</mark>}{text}</>
        )}
      </p>
    </div>
  );
}
