"use client";

// 검토 대기: a pending draft beside its original spans, decided with a note by someone other than whoever started
// the drafting run (the service refuses that person; the screen says so before they try). The user picked the
// three-column triage layout over one-at-a-time and a table with rows that open in place (DESIGN.md).

import { useState } from "react";
import { Download } from "lucide-react";
import { cn } from "cn";
import { api, errorText, originalHref, type Schemas } from "@/lib/api";
import { ANSWERABILITY, GOLD_TYPE, label, locationText, REVIEW, REVIEW_TONE, STATUS } from "@/lib/format";
import { useMe } from "@/components/sign-in";
import { must, usePoll } from "@/lib/use-poll";
import { Section, Table, td } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Empty, Field, field, Notice } from "@/components/verify/parts";

type Pending = Schemas["Pending"];
type Candidate = Schemas["CandidateReview"];

const CRITICAL: Record<string, string> = { deadline: "마감", amount: "금액", mandatory_condition: "필수 조건", institution: "기관" };

function useCandidate(id: string | null) {
  return usePoll(id ? `cand-${id}` : null, () => must(api.GET("/api/gold/{candidate_id}", { params: { path: { candidate_id: id! } } }), errorText), null);
}

// ---------------------------------------------------------------- parts

function DraftCard({ c }: { c: Candidate }) {
  const facts: [string, React.ReactNode][] = [
    ["유형", label(GOLD_TYPE, c.type)], ["데이터셋", c.dataset], ["초안 작성", c.drafted_by ?? "-"],
    ["초안 생성 요청", c.requested_by ? <>{c.requested_by} <span className="text-warn">(승인할 수 없음)</span></> : "-"],
    ...(c.gold ? [["답변 가능성", label(ANSWERABILITY, c.answerability)], ["기대 상태", STATUS[c.expected_status ?? ""]?.label ?? c.expected_status ?? "-"],
      ["기준일", c.as_of_date ?? "-"]] as [string, React.ReactNode][] : []),
  ];
  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <StatusBadge tone="neutral">{label(GOLD_TYPE, c.type)}</StatusBadge>
        <p className="text-lg font-bold leading-snug">{c.question}</p>
        {c.expected_answer && <div className="space-y-2 rounded-xl border border-input bg-secondary/40 p-4"><h3 className="text-sm font-semibold">초안 답변</h3><p className="text-base">{c.expected_answer}</p></div>}
        {c.difficulty_reason && <p className="text-sm text-muted-foreground">난이도 이유: {c.difficulty_reason}</p>}
      </div>
      {c.current_errors.map((e) => <Notice key={e} tone="bad">현재 원문 상태와 맞지 않습니다: {e}</Notice>)}
      <dl className="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1.5 text-sm">
        {facts.map(([k, v]) => <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd>{v}</dd></div>)}
      </dl>
      {c.required_claims.length > 0 && (
        <Section title="필수 주장" aside={<span className="text-xs text-muted-foreground">답변이 반드시 맞혀야 하는 값과 단서</span>}>
          <Table caption="필수 주장" head={["값", "단서", "중요도"]}>
            {c.required_claims.map((x, i) => (
              <tr key={x.claim_id ?? i}>
                <td className={cn(td, "font-semibold")}>{Object.entries(x.match).filter(([k]) => k !== "type").map(([, v]) => String(v)).join(" ")}</td>
                <td className={td}>{x.qualifiers.map((q) => q.join("|")).join(" / ") || "-"}</td>
                <td className={td}>{x.critical_kind ? <StatusBadge tone="warn">{label(CRITICAL, x.critical_kind)}</StatusBadge> : "보통"}</td>
              </tr>
            ))}
          </Table>
        </Section>
      )}
      <DraftDocuments c={c} />
      {Object.keys(c.negative_validation).length > 0 && (
        <Section title="부재 검증 기록">
          <dl className="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1 text-sm">
            {Object.entries(c.negative_validation).map(([k, v]) => <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd>{typeof v === "string" ? v : JSON.stringify(v)}</dd></div>)}
          </dl>
        </Section>
      )}
      {c.metadata.length > 0 && (
        <Section title="CSV 메타데이터">
          <dl className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-1 text-sm">
            {c.metadata.map((m) => <div key={m.field} className="contents"><dt className="text-muted-foreground">{m.field}</dt><dd className="break-all">{JSON.stringify(m.value)}</dd></div>)}
          </dl>
        </Section>
      )}
    </div>
  );
}

