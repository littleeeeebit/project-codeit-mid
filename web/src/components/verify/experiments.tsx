"use client";

// 실험 비교. Who comes here: the person who picks what serves. Pipelines ran every variant (`compare --matrix`);
// this page puts their numbers side by side and switches serving to the row they pick. Reading order: what serves
// now, the matrix, the best value of its first quality column, then the table itself (sortable, best value per
// column and the serving row marked). Opening a row shows the questions it failed and the activation; model
// identities and hashes stay folded.

import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { useMember } from "@/lib/member";
import { must, usePoll } from "@/lib/use-poll";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Empty, Field, field, Notice } from "./parts";

type Table = Schemas["ExperimentTable"];
type Row = Schemas["ExperimentRow"];
type Column = Schemas["ExperimentColumn"];
type Question = Schemas["ExperimentQuestion"];
type Value = Row["values"][string];

const MATRIX: Record<string, string> = { lexical: "K0 · K1", chunking: "청킹", embedding: "임베딩", reranker: "리랭커", regression: "회귀 (유지보수)" };
const COLUMN: Record<string, string> = {
  "dev.ndcg": "nDCG@5", "dev.support": "근거 완전", "whole.ndcg": "nDCG@5 전체 문서", "whole.support": "근거 완전 전체 문서",
  "needle.top5": "바늘 상위 5", "dev.critical": "치명 실패", "whole.critical": "치명 실패 전체 문서",
  "dev.qualifier_losses": "조건 손실", "dev.p95_ms": "검색 p95", chunks: "청크 수", duplicated_tokens: "중복 토큰",
  new_critical_vs_k1: "K1에 없던 치명", truncated_chunks: "잘린 청크", query_p50_ms: "질의 임베딩 p50",
  query_p95_ms: "질의 임베딩 p95", corpus_seconds: "코퍼스 임베딩", storage_mb: "벡터 저장", cost_usd: "비용",
  licence: "라이선스", recall20_before: "recall@20 전", "dev.recall20": "recall@20 후",
  new_critical_vs_h: "하이브리드에 없던 치명", truncated_pairs: "잘린 쌍", p95_alone_ms: "재정렬 p95 단독",
  p95_loaded_ms: "재정렬 p95 6명 동시", gate: "기존 관문",
};
const RATE = new Set(["dev.support", "whole.support", "needle.top5"]);
const SCORE = new Set(["dev.ndcg", "whole.ndcg", "dev.recall20", "recall20_before"]);
const MS = new Set(["dev.p95_ms", "query_p50_ms", "query_p95_ms", "p95_alone_ms", "p95_loaded_ms"]);
const AXIS: Record<string, Record<string, string>> = {
  retrieval: { dense: "밀집만", hybrid: "하이브리드", keyword: "키워드", hybrid_rerank: "재정렬" },
  rerank_mode: { whole: "전체 재정렬", below_head: "BM25 상위 6 고정" },
  analyzer: { whitespace: "K0 공백 분리", kiwi: "K1 Kiwi 형태소" },
};
const STATUS: Record<string, string> = { failed: "실행 실패", needs_approval: "비용 승인 대기", not_run: "아직 측정 안 함" };
const MODE: Record<string, string> = {
  kiwi_bm25: "키워드 Kiwi BM25", keyword: "키워드", dense: "밀집만", hybrid: "하이브리드", hybrid_rerank: "하이브리드 + 재정렬",
};
const FIXED: Record<string, string> = { retrieval: "검색", analyzer: "분석기", embedding: "임베딩", fusion: "융합", depth: "후보", units: "근거 단위" };
const POP: Record<string, string> = { dev: "개발 질문 · 자기 문서", whole: "전체 문서" };

