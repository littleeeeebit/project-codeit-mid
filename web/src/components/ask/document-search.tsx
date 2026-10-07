"use client";

import { useState } from "react";
import { ChevronDown, Search } from "lucide-react";
import { cn } from "cn";
import { api, type Doc, errorText } from "@/lib/api";
import { FIELD, label, REVIEW, when, wonShort } from "@/lib/format";
import { must, usePoll } from "@/lib/use-poll";
import { StatusBadge } from "@/components/status-badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Skeleton } from "@/components/ui/skeleton";

const PAGE = 12;

type Filters = { query: string; institution: string; amount_min?: number; amount_max?: number;
  closing_from?: string; closing_to?: string };

const field = "h-10 w-full rounded-lg border border-input bg-background px-3 text-base sm:text-sm outline-none placeholder:text-muted-foreground/80 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50";

export function DocBadges({ doc }: { doc: Doc }) {
  const conflicts = [...new Set(doc.conflicts.map((c) => String(c.field)))].filter((f) => !(f in doc.resolutions));
  return (
    <div className="flex flex-wrap gap-1">
      {doc.indexed ? <StatusBadge tone="ok">질문 가능</StatusBadge>
        : doc.parse_status === "quarantined" ? <StatusBadge tone="bad">원문 수집 실패</StatusBadge>
          : <StatusBadge tone="neutral">색인 전</StatusBadge>}
      {doc.review_status !== "sample_checked" && doc.review_status !== "reviewed" && (
        <StatusBadge tone={["auto_flagged", "needs_recovery"].includes(doc.review_status) ? "warn" : "neutral"}>
          {label(REVIEW, doc.review_status)}
        </StatusBadge>
      )}
      {conflicts.length > 0 && <StatusBadge tone="warn">기록 충돌: {conflicts.map((f) => label(FIELD, f)).join(", ")}</StatusBadge>}
      {doc.filter_undecided.map((note) => {
        const [f, state] = note.split(":");
        return <StatusBadge key={note} tone="warn">{label(FIELD, f)} 조건 판단 불가({state === "conflict" ? "충돌" : "값 없음"})</StatusBadge>;
      })}
    </div>
  );
}

export function DocumentSearch({ selected, onToggle }: { selected: Doc[]; onToggle: (d: Doc) => void }) {
  const [draft, setDraft] = useState<Filters>({ query: "", institution: "" });
  const [filters, setFilters] = useState<Filters>({ query: "", institution: "" });
  const [limit, setLimit] = useState(PAGE);
  const key = JSON.stringify(filters);
  const { data, error, loading } = usePoll(key, () => must(api.GET("/api/documents", { params: { query: filters } }), errorText), null);
  const chosen = new Set(selected.map((d) => d.doc_id));

  return (
    <div className="flex h-full flex-col">
      <SearchForm draft={draft} onDraft={setDraft} onSearch={() => { setFilters(draft); setLimit(PAGE); }} />
      <Results data={data} error={error} loading={loading} limit={limit} selected={selected} chosen={chosen}
               onToggle={onToggle} onMore={() => setLimit(limit + PAGE)} />
    </div>
  );
}

