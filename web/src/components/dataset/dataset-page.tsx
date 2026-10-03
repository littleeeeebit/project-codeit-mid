"use client";

// 데이터셋 만들기. Who comes here: a verifier building the development gold set. They draft source-bound questions
// with gpt-6-luna (paid, through the budget gateway), send valid drafts to review, and decide others' drafts with a
// note beside the original spans. Development documents only; sealed candidates never appear here.

import { useState } from "react";
import { api, errorText } from "@/lib/api";
import { must, usePoll } from "@/lib/use-poll";
import { type MenuGroup, SideMenu } from "@/components/side-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { RecentDecisions } from "@/components/verify/gold";
import { Notice, PageTitle } from "@/components/verify/parts";
import { DraftBuilder, DraftRuns } from "./draft";
import { ColumnsQueue, NoPending } from "./review";

type Key = "review" | "draft" | "runs" | "decided";
const TITLES: Record<Key, string> = { review: "검토 대기", draft: "초안 만들기", runs: "초안 생성 기록", decided: "처리 기록" };
const NOTES: Record<Key, string> = {
  review: "초안과 원문 구절을 나란히 보고 메모와 함께 승인하거나 거절합니다. 초안 생성을 요청한 사람은 승인할 수 없습니다.",
  draft: "개발(dev) 문서의 원문 구절에 묶어 gpt-6-luna가 질문 초안을 씁니다. 비용 추정은 무료, 생성은 동의 후 유료입니다.",
  runs: "생성이 끝난 유효한 초안을 검토 대기열에 올립니다.",
  decided: "최근 승인·거절 결정입니다.",
};

export function DatasetPage() {
  const [section, setSection] = useState<Key | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const pending = usePoll("gold-pending", () => must(api.GET("/api/gold/pending"), errorText), null);
  const runs = usePoll("draft-runs", () => must(api.GET("/api/drafting/runs"), errorText), running ? 2000 : null);
  const isRunning = !!runs.data?.some((r) => r.status === "running");
  if (isRunning !== running) setRunning(isRunning);  // poll the runs only while drafting runs

  if (pending.error) return <Shell><Notice tone="bad">{pending.error}</Notice></Shell>;
  if (!pending.data) return <Shell><Skeleton className="h-96 w-full" /></Shell>;
  const current: Key = section ?? (pending.data.length ? "review" : "draft");
  const waiting = runs.data?.filter((r) => r.status === "completed" && r.rows.length && !r.submitted).length ?? 0;
  const groups: MenuGroup<Key>[] = [
    { title: "사람이 볼 차례", items: [{ k: "review", count: pending.data.length, urgent: pending.data.length > 0 }] },
    { title: "만들기", items: [{ k: "draft" }, { k: "runs", count: isRunning ? "생성 중" : waiting ? `올릴 것 ${waiting}` : runs.data?.length, urgent: !!waiting }] },
    { title: "기록", items: [{ k: "decided" }] },
  ];
  const decided = (text: string) => { setFlash(text); pending.reload(); };
  return (
    <Shell>
      <SideMenu title="데이터셋 만들기" groups={groups} titles={TITLES} section={current} onSection={(k) => { setFlash(null); setSection(k); }}>
        <PageTitle title={current === "review" ? `${TITLES.review} ${pending.data.length}건` : TITLES[current]}>{NOTES[current]}</PageTitle>
        {flash && <p role="status" className="rounded-xl bg-ok-bg px-4 py-3 text-sm font-medium text-ok">{flash}</p>}
        {current === "review" && (pending.data.length ? <ColumnsQueue pending={pending.data} onDecided={decided} /> : <NoPending />)}
        {current === "draft" && <DraftBuilder onStarted={() => { setFlash("초안 생성을 시작했습니다. 초안 생성 기록에서 진행을 볼 수 있습니다."); runs.reload(); setSection("runs"); }} />}
        {current === "runs" && (runs.data ? <DraftRuns runs={runs.data} onChanged={() => { runs.reload(); pending.reload(); }} /> : <Skeleton className="h-40 w-full" />)}
        {current === "decided" && <RecentDecisions />}
      </SideMenu>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return <div className="mx-auto w-full max-w-[1440px] flex-1 px-6 py-8 lg:px-10">{children}</div>;
}
