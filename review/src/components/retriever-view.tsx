"use client";

import { useState } from "react";
import type { Candidate, Passage, Question, QuestionResult, RetrieverView as View } from "@/lib/api";
import { Badge, CandidateHead, Failure, Segmented, columns } from "./ui";
import { NoteField } from "./note-field";

type Filter = "outcome" | "ranking" | "all";

export function RetrieverView({ view, notes, setNote }: {
  view: View;
  notes: Record<string, string>;
  setNote: (key: string, note: string) => void;
}) {
  const [filter, setFilter] = useState<Filter>("outcome");
  const [population, setPopulation] = useState<"all" | "dev" | "needle">("all");
  const inPopulation = view.questions.filter((q) => population === "all" || q.population === population);
  const counts = {
    outcome: inPopulation.filter((q) => q.outcome_differs ?? q.differs).length,
    ranking: inPopulation.filter((q) => q.differs).length,
    all: inPopulation.length,
  };
  const shown = inPopulation.filter((q) =>
    filter === "all" ? true : filter === "ranking" ? q.differs : (q.outcome_differs ?? q.differs));
  return (
    <>
      <section aria-label="후보별 결과" className="overflow-x-auto">
        <div className="grid gap-4" style={columns(view.candidates.length)}>
          {view.candidates.map((c) => <Summary key={c.id} c={c} view={view} />)}
        </div>
        <p className="mt-3 text-[13px] text-muted">
          모든 후보는 서빙 중인 설정({view.activation.run_id ?? "기본"} · {view.activation.mode} · 색인{" "}
          {view.activation.index_version})에서 시작하고, 후보의 코드와 review-variant.json만 다릅니다. 같은 질문{" "}
          {view.populations.dev?.rows ?? 0}개(개발)와 니들 {view.populations.needle?.rows ?? 0}개를 모두 같은 채점기로
          매겼습니다.
        </p>
      </section>

      <section aria-label="질문별 비교" className="mt-10">
        <div className="sticky top-0 z-10 -mx-2 flex flex-wrap items-center gap-3 bg-white/95 px-2 py-3 backdrop-blur">
          <h2 className="mr-2 text-[18px] font-bold">질문별 상위 결과</h2>
          <Segmented value={filter} onChange={setFilter} options={[
            ["outcome", `정답 결과가 다른 질문 ${counts.outcome}`],
            ["ranking", `상위 5개가 다른 질문 ${counts.ranking}`],
            ["all", `전체 ${counts.all}`],
          ]} />
          <Segmented value={population} onChange={setPopulation} options={[
            ["all", "모두"], ["dev", "개발 질문"], ["needle", "니들"],
          ]} />
        </div>
        {shown.length === 0 && (
          <p className="py-10 text-center text-muted">
            {Object.keys(view.summary).length < 2
              ? "결과가 있는 후보가 하나뿐이라 비교할 차이가 없습니다. '전체'에서 질문별 결과를 봅니다."
              : "이 조건에서 후보 간 차이가 나는 질문이 없습니다."}
          </p>
        )}
        <ol className="divide-y divide-line">
          {shown.map((q) => (
            <QuestionRow key={q.key} q={q} candidates={view.candidates} note={notes[q.key] ?? ""}
                         setNote={(v) => setNote(q.key, v)} />
          ))}
        </ol>
      </section>
    </>
  );
}

function Summary({ c, view }: { c: Candidate; view: View }) {
  const s = view.summary[c.id];
  return (
    <div className="border-t-2 border-ink pt-3">
      <CandidateHead c={c} />
      {c.status !== "complete" || !s ? <Failure c={c} /> : (
        <div className="mt-3 flex flex-wrap items-end gap-x-8 gap-y-3">
          <div>
            <div className="text-[32px] font-bold leading-none tabular-nums">{s.ndcg5?.toFixed(3) ?? "—"}</div>
            <div className="mt-1 whitespace-nowrap text-[13px] text-muted">nDCG@5 · 개발 질문 {s.ndcg5_n}/{s.dev_rows}개 채점</div>
          </div>
          <div>
            <div className="text-[24px] font-bold leading-none tabular-nums">{s.needle_hits}/{s.needle_rows}</div>
            <div className="mt-1 whitespace-nowrap text-[13px] text-muted">니들 top-5 적중</div>
          </div>
        </div>
      )}
      {s && s.errors > 0 && <p className="mt-2 text-[13px] text-bad">채점하지 못한 질문 {s.errors}개</p>}
    </div>
  );
}