function SearchForm({ draft, onDraft: setDraft, onSearch }: { draft: Filters; onDraft: (f: Filters) => void; onSearch: () => void }) {
  const num = (v: string) => (v.trim() ? Number(v.replace(/[^0-9]/g, "")) : undefined);
  return (
      <form className="space-y-3 border-b p-5" role="search" aria-label="문서 찾기"
            onSubmit={(e) => { e.preventDefault(); onSearch(); }}>
        <h2 className="text-lg font-bold">문서 찾기</h2>
        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <label htmlFor="q" className="sr-only">검색어</label>
          <input id="q" className={cn(field, "pl-9")} placeholder="사업명, 내용, 요구사항 코드" value={draft.query}
                 onChange={(e) => setDraft({ ...draft, query: e.target.value })} />
        </div>
        <div>
          <label htmlFor="inst" className="mb-1 block text-[13px] font-medium text-muted-foreground">발주 기관</label>
          <input id="inst" className={field} placeholder="예: 한국교육학술정보원" value={draft.institution}
                 onChange={(e) => setDraft({ ...draft, institution: e.target.value })} />
        </div>
        <Collapsible>
          <CollapsibleTrigger className="group flex min-h-6 items-center gap-1 rounded text-[13px] font-semibold text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
            상세 조건 <ChevronDown className="size-3.5 transition-transform group-data-[state=open]:rotate-180" aria-hidden />
          </CollapsibleTrigger>
          <CollapsibleContent className="grid grid-cols-2 gap-2 pt-2">
            {([["amin", "최소 금액(원)", "amount_min"], ["amax", "최대 금액(원)", "amount_max"]] as const).map(([id, text, k]) => (
              <div key={id}>
                <label htmlFor={id} className="mb-1 block text-[13px] text-muted-foreground">{text}</label>
                <input id={id} inputMode="numeric" className={field} value={draft[k] ?? ""}
                       onChange={(e) => setDraft({ ...draft, [k]: num(e.target.value) })} />
              </div>
            ))}
            {([["cfrom", "마감 시작", "closing_from"], ["cto", "마감 끝", "closing_to"]] as const).map(([id, text, k]) => (
              <div key={id}>
                <label htmlFor={id} className="mb-1 block text-[13px] text-muted-foreground">{text}</label>
                <input id={id} type="date" className={field} value={draft[k] ?? ""}
                       onChange={(e) => setDraft({ ...draft, [k]: e.target.value || undefined })} />
              </div>
            ))}
          </CollapsibleContent>
        </Collapsible>
        <button type="submit" className="h-10 w-full rounded-lg bg-foreground text-[15px] font-semibold text-background outline-none hover:bg-foreground/85 focus-visible:ring-3 focus-visible:ring-ring/50">
          검색
        </button>
      </form>
  );
}

function Results({ data, error, loading, limit, selected, chosen, onToggle, onMore }: {
  data?: Doc[]; error?: string; loading: boolean; limit: number; selected: Doc[]; chosen: Set<string>;
  onToggle: (d: Doc) => void; onMore: () => void;
}) {
  return (
      <div className="flex-1 overflow-y-auto p-3" aria-live="polite">
        {loading && <div className="space-y-2 p-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-20 w-full rounded-xl" />)}</div>}
        {error && <p role="alert" className="p-2 text-sm text-bad">{error}</p>}
        {data && (
          <>
            <p className="px-2 pb-2 text-[13px] text-muted-foreground">
              {data.length}건 · 질문할 수 있는 문서가 먼저 나옵니다 · 최대 2개 선택
            </p>
            <ul className="space-y-1.5">
              {data.slice(0, limit).map((d) => {
                const on = chosen.has(d.doc_id);
                const disabled = !on && selected.length >= 2;
                const id = `pick-${d.doc_id}`;
                return (
                  <li key={d.doc_id}>
                    <div className={cn("flex gap-3 rounded-xl border p-3 transition-colors",
                      on ? "border-primary bg-accent/60" : "border-input hover:bg-secondary/70")}>
                      <Checkbox id={id} checked={on} disabled={disabled} onCheckedChange={() => onToggle(d)} className="mt-0.5" />
                      <label htmlFor={id} className={cn("min-w-0 flex-1 space-y-1.5", !disabled && "cursor-pointer")}>
                        <span className="block text-base font-semibold text-foreground">{d.title || "제목 없음"}</span>
                        <span className="block text-[13px] text-muted-foreground">
                          {d.institution || "기관 미상"} · {wonShort(d.amount_krw)} · 마감 {when(d.bid_close)}
                        </span>
                        <DocBadges doc={d} />
                      </label>
                    </div>
                  </li>
                );
              })}
            </ul>
            {data.length > limit && (
              <button type="button" onClick={onMore}
                      className="mt-2 h-10 w-full rounded-lg text-sm font-semibold text-muted-foreground outline-none hover:bg-secondary focus-visible:ring-3 focus-visible:ring-ring/50">
                더 보기 ({data.length - limit}건 남음)
              </button>
            )}
          </>
        )}
      </div>
  );
}
