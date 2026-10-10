"use client";

import { useState } from "react";
import type { Candidate, OcrImage, OcrView as View, Read } from "@/lib/api";
import { usd } from "@/lib/api";
import { Badge, CandidateHead, Failure, LedgerLine, Segmented, Stopped } from "./ui";
import { NoteField } from "./note-field";

const STATUS: Record<Read["status"], [string, "ok" | "warn" | "bad" | "neutral" | "info"]> = {
  local: ["로컬 판독", "neutral"], remote: ["gpt-5-mini 재판독", "info"], unresolved: ["미해결", "warn"],
  unreadable: ["판독 불가", "bad"], missing: ["결과 없음", "bad"],
};
const REASON: Record<string, string> = {
  loop: "반복", truncated: "잘림", low_confidence: "낮은 확신", text_invented: "글자 수 과다",
  unrecorded_truncation: "잘림 미기록",
};
const reason = (r: string) => REASON[r] ?? (r.startsWith("unavailable:") ? `디코딩 불가 (${r.slice(12)})` : r);

// The image column, then one column per candidate.
const grid = (n: number) => ({ gridTemplateColumns: `minmax(220px, 320px) repeat(${n}, minmax(260px, 1fr))` });

export function OcrView({ view, notes, setNote }: {
  view: View;
  notes: Record<string, string>;
  setNote: (key: string, note: string) => void;
}) {
  const [filter, setFilter] = useState<"differs" | "all">("differs");
  const differing = view.images.filter((i) => i.differs).length;
  const shown = filter === "all" ? view.images : view.images.filter((i) => i.differs);
  return (
    <>
      <section aria-label="후보별 결과" className="overflow-x-auto">
        <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${view.candidates.length}, minmax(280px, 1fr))` }}>
          {view.candidates.map((c) => <Summary key={c.id} c={c} view={view} />)}
        </div>
        <p className="mt-3 text-[13px] text-muted">
          모든 후보가 같은 이미지 {view.set.flagged + view.set.sample}개를 읽었습니다: 지금 캐시에서 판독 검사에 걸렸거나
          디코딩되지 않은 이미지 {view.set.flagged}개와, 검사를 통과한 이미지 중 고정 표본 {view.set.sample}개(시드{" "}
          {view.set.seed}). 각 후보는 원본 바이트를 직접 디코딩하고 PaddleOCR-VL로 읽은 뒤, 자기 검사에 걸린 이미지만
          gpt-5-mini로 다시 읽습니다.
        </p>
      </section>

      <section aria-label="이미지별 판독" className="mt-10">
        <div className="sticky top-0 z-10 -mx-2 flex flex-wrap items-center gap-3 bg-white/95 px-2 py-3 backdrop-blur">
          <h2 className="mr-2 text-[18px] font-bold">이미지별 판독</h2>
          <Segmented value={filter} onChange={setFilter} options={[
            ["differs", `판독이 다른 이미지 ${differing}`], ["all", `전체 ${view.images.length}`],
          ]} />
          <span className="text-[13px] text-muted">
            <ins className="bg-ok-bg text-ok no-underline">추가</ins> · <del className="bg-bad-bg text-bad">삭제</del> 는 기준 후보의 글과 비교한 글자 차이입니다
          </span>
        </div>
        {shown.length === 0 && <p className="py-10 text-center text-muted">판독이 다른 이미지가 없습니다.</p>}
        <ol className="divide-y divide-line">
          {shown.map((img) => (
            <ImageRow key={img.n} img={img} view={view} note={notes[String(img.n)] ?? ""}
                      setNote={(v) => setNote(String(img.n), v)} />
          ))}
        </ol>
      </section>
    </>
  );
}

function Summary({ c, view }: { c: Candidate; view: View }) {
  const s = view.summary[c.id];
  if (!s || (c.status !== "complete" && c.status !== "stopped")) return <div className="border-t-2 border-ink pt-3"><CandidateHead c={c} /><Failure c={c} /></div>;
  return (
    <div className="border-t-2 border-ink pt-3">
      <CandidateHead c={c} />
      <Stopped c={c} />
      <div className="mt-3 flex flex-wrap items-end gap-x-8 gap-y-3">
        <div>
          <div className="text-[32px] font-bold leading-none tabular-nums">{s.flagged}/{s.read}</div>
          <div className="mt-1 whitespace-nowrap text-[13px] text-muted">검사에 걸린 이미지 · 읽은 이미지 중</div>
        </div>
        <div>
          <div className="text-[24px] font-bold leading-none tabular-nums">{s.reread}/{s.flagged}</div>
          <div className="mt-1 whitespace-nowrap text-[13px] text-muted">gpt-5-mini로 다시 읽음</div>
        </div>
        <div>
          <div className={`text-[24px] font-bold leading-none tabular-nums ${s.unreadable ? "text-bad" : ""}`}>
            {s.unreadable}/{s.images}
          </div>
          <div className="mt-1 whitespace-nowrap text-[13px] text-muted">판독 불가 (OCR 안 됨)</div>
        </div>
      </div>
      <p className="mt-3 text-[14px] tabular-nums">
        <span className="text-muted">미해결</span> {s.unresolved}/{s.flagged}
        <span className="ml-4 text-muted">비용</span> {usd(s.spent_micro_usd)}
        {s.not_reached > 0 && <span className="ml-4 text-warn">도달 못한 이미지 {s.not_reached}개</span>}
      </p>
      <LedgerLine ledger={s.ledger ?? c.ledger} />
    </div>
  );
}

function ImageRow({ img, view, note, setNote }: {
  img: OcrImage; view: View; note: string; setNote: (v: string) => void;
}) {
  return (
    <li className="py-8">
      <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
        <Badge tone={img.sample ? "neutral" : "warn"}>{img.sample ? "표본" : "검사에 걸림"}</Badge>
        <span className="font-semibold text-ink">{img.title ?? img.doc_id}</span>
        <code>{img.where}</code>
        {img.cached_reasons.length > 0 && <span>캐시 판정: {img.cached_reasons.map(reason).join(", ")}</span>}
      </div>
      <div className="mt-3 overflow-x-auto">
        <div className="grid gap-5" style={grid(view.candidates.length)}>
          <div className="min-w-0">
            {img.shown ? (
              <a href={`/api/runs/${view.run_id}/images/${img.n}.png`} target="_blank" rel="noreferrer">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={`/api/runs/${view.run_id}/images/${img.n}.png`} alt={`${img.title ?? ""} ${img.where} 원본 이미지`}
                     className="max-h-[420px] w-full rounded-md border border-line object-contain" />
              </a>
            ) : (
              <p className="rounded-md bg-surface px-3 py-6 text-center text-[13px] text-muted">이 기기에서 그릴 수 없는 이미지</p>
            )}
          </div>
          {view.candidates.map((c, i) => <ReadColumn key={c.id} c={c} r={img.candidates[c.id]} base={i === 0} />)}
        </div>
      </div>
      <div className="mt-3"><NoteField value={note} onChange={setNote} /></div>
    </li>
  );
}

function ReadColumn({ c, r, base }: { c: Candidate; r: Read | undefined; base: boolean }) {
  const [local, setLocal] = useState(false);
  if (!r) return <div className="text-[13px] text-muted">{c.label}: 결과 없음</div>;
  const [label, tone] = STATUS[r.status];
  const same = !base && r.diff?.every(([tag]) => tag === "equal");
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-2 border-b border-line pb-1.5">
        <span className="truncate text-[14px] font-semibold text-muted">{c.label}</span>
        <Badge tone={tone}>{label}</Badge>
        {(r.reasons ?? []).map((x) => <Badge key={x} tone="warn">{reason(x)}</Badge>)}
        {r.micro_usd ? <span className="ml-auto text-[13px] tabular-nums text-muted">{usd(r.micro_usd)}</span> : null}
      </div>
      {r.error && <p className="mt-1 text-[13px] text-bad">{r.error}</p>}
      {same ? (
        <p className="mt-2 text-[13px] text-muted">기준과 같은 글</p>
      ) : (
        <p className="mt-2 max-h-[420px] overflow-y-auto whitespace-pre-wrap break-words font-mono text-[14px] leading-relaxed">
          {base || !r.diff ? (r.text || <span className="text-muted">(글자 없음)</span>) : <Diff ops={r.diff} />}
        </p>
      )}
      {r.status === "remote" && r.local_text != null && (
        <button type="button" onClick={() => setLocal(!local)} className="mt-1 text-[13px] font-medium text-primary hover:underline">
          {local ? "로컬 판독 접기" : "로컬 판독 보기"}
        </button>
      )}
      {local && <p className="mt-1 whitespace-pre-wrap break-words font-mono text-[13px] text-muted">{r.local_text}</p>}
    </div>
  );
}

function Diff({ ops }: { ops: [string, string, string][] }) {
  return (
    <>
      {ops.map(([tag, a, b], i) => tag === "equal" ? <span key={i}>{b}</span> : (
        <span key={i}>
          {a && <del className="bg-bad-bg text-bad">{a}</del>}
          {b && <ins className="bg-ok-bg text-ok no-underline">{b}</ins>}
        </span>
      ))}
    </>
  );
}
