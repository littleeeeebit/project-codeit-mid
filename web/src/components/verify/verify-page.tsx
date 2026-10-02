"use client";

// 검증. Who comes here: a verifier deciding whether the assistant can be trusted on these documents. They clear what
// waits for a person (flagged originals, disputed gold rows), reproduce a question's retrieval and read its
// evidence, and read the evaluation and release verdict. The user picked the Linear-style left menu with counts
// over summary tiles with tabs and a to-do inbox page (DESIGN.md, 검증).

import { useState } from "react";
import { api, errorText, type Schemas } from "@/lib/api";
import { must, usePoll } from "@/lib/use-poll";
import { type MenuGroup, SideMenu } from "@/components/side-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { DatasetState, SecondReviewDetail, type Waiting, WaitingRow } from "./gold";
import { EvaluationSection, ReleaseBadge } from "./evaluation";
import { FidelityDetail, FidelityRow, flagged, type Source } from "./fidelity";
import { CorrectionsSection, IngestionSection, RequestExports } from "./records";
import { CompareRuns, RunList, RunView, TraceForm } from "./trace";
import { Empty, Notice, PageTitle } from "./parts";

type Key = "todo" | "trace" | "compare" | "evaluation" | "dataset" | "ingestion" | "corrections" | "exports";

function useVerifyData() {
  const [running, setRunning] = useState(false);
  const ov = usePoll("verify-overview", () => must(api.GET("/api/verify/overview"), errorText), running ? 2000 : null);
  const fid = usePoll("verify-fidelity", () => must(api.GET("/api/verify/fidelity"), errorText), null);
  const ing = usePoll("verify-ingestion", () => must(api.GET("/api/verify/ingestion"), errorText), null);
  const runs = usePoll("verify-runs", () => must(api.GET("/api/verify/traces"), errorText), null);
  const isRunning = !!ov.data?.evaluation.answer_runs.some((r) => r.running);
  if (isRunning !== running) setRunning(isRunning);  // poll the overview only while an evaluation runs
  return { ov, fid, ing, runs };
}
type Data = ReturnType<typeof useVerifyData>;