function QuestionRow({ q, candidates, note, setNote }: {
  q: Question; candidates: Candidate[]; note: string; setNote: (v: string) => void;
}) {
  const [more, setMore] = useState(false);
  return (
    <li className="py-8">
      <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
        <Badge tone={q.population === "dev" ? "info" : "neutral"}>{q.population === "dev" ? "개발" : "니들"}</Badge>
        <span>{q.type}</span>
        <code>{q.id}</code>
      </div>
      <h3 className="mt-2 max-w-[960px] text-[17px] font-semibold leading-snug">{q.question}</h3>
      <div className="mt-3 max-w-[960px] text-[14px]">
        <span className="mr-2 font-semibold text-muted">정답 근거</span>
        {q.gold.map((g) => (
          <span key={g.group_id} className="mr-2 inline rounded bg-cite px-1 leading-7">{g.quotes[0]}</span>
        ))}
      </div>
      <div className="mt-4 overflow-x-auto">
        <div className="grid gap-4" style={columns(candidates.length)}>
          {candidates.map((c) => (
            <Column key={c.id} c={c} r={q.candidates[c.id]} depth={more ? 10 : 5} showDoc={q.population === "needle"} />
          ))}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-start gap-4">
        <button type="button" onClick={() => setMore(!more)} className="h-7 text-[14px] font-medium text-primary hover:underline">
          {more ? "상위 5개만 보기" : "6–10위까지 보기"}
        </button>
        <NoteField value={note} onChange={setNote} />
      </div>
    </li>
  );
}

function Column({ c, r, depth, showDoc }: {
  c: Candidate; r: QuestionResult | undefined; depth: number; showDoc: boolean;
}) {
  if (c.status !== "complete" || !r) {
    return <div className="rounded-[10px] bg-surface px-3 py-2 text-[14px] text-muted">{c.label}: 결과 없음</div>;
  }
  if (r.error) return <div className="rounded-[10px] bg-bad-bg px-3 py-2 text-[14px] text-bad">{c.label}: {r.error}</div>;
  return (
    <div className="min-w-0">
      <div className="flex items-baseline gap-3 border-b border-line pb-1.5">
        <span className="truncate text-[14px] font-semibold text-muted">{c.label}</span>
        <span className="ml-auto whitespace-nowrap text-[15px] font-bold tabular-nums">
          {r.gold_rank ? `정답 ${r.gold_rank}위` : (r.sides?.length ?? 0) > 1 ? "문서별 검색" : "top-20 밖"}
        </span>
        <span className="whitespace-nowrap text-[13px] tabular-nums text-muted">nDCG {r.ndcg?.toFixed(2) ?? "—"}</span>
      </div>
      {r.fallback && <p className="mt-1 text-[13px] text-warn">대체 검색: {r.fallback}</p>}
      {r.sides?.map((side, i) => (
        <div key={i}>
          {(r.sides?.length ?? 0) > 1 && <div className="mt-2 text-[13px] font-semibold text-muted">문서 {r.side_docs?.[i]?.slice(0, 8)}</div>}
          <ol>{side.slice(0, depth).map((p) => <PassageItem key={p.chunk_id + p.rank} p={p} showDoc={showDoc} />)}</ol>
        </div>
      ))}
    </div>
  );
}

function PassageItem({ p, showDoc }: { p: Passage; showDoc: boolean }) {
  // Scoped questions search one document, so only its section helps; whole-corpus needles also need the document.
  const where = [showDoc ? p.doc : null, p.section].filter(Boolean).join(" · ");
  const [open, setOpen] = useState(false);
  const mark = p.grade === 2 ? "border-ok bg-ok-bg/50" : p.grade === 1 ? "border-warn bg-warn-bg/50" : "border-transparent";
  return (
    <li className={`mt-1.5 border-l-[3px] py-1 pl-2 ${mark}`}>
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="block w-full text-left">
        <div className="flex min-w-0 items-center gap-2 text-[13px] text-muted">
          <span className="w-5 shrink-0 font-bold tabular-nums text-ink">{p.rank}</span>
          {p.grade === 2 && <Badge tone="ok">정답</Badge>}
          {p.grade === 1 && <Badge tone="warn">부분</Badge>}
          <span className="truncate" title={[p.doc, p.section].filter(Boolean).join(" · ")}>{where}</span>
        </div>
        <p className={`mt-0.5 whitespace-pre-line break-words text-[14px] leading-relaxed ${open ? "" : "line-clamp-3"}`}>{p.text}</p>
      </button>
    </li>
  );
}