function fmt(key: string, v: Value): string {
  if (v == null) return "-";
  if (typeof v === "string") return key === "gate" ? (v === "pass" ? "통과" : "미달") : v;
  if (RATE.has(key)) return `${(v * 100).toFixed(1)}%`;
  if (SCORE.has(key)) return v.toFixed(3);
  if (MS.has(key)) return `${v.toFixed(v < 100 ? 1 : 0)}ms`;
  if (key === "corpus_seconds") return v < 120 ? `${v.toFixed(0)}초` : `${(v / 60).toFixed(1)}분`;
  if (key === "storage_mb") return `${v.toFixed(0)}MB`;
  if (key === "cost_usd") return v === 0 ? "무료" : `$${v.toFixed(4)}`;
  return v.toLocaleString("ko-KR");
}

/** What distinguishes the row inside its matrix: the varied axes, in words. */
function rowTitle(t: Table, r: Row): string {
  const varied = Object.keys(r.axes).filter((a) => new Set(t.rows.map((x) => String(x.axes[a]))).size > 1);
  return (varied.length ? varied : ["profile"]).map((a) => {
    const v = r.axes[a];
    return AXIS[a]?.[String(v)] ?? String(v ?? "-");
  }).join(" · ");
}

/** The column's best value among complete rows and the rows holding it. */
function top(t: Table, c: Column): { value: Value; rows: Row[]; complete: number } {
  const done = t.rows.filter((r) => r.status === "complete");
  const nums = done.map((r) => r.values[c.key]).filter((v): v is number => typeof v === "number");
  if (!c.better || !nums.length) return { value: null, rows: [], complete: done.length };
  const value = c.better === "high" ? Math.max(...nums) : Math.min(...nums);
  return { value, rows: done.filter((r) => r.values[c.key] === value), complete: done.length };
}

/** Marked only when it sets rows apart: a best value held by more than half the rows marks nothing. */
function best(t: Table, c: Column): Value {
  const b = top(t, c);
  return b.rows.length * 2 <= b.complete ? b.value : null;
}