/** The draft's documents: title, review state, cited PDF pages and the original file. */
function DraftDocuments({ c }: { c: Candidate }) {
  return (
      <ul className="space-y-2">
        {c.documents.map((d, i) => (
          <li key={i} className="space-y-1.5 rounded-xl border p-3">
            {!d.found ? <p className="text-sm text-bad">문서를 찾을 수 없습니다.</p> : (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  {c.documents.length > 1 && <span className="rounded bg-foreground px-1.5 text-xs font-bold text-background">문서 {i + 1}</span>}
                  <span className="text-sm font-semibold">{d.title}</span>
                  <StatusBadge tone={REVIEW_TONE[d.review_status ?? ""] ?? "neutral"}>{label(REVIEW, d.review_status) || "원문 대조 미상"}</StatusBadge>
                </div>
                <p className="text-xs text-muted-foreground">{d.filename} · {(d.format ?? "").toUpperCase()}</p>
                {d.pdf_pages.map((p) => <p key={p.group_id} className="text-xs text-muted-foreground">근거 {p.group_id} · PDF 파일 {p.pdf_pages.join(", ")}쪽 (파일 페이지 기준)</p>)}
                {d.unavailable_reason && <p className="text-xs text-bad">{d.unavailable_reason}</p>}
                {d.doc_id && d.source_hash && (
                  <Button variant="outline" size="sm" asChild>
                    <a href={originalHref(d.doc_id, d.source_hash)}><Download aria-hidden />원문 파일 받기</a>
                  </Button>
                )}
              </>
            )}
          </li>
        ))}
      </ul>
  );
}

function Spans({ c }: { c: Candidate }) {
  return (
    <section aria-label="원문 구절" className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-lg font-bold">원문 구절 {c.spans.length}개</h3>
        <span className="text-xs text-muted-foreground">노란 바탕이 초안이 인용한 부분</span>
      </div>
      {c.spans.map((s, i) => (
        <article key={i} className="space-y-4 rounded-xl border border-input bg-background p-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">{s.label}</span>
            {s.cited_found ? <StatusBadge tone="ok">인용 위치 확인</StatusBadge> : <StatusBadge tone="bad">인용 구절을 찾지 못함</StatusBadge>}
          </div>
          <p className="text-xs text-muted-foreground">{s.location ? locationText(s.location) : "위치 없음"}</p>
          {s.missing ? <p className="text-sm text-bad">원문 요소를 찾을 수 없습니다.</p> : (
            <p className="text-base whitespace-pre-wrap">
              {s.segments.map(({ text, cited }, j) => cited
                ? <mark key={j} className="rounded bg-cite-bg px-0.5 font-semibold text-foreground [box-decoration-break:clone]">{text}</mark>
                : <span key={j}>{text}</span>)}
            </p>
          )}
          {!s.cited_found && s.quote && <p className="text-base"><span className="mr-2 text-xs font-semibold text-muted-foreground">초안 인용</span>{s.quote}</p>}
        </article>
      ))}
    </section>
  );
}

