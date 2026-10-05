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

/** How an answer's citations open: marker numbers by evidence ID, the open one, and what a click does. */
export type Cite = { numbers: Map<string, number>; active?: string | null; onCite?: (id: string) => void };

/** Numbers 1, 2, … in first-cited order (summary, sentences, conflicts, requirements), so a reader meets them
 *  in order. A streamed answer is numbered by the same rule, so its numbers survive validation. */
export function citationNumbers(ids: string[]): Map<string, number> {
  return new Map(ids.map((id, i) => [id, i + 1]));
}

/** Numbered citation markers at the end of a sentence. Without `onCite` (a streamed, unvalidated answer) they
 *  are plain numbers: nothing can be opened before validation keeps the evidence. */
export function Markers({ ids, cite }: { ids: string[]; cite: Cite }) {
  const shown = [...new Set(ids.filter((id) => cite.numbers.has(id)))];
  if (!shown.length) return null;
  // An inline wrapper and zero-size brackets: copied text reads "문장.[1][2]" on the sentence's own line
  const bracket = (c: string) => <span className="text-[0px]">{c}</span>;
  return (
    <span className="ml-1 whitespace-nowrap [&>*+*]:ml-0.5">
      {shown.map((id) => {
        const n = cite.numbers.get(id);
        return cite.onCite ? (
          <button key={id} type="button" onClick={() => cite.onCite!(id)} aria-pressed={cite.active === id}
                  aria-label={`근거 ${n} 원문 보기`}
                  className={cn("inline-block h-6 min-w-6 rounded-md px-1 text-center align-[0.1em] text-[13px] leading-6 font-bold tabular-nums outline-none",
                    "focus-visible:ring-3 focus-visible:ring-ring/50",
                    cite.active === id ? "bg-primary text-primary-foreground" : "bg-accent text-primary hover:bg-primary/15")}>
            {bracket("[")}{n}{bracket("]")}
          </button>
        ) : (
          <span key={id} className="inline-block h-6 min-w-6 rounded-md bg-secondary px-1 text-center align-[0.1em] text-[13px] leading-6 font-semibold text-muted-foreground tabular-nums">
            {bracket("[")}{n}{bracket("]")}
          </span>
        );
      })}
    </span>
  );
}

type Sentence = { text: string; kind: string; doc_id: string; evidence_ids: string[] };

/** The answer as sentences, each ending with its markers; a comparison groups them under 문서 1 and 문서 2. */
export function Sentences({ claims, cite, labelOf }: {
  claims: Sentence[]; cite: Cite; labelOf: (docId: string) => string;
}) {
  if (!claims.length) return null;
  const groups = new Map<string, Sentence[]>();
  for (const c of claims) groups.set(labelOf(c.doc_id), [...(groups.get(labelOf(c.doc_id)) ?? []), c]);
  return (
    <section className="space-y-4" aria-label="답변 내용">
      {[...groups.entries()].map(([doc, items]) => (
        <div key={doc} className="space-y-2.5">
          {doc && <h3 className="text-sm font-bold text-muted-foreground">{doc}</h3>}
          {items.map((c, i) => (
            <p key={i} className="text-base [overflow-wrap:anywhere]">
              {c.kind === "inference" && <StatusBadge tone="warn" className="mr-1.5 align-[0.1em]">추론</StatusBadge>}
              {c.text}
              <Markers ids={c.evidence_ids} cite={cite} />
            </p>
          ))}
        </div>
      ))}
    </section>
  );
}

/** The state badge and the conclusion; every state has its own body (DESIGN.md, result states). */
export function StateHead({ answer, large = false, cite }: { answer: Answer; large?: boolean; cite?: Cite }) {
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
    <div className="space-y-2">
      <StatusBadge tone={s.tone}>{s.label}</StatusBadge>
      <p className={cn("font-semibold text-foreground [overflow-wrap:anywhere]", large ? "text-[22px] leading-snug" : "text-lg leading-relaxed")}>
        {answer.summary}
        {cite && <Markers ids={answer.summary_evidence_ids} cite={cite} />}
      </p>
    </div>
  );
}

