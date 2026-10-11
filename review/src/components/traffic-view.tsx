"use client";

// Who comes here: the person doing error analysis on real consultant answers. What for: read a small, explained
// sample of one window of 질문하기 traffic with the same notes as a gold run, see on one map what the sample leaves
// out, and pull any request from there into the sample. The sample is drawn by rules (traffic_review.py), so it is a
// guess: the map, not the sample, is the whole window.

import { useEffect, useMemo, useRef, useState } from "react";
import type { GenerationQuestion, RunFailures, TrafficGroup, TrafficView as View } from "@/lib/api";
import { getJson } from "@/lib/api";
import { label, VALIDATION } from "@/lib/labels";
import { Badge, Metric, Metrics, Segmented } from "./ui";
import { AnnotationProvider, useAnnotationVersion, useReview, useSample } from "./annotations";
import { FailureTypes, MODE, OUTCOME, ReadingView } from "./generation-view";

// passed: an answer call settled and its output passed the validator; not_validated: no answer call to check;
// none: a run frozen before the two were told apart, so only the absence of a failure is known.
const CHECK: Record<string, string> = {
  passed: "검증 통과", not_validated: "검증 안 함", none: "검증 실패 기록 없음", processing_failure: "처리 실패",
};
const failed = (g: TrafficGroup) => !["passed", "not_validated", "none"].includes(g.validation);
const check = (v: string) => CHECK[v] ?? label(VALIDATION, v);
const groupName = (g: TrafficGroup) => `${label(MODE, g.mode)} · ${OUTCOME[g.outcome] ?? g.outcome} · ${check(g.validation)}`;
const day = (iso?: string) => (iso ? new Date(iso).toLocaleDateString("ko-KR", { month: "numeric", day: "numeric" }) : "");

export function TrafficView({ view }: { view: View }) {
  return (
    <AnnotationProvider key={view.run_id} run={view.run_id}>
      <Traffic view={view} />
    </AnnotationProvider>
  );
}

type Reading = { list: GenerationQuestion[]; at: number; back: "sample" | "map" };