function DecideForm({ c, onDone }: { c: Candidate; onDone: (text: string) => void }) {
  const member = useMe().name;
  const cats = usePoll("gold-cats", () => must(api.GET("/api/gold/reject-categories"), errorText), null);
  const [note, setNote] = useState("");
  const [inspected, setInspected] = useState(false);
  const [disputed, setDisputed] = useState(false);
  const [reasons, setReasons] = useState<string[]>([]);
  const [state, setState] = useState<{ busy?: boolean; error?: string }>({});
  const isDrafter = !!c.requested_by && c.requested_by === member;
  const decide = async (decision: "approve" | "reject") => {
    setState({ busy: true });
    const { error } = await api.POST("/api/gold/{candidate_id}/decide", { params: { path: { candidate_id: c.candidate_id } },
      body: { decision, expected_sha: c.row_sha256, note, categories: reasons, original_inspected: inspected, disputed } });
    if (error) return setState({ error: errorText(error) });
    onDone(`${c.candidate_id}: ${decision === "approve" ? "승인" : "거절"}했습니다.`);
  };
  const id = `decide-${c.candidate_id}`;
  return (
    <form onSubmit={(e) => e.preventDefault()} className="space-y-4 rounded-2xl border-2 border-foreground/10 bg-secondary/40 p-4">
      <h3 className="text-lg font-bold">결정</h3>
      {isDrafter && <Notice tone="warn">{member} 님이 이 초안의 생성을 요청했습니다. 승인은 다른 검토자가 해야 합니다. 거절은 할 수 있습니다.</Notice>}
      <Field id={`${id}-note`} label="메모 (필수: 확인한 원문 위치, 또는 무엇이 틀렸는지)">
        <textarea id={`${id}-note`} rows={3} required value={note} onChange={(e) => setNote(e.target.value)} className={cn(field, "py-2")} />
      </Field>
      {c.gold && (
        <div className="space-y-1.5 text-sm">
          <label className="flex items-start gap-2"><input type="checkbox" checked={inspected} onChange={(e) => setInspected(e.target.checked)} className="mt-0.5 size-4 accent-primary" />원문 파일에서 질문·값·단위·조건·근거를 직접 확인했습니다</label>
          <label className="flex items-start gap-2"><input type="checkbox" checked={disputed} onChange={(e) => setDisputed(e.target.checked)} className="mt-0.5 size-4 accent-primary" />쟁점 있음: 마감·금액·기관·필수 조건을 다른 사람이 한 번 더 확인해야 합니다</label>
        </div>
      )}
      <fieldset className="space-y-1.5">
        <legend className="text-[13px] font-semibold">거절 사유 (거절할 때)</legend>
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(cats.data ?? {}).map(([k, v]) => {
            const on = reasons.includes(k);
            return (
              <button key={k} type="button" aria-pressed={on} onClick={() => setReasons(on ? reasons.filter((x) => x !== k) : [...reasons, k])}
                      className={cn("rounded-full border px-2.5 py-1 text-[13px] outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
                        on ? "border-bad bg-bad-bg font-semibold text-bad" : "bg-background hover:bg-secondary")}>{v}</button>
            );
          })}
        </div>
      </fieldset>
      <div className="flex flex-wrap gap-2">
        <Button size="lg" className="h-10 flex-1" disabled={state.busy || isDrafter} onClick={() => decide("approve")}>승인 → 데이터셋</Button>
        <Button size="lg" variant="destructive" className="h-10 flex-1" disabled={state.busy} onClick={() => decide("reject")}>거절 → 거절 위키</Button>
      </div>
      {state.error && <Notice tone="bad">{state.error}</Notice>}
    </form>
  );
}

function QueueRow({ p, active, onOpen }: { p: Pending; active: boolean; onOpen: () => void }) {
  return (
    <button type="button" onClick={onOpen} aria-current={active || undefined}
            className={cn("w-full rounded-lg px-3 py-2.5 text-left outline-none hover:bg-secondary/70 focus-visible:ring-3 focus-visible:ring-ring/50", active && "bg-accent hover:bg-accent")}>
      <span className="block text-base font-semibold">{p.question}</span>
      <span className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
        <span className="font-mono">{p.candidate_id}</span>·<span>{label(GOLD_TYPE, p.type)}</span>
      </span>
    </button>
  );
}

function Loaded({ id, children }: { id: string; children: (c: Candidate) => React.ReactNode }) {
  const c = useCandidate(id);
  if (c.error) return <Notice tone="bad">{c.error}</Notice>;
  if (!c.data) return <Skeleton className="h-96 w-full" />;
  return <>{children(c.data)}</>;
}

// ---------------------------------------------------------------- the queue: list | draft and decision | original

export function ColumnsQueue({ pending, onDecided }: { pending: Pending[]; onDecided: (text: string) => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const cur = pending.find((p) => p.candidate_id === open) ?? pending[0];
  return (
    <div className="grid gap-5 xl:grid-cols-[260px_minmax(0,1fr)_minmax(0,1fr)]">
      <nav aria-label="검토 대기 목록" className="space-y-2 xl:sticky xl:top-20 xl:max-h-[calc(100dvh-7rem)] xl:self-start xl:overflow-y-auto">
        {pending.map((p) => <QueueRow key={p.candidate_id} p={p} active={p === cur} onOpen={() => setOpen(p.candidate_id)} />)}
      </nav>
      <Loaded key={cur.candidate_id} id={cur.candidate_id}>{(c) => (
        <>
          <div className="min-w-0 space-y-6"><DraftCard c={c} /><DecideForm c={c} onDone={onDecided} /></div>
          <div className="min-w-0"><Spans c={c} /></div>
        </>
      )}</Loaded>
    </div>
  );
}

export function NoPending() {
  return <Empty>검토할 초안이 없습니다. 초안 만들기에서 초안을 만들어 검토 대기열에 올리세요.</Empty>;
}
