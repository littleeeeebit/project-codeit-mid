"use client";

// 유지보수. Who comes here: the owner after new or changed originals arrive, or before a review. One button runs the
// same sequence as `maintain`: backup, restore check, manifest and ingest, HWP fidelity for changed files, keyword
// index, embedding of new chunks, the regression table, one report. Reading order: how the last run ended (and
// where it stopped), the button, the eight steps, then the regression rows. Nothing here activates anything.

import { useState } from "react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { must, usePoll } from "@/lib/use-poll";
import { stamp, type Tone, usd } from "@/lib/format";
import { Table, td } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Notice } from "./parts";

type Run = Schemas["MaintenanceRun"];
type Step = Schemas["MaintenanceStep"];

const STEP: Record<Step["name"], string> = {
  backup: "백업", restore_check: "복원 확인", ingest: "매니페스트·수집", fidelity: "HWP 원문 대조 (바뀐 것만)",
  keyword: "키워드 색인", embedding: "새 청크 임베딩", regression: "회귀 비교표", report: "상태 보고",
};
const STATE: Record<Step["status"], { label: string; tone: Tone }> = {
  pending: { label: "대기", tone: "neutral" }, running: { label: "실행 중", tone: "info" }, done: { label: "완료", tone: "ok" },
  reused: { label: "재사용", tone: "ok" }, skipped: { label: "해당 없음", tone: "neutral" }, failed: { label: "실패", tone: "bad" },
  needs_approval: { label: "비용 승인 대기", tone: "warn" },
};
const HEAD: Record<Run["status"], { label: string; text: string }> = {
  running: { label: "실행 중", text: "text-info" }, complete: { label: "완료", text: "text-ok" },
  failed: { label: "실패", text: "text-bad" }, needs_approval: { label: "비용 승인 대기", text: "text-warn" },
  interrupted: { label: "중단됨", text: "text-warn" },
};

type Regression = { name: string; dev_ndcg?: number | null; dev_support?: number | null; whole_ndcg?: number | null;
  needle_top5?: number | null; dev_critical?: number | null };
/** What the steps record in `detail` (maintenance.py). */
type Detail = Partial<{
  taken_at: string; tables: number; checks: number; checked_at: string; documents: number; reparsed: number; changed: { filename: string }[];
  checked: number; same_as_served: boolean; index_version: string; served_index_version: string; model: string;
  embedded: number; cost_usd: number; rows: Regression[]; judge_set: { negatives: number; positives: number };
  development_rows: number; estimate: { estimate_id: string; total_micro_usd: number };
}>;
const n = (v: unknown) => (typeof v === "number" ? v.toLocaleString("ko-KR") : String(v ?? "-"));
const short = (v: unknown) => String(v ?? "-").slice(0, 12);

/** One line saying what the step did, from its recorded detail. */
function summary(s: Step): string {
  const d = s.detail as Detail;
  if (s.reason) return s.reason;
  if (s.status === "pending" || s.status === "running") return "";
  switch (s.name) {
    case "backup": return s.status === "reused" ? `데이터가 그대로라 ${stamp(d.taken_at)} 백업을 씀` : `새 백업 · 표 ${n(d.tables)}개`;
    case "restore_check": return `검사 ${n(d.checks)}개 통과${s.status === "reused" ? ` · ${stamp(d.checked_at)}에 확인함` : ""}`;
    case "ingest": return d.changed?.length ? `원문 ${n(d.documents)}개 중 ${d.changed.length}개 추출이 바뀜: ${d.changed.slice(0, 3).map((c) => c.filename).join(", ")}${d.changed.length > 3 ? ` 외 ${d.changed.length - 3}개` : ""}`
      : `원문 ${n(d.documents)}개 추출 모두 그대로${d.reparsed ? ` (${d.reparsed}개는 입력 설정이 달라 다시 읽었지만 결과가 같음)` : ""}`;
    case "fidelity": return d.checked ? `HWP ${d.checked}개 대조` : "바뀐 HWP 없음";
    case "keyword": return d.same_as_served ? `서비스 중인 색인 ${short(d.index_version)} 그대로` : `새 색인 ${short(d.index_version)} · 서비스 중 ${short(d.served_index_version)} (활성화 전)`;
    case "embedding": return s.status === "skipped" ? "키워드 검색만 서비스 중"
      : s.status === "reused" ? `${d.model} · 새 청크 없음` : `${d.model} · 새로 임베딩 ${n(d.embedded ?? 0)}개 · ${d.cost_usd ? `$${d.cost_usd}` : "무료"}`;
    case "regression": return `${d.rows?.length ?? 0}행 · 실험 비교의 '회귀 (유지보수)' 표`;
    case "report": return d.judge_set ? `개발 세트 ${n(d.development_rows)}행 · 판정 골든 세트 오답 ${n(d.judge_set.negatives)} / 정답 ${n(d.judge_set.positives)}` : `개발 세트 ${n(d.development_rows)}행`;
  }
}

