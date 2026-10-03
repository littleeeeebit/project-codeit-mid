"use client";

// 수집 상태, 수정 기록 and 요청 기록: what was ingested, the append-only corrections, and redacted request exports.

import { useState } from "react";
import { Download } from "lucide-react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { label, REQUEST, REVIEW, REVIEW_TONE, stamp } from "@/lib/format";
import { must, usePoll } from "@/lib/use-poll";
import { Section, Table, td } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Empty, Field, field, Notice } from "./parts";
import { Skeleton } from "@/components/ui/skeleton";

type Ingested = Schemas["IngestionRow"];
const INGESTION_WARNING: Record<string, string> = {
  converter_stderr: "변환기 메시지 있음", page_blank_or_image: "텍스트 없는 쪽 · 빈 쪽 또는 이미지",
};
const QUESTION_MODE: Record<string, string> = { inventory: "요구사항 목록", metadata: "기본 정보", single: "문서 질문", compare: "두 문서 비교" };

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
            <td className={cn(td, "max-w-md text-base [overflow-wrap:anywhere]")}>{r.filename}</td>
            <td className={cn(td, "uppercase")}>{r.format}</td>
            <td className={td}>{r.parse_status === "parsed" ? <StatusBadge tone="ok">수집됨</StatusBadge> : <StatusBadge tone="bad">{r.reason_code ?? r.parse_status}</StatusBadge>}</td>
            <td className={td}><StatusBadge tone={REVIEW_TONE[r.review_status] ?? "neutral"}>{label(REVIEW, r.review_status)}</StatusBadge></td>
            <td className={td}>{[...new Set(r.warnings.filter(Boolean))].map((w) => label(INGESTION_WARNING, w)).join(", ") || "-"}</td>
          </tr>
        ))}
      </Table>
    </Section>
  );
}

const ACTIONS: Record<string, string> = {
  ...REVIEW, "decision:approve": "초안 승인", "decision:reject": "초안 거절",
  "decision:approve:disputed": "초안 승인 · 2차 검토 필요",
  "second:agree": "2차 검토 동의", "second:disagree": "2차 검토 이견", unapplied: "수정 제안 · 미적용",
};

function historyLocation(value: unknown): string {
  if (typeof value !== "object" || !value) return String(value);
  const location = value as Record<string, unknown>;
  return [location.page ? `${location.page}쪽` : "쪽 미상", location.element_id ? `요소 ${location.element_id}` : "",
    location.cell ? `셀 ${location.cell}` : "", location.check ? String(location.check) : "",
    location.findings === 0 ? "자동 대조 불일치 없음" : ""].filter(Boolean).join(" · ");
}

export function CorrectionsSection() {
  const list = usePoll("verification-history", () => must(api.GET("/api/verify/history", { params: { query: { limit: 200 } } }), errorText), 5000);
  const [kind, setKind] = useState("");
  const [query, setQuery] = useState("");
  const rows = (list.data ?? []).filter((r) => (!kind || r.kind === kind) &&
    `${r.target} ${r.reviewer} ${r.note}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return (
    <Section title="저장된 변경·검토" aside={<span role="status" className="text-[13px] text-muted-foreground">{list.error ? "갱신 실패" : "5초마다 자동 갱신"}</span>}>
      <p className="text-sm">원문 대조와 초안 승인·거절, 2차 검토를 저장하면 이곳에 자동으로 표시됩니다.</p>
      <div className="grid gap-3 sm:grid-cols-[12rem_minmax(0,1fr)]">
        <Field id="history-kind" label="기록 종류">
          <select id="history-kind" value={kind} onChange={(e) => setKind(e.target.value)} className={cn(field, "h-11")}>
            <option value="">전체</option><option value="source">원문 대조</option><option value="gold">초안·2차 검토</option><option value="proposal">이전 수정 제안</option>
          </select>
        </Field>
        <Field id="history-query" label="대상·검토자·메모 검색">
          <input id="history-query" value={query} onChange={(e) => setQuery(e.target.value)} className={cn(field, "h-11")} />
        </Field>
      </div>
      <div className="flex flex-wrap justify-between gap-2 text-sm">
        <span className="font-semibold">{list.data ? `${rows.length}건` : "불러오는 중"}</span><span className="text-muted-foreground">최근 200건 · 최신순</span>
      </div>
        {list.error && <Notice tone="bad">{list.error}</Notice>}
        {list.loading && <Skeleton className="h-48 w-full" />}
        {list.data && !rows.length && <Empty>{list.data.length ? "조건에 맞는 기록이 없습니다." : "아직 저장된 변경·검토가 없습니다."}</Empty>}
        {!!rows.length && (
          <ol className="divide-y rounded-xl border border-input">
            {rows.map((r) => (
              <li key={r.event_id} className="space-y-3 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <StatusBadge tone={r.kind === "proposal" ? "warn" : r.action.includes("reject") || r.action.includes("disagree") ? "warn" : "neutral"}>{label(ACTIONS, r.action)}</StatusBadge>
                  <span className="text-[13px] text-muted-foreground tabular-nums">{stamp(r.created_at)} · {r.reviewer}</span>
                </div>
                <p className="text-base font-semibold [overflow-wrap:anywhere]">{r.target}</p>
                <dl className="grid grid-cols-[3rem_minmax(0,1fr)] gap-2 text-sm">
                  <dt className="text-muted-foreground">메모</dt><dd className="whitespace-pre-wrap [overflow-wrap:anywhere]">{r.note || "메모 없음"}</dd>
                </dl>
                {(r.quote || r.locations.length > 0) && <details className="rounded-lg border">
                  <summary className="min-h-11 cursor-pointer px-3 py-2.5 text-sm font-semibold outline-none focus-visible:ring-3 focus-visible:ring-ring/50">{r.quote ? "제안 당시 인용 · 원문에 적용되지 않음" : `대조한 위치 ${r.locations.length}곳`}</summary>
                  <div className="space-y-2 border-t p-3 text-sm">
                    {r.quote && <blockquote className="whitespace-pre-wrap text-base">{r.quote}</blockquote>}
                    {r.locations.map((x, i) => <p key={i}>{historyLocation(x)}</p>)}
                  </div>
                </details>}
              </li>
            ))}
          </ol>
        )}
    </Section>
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
              <td className={cn(td, "max-w-sm text-base [overflow-wrap:anywhere]")}>{r.question || label(QUESTION_MODE, r.mode)}</td>
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
