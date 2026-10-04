"use client";

// 검증. Who comes here: a verifier deciding whether the assistant can be trusted on these documents. They clear what
// waits for a person (flagged originals, disputed gold rows), reproduce a question's retrieval and read its
// evidence, and read the evaluation and release verdict. The user picked the Linear-style left menu with counts
// over summary tiles with tabs and a to-do inbox page (DESIGN.md, 검증).

import { useState } from "react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { must, usePoll } from "@/lib/use-poll";
import { type MenuGroup, SideMenu } from "@/components/side-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { DatasetState, SecondReviewDetail, type Waiting, WaitingRow } from "./gold";
import { EvaluationSection, ReleaseBadge } from "./evaluation";
import { FidelityDetail, FidelityRow, flagged, type Source } from "./fidelity";
import { JudgeSection } from "./judges";
import { CorrectionsSection, IngestionSection, RequestExports } from "./records";
import { CompareRuns, RunList, RunView, TraceForm } from "./trace";
import { Empty, Field, field, Notice, PageTitle } from "./parts";

type Key = "todo" | "trace" | "compare" | "evaluation" | "judges" | "dataset" | "ingestion" | "corrections" | "exports";

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
          <details className="mt-12 text-[13px] text-muted-foreground"><summary className="min-h-8 cursor-pointer outline-none focus-visible:ring-3 focus-visible:ring-ring/50">빌드 정보</summary><p className="pt-2 font-mono [overflow-wrap:anywhere]">{data.ov.data.build}</p></details>
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
  todo: "할 일", trace: "검색 추적", compare: "실행 비교", evaluation: "평가·릴리스", judges: "판정 모델 비교", dataset: "데이터셋",
  ingestion: "수집 상태", corrections: "수정 기록", exports: "요청 기록",
};

function Body({ k, d }: { k: Key; d: Data }) {
  const ov = d.ov.data!;
  const runs = d.runs.data ?? [];
  switch (k) {
    case "todo": return <Todo d={d} />;
    case "trace": return d.runs.error ? <Notice tone="bad">{d.runs.error}</Notice> : d.runs.loading ? <Skeleton className="h-64 w-full" /> : <TracePane runs={runs} onRun={d.runs.reload} />;
    case "compare": return d.runs.error ? <Notice tone="bad">{d.runs.error}</Notice> : d.runs.loading ? <Skeleton className="h-64 w-full" /> : <CompareRuns runs={runs} />;
    case "evaluation": return <EvaluationSection ov={ov} onChanged={d.ov.reload} />;
    case "judges": return <JudgeSection />;
    case "dataset": return <DatasetState ov={ov} />;
    case "ingestion": return d.ing.error ? <Notice tone="bad">{d.ing.error}</Notice> : d.ing.data ? <IngestionSection rows={d.ing.data} /> : <Skeleton className="h-64 w-full" />;
    case "corrections": return <CorrectionsSection />;
    case "exports": return <RequestExports />;
  }
}

function TwoPane({ list, detail, label }: { list: React.ReactNode; detail: React.ReactNode; label: string }) {
  return (
    <div className="grid gap-6 xl:grid-cols-[300px_minmax(0,1fr)]">
      <nav aria-label={label} className="min-w-0 xl:sticky xl:top-20 xl:self-start">{list}</nav>
      <div className="min-w-0">{detail}</div>
    </div>
  );
}

type Item = { kind: "fidelity"; s: Source } | { kind: "second"; w: Waiting };
const itemId = (i: Item) => (i.kind === "fidelity" ? i.s.source_hash : i.w.candidate_id);