export function MaintenanceSection() {
  const [running, setRunning] = useState(false);
  const st = usePoll("maintenance", () => must(api.GET("/api/verify/maintenance"), errorText), running ? 2000 : null);
  const [state, setState] = useState<{ busy?: boolean; error?: string }>({});
  if (!!st.data?.running !== running) setRunning(!!st.data?.running);
  if (st.error) return <Notice tone="bad">{st.error}</Notice>;
  if (!st.data) return <Skeleton className="h-96 w-full" />;
  const run = st.data.run;
  const start = async () => {
    setState({ busy: true });
    const { error } = await api.POST("/api/verify/maintenance/start");
    setState(error ? { error: errorText(error) } : {});
    st.reload();
  };
  const head = run ? HEAD[run.status] : null;
  const stopped = run?.steps.find((s) => s.name === run.stopped_at);
  const estimate = stopped?.status === "needs_approval" ? (stopped.detail as Detail).estimate : undefined;
  const found = run?.steps.find((s) => s.name === "regression");
  const regression = (found ? (found.detail as Detail).rows : undefined) ?? [];
  return (
    <div className="space-y-12">
      <section aria-labelledby="maint-answer" className="space-y-6">
        <div className="space-y-2">
          <p className="text-sm font-medium text-muted-foreground">{run ? `${run.status === "running" ? "지금 실행" : "마지막 실행"} ${run.run_id} · ${run.actor} · ${stamp(run.started_at)}` : "아직 실행한 적 없음"}</p>
          <h3 id="maint-answer" className={cn("text-4xl font-bold tracking-tight", head?.text)}>
            {!run ? "유지보수 전" : run.status === "failed" || run.status === "needs_approval"
              ? `${STEP[run.stopped_at as Step["name"]] ?? run.stopped_at}에서 멈춤` : head!.label}
          </h3>
          {run && run.status !== "running" && (
            <p className="text-lg">
              {run.status === "complete" ? (run.reused ? "바뀐 입력이 없어 모두 재사용했습니다." : "바뀐 입력을 반영했습니다.") : run.reason}
              {run.serving && <> 서비스 중인 검색은 {run.serving.unchanged ? "그대로입니다" : "바뀌었습니다"}.</>}
              {run.provider_calls != null && <> 외부 호출 {run.provider_calls}회.</>}
            </p>
          )}
        </div>
        {estimate && (
          <Notice tone="warn">
            새 청크 임베딩에 최대 {usd(Number(estimate.total_micro_usd), 4)}가 듭니다. 승인은 소유자가 CLI로 합니다:{" "}
            <code className="font-mono">compare --approve {String(estimate.estimate_id)} --approved-by 이름</code>. 승인 뒤 다시 실행하면 앞 단계는 재사용하고 이어서 합니다.
          </Notice>
        )}
        <div className="flex flex-wrap items-center gap-4">
          <Button size="lg" onClick={start} disabled={state.busy || st.data.running}>{st.data.running ? "실행 중…" : "유지보수 실행"}</Button>
          <p className="max-w-2xl text-sm text-muted-foreground">
            CLI의 <code className="font-mono">maintain</code>과 같은 순서입니다. 바뀐 것이 없으면 모두 재사용하고 호출하지 않습니다.
            유료 임베딩은 추정에서 멈추고, 실패한 단계에서 멈추며, 어느 경우에도 서비스 중인 검색은 바꾸지 않습니다. 백업 위치 {st.data.backup_root}.
          </p>
        </div>
        {state.error && <Notice tone="bad">{state.error}</Notice>}
      </section>

      {run && (
        <section aria-labelledby="maint-steps" className="space-y-3">
          <h3 id="maint-steps" className="text-lg font-bold">단계</h3>
          <ol className="divide-y">
            {run.steps.map((s, i) => (
              <li key={s.name} className="grid gap-x-4 gap-y-1 py-3 sm:grid-cols-[2rem_12rem_7rem_minmax(0,1fr)] sm:items-baseline">
                <span className="hidden text-sm tabular-nums text-muted-foreground sm:block">{i + 1}</span>
                <span className={cn("font-semibold", s.status === "pending" && "text-muted-foreground")}>{STEP[s.name]}</span>
                <span><StatusBadge tone={STATE[s.status].tone}>{STATE[s.status].label}</StatusBadge></span>
                <span className={cn("min-w-0 text-sm [overflow-wrap:anywhere]", s.status === "failed" ? "text-bad" : "text-muted-foreground")}>{summary(s)}</span>
              </li>
            ))}
          </ol>
        </section>
      )}

      {regression.length > 0 && (
        <section aria-labelledby="maint-regression" className="space-y-3">
          <h3 id="maint-regression" className="text-lg font-bold">회귀 비교</h3>
          <Table caption="유지보수 회귀 비교: 서비스 중인 색인과 다시 만든 색인에서 K1과 서비스 구성" head={["행", "nDCG@5", "근거 완전", "nDCG@5 전체 문서", "바늘 상위 5", "치명 실패"]}>
            {regression.map((r) => (
              <tr key={r.name}>
                <th scope="row" className={cn(td, "text-left font-medium")}>{r.name}</th>
                <td className={cn(td, "tabular-nums")}>{r.dev_ndcg?.toFixed(3) ?? "-"}</td>
                <td className={cn(td, "tabular-nums")}>{r.dev_support == null ? "-" : `${(r.dev_support * 100).toFixed(1)}%`}</td>
                <td className={cn(td, "tabular-nums")}>{r.whole_ndcg?.toFixed(3) ?? "-"}</td>
                <td className={cn(td, "tabular-nums")}>{r.needle_top5 == null ? "-" : `${(r.needle_top5 * 100).toFixed(1)}%`}</td>
                <td className={cn(td, "tabular-nums")}>{r.dev_critical ?? "-"}</td>
              </tr>
            ))}
          </Table>
          <p className="text-sm text-muted-foreground">다시 만든 색인으로 바꾸려면 실험 비교의 &lsquo;회귀 (유지보수)&rsquo; 표에서 그 행을 열어 활성화하세요.</p>
        </section>
      )}
    </div>
  );
}