export function VerifyPage() {
  const data = useVerifyData();
  const [section, setSection] = useState<Key>("todo");
  const error = data.ov.error ?? data.fid.error;
  return (
    <div className="mx-auto w-full max-w-[1440px] flex-1 px-6 py-8 lg:px-10">
      {error ? <Notice tone="bad">{error}</Notice> : !data.ov.data || !data.fid.data ? <Skeleton className="h-96 w-full" /> : (
        <>
          <SidebarLayout d={data} section={section} onSection={setSection} />
          <p className="mt-12 font-mono text-[11px] text-muted-foreground">빌드 {data.ov.data.build}</p>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- sections

function todoCount(d: Data) {
  return flagged(d.fid.data ?? []).length + (d.ov.data?.awaiting_second_review.length ?? 0);
}

const TITLES: Record<Key, string> = {
  todo: "할 일", trace: "검색 추적", compare: "실행 비교", evaluation: "평가·릴리스", dataset: "데이터셋",
  ingestion: "수집 상태", corrections: "수정 기록", exports: "요청 기록",
};

function Body({ k, d }: { k: Key; d: Data }) {
  const ov = d.ov.data!;
  const runs = d.runs.data ?? [];
  switch (k) {
    case "todo": return <Todo d={d} />;
    case "trace": return <TracePane runs={runs} onRun={d.runs.reload} />;
    case "compare": return <CompareRuns runs={runs} />;
    case "evaluation": return <EvaluationSection ov={ov} onChanged={d.ov.reload} />;
    case "dataset": return <DatasetState ov={ov} />;
    case "ingestion": return d.ing.data ? <IngestionSection rows={d.ing.data} /> : <Skeleton className="h-64 w-full" />;
    case "corrections": return <CorrectionsSection runs={runs} />;
    case "exports": return <RequestExports />;
  }
}

function TwoPane({ list, detail, label }: { list: React.ReactNode; detail: React.ReactNode; label: string }) {
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(280px,360px)_minmax(0,1fr)]">
      <nav aria-label={label} className="lg:sticky lg:top-20 lg:max-h-[calc(100dvh-7rem)] lg:overflow-y-auto">{list}</nav>
      <div className="min-w-0">{detail}</div>
    </div>
  );
}

type Item = { kind: "fidelity"; s: Source } | { kind: "second"; w: Waiting };
const itemId = (i: Item) => (i.kind === "fidelity" ? i.s.source_hash : i.w.candidate_id);

/** Everything waiting for a person, second reviews first (they gate gold), then flagged originals. */
function Todo({ d }: { d: Data }) {
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const sources = d.fid.data ?? [];
  const items: Item[] = [
    ...(d.ov.data?.awaiting_second_review ?? []).map((w) => ({ kind: "second" as const, w })),
    ...(all ? sources : flagged(sources)).map((s) => ({ kind: "fidelity" as const, s })),
  ];
  const cur = items.find((i) => itemId(i) === open) ?? items[0];
  const done = () => { d.fid.reload(); d.ov.reload(); };
  const list = (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2 px-1">
        <p className="text-[13px] font-semibold text-muted-foreground">{all ? `HWP 원문 ${sources.length}개` : `확인 필요 ${items.length}건`}</p>
        <button type="button" onClick={() => setAll(!all)} aria-pressed={all}
                className="rounded-md px-2 py-1 text-[13px] font-semibold text-primary outline-none hover:bg-accent focus-visible:ring-3 focus-visible:ring-ring/50">
          {all ? "확인 필요만" : "원문 전체 보기"}
        </button>
      </div>
      <ul className="space-y-0.5">
        {items.map((i) => (
          <li key={itemId(i)}>
            {i.kind === "second"
              ? <WaitingRow w={i.w} active={cur === i} onOpen={() => setOpen(itemId(i))} />
              : <FidelityRow s={i.s} active={cur === i} onOpen={() => setOpen(itemId(i))} />}
          </li>
        ))}
      </ul>
    </div>
  );
  const detail = !cur ? <Empty>사람이 확인할 곳이 없습니다.</Empty>
    : cur.kind === "second" ? <SecondReviewDetail key={itemId(cur)} w={cur.w} onDone={done} />
      : <FidelityDetail key={itemId(cur)} s={cur.s} onConfirmed={done} />;
  return <TwoPane label="할 일 목록" list={list} detail={detail} />;
}

function TracePane({ runs, onRun }: { runs: Schemas["TraceSummary"][]; onRun: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const active = open ?? runs[0]?.run_id ?? null;
  return (
    <TwoPane label="검색 추적 실행" list={
      <div className="space-y-6">
        <TraceForm onRun={(id) => { setOpen(id); onRun(); }} />
        <div className="space-y-2 border-t pt-4">
          <p className="px-1 text-[13px] font-semibold text-muted-foreground">최근 실행</p>
          <RunList runs={runs} active={active} onOpen={setOpen} />
        </div>
      </div>
    } detail={active ? <RunView key={active} runId={active} /> : <Empty>질문과 문서를 고르고 검색을 실행하면 다섯 단계가 여기에 열립니다.</Empty>} />
  );
}

function SidebarLayout({ d, section, onSection }: { d: Data; section: Key; onSection: (k: Key) => void }) {
  const ov = d.ov.data!;
  const groups: MenuGroup<Key>[] = [
    { title: "사람이 볼 차례", items: [{ k: "todo", count: todoCount(d), urgent: todoCount(d) > 0 }] },
    { title: "재현", items: [{ k: "trace", count: d.runs.data?.length }, { k: "compare" }] },
    { title: "현황", items: [
      { k: "evaluation", count: <ReleaseBadge release={ov.evaluation.release} /> },
      { k: "dataset", count: ov.evaluation.dev_validation?.rows ?? "-" },
      { k: "ingestion", count: d.ing.data?.length },
    ] },
    { title: "기록", items: [{ k: "corrections" }, { k: "exports" }] },
  ];
  return (
    <SideMenu title="검증" groups={groups} titles={TITLES} section={section} onSection={onSection}>
      <PageTitle title={TITLES[section]}>{section === "todo" ? "자동 대조가 표시한 원문과 쟁점 표시된 승인 질문입니다. 표시된 곳만 보면 됩니다." : undefined}</PageTitle>
      <Body k={section} d={d} />
    </SideMenu>
  );
}