export function ConflictTable({ answer, cite }: { answer: Answer; cite: Cite }) {
  if (!answer.conflicts.length) return null;
  const pair = answer.coverage.length > 1;
  return (
    <Section title="서로 다른 값">
      <Table caption="서로 다른 값" head={pair ? ["항목", "문서", "값", "근거"] : ["항목", "값", "근거"]}>
        {answer.conflicts.flatMap((c) => c.alternatives.map((alt, i) => (
          <tr key={`${c.field}-${i}`}>
            <td className={cn(td, "font-medium")}>{i === 0 ? label(FIELD, c.field) : ""}</td>
            {pair && <td className={cn(td, "whitespace-nowrap")}>{docLabel(answer, alt.doc_id) || "-"}</td>}
            <td className={cn(td, "font-semibold")}>{String(alt.value)}</td>
            <td className={td}><Markers ids={alt.evidence_ids} cite={cite} /></td>
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

type InventoryItem = NonNullable<Answer["inventory"]>["items"][number];

/** Requirement rows grouped as the table shows them: by source-form prefix (SFR, PER, ...; 기타 when none) in
 *  first-seen order, source order within a group. Citation numbers and the conversation (service.
 *  inventory_display_order) count rows in this order, so "세 번째 요구사항" is the third row on screen. */
function inventoryGroups(items: InventoryItem[]): [string, InventoryItem[]][] {
  const byPrefix = new Map<string, InventoryItem[]>();
  for (const item of items) {
    const prefix = item.source_form.split(/[-_]/)[0] || "기타";
    byPrefix.set(prefix, [...(byPrefix.get(prefix) ?? []), item]);
  }
  return [...byPrefix.entries()];
}

const inventoryRows = (items: InventoryItem[]) => inventoryGroups(items).flatMap(([, rows]) => rows);

export function InventoryTable({ answer, onCite }: { answer: Answer; onCite: (id: string) => void }) {
  const inv = answer.inventory;
  if (!inv) return null;
  const groups = inventoryGroups(inv.items);
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

export function useEvidence(requestId: string, evidenceId: string | null) {
  return usePoll(evidenceId ? `${requestId}/${evidenceId}` : null, () => must(
    api.GET("/api/requests/{request_id}/evidence/{evidence_id}",
      { params: { path: { request_id: requestId, evidence_id: evidenceId! } } }), errorText), null);
}

/** The opened citation: the exact quote first, optional surrounding paragraphs, location and original file. */
export function EvidenceDetail({ requestId, evidenceId, number }: {
  requestId: string; evidenceId: string | null; number?: number;
}) {
  const { data, error, loading } = useEvidence(requestId, evidenceId);
  if (!evidenceId) return null;
  if (loading) return <div className="space-y-2"><Skeleton className="h-4 w-2/3" /><Skeleton className="h-20 w-full" /></div>;
  if (error || !data) return <p role="alert" className="text-sm text-bad">{error}</p>;
  return <EvidenceBody key={data.evidence_id} ev={data} number={number} />;
}

export function EvidenceBody({ ev, number }: { ev: Evidence; number?: number }) {
  const [around, setAround] = useState(false);
  const [fullQuote, setFullQuote] = useState(false);
  const paragraphs = ev.context.length ? ev.context : [{ element_id: "q", text: ev.quote, cited: true, location: {} }];
  const cited = paragraphs.filter((p) => p.cited);
  const quoted = cited.length ? cited : [{ element_id: "q", text: ev.quote, cited: true, location: ev.location }];
  const longQuote = quoted.reduce((n, p) => n + p.text.length, 0) > 400
    || quoted.reduce((n, p) => n + p.text.split("\n").length, 0) > 12;
  const hasAround = paragraphs.some((p) => !p.cited);
  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <h3 className="text-lg font-bold">
          {number ? <>근거 <span className="text-primary">{number}</span> 원문 인용</> : <>원문 인용 <span className="ml-1 text-primary">{ev.evidence_id}</span></>}
        </h3>
        <p className="text-base font-semibold">{ev.title || "제목 없음"}</p>
        <p className="text-[13px] text-muted-foreground">{locationText(ev.location)} · {label(REVIEW, ev.review_status)}</p>
      </div>
      {ev.warnings.length > 0 && (
        <div className="space-y-2 rounded-lg bg-warn-bg p-3 text-sm">
          <p className="flex items-center gap-2 font-semibold"><AlertTriangle className="size-4 shrink-0 text-warn" aria-hidden />원문 확인이 필요한 근거</p>
          <ul className="space-y-2">{ev.warnings.map((w) => <li key={w}>{EVIDENCE_WARNING[w] ?? w}</li>)}</ul>
        </div>
      )}
      <div className="space-y-3">
        <blockquote className="border-l-4 border-primary pl-4 text-base text-foreground">
          <div className={cn("space-y-4 whitespace-pre-wrap [overflow-wrap:anywhere]", longQuote && !fullQuote && "line-clamp-[12]")}>
            {quoted.map((p) => <p key={p.element_id}>{p.text}</p>)}
          </div>
        </blockquote>
        {longQuote && (
          <button type="button" aria-expanded={fullQuote} onClick={() => setFullQuote(!fullQuote)}
                  className="min-h-9 rounded-lg border border-input px-3 text-sm font-semibold outline-none hover:bg-secondary focus-visible:ring-3 focus-visible:ring-ring/50">
            {fullQuote ? "긴 인용문 접기" : "인용문 일부 표시 · 전체 펼치기"}
          </button>
        )}
      </div>
      <div className="space-y-3">
        {hasAround && (
          <button type="button" aria-expanded={around} onClick={() => setAround(!around)}
                  className="min-h-9 rounded-lg border border-input px-3 text-sm font-semibold outline-none hover:bg-secondary focus-visible:ring-3 focus-visible:ring-ring/50">
            {around ? "주변 원문 접기" : "앞뒤 문단 포함 전체 원문 보기"}
          </button>
        )}
        {hasAround && around && <div className="space-y-4 rounded-xl border border-input bg-secondary/30 p-4 text-base">
          {paragraphs.map((p) => <p key={p.element_id} className={cn("whitespace-pre-wrap [overflow-wrap:anywhere]", p.cited && "border-l-4 border-primary pl-3")}>{p.text}</p>)}
        </div>}
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

/** IDs in first-cited order across `lists`, each once. */
export function firstCited(lists: string[][], keep: (id: string) => boolean = () => true): string[] {
  return [...new Set(lists.flat().filter(keep))];
}

/** Every evidence ID the answer cites, in first-cited order: what the markers number and the pane opens. */
export function citedInOrder(a: Answer): string[] {
  return firstCited([a.summary_evidence_ids, ...a.claims.map((c) => c.evidence_ids),
    ...a.conflicts.flatMap((c) => c.alternatives.map((alt) => alt.evidence_ids)),
    ...inventoryRows(a.inventory?.items ?? []).map((i) => (i.evidence_id ? [i.evidence_id] : []))],
  (id) => id in a.evidence);
}
