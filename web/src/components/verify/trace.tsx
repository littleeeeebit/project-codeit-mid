"use client";

// 검색 추적: reproduce one question's retrieval for free, read it as five stages, and only then, as a separate paid
// step, generate an answer from exactly that frozen evidence. Two frozen runs can be compared.

import { useMemo, useState } from "react";
import { ChevronDown, Download, X } from "lucide-react";
import { cn } from "cn";
import { api, errorText, type RequestView, type Schemas } from "@/lib/api";
import { label, locationShort, locationText, MODE, REQUEST, REVIEW, REVIEW_TONE, stamp, usd } from "@/lib/format";
import { must, usePoll } from "@/lib/use-poll";
import { Section, Table, td } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { AnswerView } from "@/components/ask/answer-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Empty, Field, field, Notice } from "./parts";

type Run = Schemas["TraceRun"];
type Doc = Schemas["Document"];
type Summary = Schemas["TraceSummary"];

const EXCLUDED: Record<string, string> = {
  unit_limit: "근거 개수 한도", token_budget: "토큰 한도", duplicate_span: "이미 포함된 부분",
  other_requirement_code: "다른 요구사항 코드", outside_scope: "범위 밖",
};

function limitationText(code: string): string {
  const [head, ...rest] = code.split(":");
  const known: Record<string, string> = {
    compare: "비교: 문서별로 근거를 나눠 담음", index_includes_unreviewed_sources: "원문 대조 전 문서가 색인에 포함됨",
    scope_not_indexed: "색인에 없는 문서", code_not_found: "요구사항 코드 없음", code_detail_unavailable: "요구사항 상세 없음",
    code_ambiguous: "요구사항 코드가 여러 곳", index_outdated: "색인이 문서 기록보다 오래됨 (build-keyword로 다시 만들기)", scope_redundant_terms: "범위와 겹쳐 뺀 검색어", linked_evidence_missing: "이어진 근거 일부 누락",
  };
  if (known[head]) return `${known[head]}${rest.length && head !== "compare" ? ` (${rest.join(":")})` : ""}`;
  if (rest.length && known[rest[0]]) return `${known[rest[0]]} (문서 ${head})`;
  return code;
}

// ---------------------------------------------------------------- the form

