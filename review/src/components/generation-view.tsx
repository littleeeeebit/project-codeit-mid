"use client";

import { Fragment, useState } from "react";
import type { Answer, Candidate, Claim, Evidence, GenerationQuestion, GenerationView as View } from "@/lib/api";
import { usd } from "@/lib/api";
import { Badge, LedgerLine, Metric, Metrics, NotedList, Segmented, Summaries, columns } from "./ui";
import { NoteField } from "./note-field";

const OUTCOME: Record<string, string> = {
  answered: "답변", insufficient_evidence: "근거 부족", clarification_required: "되묻기",
  conflicting_evidence: "근거 충돌", technical_error: "답변 거부됨", ingestion_unavailable: "문서 없음",
  budget_blocked: "예산 거절",
};
const VERDICT: Record<string, [string, "ok" | "warn" | "bad" | "neutral"]> = {
  correct: ["정답", "ok"], wrong_value: ["값 틀림", "bad"], contested: ["상충", "warn"],
  incomplete_qualifier: ["조건 빠짐", "warn"], missing: ["누락", "bad"], needs_review: ["검토 필요", "neutral"],
};

export function GenerationView({ view, notes, setNote }: {
  view: View;
  notes: Record<string, string>;
  setNote: (key: string, note: string) => void;
}) {
  const [filter, setFilter] = useState<"differs" | "all">("differs");
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
        </div>
        {shown.length === 0 && (
          <p className="py-10 text-center text-muted">결과가 다른 질문이 없습니다. &apos;전체&apos;에서 답변을 봅니다.</p>
        )}
        <NotedList items={shown} keyOf={(q) => q.key} notes={notes} setNote={setNote} row={(q, note, set) => (
          <QuestionRow q={q} candidates={view.candidates} note={note} setNote={set} />
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
        <Metric value={`${s.passed}/${s.rows}`} label="질문 통과" />
        <Metric value={`${s.validation_failed}/${s.answered}`} label="검증에서 거부된 답변"
                tone={s.validation_failed ? "text-bad" : ""} />
      </Metrics>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 text-[14px] tabular-nums">
        {/* Support is decided only by a verbatim quote or a review, so the unjudged rest is shown, not hidden. */}
        <dt className="text-muted">주장 {s.claims}개</dt>
        <dd>
          원문 그대로 인용 {s.claims_supported} · <span className={s.claims_rejected ? "text-bad" : ""}>근거 아님 {s.claims_rejected}</span>
          {" "}· 사람 확인 필요 {s.claims - s.claims_supported - s.claims_rejected}
        </dd>
        <dt className="text-muted">열리지 않는 인용</dt><dd>{s.links_invalid}/{s.links}</dd>
        <dt className="text-muted">비용</dt><dd>{usd(s.cost_micro_usd)} · 유료 답변 {s.paid_answers}개</dd>
        <dt className="text-muted">지연</dt>
        <dd>p50 {sec(s.latency_ms.p50)} · p95 {sec(s.latency_ms.p95)} · 측정 {s.latency_ms.n}/{s.answered}개</dd>
      </dl>
      {failures.length > 0 && (
        <p className="mt-2 text-[13px] text-bad">{failures.map(([code, n]) => `${code} ${n}`).join(" · ")}</p>
      )}
      {s.technical_failures > 0 && (
        <p className="mt-1 text-[13px] text-warn">처리 실패(제공자 오류 등) {s.technical_failures}/{s.answered}</p>
      )}
      {s.not_done > 0 && <p className="mt-1 text-[13px] text-warn">답하지 못한 질문 {s.not_done}개</p>}
      <LedgerLine ledger={s.ledger ?? c.ledger} />
    </>
  );
}

const sec = (ms: number | null | undefined) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)}초`);

function QuestionRow({ q, candidates, note, setNote }: {
  q: GenerationQuestion; candidates: Candidate[]; note: string; setNote: (v: string) => void;
}) {
  return (
    <li className="py-8">
      <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
        <Badge tone="info">{q.mode === "compare" ? "비교" : "단일 문서"}</Badge>
        <span>{q.type}</span>
        <span>기대: {OUTCOME[q.expected_status] ?? q.expected_status}</span>
        <code>{q.id}</code>
      </div>
      <h3 className="mt-2 max-w-[960px] text-[17px] font-semibold leading-snug">{q.question}</h3>
      <p className="mt-1 text-[13px] text-muted">{q.docs.join(" · ")}</p>
      {q.required.length > 0 && (
        <div className="mt-3 max-w-[960px] text-[14px]">
          <span className="mr-2 font-semibold text-muted">필수 주장</span>
          {q.required.map((r, i) => (
            <span key={r.claim_id} className="mr-2 inline rounded bg-cite px-1 leading-7" title={r.claim_id}>
              <span className="font-semibold">{i + 1}{r.critical && " ★"}</span> {r.text}
            </span>
          ))}
        </div>
      )}
      <div className="mt-4 overflow-x-auto">
        <div className="grid gap-6" style={columns(candidates.length)}>
          {candidates.map((c) => <AnswerColumn key={c.id} c={c} a={q.candidates[c.id]} q={q} />)}
        </div>
      </div>
      <div className="mt-3"><NoteField value={note} onChange={setNote} /></div>
    </li>
  );
}

function AnswerColumn({ c, a, q }: { c: Candidate; a: Answer | undefined; q: GenerationQuestion }) {
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
  const verdicts = Object.fromEntries((a.required ?? []).map((r) => [r.claim_id, r.verdict]));
  return (
    <div className="min-w-0">
      <ColumnHead c={c} a={a} />
      {a.validation && (
        <p className="mt-2 rounded-md bg-bad-bg px-2 py-1 text-[14px] text-bad" title={a.validation_detail ?? ""}>
          검증 실패 <code>{a.validation}</code> · 이 답변은 사용자에게 보이지 않습니다
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
      {open && a.evidence?.[open] && <EvidencePanel id={open} e={a.evidence[open]} onClose={() => setOpen(null)} />}
      {(a.missing ?? []).length > 0 && (
        <p className="mt-2 text-[13px] text-muted">빠진 항목: {a.missing!.map((m) => m.field).join(", ")}</p>
      )}
      {q.required.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {q.required.map((r, i) => {
            const [label, tone] = VERDICT[verdicts[r.claim_id]] ?? ["—", "neutral"];
            return <Badge key={r.claim_id} tone={tone}>필수 {i + 1} {label}</Badge>;
          })}
        </div>
      )}
    </div>
  );
}

function ColumnHead({ c, a }: { c: Candidate; a: Answer }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line pb-1.5">
      <span className="truncate text-[14px] font-semibold text-muted">{c.label}</span>
      {a.outcome && (
        <span className={`text-[15px] font-bold ${a.passed ? "text-ok" : a.status_ok ? "" : "text-bad"}`}>
          {OUTCOME[a.outcome] ?? a.outcome}{a.passed ? " · 통과" : ""}
        </span>
      )}
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
      {claim.evidence_ids.map((id) => (
        <Fragment key={id}>
          {" "}
          <button type="button" onClick={() => setOpen(open === id ? null : id)} aria-expanded={open === id}
                  disabled={!evidence[id]}
                  className={`rounded px-1 text-[12px] font-semibold tabular-nums ${valid[id] === false ? "bg-bad-bg text-bad" : "bg-primary-bg text-primary"} ${open === id ? "ring-2 ring-primary" : ""}`}>
            {id}
          </button>
        </Fragment>
      ))}
      <span className="ml-2 text-[12px] text-muted">
        {claim.supported === true ? "원문 그대로 인용" : claim.supported === false ? "근거 아님" : "사람 확인 필요"}
      </span>
    </li>
  );
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
