"use client";

// 원문 대조: the automatic two-way comparison of every HWP extraction against its Hancom print. A person looks only
// at the reported places, beside the printed page, and then confirms the source.

import { useState } from "react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { label, REVIEW, REVIEW_TONE } from "@/lib/format";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Empty, Field, field, Notice } from "./parts";

export type Source = Schemas["FidelitySource"];

/** Sources a person still has to look at: the automatic comparison reported places. */
export const flagged = (rows: Source[]) => rows.filter((r) => r.review_status === "auto_flagged");

export function FidelityRow({ s, active, onOpen }: { s: Source; active: boolean; onOpen: () => void }) {
  return (
    <button type="button" onClick={onOpen} aria-current={active || undefined}
            className={cn("flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left outline-none hover:bg-secondary/70 focus-visible:ring-3 focus-visible:ring-ring/50",
              active && "bg-accent hover:bg-accent")}>
      <span className="min-w-0 flex-1">
        <span className="line-clamp-1 text-sm font-medium">{s.filename ?? s.source_hash.slice(0, 12)}</span>
        <span className="text-xs text-muted-foreground">
          {s.metrics ? `표시된 곳 ${s.findings.length}곳 · ${s.metrics.pages ?? "-"}쪽` : "자동 대조 전"}
        </span>
      </span>
      <StatusBadge tone={REVIEW_TONE[s.review_status] ?? "neutral"}>{label(REVIEW, s.review_status)}</StatusBadge>
    </button>
  );
}

export function FidelityDetail({ s, onConfirmed }: { s: Source; onConfirmed: () => void }) {
  const [shown, setShown] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [state, setState] = useState<{ busy?: boolean; error?: string; done?: boolean }>({});
  const m = s.metrics;
  const confirm = async (e: React.FormEvent) => {
    e.preventDefault();
    setState({ busy: true });
    const { error } = await api.POST("/api/verify/fidelity/{source_hash}/confirm", {
      params: { path: { source_hash: s.source_hash } }, body: { note } });
    if (error) return setState({ error: errorText(error) });
    setState({ done: true });
    onConfirmed();
  };
  return (
    <article className="space-y-5">
      <header className="space-y-2">
        <StatusBadge tone={REVIEW_TONE[s.review_status] ?? "neutral"} size="md">{label(REVIEW, s.review_status)}</StatusBadge>
        <h3 className="text-lg font-bold leading-snug">{s.filename}</h3>
        {m ? (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
            {([["쪽", m.pages], ["추출 글자", m.extracted_chars], ["추출에만 있는 글자", m.extraction_unmatched],
              ["인쇄본에만 있는 글자", m.rendering_unmatched]] as const).map(([k, v]) => (
              <div key={k}><dt className="text-xs text-muted-foreground">{k}</dt><dd className="font-semibold tabular-nums">{Number(v).toLocaleString("ko-KR")}</dd></div>
            ))}
          </dl>
        ) : <Notice tone="warn">아직 자동 대조 전입니다. CLI: python -m rfp_assistant.cli fidelity run</Notice>}
        {!!m?.image_pages.length && (
          <p className="text-xs text-muted-foreground">그림이 있는 쪽(그림 속 글자는 양쪽 모두에 없음): {m.image_pages.join(", ")}</p>
        )}
      </header>

      {m && (s.findings.length === 0 ? <Empty>자동 대조가 표시한 곳이 없습니다.</Empty> : (
        <ol className="space-y-2">
          {s.findings.map((f, i) => (
            <li key={i} className="rounded-xl border">
              <div className="flex flex-wrap items-center gap-2 px-4 py-3">
                <span className="text-xs font-bold tabular-nums text-muted-foreground">{i + 1}</span>
                <span className="text-sm font-semibold">{f.page ? `${f.page}쪽` : "쪽 미상"}</span>
                <StatusBadge tone={f.side === "extraction" ? "warn" : "bad"}>
                  {f.side === "extraction" ? "추출에만 있음·다름" : "인쇄본에만 있음(누락 의심)"}
                </StatusBadge>
                <span className="text-xs text-muted-foreground">{f.unmatched_chars}자{f.digits.length ? ` · 숫자 ${f.digits.join(", ")}` : ""}</span>
                {f.page && (
                  <Button variant="outline" size="sm" className="ml-auto" aria-expanded={shown === i}
                          onClick={() => setShown(shown === i ? null : i)}>
                    {shown === i ? "인쇄본 닫기" : "인쇄본 쪽 보기"}
                  </Button>
                )}
              </div>
              <div className="space-y-1 border-t px-4 py-3 text-sm">
                <p><span className="mr-2 text-xs font-semibold text-muted-foreground">불일치</span>
                  <mark className="rounded bg-cite-bg px-1">{(f.stretches.length ? f.stretches : [f.stretch]).filter(Boolean).join(" / ") || "-"}</mark></p>
                {f.text && <p className="leading-6"><span className="mr-2 text-xs font-semibold text-muted-foreground">추출 원문</span>{f.text}</p>}
              </div>
              {shown === i && f.page && (
                // eslint-disable-next-line @next/next/no-img-element -- a server-rendered PNG of one printed page
                <img src={`/api/verify/fidelity/${s.source_hash}/pages/${f.page}`} alt={`${s.filename} 한컴 인쇄본 ${f.page}쪽`}
                     className="w-full border-t" />
              )}
            </li>
          ))}
        </ol>
      ))}

      {["auto_verified", "auto_flagged"].includes(s.review_status) && !state.done && (
        <form onSubmit={confirm} className="space-y-3 rounded-2xl bg-secondary/60 p-4">
          <Field id={`fid-note-${s.source_hash}`} label="메모 (선택)">
            <input id={`fid-note-${s.source_hash}`} maxLength={300} value={note} onChange={(e) => setNote(e.target.value)} className={cn(field, "h-10")} />
          </Field>
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" size="lg" disabled={state.busy}>표시된 곳을 모두 원문과 대조했습니다</Button>
            <span className="text-xs text-muted-foreground">상태가 &lsquo;표본 대조 완료&rsquo;로 바뀝니다.</span>
          </div>
          {state.error && <Notice tone="bad">{state.error}</Notice>}
        </form>
      )}
      {state.done && <Notice tone="ok">표본 대조 완료로 기록했습니다.</Notice>}
    </article>
  );
}