export function TraceForm({ onRun }: { onRun: (runId: string) => void }) {
  const sources = usePoll("trace-sources", () => must(api.GET("/api/verify/trace-sources"), errorText), null);
  const [fromDev, setFromDev] = useState<number | null>(null);
  const [question, setQuestion] = useState("");
  const [docs, setDocs] = useState<Doc[]>([]);
  const [filter, setFilter] = useState("");
  const [mode, setMode] = useState<string | null>(null);
  const [state, setState] = useState<{ busy?: boolean; error?: string }>({});
  const data = sources.data;
  const devRow = fromDev == null ? null : data?.questions[fromDev];
  const matches = useMemo(() => {
    const q = filter.trim();
    if (!data || !q) return [];
    return data.documents.filter((d) => `${d.title} ${d.institution ?? ""}`.includes(q) && !docs.some((x) => x.doc_id === d.doc_id)).slice(0, 6);
  }, [data, filter, docs]);

  const pickDev = (i: number | null) => {
    setFromDev(i);
    const row = i == null ? null : data?.questions[i];
    if (!row || !data) return;
    setQuestion(row.question);
    const ids = row.scope.map((s) => String(s.doc_id)).concat(row.doc_ids, row.doc_id ? [row.doc_id] : []);
    setDocs(data.documents.filter((d) => ids.includes(d.doc_id)).slice(0, 2));
  };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!question.trim() || !docs.length) return setState({ error: "질문을 적고 문서를 하나 이상 고르세요." });
    setState({ busy: true });
    const { data: run, error } = await api.POST("/api/verify/traces", { body: {
      question, mode: mode ?? data!.modes[0], as_of: devRow?.as_of_date ?? devRow?.as_of ?? null,
      scope: docs.map((d) => ({ doc_id: d.doc_id, source_hash: d.source_hash })) } });
    if (error || !run) return setState({ error: errorText(error) });
    setState({});
    onRun(run.run_id);
  };

  if (sources.error) return <Notice tone="bad">{sources.error}</Notice>;
  if (!data) return <Skeleton className="h-64 w-full" />;
  if (!data.documents.length) return <Empty>색인된 문서가 없습니다.</Empty>;
  return (
    <form onSubmit={submit} className="space-y-4">
      {data.questions.length > 0 && (
        <Field id="trace-dev" label="검토된 개발 질문에서 가져오기 (선택)">
          <select id="trace-dev" value={fromDev ?? ""} onChange={(e) => pickDev(e.target.value === "" ? null : Number(e.target.value))} className={cn(field, "h-10")}>
            <option value="">직접 입력</option>
            {data.questions.map((q, i) => <option key={i} value={i}>{q.question_id ?? q.id} · {q.question.slice(0, 50)}</option>)}
          </select>
        </Field>
      )}
      <Field id="trace-q" label="질문">
        <textarea id="trace-q" rows={2} value={question} onChange={(e) => setQuestion(e.target.value)} className={cn(field, "py-2 text-[15px]")} placeholder="예: 하자보수 기간은 얼마인가요?" />
      </Field>
      <div className="space-y-2">
        <Field id="trace-doc" label={`문서 (최대 2개 · ${docs.length}개 고름)`} hint="사업명이나 기관명 일부를 입력하세요.">
          <input id="trace-doc" value={filter} onChange={(e) => setFilter(e.target.value)} disabled={docs.length >= 2}
                 aria-describedby="trace-doc-hint" className={cn(field, "h-10")} autoComplete="off" />
        </Field>
        {matches.length > 0 && (
          <ul className="divide-y rounded-lg border">
            {matches.map((d) => (
              <li key={d.doc_id}>
                <button type="button" onClick={() => { setDocs([...docs, d]); setFilter(""); }}
                        className="w-full px-3 py-2 text-left text-sm outline-none hover:bg-secondary/70 focus-visible:bg-accent">
                  <span className="line-clamp-1 font-medium">{d.title}</span>
                  <span className="text-xs text-muted-foreground">{d.institution}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {docs.length > 0 && (
          <ul className="space-y-1.5">
            {docs.map((d, i) => (
              <li key={d.doc_id} className="flex items-center gap-2 rounded-lg bg-secondary/70 px-3 py-2 text-sm">
                {docs.length > 1 && <span className="rounded bg-foreground px-1.5 text-xs font-bold text-background">문서 {i + 1}</span>}
                <span className="line-clamp-1 flex-1">{d.title}</span>
                <button type="button" aria-label={`${d.title} 빼기`} onClick={() => setDocs(docs.filter((x) => x !== d))}
                        className="rounded p-0.5 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
                  <X className="size-4" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <Field id="trace-mode" label="검색 방식" hint="의미 검색은 캐시된 질의 벡터만 씁니다(무료). 캐시에 없으면 키워드로 대체됩니다.">
        <select id="trace-mode" value={mode ?? data.modes[0]} onChange={(e) => setMode(e.target.value)} aria-describedby="trace-mode-hint" className={cn(field, "h-10")}>
          {data.modes.map((m, i) => <option key={m} value={m}>{label(MODE, m)}{i === 0 ? " (현재 운영)" : ""}</option>)}
        </select>
      </Field>
      <Button type="submit" size="lg" className="h-10 w-full text-[15px]" disabled={state.busy}>검색만 실행 · 무료</Button>
      {state.error && <Notice tone="bad">{state.error}</Notice>}
    </form>
  );
}

export function RunList({ runs, active, onOpen }: { runs: Summary[]; active: string | null; onOpen: (id: string) => void }) {
  if (!runs.length) return <p className="text-sm text-muted-foreground">아직 실행이 없습니다.</p>;
  return (
    <ul className="space-y-0.5">
      {runs.map((r) => (
        <li key={r.run_id}>
          <button type="button" onClick={() => onOpen(r.run_id)} aria-current={active === r.run_id || undefined}
                  className={cn("w-full rounded-lg px-3 py-2 text-left outline-none hover:bg-secondary/70 focus-visible:ring-3 focus-visible:ring-ring/50", active === r.run_id && "bg-accent hover:bg-accent")}>
            <span className="line-clamp-1 text-sm font-medium">{r.question}</span>
            <span className="text-xs text-muted-foreground tabular-nums">{stamp(r.created_at)} · {r.member_id}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------- one frozen run

function Stage({ n, title, value, note }: { n: number; title: string; value: string; note?: string }) {
  return (
    <li className="min-w-0 rounded-xl bg-secondary/60 p-3">
      <p className="text-xs font-semibold text-muted-foreground">{n}. {title}</p>
      <p className="mt-1 text-[15px] font-bold">{value}</p>
      {note && <p className="mt-0.5 text-xs text-muted-foreground">{note}</p>}
    </li>
  );
}

function Fold({ title, children, open }: { title: string; children: React.ReactNode; open?: boolean }) {
  return (
    <details open={open} className="group rounded-xl border">
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-xl px-4 py-3 text-sm font-semibold outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
        <ChevronDown className="size-4 -rotate-90 transition-transform group-open:rotate-0" aria-hidden />{title}
      </summary>
      <div className="border-t p-4">{children}</div>
    </details>
  );
}

export function RunView({ runId }: { runId: string }) {
  const run = usePoll(`run-${runId}`, () => must(api.GET("/api/verify/traces/{run_id}", { params: { path: { run_id: runId } } }), errorText), null);
  if (run.error) return <Notice tone="bad">{run.error}</Notice>;
  if (!run.data) return <Skeleton className="h-96 w-full" />;
  const t: Run = run.data;
  const r = t.retrieval;
  const parsed = t.docs.filter((d) => d.parse_status === "parsed").length;
  const checked = t.docs.filter((d) => !["unreviewed", "needs_recovery"].includes(d.review_status)).length;
  const docNo = (id: string | null | undefined) => (t.scope.length > 1 ? `문서 ${t.scope.findIndex((s) => s.doc_id === id) + 1}` : "");
  return (
    <article className="space-y-6">
      <header className="space-y-1">
        <h3 className="text-lg font-bold leading-snug">{t.question}</h3>
        <p className="text-xs text-muted-foreground tabular-nums">
          실행 {t.run_id} · 설정 {t.config.config_id} · {stamp(t.created_at)} · {t.member_id} · 기준일 {t.as_of ?? "-"}
        </p>
      </header>
      <ol className="grid gap-2 sm:grid-cols-3 xl:grid-cols-5">
        <Stage n={1} title="수집·검수" value={`${parsed}/${t.docs.length} 수집됨`} note={`원문 대조 ${checked}/${t.docs.length}`} />
        <Stage n={2} title="범위·분석" value={`질의 토큰 ${t.query_tokens.length}개`} note={`색인 ${t.index_version ?? "-"}`} />
        <Stage n={3} title="채널 순위" value={label(MODE, r.mode)} note={`후보 ${r.candidates.length}개 · 대체 ${r.fallback ?? "없음"}`} />
        <Stage n={4} title="선택된 근거" value={`${r.evidence.length}개 · ${r.evidence_tokens}토큰`} note={`입력 추정 ${t.input_tokens}토큰 · ${r.timings_ms.total ?? "-"}ms`} />
        <Stage n={5} title="답변 생성" value={t.generation_block ? "막힘" : "별도 유료"} note={`최대 예상 ${usd(t.estimate_micro_usd, 4)}`} />
      </ol>

      <Section title={`선택된 근거 ${r.evidence.length}개`} aside={
        r.limitations.length ? <StatusBadge tone="warn">제한 사항 {r.limitations.length}건</StatusBadge> : <StatusBadge tone="ok">제한 사항 없음</StatusBadge>}>
        {r.limitations.length > 0 && (
          <ul className="space-y-1 text-sm text-warn">{r.limitations.map((x) => <li key={x}>· {limitationText(x)}</li>)}</ul>
        )}
        {t.coverage && t.coverage.length > 1 && (
          <p className="text-sm text-muted-foreground">{t.coverage.map((c, i) => `문서 ${i + 1}: 근거 ${c.evidence ?? 0}개${c.limitation ? ` (${c.limitation})` : ""}`).join(" · ")}</p>
        )}
        <ol className="space-y-2">
          {r.evidence.map((e) => (
            <li key={e.evidence_id}>
              <details className="group rounded-xl border">
                <summary className="flex cursor-pointer list-none items-center gap-2 rounded-xl px-4 py-2.5 outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
                  <span className="rounded-md bg-cite-bg px-1.5 py-0.5 text-xs font-bold">{e.evidence_id}</span>
                  {docNo(e.doc_id) && <StatusBadge tone="neutral">{docNo(e.doc_id)}</StatusBadge>}
                  <span className="text-sm font-medium" title={locationText(e.location)}>{locationShort(e.location)}</span>
                  <span className="line-clamp-1 flex-1 text-sm text-muted-foreground">{e.quote}</span>
                  <span className="text-xs tabular-nums text-muted-foreground">{e.token_count}토큰</span>
                </summary>
                <div className="space-y-2 border-t px-4 py-3">
                  <p className="text-xs text-muted-foreground">{locationText(e.location)}</p>
                  <p className="text-sm leading-7 whitespace-pre-wrap">{e.quote}</p>
                  <p className="font-mono text-[11px] text-muted-foreground">조각 {e.chunk_id} · 추출 {e.extraction_id.slice(0, 12)} · 요소 {e.element_ids.join(", ")}</p>
                </div>
              </details>
            </li>
          ))}
        </ol>
      </Section>

      <div className="space-y-2">
        <Fold title={`1. 수집·검수 상태 · 문서 ${t.docs.length}개`}>
          <Table caption="수집·검수 상태" head={["파일", "수집", "원문 대조", "사유"]}>
            {t.docs.map((d) => (
              <tr key={d.doc_id}>
                <td className={td}>{d.filename ?? d.doc_id.slice(0, 8)}</td>
                <td className={td}>{d.parse_status === "parsed" ? <StatusBadge tone="ok">수집됨</StatusBadge> : <StatusBadge tone="bad">{d.parse_status}</StatusBadge>}</td>
                <td className={td}><StatusBadge tone={REVIEW_TONE[d.review_status] ?? "neutral"}>{label(REVIEW, d.review_status)}</StatusBadge></td>
                <td className={td}>{d.reason_code ?? "-"}</td>
              </tr>
            ))}
          </Table>
        </Fold>
        <Fold title="2. 범위·분석">
          <dl className="grid gap-2 text-sm sm:grid-cols-[8rem_1fr]">
            <dt className="text-muted-foreground">색인</dt><dd>{t.index_version} ({t.review_scope ?? "-"})</dd>
            <dt className="text-muted-foreground">질의 토큰</dt><dd className="flex flex-wrap gap-1">{t.query_tokens.map((q, i) => <span key={i} className="rounded bg-secondary px-1.5 text-xs">{q}</span>)}</dd>
            <dt className="text-muted-foreground">요구사항 코드</dt><dd>{t.codes.join(", ") || "없음"}</dd>
          </dl>
        </Fold>
        <Fold title={`3. 채널 순위 · 후보 ${r.candidates.length}개`}>
          <p className="mb-3 text-xs text-muted-foreground">요청 방식 {label(MODE, t.config.mode)} → 실제 {label(MODE, r.mode)}. 점수는 각 채널 안의 순위용 값이며 답변 신뢰도가 아닙니다.</p>
          <Table caption="채널 순위" head={["채널", "순위", "점수", "조각"]}>
            {r.candidates.map((c, i) => (
              <tr key={i}>
                <td className={td}>{c.channel}</td>
                <td className={cn(td, "tabular-nums")}>{c.rank}</td>
                <td className={cn(td, "tabular-nums")}>{c.score ?? "-"}</td>
                <td className={cn(td, "font-mono text-xs")}>{c.chunk_id.slice(0, 12)}</td>
              </tr>
            ))}
          </Table>
        </Fold>
        {r.excluded.length > 0 && (
          <Fold title={`제외된 후보 ${r.excluded.length}개`}>
            <Table caption="제외된 후보" head={["조각", "사유"]}>
              {r.excluded.map((x, i) => (
                <tr key={i}><td className={cn(td, "font-mono text-xs")}>{x.chunk_id.slice(0, 12)}</td><td className={td}>{label(EXCLUDED, x.reason)}</td></tr>
              ))}
            </Table>
          </Fold>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        <Button variant="outline" asChild>
          <a href={`/api/verify/traces/${t.run_id}/export`} download={`${t.run_id}.json`}><Download aria-hidden />실행 내보내기 (JSON)</a>
        </Button>
      </div>

      <Generate key={t.run_id} run={t} />
    </article>
  );
}

function Generate({ run }: { run: Run }) {
  const [agree, setAgree] = useState(false);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const status = usePoll(requestId ? `vgen-${requestId}` : null,
    () => must(api.GET("/api/requests/{request_id}", { params: { path: { request_id: requestId! } } }), errorText), 1000,
    (d) => !["queued", "running"].includes(d.view.status));
  const go = async () => {
    setError(null);
    const { data, error: e } = await api.POST("/api/verify/traces/{run_id}/generate", { params: { path: { run_id: run.run_id } } });
    if (e || !data) return setError(errorText(e));
    setRequestId(data.request_id);
  };
  const v: RequestView | undefined = status.data?.view;
  return (
    <section aria-label="답변 생성" className="space-y-3 rounded-2xl border p-5">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-[15px] font-bold">5. 답변 생성 <span className="font-normal text-warn">유료</span></h3>
        <span className="text-xs text-muted-foreground">최대 예상 {usd(run.estimate_micro_usd, 4)}</span>
      </div>
      {run.generation_block ? <Notice tone="warn">{run.generation_block}</Notice> : !requestId ? (
        <>
          <p className="text-sm text-muted-foreground">다시 검색하지 않고 4단계의 고정 근거 {run.retrieval.evidence.length}개와 기준일 {run.as_of ?? "-"}로 1회 실행합니다. 예약이 최대 예상 비용을 넘으면 호출 없이 거절됩니다.</p>
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} className="size-4 accent-primary" />
              이 범위로 유료 답변 생성을 1회 실행합니다
            </label>
            <Button size="lg" disabled={!agree} onClick={go}>유료 답변 생성</Button>
          </div>
        </>
      ) : !v || ["queued", "running"].includes(v.status) ? (
        <div aria-live="polite" className="flex items-center gap-3">
          <StatusBadge tone="neutral" size="md">{v ? REQUEST[v.status] : "요청 중"}</StatusBadge>
          <span className="text-sm text-muted-foreground">예약 최대 비용 {usd(v?.reserved_micro_usd, 4)}</span>
        </div>
      ) : v.result ? <AnswerView view={v} answer={v.result} /> : <p className="text-sm">{REQUEST[v.status]}: 결과가 없습니다.</p>}
      {error && <Notice tone="bad">{error}</Notice>}
    </section>
  );
}

// ---------------------------------------------------------------- two frozen runs

export function CompareRuns({ runs }: { runs: Summary[] }) {
  const [a, setA] = useState(runs[1]?.run_id ?? "");
  const [b, setB] = useState(runs[0]?.run_id ?? "");
  const diff = usePoll(a && b && a !== b ? `cmp-${a}-${b}` : null,
    () => must(api.GET("/api/verify/compare", { params: { query: { a, b } } }), errorText), null);
  if (runs.length < 2) return <Empty>비교하려면 검색 추적 실행이 두 개 이상 필요합니다.</Empty>;
  const name = (r: Summary) => `${r.question.slice(0, 30)} · ${stamp(r.created_at)}`;
  const d = diff.data;
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        {([["cmp-a", "실행 A", a, setA], ["cmp-b", "실행 B", b, setB]] as const).map(([id, text, value, set]) => (
          <Field key={id} id={id} label={text}>
            <select id={id} value={value} onChange={(e) => set(e.target.value)} className={cn(field, "h-10")}>
              {runs.map((r) => <option key={r.run_id} value={r.run_id}>{name(r)}</option>)}
            </select>
          </Field>
        ))}
      </div>
      {a === b && <p className="text-sm text-muted-foreground">서로 다른 실행을 고르세요.</p>}
      {diff.error && <Notice tone="bad">{diff.error}</Notice>}
      {d && (
        <>
          {!(d.same_question && d.same_scope) && <Notice tone="warn">질문 또는 문서 범위가 다릅니다. 설정 효과만 비교하려면 같은 질문과 범위를 쓰세요.</Notice>}
          <Table caption="실행 비교" head={["항목", "A", "B"]}>
            {Object.entries(d.config_changes).sort().map(([k, [va, vb]]) => (
              <tr key={k}><td className={cn(td, "font-medium")}>설정 {k}</td><td className={td}>{JSON.stringify(va)}</td><td className={td}>{JSON.stringify(vb)}</td></tr>
            ))}
            {([["근거 토큰", d.evidence_tokens], ["입력 추정 토큰", d.input_tokens], ["소요(ms)", d.total_ms]] as const).map(([k, [x, y]]) => (
              <tr key={k}><td className={cn(td, "font-medium")}>{k}</td><td className={cn(td, "tabular-nums")}>{x ?? "-"}</td><td className={cn(td, "tabular-nums")}>{y ?? "-"}</td></tr>
            ))}
            <tr><td className={cn(td, "font-medium")}>공통 근거</td><td className={cn(td, "tabular-nums")} colSpan={2}>{d.evidence_common.length}개</td></tr>
            <tr><td className={cn(td, "font-medium")}>한쪽에만 있는 근거</td><td className={cn(td, "tabular-nums")}>{d.evidence_only_a.length}개</td><td className={cn(td, "tabular-nums")}>{d.evidence_only_b.length}개</td></tr>
          </Table>
          {!Object.keys(d.config_changes).length && <p className="text-xs text-muted-foreground">설정 차이 없음</p>}
          <Table caption="상위 5개 순위" head={["순위", "A", "B"]}>
            {Array.from({ length: Math.max(d.top5_a.length, d.top5_b.length) }, (_, i) => (
              <tr key={i}>
                <td className={cn(td, "tabular-nums")}>{i + 1}</td>
                <td className={cn(td, "font-mono text-xs", d.top5_a[i] !== d.top5_b[i] && "bg-warn-bg")}>{d.top5_a[i]?.slice(0, 12) ?? "-"}</td>
                <td className={cn(td, "font-mono text-xs", d.top5_a[i] !== d.top5_b[i] && "bg-warn-bg")}>{d.top5_b[i]?.slice(0, 12) ?? "-"}</td>
              </tr>
            ))}
          </Table>
        </>
      )}
    </div>
  );
}
