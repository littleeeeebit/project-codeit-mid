"use client";

import { Fragment, useState } from "react";
import type { Answer, Candidate, Claim, Evidence, GenerationQuestion, GenerationView as View } from "@/lib/api";
import { usd } from "@/lib/api";
import { FACT_STATE, FIELD, label, TYPE, VALIDATION } from "@/lib/labels";
import { Badge, LedgerLine, Metric, Metrics, NotedList, Segmented, Summaries, columns, useShown } from "./ui";
import { NoteField } from "./note-field";

const OUTCOME: Record<string, string> = {
  answered: "답변", insufficient_evidence: "근거 부족", clarification_required: "되묻기",
  conflicting_evidence: "근거 충돌", technical_error: "답변 거부됨", ingestion_unavailable: "문서 없음",
  budget_blocked: "예산 거절",
};
const VERDICT: Record<string, [string, string]> = {
  correct: ["정답", "text-ok"], wrong_value: ["값 틀림", "text-bad"], contested: ["상충", "text-warn"],
  incomplete_qualifier: ["조건 빠짐", "text-warn"], missing: ["누락", "text-bad"], needs_review: ["검토 필요", "text-muted"],
};

export function GenerationView({ view, notes, setNote, decide }: {
  view: View;
  notes: Record<string, string>;
  setNote: (key: string, note: string) => void;
  decide: React.ReactNode;
}) {
  const [filter, setFilter] = useState<"differs" | "all">("differs");
  const { shown: cands, all, picker } = useShown(view.candidates, 2);  // answers are long: two columns at most
  const differing = view.questions.filter((q) => q.differs).length;
  const shown = filter === "all" ? view.questions : view.questions.filter((q) => q.differs);
  return (
    <>
      <Summaries candidates={view.candidates} summary={view.summary} note={<>
        모든 후보가 gpt-5-mini로 같은 개발 질문 {view.dataset.rows}개에 한 번씩 답했습니다
        {view.dataset.skipped > 0 && ` (검토되지 않았거나 근거가 맞지 않는 ${view.dataset.skipped}개 제외)`}. 검색은 서빙 중인
        설정({view.activation.run_id ?? "기본"} · {view.activation.mode})으로 같고, 후보의 코드와 review-variant.json만
        다릅니다. 비용은 공유 원장의 정산 금액입니다.
      </>}>
        {(s, c) => <Summary s={s} c={c} />}
      </Summaries>

      <section aria-label="질문별 답변" className="mt-10">
        <div className="sticky top-0 z-10 -mx-2 flex flex-wrap items-center gap-3 bg-white/95 px-2 py-3 backdrop-blur">
          <h2 className="mr-2 text-[18px] font-bold">질문별 답변</h2>
          <Segmented value={filter} onChange={setFilter} options={[
            ["differs", `결과가 다른 질문 ${differing}`], ["all", `전체 ${view.questions.length}`],
          ]} />
          {picker}
          {decide}
        </div>
        {shown.length === 0 && (
          <p className="py-10 text-center text-muted">결과가 다른 질문이 없습니다. &apos;전체&apos;에서 답변을 봅니다.</p>
        )}
        <NotedList items={shown} keyOf={(q) => q.key} notes={notes} setNote={setNote} row={(q, note, set) => (
          <QuestionRow q={q} candidates={cands} all={all} note={note} setNote={set} />
        )} />
      </section>
    </>
  );
}

