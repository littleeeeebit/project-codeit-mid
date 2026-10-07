"use client";

// 평가·릴리스. Who comes here: a verifier asking whether the current candidate passes. Reading order, the
// conclusion-first pattern of 판정 모델 비교 (DESIGN.md): the release verdict and its checks, then the newest
// development answer run against the same pass lines (both in pass-lines.tsx), then the free estimate and the paid
// run. Sealed scoring happens only through the owner's CLI.

import { useState } from "react";
import { api, errorText, type Schemas } from "@/lib/api";
import { RELEASE, usd } from "@/lib/format";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Notice } from "./parts";
import { DevRun, Verdict } from "./pass-lines";

type Overview = Schemas["VerifyOverview"];
type Estimate = Schemas["Estimate"];

export function ReleaseBadge({ release, size }: { release: Overview["evaluation"]["release"]; size?: "sm" | "md" }) {
  if (!release) return <StatusBadge tone="neutral" size={size}>판정 없음</StatusBadge>;
  const r = RELEASE[release.status] ?? { label: release.status, tone: "neutral" as const };
  return <StatusBadge tone={r.tone} size={size}>{r.label}</StatusBadge>;
}

export function EvaluationSection({ ov, onChanged }: { ov: Overview; onChanged: () => void }) {
  const e = ov.evaluation;
  return (
    <div className="space-y-16">
      <Verdict release={e.release} targets={e.targets} />
      <DevRun e={e} />
      <PaidRun onStarted={onChanged} running={e.answer_runs.some((r) => r.running)} />
    </div>
  );
}

// ---------------------------------------------------------------- the free estimate, then the paid run

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
    <section aria-labelledby="paid-run" className="space-y-3">
      <h3 id="paid-run" className="text-lg font-bold">남은 개발 답변 평가하기</h3>
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
    </section>
  );
}
