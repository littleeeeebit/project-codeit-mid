"use client";

import { useEffect, useState } from "react";
import type { Candidate, ChunkingView as View, DocView, Sizes } from "@/lib/api";
import { getJson } from "@/lib/api";
import { Badge, CandidateHead, Failure, columns } from "./ui";
import { NoteField } from "./note-field";

export function ChunkingView({ view, notes, setNote }: {
  view: View;
  notes: Record<string, string>;
  setNote: (key: string, note: string) => void;
}) {
  const [n, setN] = useState(view.documents[0]?.n ?? 0);
  const [loaded, setLoaded] = useState<DocView | null>(null);
  const [failed, setFailed] = useState<{ n: number; message: string } | null>(null);
  const [onlyDiff, setOnlyDiff] = useState(true);
  useEffect(() => {
    let live = true;
    getJson<DocView>(`/api/runs/${view.run_id}/docs/${n}`)
      .then((d) => { if (live) { setLoaded(d); setFailed(null); } })
      .catch((e: Error) => { if (live) setFailed({ n, message: e.message }); });
    return () => { live = false; };
  }, [view.run_id, n]);
  // Only the selected document's chunks are shown (and so can be noted on); an earlier one never stands in for it.
  const doc = loaded?.n === n ? loaded : null;
  const error = failed?.n === n ? failed.message : "";
  const done = view.candidates.filter((c) => c.status === "complete");
  const rows = doc ? doc.rows.filter((r) => !onlyDiff || r.differs) : [];
  const listed = view.documents.find((d) => d.n === n);
  return (
    <>
      <section aria-label="후보별 결과" className="overflow-x-auto">
        <div className="grid gap-4" style={columns(view.candidates.length)}>
          {view.candidates.map((c) => {
            const s = view.summary[c.id];
            return (
              <div key={c.id} className="border-t-2 border-ink pt-3">
                <CandidateHead c={c} />
                {c.status !== "complete" || !s ? <Failure c={c} /> : (
                  <>
                    <div className="mt-3 flex flex-wrap items-end gap-x-8 gap-y-3">
                      <div>
                        <div className="text-[32px] font-bold leading-none tabular-nums">{s.sizes.count.toLocaleString()}</div>
                        <div className="mt-1 whitespace-nowrap text-[13px] text-muted">조각 · 문서 {s.documents}개</div>
                      </div>
                      <div>
                        <div className={`text-[24px] font-bold leading-none tabular-nums ${s.tables_kept < s.tables_titled ? "text-warn" : ""}`}>
                          {s.tables_kept}/{s.tables_titled}
                        </div>
                        <div className="mt-1 whitespace-nowrap text-[13px] text-muted">표 제목이 행과 함께 남음</div>
                      </div>
                    </div>
                    <SizeBars sizes={s.sizes} />
                  </>
                )}
              </div>
            );
          })}
        </div>
      </section>

      <section aria-label="문서별 경계" className="mt-10">
        <div className="sticky top-0 z-10 -mx-2 flex flex-wrap items-center gap-3 bg-white/95 px-2 py-3 backdrop-blur">
          <h2 className="mr-2 text-[18px] font-bold">문서별 조각 경계</h2>
          <label className="flex min-w-0 items-center gap-2 text-[14px]">
            <span className="sr-only">문서</span>
            <select value={n} onChange={(e) => setN(Number(e.target.value))}
                    className="h-10 max-w-[560px] rounded-[10px] border border-line bg-white px-3 text-[15px]">
              {view.documents.map((d) => (
                <option key={d.n} value={d.n}>
                  {d.differing ? `차이 ${d.differing}` : "같음"} · {d.title ?? d.doc_id}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-[14px] font-medium">
            <input type="checkbox" checked={onlyDiff} onChange={(e) => setOnlyDiff(e.target.checked)} className="size-4" />
            차이 나는 경계만
          </label>
        </div>
        {error && <p className="text-bad">문서를 읽지 못했습니다: {error}</p>}
        {!doc && !error && <p className="py-10 text-center text-muted">불러오는 중…</p>}
        {doc && (
          <>
            <div className="mt-2 grid gap-4 overflow-x-auto" style={columns(view.candidates.length)}>
              {view.candidates.map((c) => {
                const d = doc.candidates[c.id];
                return (
                  <div key={c.id} className="text-[14px]">
                    <span className="font-semibold text-muted">{c.label}</span>
                    {d ? (
                      <span className="ml-2 tabular-nums">
                        조각 {d.sizes.count} · 중앙 {d.sizes.p50 ?? "—"} · 최대 {d.sizes.max ?? "—"} 토큰
                        {d.titles_lost.length > 0 && <span className="ml-2"><Badge tone="warn">표 제목 빠짐 {d.titles_lost.length}</Badge></span>}
                      </span>
                    ) : <span className="ml-2 text-muted">결과 없음</span>}
                  </div>
                );
              })}
            </div>
            <div className="mt-3"><NoteField value={notes[String(n)] ?? ""} onChange={(v) => setNote(String(n), v)} /></div>
            <p className="mt-4 text-[13px] text-muted">
              한 줄은 원문에서 조각이 끝나는 한 위치입니다. 어떤 후보는 그 자리에서 끝나지 않거나 내용이 다르면 차이로
              표시합니다. {listed && `이 문서는 경계 ${listed.boundaries}곳 중 ${listed.differing}곳이 다릅니다.`}
            </p>
            {rows.length === 0 && <p className="py-10 text-center text-muted">이 문서에서는 모든 후보의 조각이 같습니다.</p>}
            <ol className="mt-2 overflow-x-auto">
              {rows.map((r) => (
                <li key={`${r.at.element}-${r.at.row}-${Object.values(r.cells)[0]?.chunk_id}`}
                    className={`grid gap-4 border-t border-line py-3 ${r.differs ? "" : "opacity-60"}`}
                    style={columns(view.candidates.length)}>
                  {view.candidates.map((c, i) => (
                    <ChunkCell key={c.id} c={c} cell={r.cells[c.id]} done={done.includes(c)} base={i === 0}
                               same={i > 0 && !!r.cells[c.id] && r.cells[c.id]?.text === r.cells[view.candidates[0].id]?.text} />
                  ))}
                </li>
              ))}
            </ol>
          </>
        )}
      </section>
    </>
  );
}

function ChunkCell({ c, cell, done, base, same }: {
  c: Candidate; cell: DocView["rows"][number]["cells"][string] | undefined; done: boolean; base: boolean; same: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (!done) return <div className="text-[13px] text-muted">{c.label}: 결과 없음</div>;
  if (!cell) {
    return <div className="self-start rounded-md bg-warn-bg px-2 py-1 text-[13px] text-warn">여기서 끝나는 조각 없음</div>;
  }
  if (same) return <div className="self-start text-[13px] text-muted">기준과 같은 조각 · {cell.tokens} 토큰</div>;
  return (
    <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
            className={`block min-w-0 self-start rounded-md border-l-[3px] pl-2 text-left ${base ? "border-line" : "border-primary"}`}>
      <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
        <span className="tabular-nums">{cell.tokens} 토큰</span>
        <span>{cell.type}</span>
        {cell.title_lost && <Badge tone="warn">표 제목 빠짐</Badge>}
      </div>
      <p className={`mt-0.5 whitespace-pre-line break-words text-[14px] leading-relaxed ${open ? "" : "line-clamp-4"}`}>{cell.text}</p>
    </button>
  );
}

function SizeBars({ sizes }: { sizes: Sizes }) {
  const hist = sizes.histogram ?? [];
  const top = Math.max(1, ...hist);
  const labels = [...(sizes.edges ?? []).map((e) => `≤${e}`), ">800"];
  return (
    <div className="mt-4">
      <div className="text-[13px] text-muted tabular-nums">
        토큰 중앙 {sizes.p50} · p90 {sizes.p90} · 최대 {sizes.max}
      </div>
      <div className="mt-2 flex h-12 items-end gap-1" role="img"
           aria-label={hist.map((v, i) => `${labels[i]} ${v}개`).join(", ")}>
        {hist.map((v, i) => (
          <div key={i} className="flex-1 rounded-t-sm bg-primary/70" style={{ height: `${(v / top) * 100}%` }} title={`${labels[i]} 토큰: ${v}개`} />
        ))}
      </div>
      <div className="mt-1 flex gap-1 text-[11px] text-muted">
        {labels.map((l) => <span key={l} className="flex-1 text-center">{l}</span>)}
      </div>
    </div>
  );
}