function Traffic({ view }: { view: View }) {
  const c = view.candidates[0];
  const review = useReview();
  const sample = useSample();
  const [tab, setTab] = useState<"sample" | "map">("sample");
  const [only, setOnly] = useState<"all" | "uncovered">("all");
  const [reading, setReading] = useState<Reading | null>(null);
  const byKey = useMemo(() => new Map(view.questions.map((q) => [q.key, q])), [view.questions]);
  const groupOf = useMemo(() => new Map(view.groups.map((g) => [g.key, g])), [view.groups]);

  // The sample: what the rules drew, then what the person added by hand, each with why it is there.
  const drawn = new Set(view.sample.map((s) => s.key));
  const picked: { q: GenerationQuestion; kind: string; why: string }[] = [
    ...view.sample.map((s) => ({ q: byKey.get(s.key)!, kind: s.kind === "representative" ? "묶음 대표" : "추가 표본", why: s.reason })),
    ...[...sample.added.entries()].filter(([k]) => !drawn.has(k) && byKey.has(k)).map(([k, s]) => ({
      q: byKey.get(k)!, kind: "직접 추가", why: `직접 추가 · ${s.author}${s.reason ? ` · ${s.reason}` : ""}`,
    })),
  ];
  const inSample = new Map(picked.map((p) => [p.q.key, p.why]));
  const read = (q: GenerationQuestion) => review.reviewed(q.key, c.id);
  const noted = (q: GenerationQuestion) => review.noteCount(q.key, c.id) > 0;
  const covered = view.groups.filter((g) => g.requests.some((k) => inSample.has(k))).length;
  const withPick = (q: GenerationQuestion) => ({ ...q, pick: inSample.get(q.key) });

  // Failure-type counts come from the server's note store: read again after every note or mark.
  const [failures, setFailures] = useState<RunFailures | null>(null);
  const version = useAnnotationVersion();
  const asked = useRef(0);
  useEffect(() => {
    const mine = ++asked.current;
    getJson<RunFailures>(`/api/runs/${view.run_id}/failure-types`).then(
      (f) => mine === asked.current && setFailures(f), () => mine === asked.current && setFailures(null));
  }, [view.run_id, version]);

  return (
    <>
      <section aria-label="표본에 대한 주의" className="max-w-[980px] rounded-[12px] bg-warn-bg px-5 py-4">
        <p className="text-[17px] font-bold text-warn">이 표본은 추측입니다. 모든 경우를 덮지 않습니다.</p>
        <p className="mt-1 text-[15px] leading-relaxed text-warn">
          규칙으로 묶은 {view.groups.length}개 묶음에서 {picked.length}건을 골랐을 뿐입니다. 표본이 닿지 않은 묶음{" "}
          <b>{view.groups.length - covered}개</b>가 남아 있고, 규칙이 나누지 못한 차이는 묶음 안에 숨어 있을 수 있습니다.
          전체 지도에서 어떤 요청이든 열어 보고 표본에 넣을 수 있습니다.
        </p>
        <p className="mt-2 text-[13px] text-warn">{view.rule} 시드 {view.seed}.</p>
      </section>

      <div className="mt-8 grid max-w-[1180px] grid-cols-[minmax(0,1fr)_minmax(260px,360px)] gap-10">
        <Metrics>
          <Metric lead value={`${picked.filter((p) => read(p.q)).length}/${picked.length}`} label="읽은 표본" />
          <Metric value={view.questions.filter(noted).length} label="메모 달린 답변" tone="text-bad" />
          <Metric value={`${covered}/${view.groups.length}`} label="표본이 닿은 묶음" />
          <Metric value={view.questions.length} label="이 기간의 요청 (전체 지도)" />
        </Metrics>
        <div>
          {failures && failures.types.length > 0
            ? <FailureTypes f={failures} c={c} />
            : <p className="text-[14px] text-muted">실패 유형은 골드 실행과 같은 분류를 씁니다. 여기 메모는 &apos;실제 요청&apos;으로 표시되어 같은 유형에 모입니다.</p>}
        </div>
      </div>

      <section aria-label="요청" className="mt-10">
        <div className="sticky top-0 z-10 -mx-2 flex flex-wrap items-center gap-3 bg-white/95 px-2 py-3 backdrop-blur">
          {reading ? (
            <button type="button" onClick={() => setReading(null)}
                    className="h-9 rounded-[10px] bg-surface px-3 text-[14px] font-semibold">← {reading.back === "map" ? "지도로" : "표본으로"}</button>
          ) : (
            <Segmented value={tab} onChange={setTab}
                       options={[["sample", `표본 ${picked.length}`], ["map", `전체 지도 ${view.questions.length}`]]} />
          )}
          <span className="text-[13px] text-muted">
            <Dot kind="noted" /> 메모 <Dot kind="read" /> 검토함 <Dot kind="sampled" /> 표본 <Dot kind="none" /> 표본 밖
          </span>
        </div>

        {reading ? (
          <ReadingView items={reading.list.map((q) => ({ q: withPick(q), c }))} at={reading.at}
                       setAt={(at) => setReading({ ...reading, at })}
                       extra={(q) => <SampleToggle q={q.key} drawn={drawn.has(q.key)} />} />
        ) : tab === "sample" ? (
          <ol className="divide-y divide-line">
            {picked.map((p, i) => (
              <li key={p.q.key}>
                <button type="button" onClick={() => setReading({ list: picked.map((x) => x.q), at: i, back: "sample" })}
                        className="block w-full py-4 text-left hover:bg-surface/60">
                  <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
                    <Badge tone={p.kind === "직접 추가" ? "neutral" : "info"}>{p.kind}</Badge>
                    <span>{groupName(groupOf.get(p.q.group ?? "")!)}</span>
                    <span>{day(p.q.created_at)}</span>
                    <span className="ml-auto"><Marks noted={noted(p.q)} read={read(p.q)} /></span>
                  </div>
                  <p className="mt-1 max-w-[960px] text-[17px] font-semibold leading-snug">{p.q.question}</p>
                  <p className="mt-1 max-w-[960px] truncate text-[14px] text-muted">{p.q.docs.join(" · ") || "전체 문서"}</p>
                  <p className="max-w-[960px] text-[14px] text-muted">{p.why}</p>
                </button>
              </li>
            ))}
          </ol>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-3 pb-2 pt-1">
              <Segmented value={only} onChange={setOnly} options={[
                ["all", `모든 묶음 ${view.groups.length}`], ["uncovered", `표본 밖 묶음 ${view.groups.length - covered}`]]} />
              <span className="text-[14px] text-muted">네모 하나가 요청 하나입니다. 누르면 그 묶음을 한 답변씩 읽습니다.</span>
            </div>
            <ol className="divide-y divide-line">
              {view.groups.filter((g) => only === "all" || !g.requests.some((k) => inSample.has(k))).map((g) => (
                <GroupRow key={g.key} g={g} questions={g.requests.map((k) => byKey.get(k)!)} inSample={inSample}
                          read={read} noted={noted} drawn={drawn}
                          open={(list, at) => setReading({ list, at, back: "map" })} />
              ))}
            </ol>
          </>
        )}
      </section>
    </>
  );
}