/** Everything waiting for a person, second reviews first (they gate gold), then flagged originals. */
function Todo({ d }: { d: Data }) {
  const [all, setAll] = useState(false);
  const [kind, setKind] = useState("all");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const sources = d.fid.data ?? [];
  const items: Item[] = [
    ...(d.ov.data?.awaiting_second_review ?? []).map((w) => ({ kind: "second" as const, w })),
    ...(all ? sources : flagged(sources)).map((s) => ({ kind: "fidelity" as const, s })),
  ];
  const visible = items.filter((i) => (kind === "all" || kind === i.kind) &&
    (i.kind === "fidelity" ? i.s.filename ?? "" : i.w.question).toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const cur = visible.find((i) => itemId(i) === open) ?? visible[0];
  const done = () => { d.fid.reload(); d.ov.reload(); };
  const list = (
    <div className="overflow-hidden rounded-xl border border-input">
      <div className="space-y-3 border-b p-4">
        <Field id="todo-query" label="문서·질문 찾기">
          <input id="todo-query" value={query} onChange={(e) => setQuery(e.target.value)} className={cn(field, "h-11")} />
        </Field>
        <Field id="todo-kind" label="검토 종류">
          <select id="todo-kind" value={kind} onChange={(e) => setKind(e.target.value)} className={cn(field, "h-11")}>
            <option value="all">전체</option><option value="second">2차 검토 · {d.ov.data?.awaiting_second_review.length ?? 0}건</option><option value="fidelity">원문 대조 · {flagged(sources).length}건 대기</option>
          </select>
        </Field>
        <label className="flex min-h-8 items-center gap-2 text-sm"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} className="size-4 accent-primary" />완료된 원문도 표시</label>
        <p className="text-sm font-semibold">{visible.length}건</p>
      </div>
      <ul className="max-h-[44dvh] divide-y overflow-y-auto xl:max-h-[calc(100dvh-24rem)]">
        {visible.map((i) => (
          <li key={itemId(i)} className="p-1">
            {i.kind === "second"
              ? <WaitingRow w={i.w} active={cur === i} onOpen={() => setOpen(itemId(i))} />
              : <FidelityRow s={i.s} active={cur === i} onOpen={() => setOpen(itemId(i))} />}
          </li>
        ))}
      </ul>
      {!visible.length && <p className="p-4 text-sm text-muted-foreground">조건에 맞는 항목이 없습니다.</p>}
    </div>
  );
  const detail = !cur ? <Empty>사람이 확인할 곳이 없습니다.</Empty>
    : cur.kind === "second" ? <SecondReviewDetail key={itemId(cur)} w={cur.w} onDone={done} />
      : <FidelityDetail key={itemId(cur)} s={cur.s} onConfirmed={done} />;
  return <TwoPane label="할 일 목록" list={list} detail={detail} />;
}

function TracePane({ runs, onRun }: { runs: Schemas["TraceSummary"][]; onRun: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(runs.length === 0);
  const active = open ?? runs[0]?.run_id ?? null;
  return (
    <TwoPane label="검색 추적 실행" list={
      <div className="space-y-6">
        <button type="button" aria-expanded={creating} onClick={() => setCreating(!creating)} className="min-h-11 w-full rounded-lg border border-primary px-3 text-sm font-semibold text-primary outline-none focus-visible:ring-3 focus-visible:ring-ring/50">{creating ? "새 검색 입력 닫기" : "+ 새 검색 실행"}</button>
        {creating && <div className="rounded-xl border border-input p-4"><TraceForm onRun={(id) => { setOpen(id); setCreating(false); onRun(); }} /></div>}
        <div className="space-y-3">
          <p className="text-sm font-semibold">저장된 실행 · {runs.length}건</p>
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
      { k: "judges" },
      { k: "dataset", count: ov.evaluation.dev_validation?.rows ?? "-" },
      { k: "ingestion", count: d.ing.data?.length },
    ] },
    { title: "기록", items: [{ k: "corrections" }, { k: "exports" }] },
  ];
  return (
    <SideMenu compact title="검증" groups={groups} titles={TITLES} section={section} onSection={onSection}>
      <PageTitle title={TITLES[section]} />
      <Body k={section} d={d} />
    </SideMenu>
  );
}
