"use client";

import { useState } from "react";
import { AlertTriangle, Download, Info, OctagonX } from "lucide-react";
import { cn } from "cn";
import { type Answer, api, errorText, type Evidence, originalHref, type RequestView } from "@/lib/api";
import {
  BILLING, EVIDENCE_WARNING, FACT_STATE, FIELD, label, locationShort, locationText, MISSING_REASON, REQUEST, REVIEW,
  STATUS, usd,
  when, won,
} from "@/lib/format";
import { must, usePoll } from "@/lib/use-poll";
import { Section, Table, td } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Skeleton } from "@/components/ui/skeleton";

/** Which selected document (1 or 2) a claim belongs to, in a comparison. */
export function docLabel(a: Answer, docId: string): string {
  const order = a.coverage.map((c) => c.doc_id);
  return order.length > 1 && order.includes(docId) ? `문서 ${order.indexOf(docId) + 1}` : "";
}

export function citedIds(a: Answer, ids: string[]): string[] {
  return ids.filter((id) => id in a.evidence);
}

/** The state badge and the conclusion; every state has its own body (DESIGN.md, result states). */
export function StateHead({ answer, large = false }: { answer: Answer; large?: boolean }) {
  const s = STATUS[answer.status] ?? { label: answer.status, tone: "neutral" as const };
  if (answer.status === "technical_error") {
    return (
      <div role="alert" className="flex gap-3 rounded-xl border border-bad/30 bg-bad-bg p-4 text-bad">
        <OctagonX className="mt-0.5 size-5 shrink-0" aria-hidden />
        <div className="space-y-1">
          <p className="font-semibold">기술 오류로 답변을 만들지 못했습니다</p>
          <p className="text-sm">{answer.summary}</p>
          {answer.error && <p className="font-mono text-xs opacity-80">사유 코드: {answer.error.slice(0, 160)}</p>}
        </div>
      </div>
    );
  }
  if (answer.status === "budget_blocked") {
    return (
      <div className="space-y-2 rounded-xl border border-bad/30 bg-bad-bg p-4">
        <StatusBadge tone="bad" size="md">{s.label}</StatusBadge>
        <p className="text-[15px] text-foreground">{answer.summary}</p>
        <p className="text-sm text-muted-foreground">검색, 기본 정보, 요구사항 목록, 원문 열람은 계속 무료로 쓸 수 있습니다.</p>
      </div>
    );
  }
  if (answer.status === "clarification_required") {
    return (
      <div className="space-y-2">
        <StatusBadge tone={s.tone} size="md">{s.label}</StatusBadge>
        <div className="flex gap-3 rounded-xl bg-info-bg p-4 text-foreground">
          <Info className="mt-0.5 size-5 shrink-0 text-info" aria-hidden />
          <div className="space-y-1">
            <p className={cn("font-semibold", large && "text-lg")}>{answer.summary}</p>
            {answer.next_action && <p className="text-sm">다음 확인: {answer.next_action}</p>}
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <StatusBadge tone={s.tone} size="md">{s.label}</StatusBadge>
      <p className={cn("leading-relaxed font-semibold text-foreground", large ? "text-[22px] leading-snug" : "text-lg")}>
        {answer.summary}
      </p>
    </div>
  );
}

export function ConflictTable({ answer, onCite }: { answer: Answer; onCite: (id: string) => void }) {
  if (!answer.conflicts.length) return null;
  const pair = answer.coverage.length > 1;
  return (
    <Section title="서로 다른 값">
      <Table caption="서로 다른 값" head={pair ? ["항목", "문서", "값", "근거"] : ["항목", "값", "근거"]}>
        {answer.conflicts.flatMap((c) => c.alternatives.map((alt, i) => (
          <tr key={`${c.field}-${i}`}>
            <td className={cn(td, "font-medium")}>{i === 0 ? label(FIELD, c.field) : ""}</td>
            {pair && <td className={td}>{docLabel(answer, alt.doc_id) || "-"}</td>}
            <td className={cn(td, "font-semibold")}>{String(alt.value)}</td>
            <td className={td}><Chips answer={answer} ids={alt.evidence_ids} onCite={onCite} /></td>
          </tr>
        )))}
      </Table>
    </Section>
  );
}

export function MissingTable({ answer }: { answer: Answer }) {
  if (!answer.missing_fields.length) return null;
  const pair = answer.coverage.length > 1;
  return (
    <Section title="확인되지 않은 정보">
      <Table caption="확인되지 않은 정보" head={pair ? ["항목", "사유", "문서"] : ["항목", "사유"]}>
        {answer.missing_fields.map((m, i) => (
          <tr key={i}>
            <td className={cn(td, "font-medium")}>{label(FIELD, m.field)}</td>
            <td className={cn(td, "whitespace-nowrap")}>{label(MISSING_REASON, m.reason)}</td>
            {pair && <td className={cn(td, "whitespace-nowrap")}>{docLabel(answer, m.doc_id) || "-"}</td>}
          </tr>
        ))}
      </Table>
    </Section>
  );
}

export function NextAction({ answer }: { answer: Answer }) {
  if (!answer.next_action || answer.status === "clarification_required") return null;
  const warn = answer.status === "insufficient_evidence";
  return (
    <div className={cn("flex gap-3 rounded-xl p-4 text-sm", warn ? "bg-warn-bg text-foreground" : "bg-secondary")}>
      <AlertTriangle className={cn("mt-0.5 size-4 shrink-0", warn ? "text-warn" : "text-muted-foreground")} aria-hidden />
      <p><span className="font-semibold">다음 확인</span> · {answer.next_action}</p>
    </div>
  );
}

export function FactsTable({ answer }: { answer: Answer }) {
  if (!answer.facts.length) return null;
  const docs = [...new Set(answer.facts.map((f) => f.doc_id))];
  return (
    <Section title="기본 정보" aside={<span className="text-[13px] text-muted-foreground">CSV 기록 기준 · 충돌과 빈 값은 추정하지 않습니다</span>}>
      <Table caption="기본 정보" head={docs.length > 1 ? ["항목", ...docs.map((_, i) => `문서 ${i + 1}`)] : ["항목", "값", "상태"]}>
        {[...new Set(answer.facts.map((f) => f.field))].map((field) => {
          const row = docs.map((d) => answer.facts.find((f) => f.doc_id === d && f.field === field));
          const shown = (f: (typeof row)[number]) => {
            if (!f) return "-";
            const v = f.value as unknown;
            if (field === "amount_krw") return won(v as number | null);
            if (v && typeof v === "object") return when(v as { value: string; precision: string });
            return v == null ? "미상" : String(v);
          };
          const state = (f: (typeof row)[number]) => f && f.state !== "known" && (
            <StatusBadge tone={f.state === "resolved" ? "info" : "warn"} className="ml-2">{label(FACT_STATE, f.state)}</StatusBadge>
          );
          return (
            <tr key={field}>
              <th scope="row" className={cn(td, "w-32 text-left font-medium text-muted-foreground")}>{label(FIELD, field)}</th>
              {docs.length > 1
                ? row.map((f, i) => <td key={i} className={td}>{shown(f)}{state(f)}</td>)
                : <><td className={cn(td, "font-semibold")}>{shown(row[0])}</td><td className={td}>{state(row[0]) || <span className="text-muted-foreground">기록됨</span>}</td></>}
            </tr>
          );
        })}
      </Table>
    </Section>
  );
}

export function InventoryTable({ answer, onCite }: { answer: Answer; onCite: (id: string) => void }) {
  const inv = answer.inventory;
  if (!inv) return null;
  const byPrefix = new Map<string, typeof inv.items>();  // SFR, PER, ...: source order kept within a group
  for (const item of inv.items) {
    const prefix = item.source_form.split(/[-_]/)[0] || "기타";
    byPrefix.set(prefix, [...(byPrefix.get(prefix) ?? []), item]);
  }
  const groups = [...byPrefix.entries()];
  return (
    <Section title={`요구사항 ${inv.counts.codes ?? inv.items.length}개`}
             aside={<span className="text-[13px] text-muted-foreground">상세 {inv.counts.detail ?? 0} · 요약만 {inv.counts.summary ?? 0}</span>}>
      {!answer.summary.includes(inv.completeness_text) && <p className="text-sm text-muted-foreground">{inv.completeness_text}</p>}
      {groups.length > 1 && (
        <p className="flex flex-wrap gap-1.5">
          {groups.map(([prefix, items]) => (
            <span key={prefix} className="rounded-md bg-secondary px-2 py-1 text-[13px] font-semibold tabular-nums">
              {prefix} <span className="font-normal text-muted-foreground">{items.length}</span>
            </span>
          ))}
        </p>
      )}
      <Table caption="요구사항 목록" head={["코드", "이름", "구분", "위치"]}>
        {groups.map(([prefix, items]) => [
          groups.length > 1 && (
            <tr key={`g-${prefix}`} className="bg-secondary/40">
              <th scope="rowgroup" colSpan={4} className="px-3 py-1.5 text-left text-[13px] font-bold">
                {prefix} <span className="font-normal text-muted-foreground">{items.length}개</span>
              </th>
            </tr>
          ),
          ...items.map((item, i) => (
            <tr key={`${item.code}-${i}`} className="hover:bg-secondary/50">
              <td className={cn(td, "w-28 font-mono text-[13px] whitespace-nowrap")}>
                {item.evidence_id ? (
                  <button type="button" onClick={() => onCite(item.evidence_id!)}
                          aria-label={`${item.source_form} 원문 보기`}
                          className="rounded font-semibold text-primary underline-offset-2 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50">
                    {item.source_form}
                  </button>
                ) : item.source_form}
              </td>
              <td className={td}>{item.name || "-"}</td>
              <td className={cn(td, "w-16 whitespace-nowrap")}>{item.kind === "detail" ? "상세" : <StatusBadge tone="neutral">요약</StatusBadge>}</td>
              <td className={cn(td, "w-24 text-[13px] whitespace-nowrap text-muted-foreground")} title={locationText(item.location)}>
                {locationShort(item.location)}
              </td>
            </tr>
          )),
        ])}
      </Table>
      {inv.repeated_detail_codes.length > 0 && (
        <p className="text-[13px] text-muted-foreground">같은 코드의 상세 행이 여러 번 나옵니다: {inv.repeated_detail_codes.join(", ")}</p>
      )}
    </Section>
  );
}

/** Citation chips: a button per evidence ID, opening the original quote. */
export function Chips({ answer, ids, onCite, active }: {
  answer: Answer; ids: string[]; onCite: (id: string) => void; active?: string | null;
}) {
  const cited = citedIds(answer, ids);
  if (!cited.length) return null;
  return (
    <span className="inline-flex flex-wrap gap-1 align-middle">
      {cited.map((id) => (
        <button key={id} type="button" onClick={() => onCite(id)} aria-pressed={active === id}
                aria-label={`근거 ${id} 원문 보기`}
                className={cn("inline-flex h-6 min-w-8 items-center justify-center rounded-md px-1.5 text-xs font-bold tabular-nums outline-none",
                  "focus-visible:ring-3 focus-visible:ring-ring/50",
                  active === id ? "bg-primary text-primary-foreground" : "bg-accent text-accent-foreground hover:bg-primary/15")}>
          {id}
        </button>
      ))}
    </span>
  );
}

export function useEvidence(requestId: string, evidenceId: string | null) {
  return usePoll(evidenceId ? `${requestId}/${evidenceId}` : null, () => must(
    api.GET("/api/requests/{request_id}/evidence/{evidence_id}",
      { params: { path: { request_id: requestId, evidence_id: evidenceId! } } }), errorText), null);
}

/** The opened citation: the exact quote among its neighbouring paragraphs, where it is, and the original file. */
export function EvidenceDetail({ requestId, evidenceId }: {
  requestId: string; evidenceId: string | null;
}) {
  const { data, error, loading } = useEvidence(requestId, evidenceId);
  if (!evidenceId) return null;
  if (loading) return <div className="space-y-2"><Skeleton className="h-4 w-2/3" /><Skeleton className="h-20 w-full" /></div>;
  if (error || !data) return <p role="alert" className="text-sm text-bad">{error}</p>;
  return <EvidenceBody ev={data} />;
}

export function EvidenceBody({ ev }: { ev: Evidence }) {
  const [around, setAround] = useState(false);  // neighbouring paragraphs are context: folded to two lines first
  const paragraphs = ev.context.length ? ev.context : [{ element_id: "q", text: ev.quote, cited: true, location: {} }];
  const hasAround = paragraphs.some((p) => !p.cited);
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <p className="text-[13px] font-semibold text-muted-foreground">근거 {ev.evidence_id}</p>
        <p className="font-semibold leading-snug">{ev.title || "제목 없음"}</p>
        <p className="text-[13px] text-muted-foreground">{locationText(ev.location)} · {label(REVIEW, ev.review_status)}</p>
      </div>
      {ev.warnings.map((w) => (
        <p key={w} className="flex gap-2 rounded-lg bg-warn-bg px-3 py-2 text-[13px] text-foreground">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />{EVIDENCE_WARNING[w] ?? w}
        </p>
      ))}
      <div className="space-y-2 text-base">
        {paragraphs.map((p) => (
          <p key={p.element_id} className={cn("whitespace-pre-wrap", !p.cited && "text-muted-foreground", !p.cited && !around && "line-clamp-2")}>
            {p.cited ? <mark className="rounded bg-cite-bg px-0.5 text-foreground [box-decoration-break:clone]">{p.text}</mark> : p.text}
          </p>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-muted-foreground">
        <span>노란 바탕이 답변이 인용한 원문입니다.</span>
        {hasAround && (
          <button type="button" aria-expanded={around} onClick={() => setAround(!around)}
                  className="min-h-6 rounded font-semibold text-foreground underline-offset-2 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50">
            {around ? "앞뒤 문단 접기" : "앞뒤 문단 모두 보기"}
          </button>
        )}
      </div>
      {ev.download_available && (
        <a href={originalHref(ev.doc_id, ev.source_hash)}
           className="inline-flex h-9 items-center gap-2 rounded-lg border px-3 text-sm font-semibold outline-none hover:bg-secondary focus-visible:ring-3 focus-visible:ring-ring/50">
          <Download className="size-4" aria-hidden />원문 파일 받기
        </a>
      )}
    </div>
  );
}

export function RequestFooter({ view }: { view: RequestView }) {
  const cost = `정산 ${usd(view.settled_micro_usd, 4)}` + (view.reserved_micro_usd ? ` · 보류 ${usd(view.reserved_micro_usd, 4)}` : "");
  return (
    <p className="border-t pt-3 text-[13px] text-muted-foreground tabular-nums">
      요청 {view.request_id.slice(0, 8)} · {label(REQUEST, view.status)} · {label(BILLING, view.billing_state)} · {cost}
    </p>
  );
}

/** Every evidence ID the answer cites, in first-cited order: what a source row or pane lists. */
export function citedInOrder(a: Answer): string[] {
  const seen: string[] = [];
  const add = (ids: string[]) => ids.forEach((id) => id in a.evidence && !seen.includes(id) && seen.push(id));
  a.claims.forEach((c) => add(c.evidence_ids));
  a.conflicts.forEach((c) => c.alternatives.forEach((alt) => add(alt.evidence_ids)));
  a.inventory?.items.forEach((i) => i.evidence_id && add([i.evidence_id]));
  return seen;
}