export function ExperimentsSection() {
  const data = usePoll("experiments", () => must(api.GET("/api/verify/experiments"), errorText), null);
  const [matrix, setMatrix] = useState<string | null>(null);
  if (data.error) return <Notice tone="bad">{data.error}</Notice>;
  if (!data.data) return <Skeleton className="h-96 w-full" />;
  const tables = data.data.tables;
  if (!tables.length) return <Empty>비교표가 아직 없습니다. 소유자가 <code>compare --matrix lexical</code>부터 실행하면 여기에 열립니다.</Empty>;
  const cur = tables.find((t) => t.matrix === matrix) ?? tables[0];
  const sd = data.data.serving_detail;
  const servingName = [MODE[sd.mode ?? ""] ?? sd.mode ?? "키워드 기본값", sd.embedding,
    sd.reranker && `${sd.reranker}${sd.protect ? ` (BM25 상위 ${sd.protect} 고정)` : ""}`].filter(Boolean).join(" · ");
  return (
    <div className="space-y-12">
      <section aria-labelledby="serving-now" className="space-y-2">
        <p className="text-sm font-medium text-muted-foreground">지금 서비스 중</p>
        <h3 id="serving-now" className="text-3xl font-bold tracking-tight [overflow-wrap:anywhere]">{servingName}</h3>
        {sd.fallback && <p className="text-base text-warn">활성화한 실행을 쓰지 못해 키워드 기본값으로 답합니다: {sd.fallback}</p>}
        {data.data.active_run_id && <p className="text-[13px] text-muted-foreground">실행 <span className="font-mono">{data.data.active_run_id}</span></p>}
      </section>
      <div role="tablist" aria-label="비교 축" className="flex flex-wrap gap-x-6 gap-y-2 border-b border-input">
        {tables.map((t) => (
          <button key={t.matrix} role="tab" type="button" aria-selected={t === cur} onClick={() => setMatrix(t.matrix)}
                  className={cn("-mb-px min-h-11 border-b-2 px-1 text-base font-semibold outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
                    t === cur ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground")}>
            {MATRIX[t.matrix] ?? t.title}<span className="ml-1.5 text-sm font-normal tabular-nums">{t.rows.length}</span>
          </button>
        ))}
      </div>
      <MatrixView key={cur.matrix} t={cur} onActivated={data.reload} />
      {data.data.golden_counts && <GoldenCounts g={data.data.golden_counts} />}
    </div>
  );
}

function Headline({ t }: { t: Table }) {
  const lead = t.columns.find((c) => c.better === "high");
  if (!lead) return null;
  const { value: topValue, rows: winners } = top(t, lead);
  const serving = t.rows.find((r) => r.active);
  if (topValue == null) return null;
  return (
    <div className="space-y-1">
      <p className="text-sm font-medium text-muted-foreground">가장 높은 {COLUMN[lead.key] ?? lead.label}</p>
      <p className="text-4xl font-bold tabular-nums tracking-tight">{fmt(lead.key, topValue)}</p>
      <p className="text-base [overflow-wrap:anywhere]">{winners.length > 3 ? `${winners.length}개 행이 같은 값` : winners.map((r) => rowTitle(t, r)).join(", ")}
        {serving && !winners.includes(serving) && <span className="text-muted-foreground"> · 서비스 중인 행은 {fmt(lead.key, serving.values[lead.key])}</span>}
        {serving && winners.includes(serving) && <span className="text-muted-foreground"> · 지금 서비스 중</span>}
      </p>
    </div>
  );
}

function MatrixView({ t, onActivated }: { t: Table; onActivated: () => void }) {
  const [sort, setSort] = useState<{ key: string; desc: boolean } | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const bests = useMemo(() => Object.fromEntries(t.columns.map((c) => [c.key, best(t, c)])), [t]);
  const rows = useMemo(() => {
    const done = t.rows.filter((r) => r.status === "complete");
    const rest = t.rows.filter((r) => r.status !== "complete");
    if (sort) {
      done.sort((a, b) => {
        const x = a.values[sort.key], y = b.values[sort.key];
        if (x == null || y == null) return x == null ? 1 : -1;
        const d = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
        return sort.desc ? -d : d;
      });
    }
    return [...done, ...rest];  // rows without numbers stay below, with their reason
  }, [t, sort]);
  const cur = t.rows.find((r) => r.index === open);
  const toggle = (c: Column) => setSort((s) => s?.key === c.key ? { key: c.key, desc: !s.desc } : { key: c.key, desc: c.better !== "low" });
  return (
    <section aria-label={`${MATRIX[t.matrix] ?? t.title} 비교표`} className="space-y-6">
      <Headline t={t} />
      <div className="relative overflow-x-auto rounded-xl border border-input">
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">{MATRIX[t.matrix] ?? t.title} 비교. 열 제목을 누르면 정렬되고, 행을 누르면 실패한 질문과 활성화가 열립니다.</caption>
          <thead className="bg-secondary/70">
            <tr>
              <th scope="col" className="sticky left-0 z-10 bg-secondary px-3 py-3 text-left font-semibold">행</th>
              {t.columns.map((c) => (
                <th key={c.key} scope="col" aria-sort={sort?.key === c.key ? (sort.desc ? "descending" : "ascending") : undefined}
                    className="px-2 py-1 text-right font-semibold">
                  <button type="button" onClick={() => toggle(c)} className="inline-flex min-h-11 items-center gap-1 whitespace-nowrap rounded px-1 outline-none hover:text-primary focus-visible:ring-3 focus-visible:ring-ring/50">
                    {COLUMN[c.key] ?? c.label}<span aria-hidden className="w-3 text-xs text-muted-foreground">{sort?.key === c.key ? (sort.desc ? "↓" : "↑") : ""}</span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-input">
            {rows.map((r) => {
              const selected = r.index === open;
              const done = r.status === "complete";
              return (
                <tr key={r.index} className={cn(selected ? "bg-accent" : "group hover:bg-secondary", r.active && "border-l-4 border-l-primary")}>
                  <th scope="row" className={cn("sticky left-0 z-10 min-w-[15rem] max-w-[20rem] px-3 py-2 text-left font-medium", selected ? "bg-accent" : "bg-background group-hover:bg-secondary")}>
                    <button type="button" aria-expanded={selected} onClick={() => setOpen(selected ? null : r.index)}
                            className="min-h-11 w-full text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50 [overflow-wrap:anywhere]">
                      {rowTitle(t, r)}
                      {r.active && <span className="block text-xs font-semibold text-primary">서비스 중</span>}
                    </button>
                  </th>
                  {done ? t.columns.map((c) => {
                    const v = r.values[c.key];
                    const isBest = c.better && v != null && v === bests[c.key];
                    return <td key={c.key} className={cn("whitespace-nowrap px-3 py-2 text-right tabular-nums", isBest ? "font-bold text-primary" : "text-foreground")}>
                      {fmt(c.key, v)}{isBest && <span className="sr-only"> (최고)</span>}
                    </td>;
                  }) : (
                    <td colSpan={t.columns.length} className="px-3 py-2 text-[13px] text-muted-foreground">
                      <span className="font-semibold text-warn">{STATUS[r.status] ?? r.status}</span>
                      {r.status === "needs_approval" && r.estimate ? ` · 최대 $${(Number(r.estimate.total_micro_usd) / 1e6).toFixed(4)}, ${Number(r.estimate.corpus_tokens ?? 0).toLocaleString("ko-KR")} 토큰` : ""}
                      {r.reason ? ` · ${r.reason}` : ""}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-[13px] text-muted-foreground">굵은 파란 값이 열마다 가장 좋은 값입니다. 절반이 넘는 행이 같은 값이면 표시하지 않습니다. 개발 질문은 자기 문서 안에서, 전체 문서 열은 개발 질문과 바늘 질문을 모든 문서에서 검색한 결과입니다. 고정값: {Object.entries(t.fixed).map(([k, v]) => `${FIXED[k] ?? k} ${AXIS[k]?.[String(v)] ?? MODE[String(v)] ?? String(v)}`).join(", ") || "서비스 설정"}.
        {t.matrix === "embedding" && " API 모델의 질의 임베딩 시간은 비용 장부에 남은 질문 호출의 왕복 시간입니다."}
        {String(t.fixed.fusion ?? "").startsWith("keyword_first") && " 융합이 BM25 상위 6개를 제자리에 두므로 하이브리드와 상위 고정 행의 nDCG@5는 K1과 같습니다. 차이는 그 뒤 근거에서 나며, 근거 완전과 치명 실패 열에 보입니다."}</p>
      {cur && <RowDetail key={cur.index} t={t} r={cur} onActivated={onActivated} />}
    </section>
  );
}

function RowDetail({ t, r, onActivated }: { t: Table; r: Row; onActivated: () => void }) {
  const qs = usePoll(r.status === "complete" ? `experiment-q:${t.matrix}:${r.index}` : null, () => must(api.GET(
    "/api/verify/experiments/{matrix}/{index}/questions", { params: { path: { matrix: t.matrix, index: r.index } } }), errorText), null);
  const ref = useRef<HTMLElement>(null);
  useEffect(() => ref.current?.scrollIntoView({ behavior: "smooth", block: "start" }), []);  // the detail opens below the table
  return (
    <section ref={ref} aria-labelledby="experiment-row" className="scroll-mt-20 space-y-8 border-t border-input pt-8">
      <div className="space-y-1">
        <p className="text-sm font-medium text-muted-foreground">{MATRIX[t.matrix] ?? t.title}</p>
        <h4 id="experiment-row" className="text-2xl font-bold tracking-tight [overflow-wrap:anywhere]">{rowTitle(t, r)}</h4>
        {r.status !== "complete" && <p className="text-base text-warn">{STATUS[r.status] ?? r.status}{r.reason ? `: ${r.reason}` : ""}</p>}
      </div>
      {r.status === "complete" && (
        <div className="grid gap-10 xl:grid-cols-[minmax(0,1fr)_22rem]">
          <Failures q={qs} />
          <Activate r={r} onActivated={onActivated} />
        </div>
      )}
      <Facts r={r} />
    </section>
  );
}

const FLAG = (q: Question) => [
  !q.complete && "근거 불완전", q.hit5 === 0 && "상위 5 밖",
  q.qualifier_loss > 0 && `조건 ${q.qualifier_loss}개 잘림`, q.ndcg != null && q.ndcg < 1 && `nDCG ${q.ndcg.toFixed(2)}`,
].filter(Boolean) as string[];

const SHOWN = 10;

function Failures({ q }: { q: { data?: Question[]; error?: string } }) {
  const [pop, setPop] = useState("dev");
  const [all, setAll] = useState(false);
  if (q.error) return <Notice tone="bad">{q.error}</Notice>;
  if (!q.data) return <Skeleton className="h-48 w-full" />;
  const missed = q.data.filter((x) => x.population === pop);
  const shown = all ? missed : missed.slice(0, SHOWN);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h5 className="text-lg font-bold">놓친 질문 <span className="tabular-nums">{missed.length}</span>건</h5>
        <label className="flex items-center gap-2 text-sm"><span className="font-semibold">범위</span>
          <select value={pop} onChange={(e) => { setPop(e.target.value); setAll(false); }} className={cn(field, "h-11 w-auto")}>
            {Object.entries(POP).map(([k, v]) => <option key={k} value={k}>{v} · {q.data!.filter((x) => x.population === k).length}건</option>)}
          </select>
        </label>
      </div>
      {!shown.length ? <Empty>이 범위에서 놓친 질문이 없습니다.</Empty> : (
        <ul className="divide-y divide-input">
          {shown.map((x) => (
            <li key={`${x.population}:${x.id}`} className="space-y-1 py-3">
              <p className="text-base">{x.question ?? x.id}</p>
              <p className="text-[13px] text-muted-foreground">
                {x.critical && <span className="font-semibold text-bad">치명{FLAG(x).length ? " · " : ""}</span>}{FLAG(x).join(" · ")}
                {x.missing.length > 0 && ` · 빠진 근거 ${x.missing.join(", ")}`}
              </p>
            </li>
          ))}
        </ul>
      )}
      {missed.length > SHOWN && (
        <Button variant="outline" onClick={() => setAll(!all)}>{all ? "처음 10건만 보기" : `나머지 ${missed.length - SHOWN}건 보기`}</Button>
      )}
    </div>
  );
}

function Activate({ r, onActivated }: { r: Row; onActivated: () => void }) {
  const member = useMember();
  const [name, setName] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [state, setState] = useState<{ busy: boolean; error?: string; done?: string }>({ busy: false });
  if (r.active) return <p className="text-base font-semibold text-primary">이 행이 지금 서비스 중입니다.</p>;
  if (!r.run_id) return null;
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setState({ busy: true });
    const { data, error } = await api.POST("/api/verify/experiments/activate", {
      body: { run_id: r.run_id!, decided_by: (name ?? member).trim(), note },
    });
    if (error) setState({ busy: false, error: errorText(error) });
    else { setState({ busy: false, done: data.activated_at }); onActivated(); }
  };
  return (
    <form onSubmit={submit} className="space-y-4">
      <h5 className="text-lg font-bold">이 행으로 서비스 전환</h5>
      <p className="text-sm text-muted-foreground">다음 질문부터 이 설정으로 검색합니다. 이전 설정은 활성화 기록에 남아 언제든 되돌릴 수 있습니다.</p>
      <Field id="activate-name" label="고른 사람">
        <input id="activate-name" required value={name ?? member} onChange={(e) => setName(e.target.value)} className={cn(field, "h-11")} />
      </Field>
      <Field id="activate-note" label="메모 (선택)">
        <textarea id="activate-note" value={note} onChange={(e) => setNote(e.target.value)} rows={3} className={cn(field, "py-2")} />
      </Field>
      <Button type="submit" size="lg" disabled={state.busy || !(name ?? member).trim()}>{state.busy ? "전환 중" : "서비스 전환"}</Button>
      {state.error && <Notice tone="bad">{state.error}</Notice>}
      {state.done && <Notice tone="ok">전환했습니다.</Notice>}
    </form>
  );
}

const FACT: Record<string, string> = {
  licence: "라이선스", revision: "리비전", dims: "차원", context: "최대 입력 토큰", backend: "실행 위치", size_bytes: "가중치 크기",
  peak_vram_mb: "최대 GPU 메모리", precision: "정밀도", max_length: "입력 길이", layer: "레이어 컷오프", batch: "GPU에 맞춘 배치",
  cold_load_seconds: "첫 로드", chunks: "청크 수", duplicated_tokens: "중복 토큰", dense_version: "벡터 세트",
};

function factText(k: string, v: unknown): string {
  if (k === "size_bytes") return `${(Number(v) / 2 ** 30).toFixed(2)}GB`;
  if (k === "peak_vram_mb") return `${(Number(v) / 1024).toFixed(2)}GB`;
  if (k === "cold_load_seconds") return `${Number(v).toFixed(1)}초`;
  if (k === "backend") return ({ local: "이 PC의 GPU", openai: "OpenAI API", gemini: "Gemini API" } as Record<string, string>)[String(v)] ?? String(v);
  return typeof v === "number" ? v.toLocaleString("ko-KR") : String(v);
}

function Facts({ r }: { r: Row }) {
  const entries = Object.entries(r.facts);
  if (!entries.length && !r.run_id) return null;
  return (
    <details className="group text-sm">
      <summary className="inline-flex min-h-11 cursor-pointer list-none items-center gap-1.5 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 [&::-webkit-details-marker]:hidden">
        <span aria-hidden className="transition-transform group-open:rotate-90">›</span>모델과 실행 정보
      </summary>
      <dl className="grid gap-x-8 gap-y-2 pt-3 sm:grid-cols-[auto_minmax(0,1fr)]">
        {entries.map(([k, v]) => (
          <div key={k} className="contents"><dt className="text-muted-foreground">{FACT[k] ?? k}</dt><dd className="font-mono text-[13px] [overflow-wrap:anywhere]">{factText(k, v)}</dd></div>
        ))}
        {r.run_id && <div className="contents"><dt className="text-muted-foreground">실행</dt><dd className="font-mono text-[13px]">{r.run_id}</dd></div>}
      </dl>
    </details>
  );
}

type Golden = { live: { dataset: string; status: string; n: number }[]; pilot_archive: { dataset: string; status: string; n: number }[];
  judge_reference: { items?: number | null }; development_count: { live_approved: number; release_report: number }; explanation: string };
const SET: Record<string, string> = { dev: "개발", test: "봉인", corpus: "바늘", "dev-pilot": "후보" };
const GSTATUS: Record<string, string> = { approved: "승인", pending: "대기", rejected: "거절" };

function GoldenCounts({ g }: { g: Record<string, unknown> }) {
  const d = g as unknown as Golden;
  const sets = [...new Set(d.live.map((x) => x.dataset))];
  return (
    <details className="group">
      <summary className="inline-flex min-h-11 cursor-pointer list-none items-center gap-1.5 text-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 [&::-webkit-details-marker]:hidden">
        <span aria-hidden className="transition-transform group-open:rotate-90">›</span>골든셋 행 수 · 개발 {d.development_count.live_approved}행
      </summary>
      <div className="max-w-3xl space-y-3 pt-3 text-sm">
        <ul className="space-y-1">
          {sets.map((s) => (
            <li key={s}><span className="font-semibold">{SET[s] ?? s}</span> {d.live.filter((x) => x.dataset === s).map((x) => `${GSTATUS[x.status] ?? x.status} ${x.n}`).join(" · ")}</li>
          ))}
          {d.judge_reference.items ? <li><span className="font-semibold">판정 기준</span> {d.judge_reference.items}개</li> : null}
          {d.pilot_archive.length > 0 && <li><span className="font-semibold">4단계 파일럿 보관본</span> {d.pilot_archive.map((x) => `${SET[x.dataset] ?? x.dataset} ${GSTATUS[x.status] ?? x.status} ${x.n}`).join(" · ")}</li>}
        </ul>
        <p className="text-muted-foreground">{d.explanation}</p>
      </div>
    </details>
  );
}