function Summary({ s, c }: { s: View["summary"][string]; c: Candidate }) {
  const failures = Object.entries(s.validation_failures);
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

function QuestionRow({ q, candidates, all, note, setNote }: {
  q: GenerationQuestion; candidates: Candidate[]; all: boolean; note: string; setNote: (v: string) => void;
}) {
  return (
    <li className="py-8">
      <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
        <Badge tone="info">{q.mode === "compare" ? "두 문서 비교" : "단일 문서"}</Badge>
        <span title={q.type}>{label(TYPE, q.type)}</span>
        <span>기대: {OUTCOME[q.expected_status] ?? q.expected_status}</span>
      </div>
      <h3 className="mt-2 max-w-[960px] text-[17px] font-semibold leading-snug" title={`질문 ID ${q.id}`}>{q.question}</h3>
      <p className="mt-1 text-[13px] text-muted">{q.docs.join(" · ")}</p>
      {q.required.length > 0 && <Required q={q} candidates={candidates} />}
      <div className="mt-5 overflow-x-auto">
        <div className="grid gap-6" style={columns(candidates.length, all ? 340 : 280)}>
          {candidates.map((c) => <AnswerColumn key={c.id} c={c} a={q.candidates[c.id]} />)}
        </div>
      </div>
      <div className="mt-3"><NoteField value={note} onChange={setNote} /></div>
    </li>
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

function AnswerColumn({ c, a }: { c: Candidate; a: Answer | undefined }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!a) return <p className="self-start text-[14px] text-muted">{c.label}: 결과 없음</p>;
  if (a.error) {
    return (
      <div className="min-w-0">
        <ColumnHead c={c} a={a} />
        <p className="mt-2 rounded-[10px] bg-bad-bg px-3 py-2 text-[14px] text-bad">{a.error}{a.detail && ` · ${a.detail}`}</p>
      </div>
    );
  }
  return (
    <div className="min-w-0">
      <ColumnHead c={c} a={a} />
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
      {a.summary && <p className="mt-2 whitespace-pre-line break-words text-[15px] leading-relaxed">{a.summary}</p>}
      {(a.claims ?? []).length > 0 && (
        <ol className="mt-3 flex flex-col gap-2">
          {a.claims!.map((claim, i) => (
            <ClaimItem key={i} claim={claim} evidence={a.evidence ?? {}} open={open} setOpen={setOpen} />
          ))}
        </ol>
      )}
      {(a.conflicts ?? []).map((conflict) => (
        <div key={conflict.field} className="mt-3">
          <p className="text-[13px] font-semibold text-warn">근거 충돌 · {conflict.field}</p>
          <ul className="mt-1 flex flex-col gap-1">
            {conflict.alternatives.map((alt, i) => (
              <li key={i} className="border-l-[3px] border-warn pl-2 text-[14px] leading-relaxed">
                <span className="text-muted">{alt.doc}</span> <span className="break-words font-semibold">{alt.value}</span>
                <Cites ids={alt.evidence_ids} evidence={a.evidence ?? {}} open={open} setOpen={setOpen} />
              </li>
            ))}
          </ul>
        </div>
      ))}
      {open && a.evidence?.[open] && <EvidencePanel id={open} e={a.evidence[open]} onClose={() => setOpen(null)} />}
      {a.next_action && (
        <p className="mt-2 text-[14px]"><span className="mr-1 font-semibold text-muted">다음 조치</span>{a.next_action}</p>
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
        <p className="mt-2 text-[13px] text-muted">빠진 항목: {a.missing!.map((m) => label(FIELD, m.field)).join(", ")}</p>
      )}
    </div>
  );
}

function ColumnHead({ c, a }: { c: Candidate; a: Answer }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line pb-1.5">
      <span className="w-full truncate text-[13px] font-semibold text-muted" title={c.ref}>{c.label}</span>
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

function ClaimItem({ claim, evidence, open, setOpen }: {
  claim: Claim; evidence: Record<string, Evidence>; open: string | null; setOpen: (id: string | null) => void;
}) {
  const valid = Object.fromEntries(claim.links.map((l) => [l.evidence_id, l.valid]));
  const mark = claim.supported === true ? "border-ok" : claim.supported === false ? "border-bad" : "border-line";
  return (
    <li className={`border-l-[3px] pl-2 text-[14px] leading-relaxed ${mark}`}>
      <span className="break-words">{claim.text}</span>
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

function EvidencePanel({ id, e, onClose }: { id: string; e: Evidence; onClose: () => void }) {
  const text = e.text ?? "";
  const at = e.quote ? text.indexOf(e.quote) : -1;
  return (
    <div className="mt-3 rounded-[10px] bg-surface px-3 py-2">
      <div className="flex items-center gap-2 text-[13px] text-muted">
        <span className="font-semibold text-ink">{id}</span>
        <span className="truncate">{[e.doc, e.section].filter(Boolean).join(" · ")}</span>
        <button type="button" onClick={onClose} className="ml-auto shrink-0 font-medium text-primary hover:underline">닫기</button>
      </div>
      <p className="mt-1 max-h-[320px] overflow-y-auto whitespace-pre-line break-words text-[14px] leading-relaxed">
        {at >= 0 ? (
          <>{text.slice(0, at)}<mark className="bg-cite">{e.quote}</mark>{text.slice(at + (e.quote?.length ?? 0))}</>
        ) : (
          <>{e.quote && <mark className="block bg-cite">{e.quote}</mark>}{text}</>
        )}
      </p>
    </div>
  );
}
