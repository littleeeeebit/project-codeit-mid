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
            className={cn("flex w-full items-center gap-3 rounded-lg border-l-4 px-3 py-3 text-left outline-none hover:bg-secondary/70 focus-visible:ring-3 focus-visible:ring-ring/50",
              active ? "border-primary bg-accent hover:bg-accent" : "border-transparent")}>
      <span className="min-w-0 flex-1">
        <span className="block text-base font-semibold [overflow-wrap:anywhere]">{s.filename ?? s.source_hash.slice(0, 12)}</span>
        <span className="mt-1 block text-xs text-muted-foreground">
          {s.metrics ? `표시된 곳 ${s.findings.length}곳 · ${s.metrics.pages ?? "-"}쪽` : "자동 대조 전"}
        </span>
        {s.review_status !== "auto_flagged" && <span className="mt-1 block text-[13px] text-muted-foreground">{label(REVIEW, s.review_status)}</span>}
      </span>
    </button>
  );
}

export function FidelityDetail({ s, onConfirmed }: { s: Source; onConfirmed: () => void }) {
  const [note, setNote] = useState("");
  const [state, setState] = useState<{ busy?: boolean; error?: string; done?: boolean }>({});
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
      <FidelityHeader s={s} />
      {s.metrics && (s.findings.length === 0 ? <Empty>자동 대조가 표시한 곳이 없습니다.</Empty> : <FindingsReview s={s} />)}

      {["auto_verified", "auto_flagged"].includes(s.review_status) && !state.done && (
        <form onSubmit={confirm} className="space-y-3 rounded-2xl bg-secondary/60 p-4">
          <Field id={`fid-note-${s.source_hash}`} label="메모 (선택)">
            <input id={`fid-note-${s.source_hash}`} maxLength={300} value={note} onChange={(e) => setNote(e.target.value)} className={cn(field, "h-10")} />
          </Field>
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" size="lg" disabled={state.busy}>표시된 곳을 모두 원문과 대조했습니다</Button>
            <span className="text-[13px] text-muted-foreground">대조 상태와 검토 메모가 수정 기록에 자동으로 남습니다.</span>
          </div>
          {state.error && <Notice tone="bad">{state.error}</Notice>}
        </form>
      )}
      {state.done && <Notice tone="ok">표본 대조 완료로 기록했습니다.</Notice>}
    </article>
  );
}

function FidelityHeader({ s }: { s: Source }) {
  const m = s.metrics;
  return (
      <header className="space-y-2">
        <StatusBadge tone={REVIEW_TONE[s.review_status] ?? "neutral"} size="md">{label(REVIEW, s.review_status)}</StatusBadge>
        <h3 className="text-lg font-bold leading-snug">{s.filename}</h3>
        {m ? (
          <dl className="grid grid-cols-2 gap-4 rounded-xl border border-input p-4 text-sm sm:grid-cols-4">
            {([["확인할 곳", s.findings.length], ["전체 쪽", m.pages], ["추출에만 있는 글자", m.extraction_unmatched],
              ["인쇄본에만 있는 글자", m.rendering_unmatched]] as const).map(([k, v]) => (
              <div key={k}><dt className="text-xs text-muted-foreground">{k}</dt><dd className="font-semibold tabular-nums">{Number(v).toLocaleString("ko-KR")}</dd></div>
            ))}
          </dl>
        ) : <Notice tone="warn">아직 자동 대조 전입니다. CLI: python -m rfp_assistant.cli fidelity run</Notice>}
        {!!m?.image_pages.length && (
          <details className="text-[13px] text-muted-foreground"><summary className="min-h-8 cursor-pointer outline-none focus-visible:ring-3 focus-visible:ring-ring/50">그림 포함 {m.image_pages.length}쪽 · 그림 속 글자는 대조 범위 밖</summary><p className="pt-2">{m.image_pages.join(", ")}</p></details>
        )}
      </header>
  );
}

function FindingsReview({ s }: { s: Source }) {
  const [shown, setShown] = useState<number | null>(null);
  const [selected, setSelected] = useState(0);
  const finding = s.findings[selected];
  return (
        <section aria-label="불일치 검토" className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-base font-bold">확인할 위치</h4><span className="text-[13px] text-muted-foreground">한 위치씩 선택해 대조하세요</span></div>
        <ol className="max-h-64 divide-y overflow-y-auto rounded-xl border border-input">
          {s.findings.map((f, i) => (
            <li key={i}>
              <button type="button" aria-pressed={selected === i} aria-label={`불일치 ${i + 1} · ${f.page ? `${f.page}쪽` : "쪽 미상"}`} onClick={() => { setSelected(i); setShown(null); }}
                className={cn("grid min-h-12 w-full grid-cols-[2rem_4rem_minmax(0,1fr)_3rem] items-start gap-2 border-l-4 px-3 py-3 text-left text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50", selected === i ? "border-primary bg-accent" : "border-transparent hover:bg-secondary")}>
                <span className="text-muted-foreground tabular-nums">{i + 1}</span><span className="font-semibold">{f.page ? `${f.page}쪽` : "쪽 미상"}</span>
                <span>{f.side === "extraction" ? "추출 차이" : "누락 의심"}{f.digits.length > 0 && <span className="ml-2 text-warn">숫자 포함</span>}</span>
                <span className="text-right tabular-nums">{f.unmatched_chars}자</span>
              </button>
            </li>
          ))}
        </ol>
        {finding && <div className="overflow-hidden rounded-xl border border-input">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b bg-secondary/40 px-4 py-3">
            <h4 className="text-base font-bold">{selected + 1} / {s.findings.length} · {finding.page ? `${finding.page}쪽` : "쪽 미상"}</h4>
            {finding.page && <Button variant="outline" className="min-h-11" aria-expanded={shown === selected} onClick={() => setShown(shown === selected ? null : selected)}>{shown === selected ? "인쇄본 닫기" : "인쇄본 쪽 보기"}</Button>}
          </div>
          <div className="space-y-4 p-4">
            <div><p className="mb-2 text-sm font-semibold">{finding.side === "extraction" ? "추출에서 다른 부분" : "인쇄본에만 있는 부분"}</p><p className="text-base whitespace-pre-wrap"><mark className="rounded bg-cite-bg px-1">{(finding.stretches.length ? finding.stretches : [finding.stretch]).filter(Boolean).join(" / ") || "-"}</mark></p></div>
            {finding.text && <div><p className="mb-2 text-sm font-semibold">추출 원문</p><blockquote className="border-l-4 border-primary pl-4 text-base leading-relaxed whitespace-pre-wrap">{finding.text}</blockquote></div>}
            {finding.digits.length > 0 && <dl className="flex flex-wrap gap-2 text-sm"><dt className="font-semibold">포함된 숫자</dt><dd>{finding.digits.join(", ")}</dd></dl>}
            {!finding.page && <p className="text-sm text-muted-foreground">쪽 번호를 찾지 못했습니다. 원본 파일에서 해당 문구를 확인하세요.</p>}
          </div>
          {shown === selected && finding.page && (
            // eslint-disable-next-line @next/next/no-img-element -- one server-rendered original page
            <img src={`/api/verify/fidelity/${s.source_hash}/pages/${finding.page}`} alt={`${s.filename} 한컴 인쇄본 ${finding.page}쪽`} className="w-full border-t" />
          )}
        </div>}
        </section>
  );
}
