"use client";

// 평가·릴리스: development answer runs and their scores, the free estimate before a paid evaluation run, and the
// latest release decision. Sealed scoring happens only through the owner's CLI.

import { useState } from "react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { label, MODE, rate, RELEASE, usd } from "@/lib/format";
import { Section, Table, td } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Empty, Notice } from "./parts";

type Overview = Schemas["VerifyOverview"];
type Estimate = Schemas["Estimate"];

export function ReleaseBadge({ release, size }: { release: Overview["evaluation"]["release"]; size?: "sm" | "md" }) {
  if (!release) return <StatusBadge tone="neutral" size={size}>판정 없음</StatusBadge>;
  const r = RELEASE[release.status] ?? { label: release.status, tone: "neutral" as const };
  return <StatusBadge tone={r.tone} size={size}>{r.label}</StatusBadge>;
}

export function EvaluationSection({ ov, onChanged }: { ov: Overview; onChanged: () => void }) {
  const e = ov.evaluation;
  const runs = [...e.answer_runs].reverse();
  return (
    <div className="space-y-8">
      <Section title="릴리스 판정" aside={<ReleaseBadge release={e.release} />}>
        {!e.release ? <Empty>기록된 릴리스 판정이 없습니다. freeze-release 이후에 기록됩니다.</Empty> : (
          <div className="space-y-2">
            <p className="font-mono text-xs text-muted-foreground">{e.release.release_id}</p>
            {e.release.reasons.length > 0 && (
              <ol className="list-decimal space-y-1 pl-5 text-sm">{e.release.reasons.map((r, i) => <li key={i}>{r}</li>)}</ol>
            )}
          </div>
        )}
      </Section>

      <Section title="개발 답변 평가" aside={<span className="text-[13px] text-muted-foreground">개발(dev) 질문만 · 점수는 표본 수와 함께 읽으세요</span>}>
        {runs.length === 0 ? <Empty>아직 평가 실행이 없습니다.</Empty> : runs.map((run, i) => (
          <details key={run.run_id} open={i === 0} className="group rounded-xl border">
            <summary className="flex cursor-pointer list-none items-center gap-3 rounded-xl px-4 py-3 outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
              <span className="font-mono text-sm font-semibold">{run.run_id}</span>
              <span className="text-sm text-muted-foreground">{run.config.label ?? run.config.config_id}</span>
              {run.running ? <StatusBadge tone="info">실행 중</StatusBadge>
                : run.scores?.status === "complete" ? <StatusBadge tone="ok">완료</StatusBadge>
                  : run.scores ? <StatusBadge tone="warn">일부 완료</StatusBadge> : <StatusBadge tone="neutral">점수 없음</StatusBadge>}
              <span className="ml-auto text-sm tabular-nums">{run.progress.done}/{run.progress.total}</span>
            </summary>
            <div className="space-y-3 border-t p-4">
              {run.scores?.stop_reason && <Notice tone="warn">중단 사유: {run.scores.stop_reason}</Notice>}
              {run.scores && Object.keys(run.scores.finalists).length > 0 ? (
                <Table caption={`${run.run_id} 후보별 점수`}
                       head={["후보", "방식", "완료", "필수 주장 정확도", "치명 오류", "인용 정밀도(하한)", "미판정 인용", "부정 질문 처리", "비용", "p95"]}>
                  {Object.entries(run.scores.finalists).map(([id, f]) => (
                    <tr key={id}>
                      <td className={cn(td, "font-semibold")}>{id}</td>
                      <td className={td}>{label(MODE, f.mode)}</td>
                      <td className={cn(td, "tabular-nums")}>{f.completed}/{f.of}</td>
                      <td className={cn(td, "tabular-nums")}>{rate(f.required_claim_correctness)}</td>
                      <td className={cn(td, "tabular-nums", f.critical_wrong.length && "font-bold text-bad")}>{f.critical_wrong.length}</td>
                      <td className={cn(td, "tabular-nums")}>{rate(f.citation_precision_lower_bound)}</td>
                      <td className={cn(td, "tabular-nums")}>{f.links_unjudged}</td>
                      <td className={cn(td, "tabular-nums")}>{rate(f.negative_handling)}</td>
                      <td className={cn(td, "tabular-nums")}>{usd(f.cost?.settled_micro_usd, 4)}</td>
                      <td className={cn(td, "tabular-nums")}>{f.latency_ms?.p95 == null ? "-" : `${Math.round(f.latency_ms.p95)}ms`}</td>
                    </tr>
                  ))}
                </Table>
              ) : !run.running && <p className="text-sm text-muted-foreground">점수가 없습니다.</p>}
              <p className="text-xs text-muted-foreground">인용 정밀도 하한은 사람이 아직 판정하지 않은 인용을 지지하지 않는 것으로 셉니다. 블라인드 검토표: export-review → import-review (CLI).</p>
            </div>
          </details>
        ))}
        <PaidRun onStarted={onChanged} running={runs.some((r) => r.running)} />
      </Section>
    </div>
  );
}

function PaidRun({ onStarted, running }: { onStarted: () => void; running: boolean }) {
  const [est, setEst] = useState<Estimate | null>(null);
  const [agree, setAgree] = useState(false);
  const [state, setState] = useState<{ busy?: boolean; error?: string }>({});
  const plan = async () => {
    setState({ busy: true });
    const { data, error } = await api.POST("/api/verify/evaluation/plan");
    setState(error ? { error: errorText(error) } : {});
    setEst(data ?? null);
    setAgree(false);
  };
  const start = async () => {
    if (!est) return;
    setState({ busy: true });
    const { error } = await api.POST("/api/verify/evaluation/start", { body: { estimate_id: est.estimate_id } });
    if (error) return setState({ error: errorText(error) });
    setEst(null);
    setState({});
    onStarted();
  };
  return (
    <div className="space-y-3 rounded-2xl bg-secondary/60 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" size="lg" onClick={plan} disabled={state.busy || running}>비용 추정 <span className="text-ok">무료</span></Button>
        <span className="text-xs text-muted-foreground">{running ? "실행 중인 평가가 끝나면 다시 추정할 수 있습니다." : "추정은 호출 없이 남은 개발 답변의 최대 비용만 계산합니다."}</span>
      </div>
      {est && (
        <>
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            {[["후보", est.finalists.join(", ")], ["남은 답변", `${est.rows_remaining}개`], ["최대 비용", usd(est.max_micro_usd, 4)],
              ["평가 예산 남음", usd(est.envelope_remaining_micro_usd, 4)]].map(([k, v]) => (
              <div key={k}><dt className="text-xs text-muted-foreground">{k}</dt><dd className="font-semibold tabular-nums">{v}</dd></div>
            ))}
          </dl>
          {!est.fits ? <Notice tone="bad">최대 비용이 평가 예산 또는 운영 한도를 넘어 실행할 수 없습니다.</Notice> : (
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} className="size-4 accent-primary" />
                이 추정으로 실행합니다 (최대 {usd(est.max_micro_usd, 4)}, 공유 예산에서 차감)
              </label>
              <Button size="lg" onClick={start} disabled={!agree || state.busy}>평가 실행 · 유료</Button>
            </div>
          )}
        </>
      )}
      {state.error && <Notice tone="bad">{state.error}</Notice>}
    </div>
  );
}
