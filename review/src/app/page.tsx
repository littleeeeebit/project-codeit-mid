"use client";

// Who comes here: the person deciding whether a retriever, chunking, generation or OCR change ships. What for: see,
// on the same questions, documents or images, where each candidate ref differs from the baseline, then record which
// one to keep and why.

import { useCallback, useEffect, useRef, useState } from "react";
import type { Decision, RunListItem, RunView } from "@/lib/api";
import { getJson, hostLabel, postJson, short, when } from "@/lib/api";
import { Badge } from "@/components/ui";
import { RetrieverView } from "@/components/retriever-view";
import { ChunkingView } from "@/components/chunking-view";
import { GenerationView } from "@/components/generation-view";
import { OcrView } from "@/components/ocr-view";
import { DecisionPanel } from "@/components/decision-panel";

const STAGE = { retriever: "리트리버", chunking: "청킹", generation: "답변 생성", ocr: "OCR" } as const;

export default function Page() {
  const [runs, setRuns] = useState<RunListItem[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<RunView | null>(null);
  const [decision, setDecision] = useState<Decision | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");

  const loadRuns = useCallback(async () => {
    const list = await getJson<RunListItem[]>("/api/runs");
    setRuns(list);
    return list;
  }, []);

  useEffect(() => {
    loadRuns().then((list) => {
      const wanted = new URLSearchParams(window.location.search).get("run");
      setSelected(list.find((r) => r.run_id === wanted)?.run_id ?? list[0]?.run_id ?? null);
    }).catch((e: Error) => setMessage(`실행 목록을 읽지 못했습니다: ${e.message}`));
  }, [loadRuns]);

  useEffect(() => {
    if (!selected) return;
    let live = true;
    window.history.replaceState(null, "", `?run=${selected}`);
    setView(null);
    Promise.all([getJson<RunView>(`/api/runs/${selected}`), getJson<Decision | null>(`/api/runs/${selected}/decision`)])
      .then(([v, d]) => {
        if (!live) return;
        setView(v);
        setDecision(d);
        setNotes(Object.fromEntries((d?.notes ?? []).map((n) => [n.item, n.note])));
      })
      .catch((e: Error) => live && setMessage(`실행을 읽지 못했습니다: ${e.message}`));
    return () => { live = false; };
  }, [selected]);

  const setNote = useCallback((key: string, note: string) => setNotes((n) => ({ ...n, [key]: note })), []);
  const current = runs?.find((r) => r.run_id === selected);

  return (
    <div className="flex min-h-screen">
      <RunRail runs={runs} selected={selected} onSelect={setSelected}
               onImported={async (id) => { await loadRuns(); setSelected(id); setMessage(""); }}
               onError={setMessage} />
      <main className="min-w-0 flex-1 px-10 pb-24 pt-8">
        {message && <p role="alert" className="mb-4 rounded-[10px] bg-bad-bg px-4 py-3 text-[14px] text-bad">{message}</p>}
        {runs && runs.length === 0 && <EmptyState />}
        {current && view && (
          <>
            <header className="flex flex-wrap items-start gap-4">
              <div className="min-w-0">
                <h1 className="text-[24px] font-bold">{STAGE[view.stage]} 비교</h1>
                <p className="mt-1 text-[14px] text-muted">
                  기준 {view.candidates[0]?.label} <code>{short(view.candidates[0]?.commit ?? "")}</code> · 후보{" "}
                  {view.candidates.length - 1}개 · {when(current.created_at)}
                  {current.imported && ` · ${when(current.imported.imported_at)} 가져옴`} · <code>{view.run_id}</code>
                </p>
              </div>
              <div className="ml-auto flex items-center gap-3">
                <a href="#decision" className="h-10 rounded-[10px] bg-primary px-4 text-[14px] font-semibold leading-10 text-white">
                  {current.decided ? "결정 다시 보기" : "결정하러 가기"}
                </a>
                <a href={`/api/runs/${view.run_id}/export`} className="h-10 rounded-[10px] bg-surface px-4 text-[14px] font-semibold leading-10">
                  내보내기 (.zip)
                </a>
              </div>
            </header>
            <div className="mt-8">
              {view.stage === "retriever" && <RetrieverView view={view} notes={notes} setNote={setNote} />}
              {view.stage === "chunking" && <ChunkingView view={view} notes={notes} setNote={setNote} />}
              {view.stage === "generation" && <GenerationView view={view} notes={notes} setNote={setNote} />}
              {view.stage === "ocr" && <OcrView view={view} notes={notes} setNote={setNote} />}
            </div>
            <DecisionPanel key={view.run_id} view={view} notes={notes} initial={decision} onSaved={loadRuns} />
          </>
        )}
        {selected && !view && !message && <p className="text-muted">불러오는 중…</p>}
      </main>
    </div>
  );
}

function RunRail({ runs, selected, onSelect, onImported, onError }: {
  runs: RunListItem[] | null;
  selected: string | null;
  onSelect: (id: string) => void;
  onImported: (id: string) => Promise<void>;
  onError: (m: string) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  async function upload(file: File) {
    setBusy(true);
    try {
      const { run_id } = await postJson<{ run_id: string }>("/api/import", file, "application/zip");
      await onImported(run_id);
    } catch (e) {
      onError(`가져오지 못했습니다: ${(e as Error).message}`);
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }
  return (
    <nav aria-label="비교 실행" className="sticky top-0 flex h-screen w-[280px] shrink-0 flex-col border-r border-line bg-surface">
      <div className="px-5 pb-3 pt-6">
        <div className="text-[18px] font-bold">단계 비교 검토</div>
        <p className="mt-1 text-[13px] text-muted">실행은 CLI가 만들고, 이 화면은 보기와 결정만 합니다.</p>
        <button type="button" disabled={busy} onClick={() => input.current?.click()}
                className="mt-3 h-9 w-full rounded-[10px] border border-line bg-white text-[14px] font-semibold disabled:text-muted">
          {busy ? "가져오는 중…" : "실행 폴더 가져오기 (.zip)"}
        </button>
        <input ref={input} type="file" accept=".zip,application/zip" className="hidden"
               onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
      </div>
      <ol className="flex-1 overflow-y-auto px-3 pb-6">
        {runs?.map((r) => {
          const failed = r.candidates.filter((c) => c.status !== "complete").length;
          return (
            <li key={r.run_id}>
              <button type="button" onClick={() => onSelect(r.run_id)} aria-current={r.run_id === selected}
                      className={`mt-1 block w-full rounded-[10px] px-3 py-2.5 text-left ${r.run_id === selected ? "bg-white shadow-sm" : "hover:bg-white/60"}`}>
                <div className="flex items-center gap-2">
                  <span className="text-[15px] font-semibold">{STAGE[r.stage]}</span>
                  <span className="text-[13px] text-muted">{when(r.created_at)}</span>
                  {r.decided && <span className="ml-auto"><Badge tone="ok">결정됨</Badge></span>}
                </div>
                <div className="mt-0.5 truncate text-[13px] text-muted">
                  {r.candidates.slice(1).map((c) => c.label).join(", ")}
                </div>
                <div className="mt-0.5 flex flex-wrap gap-x-2 text-[13px] text-muted">
                  <span>{hostLabel(r.candidates[0]?.host)}</span>
                  {r.imported && <span>가져옴</span>}
                  {failed > 0 && <span className="text-bad">결과 없음 {failed}</span>}
                </div>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function EmptyState() {
  return (
    <div className="max-w-[720px]">
      <h1 className="text-[24px] font-bold">아직 비교 실행이 없습니다</h1>
      <p className="mt-3 text-muted">
        저장소 루트에서 실행을 만든 뒤 이 화면을 새로 고치세요. 다른 기기에서 만든 실행은 왼쪽에서 가져옵니다. 답변 생성과
        OCR은 비용 추정에서 멈추고, 승인한 뒤 이어서 실행한 결과만 여기에 나타납니다.
      </p>
      <pre className="mt-4 overflow-x-auto rounded-[10px] bg-surface p-4 text-[14px]">
        python -m rfp_assistant.cli stage-review run retriever --base main --cand {"<branch>"}{"\n"}
        python -m rfp_assistant.cli stage-review run chunking --base main --cand .{"\n"}
        python -m rfp_assistant.cli stage-review run generation --base main --cand {"<branch>"}{"\n"}
        python -m rfp_assistant.cli stage-review approve --run {"<run id>"} --approved-by {"<name>"}{"\n"}
        python -m rfp_assistant.cli stage-review resume --run {"<run id>"}
      </pre>
    </div>
  );
}
