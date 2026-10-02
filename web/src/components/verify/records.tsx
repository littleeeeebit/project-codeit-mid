"use client";

// 수집 상태, 수정 기록 and 요청 기록: what was ingested, the append-only corrections, and redacted request exports.

import { useState } from "react";
import { Download } from "lucide-react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { label, locationShort, REQUEST, REVIEW, REVIEW_TONE, stamp } from "@/lib/format";
import { must, usePoll } from "@/lib/use-poll";
import { Section, Table, td } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Empty, Field, field, Notice } from "./parts";

type Ingested = Schemas["IngestionRow"];
type Summary = Schemas["TraceSummary"];

export function IngestionSection({ rows }: { rows: Ingested[] }) {
  const [only, setOnly] = useState<string | null>(null);
  const counts = rows.reduce<Record<string, number>>((m, r) => ({ ...m, [r.review_status]: (m[r.review_status] ?? 0) + 1 }), {});
  const failed = rows.filter((r) => r.parse_status !== "parsed").length;
  const shown = only ? rows.filter((r) => r.review_status === only) : rows;
  return (
    <Section title={`수집 상태 · 문서 ${rows.length}개`} aside={failed ? <StatusBadge tone="bad">수집 실패 {failed}</StatusBadge> : <StatusBadge tone="ok">모두 수집됨</StatusBadge>}>
      <div role="group" aria-label="원문 대조 상태로 거르기" className="flex flex-wrap gap-1.5">
        {[null, ...Object.keys(counts)].map((k) => (
          <button key={k ?? "all"} type="button" aria-pressed={only === k} onClick={() => setOnly(k)}
                  className={cn("rounded-full border px-3 py-1 text-[13px] font-semibold outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
                    only === k ? "border-foreground bg-foreground text-background" : "hover:bg-secondary")}>
            {k ? `${label(REVIEW, k)} ${counts[k]}` : `전체 ${rows.length}`}
          </button>
        ))}
      </div>
      <Table caption="수집 상태" head={["파일", "형식", "수집", "원문 대조", "경고"]}>
        {shown.map((r) => (
          <tr key={r.doc_id}>
            <td className={cn(td, "max-w-md")}><span className="line-clamp-1" title={r.filename ?? ""}>{r.filename}</span></td>
            <td className={cn(td, "uppercase")}>{r.format}</td>
            <td className={td}>{r.parse_status === "parsed" ? <StatusBadge tone="ok">수집됨</StatusBadge> : <StatusBadge tone="bad">{r.reason_code ?? r.parse_status}</StatusBadge>}</td>
            <td className={td}><StatusBadge tone={REVIEW_TONE[r.review_status] ?? "neutral"}>{label(REVIEW, r.review_status)}</StatusBadge></td>
            <td className={cn(td, "text-xs")}>{r.warnings.filter(Boolean).join(", ") || "-"}</td>
          </tr>
        ))}
      </Table>
    </Section>
  );
}