/** One request's square on the map; the gray of one outside the sample keeps 3:1 against white. */
const SQUARE = {
  noted: "bg-bad", read: "bg-ok", sampled: "bg-white ring-2 ring-inset ring-primary", none: "bg-[#949ba4]",
} as const;

function Dot({ kind }: { kind: keyof typeof SQUARE }) {
  return <span aria-hidden className={`mx-0.5 inline-block size-3 rounded-[3px] align-[-1px] ${SQUARE[kind]}`} />;
}

function Marks({ noted, read }: { noted: boolean; read: boolean }) {
  if (noted) return <span className="font-semibold text-bad">메모 · 검토함</span>;
  return read ? <span className="font-semibold text-ok">검토함</span> : <span>읽지 않음</span>;
}

/** One group of the map: its rule label, how many requests it holds and how many the sample reached, and a square per
 *  request (click one to read the group from it). Opened, it lists every request with the way to add it. */
function GroupRow({ g, questions, inSample, drawn, read, noted, open }: {
  g: TrafficGroup; questions: GenerationQuestion[]; inSample: Map<string, string>; drawn: Set<string>;
  read: (q: GenerationQuestion) => boolean; noted: (q: GenerationQuestion) => boolean;
  open: (list: GenerationQuestion[], at: number) => void;
}) {
  const [shown, setShown] = useState(false);
  const sampled = questions.filter((q) => inSample.has(q.key)).length;
  const kind = (q: GenerationQuestion) => (noted(q) ? "noted" : read(q) ? "read" : inSample.has(q.key) ? "sampled" : "none");
  return (
    <li className="py-3">
      <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-6">
        <button type="button" onClick={() => setShown(!shown)} aria-expanded={shown} className="min-w-0 text-left">
          <span className={`text-[15px] font-semibold ${failed(g) ? "text-bad" : ""}`}>{groupName(g)}</span>
          <span className="ml-2 text-[14px] text-muted">{g.doc}</span>
        </button>
        <span className={`whitespace-nowrap text-[14px] tabular-nums ${sampled ? "text-muted" : "font-semibold text-warn"}`}>
          {g.requests.length}건 · 표본 {sampled}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {questions.map((q, i) => (
          <button key={q.key} type="button" onClick={() => open(questions, i)} title={q.question} aria-label={`${q.question} 읽기`}
                  className={`size-4 rounded-[3px] hover:opacity-70 ${SQUARE[kind(q)]}`} />
        ))}
      </div>
      {shown && (
        <ul className="mt-2 border-l-2 border-line pl-4">
          {questions.map((q, i) => (
            <li key={q.key} className="flex items-baseline gap-3 py-1.5">
              <span className="w-12 shrink-0 text-[13px] tabular-nums text-muted">{day(q.created_at)}</span>
              <button type="button" onClick={() => open(questions, i)} className="min-w-0 flex-1 truncate text-left text-[15px] hover:underline">
                {q.question}
              </button>
              <span className="shrink-0 text-[13px]"><Marks noted={noted(q)} read={read(q)} /></span>
              <SampleToggle q={q.key} drawn={drawn.has(q.key)} compact />
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

/** Add a request of the window to the sample by hand, or take a hand-added one back; a drawn pick stays. */
function SampleToggle({ q, drawn, compact = false }: { q: string; drawn: boolean; compact?: boolean }) {
  const sample = useSample();
  const [error, setError] = useState("");
  if (drawn) return compact ? <span className="w-20 shrink-0 text-right text-[13px] text-primary">표본</span> : null;
  const added = sample.added.has(q);
  const toggle = () => sample.set(q, !added, added ? "" : "지도에서 직접 추가").then(
    () => setError(""), (e: Error) => setError(`저장하지 못했습니다: ${e.message}`));
  return (
    <span className={compact ? "w-20 shrink-0 text-right" : "mt-3 block"}>
      <button type="button" onClick={toggle}
              className={compact ? "text-[13px] font-semibold text-primary hover:underline"
                : "h-9 rounded-[10px] bg-surface px-3 text-[14px] font-semibold"}>
        {added ? "표본에서 빼기" : "표본에 추가"}
      </button>
      {error && <span role="alert" className="block text-[13px] text-bad">{error}</span>}
    </span>
  );
}