export function CorrectionsSection({ runs }: { runs: Summary[] }) {
  const list = usePoll("corrections", () => must(api.GET("/api/verify/corrections"), errorText), null);
  return (
    <div className="space-y-8">
      <Section title="수정 기록 추가" aside={<span className="text-[13px] text-muted-foreground">추가만 됩니다 · 기존 데이터셋·실행·봉인 라벨은 바뀌지 않습니다</span>}>
        {runs.length ? <CorrectionForm runs={runs} onAdded={list.reload} /> : <Empty>검색 추적 실행이 있어야 근거를 지정할 수 있습니다.</Empty>}
      </Section>
      <Section title="수정 기록">
        {list.error && <Notice tone="bad">{list.error}</Notice>}
        {list.data?.length === 0 && <Empty>수정 기록이 없습니다.</Empty>}
        {!!list.data?.length && (
          <Table caption="수정 기록" head={["시각", "검토자", "사유", "대상", "인용"]}>
            {list.data.map((r, i) => (
              <tr key={i}>
                <td className={cn(td, "whitespace-nowrap tabular-nums")}>{stamp(r.created_at)}</td>
                <td className={td}>{r.reviewer}</td>
                <td className={td}>{r.reason}</td>
                <td className={cn(td, "font-mono text-xs")}>{r.run_id ?? r.request_id?.slice(0, 8) ?? "-"}</td>
                <td className={cn(td, "max-w-sm")}><span className="line-clamp-2">{r.quote}</span></td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}

function CorrectionForm({ runs, onAdded }: { runs: Summary[]; onAdded: () => void }) {
  const [runId, setRunId] = useState(runs[0].run_id);
  const run = usePoll(`corr-run-${runId}`, () => must(api.GET("/api/verify/traces/{run_id}", { params: { path: { run_id: runId } } }), errorText), null);
  const [evId, setEvId] = useState<string | null>(null);
  const [elId, setElId] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [quote, setQuote] = useState("");
  const [proposal, setProposal] = useState("{}");
  const [state, setState] = useState<{ busy?: boolean; error?: string; ok?: boolean }>({});
  const evidence = run.data?.retrieval.evidence ?? [];
  const ev = evidence.find((e) => e.evidence_id === evId) ?? evidence[0];
  const element = ev?.element_ids.includes(elId ?? "") ? elId! : ev?.element_ids[0];
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    let parsed: Record<string, unknown>;
    try { parsed = JSON.parse(proposal || "{}"); } catch { return setState({ error: "제안 내용이 올바른 JSON이 아닙니다." }); }
    setState({ busy: true });
    const { error } = await api.POST("/api/verify/corrections", { body: {
      run_id: runId, evidence_id: ev.evidence_id, element_id: element!, reason, quote, proposal: parsed } });
    if (error) return setState({ error: errorText(error) });
    setReason(""); setQuote(""); setProposal("{}");
    setState({ ok: true });
    onAdded();
  };
  return (
    <form onSubmit={submit} className="grid gap-4 md:grid-cols-3">
      <Field id="corr-run" label="대상 검색 추적 실행">
        <select id="corr-run" value={runId} onChange={(e) => { setRunId(e.target.value); setEvId(null); setElId(null); }} className={cn(field, "h-10")}>
          {runs.map((r) => <option key={r.run_id} value={r.run_id}>{r.question.slice(0, 30)} · {stamp(r.created_at)}</option>)}
        </select>
      </Field>
      <Field id="corr-ev" label="원문 근거">
        <select id="corr-ev" value={ev?.evidence_id ?? ""} onChange={(e) => { setEvId(e.target.value); setElId(null); }} disabled={!evidence.length} className={cn(field, "h-10")}>
          {evidence.map((e) => <option key={e.evidence_id} value={e.evidence_id}>{e.evidence_id} · {locationShort(e.location)}</option>)}
        </select>
      </Field>
      <Field id="corr-el" label="원문 요소">
        <select id="corr-el" value={element ?? ""} onChange={(e) => setElId(e.target.value)} disabled={!ev} className={cn(field, "h-10")}>
          {ev?.element_ids.map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
      </Field>
      <div className="md:col-span-3"><Field id="corr-reason" label="사유">
        <input id="corr-reason" required value={reason} onChange={(e) => setReason(e.target.value)} className={cn(field, "h-10")} />
      </Field></div>
      <div className="md:col-span-2"><Field id="corr-quote" label="원문 인용 (그대로)">
        <textarea id="corr-quote" required rows={3} value={quote} onChange={(e) => setQuote(e.target.value)} className={cn(field, "py-2")} />
      </Field></div>
      <Field id="corr-proposal" label="제안 내용 (JSON)">
        <textarea id="corr-proposal" rows={3} value={proposal} onChange={(e) => setProposal(e.target.value)} className={cn(field, "py-2 font-mono text-xs")} />
      </Field>
      <div className="flex flex-wrap items-center gap-3 md:col-span-3">
        <Button type="submit" size="lg" disabled={state.busy || !ev}>수정 기록 추가</Button>
        {state.ok && <span role="status" className="text-sm font-medium text-ok">기록했습니다.</span>}
        {state.error && <span role="alert" className="text-sm font-medium text-bad">{state.error}</span>}
      </div>
    </form>
  );
}

export function RequestExports() {
  const list = usePoll("all-requests", () => must(api.GET("/api/requests", { params: { query: { limit: 50, all_members: true } } }), errorText), null);
  return (
    <Section title="요청 기록 내보내기" aside={<span className="text-[13px] text-muted-foreground">모든 사람의 요청 · 자격 증명과 로컬 경로는 지운 JSON</span>}>
      {list.error && <Notice tone="bad">{list.error}</Notice>}
      {list.data?.length === 0 && <Empty>요청 기록이 없습니다.</Empty>}
      {!!list.data?.length && (
        <Table caption="요청 기록" head={["요청", "이름", "상태", "질문", ""]}>
          {list.data.map((r) => (
            <tr key={r.request_id}>
              <td className={cn(td, "font-mono text-xs whitespace-nowrap")}>{r.request_id.slice(0, 8)} · {stamp(r.created_at)}</td>
              <td className={td}>{r.member_id}</td>
              <td className={td}>{REQUEST[r.status] ?? r.status}</td>
              <td className={cn(td, "max-w-sm")}><span className="line-clamp-1">{r.question || r.mode}</span></td>
              <td className={td}>
                <Button variant="outline" size="sm" asChild>
                  <a href={`/api/requests/${r.request_id}/export`} download={`request-${r.request_id}.json`} aria-label={`요청 ${r.request_id.slice(0, 8)} JSON 받기`}>
                    <Download aria-hidden />JSON
                  </a>
                </Button>
              </td>
            </tr>
          ))}
        </Table>
      )}
    </Section>
  );
}
