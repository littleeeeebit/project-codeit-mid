"use client";

// 판정 모델 비교. Who comes here: a verifier deciding whether the Jev judge can replace the gpt-6-luna judge.
// Reading order, picked by the owner on 2026-10-04 (결론 먼저 + 두 칸 검토): the rule's answer and its three
// conditions as value against threshold, then the three arms in one table, then the disagreements as a list beside
// one opened item (the same two-pane shape as 할 일), and last, folded, the runs with plan and start. Every number
// shown comes from the held-out half; calibration only fits thresholds.

import { useMemo, useState } from "react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { must, usePoll } from "@/lib/use-poll";
import { stamp, type Tone, usd } from "@/lib/format";
import { Section, Table, td } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Empty, Field, field, Notice } from "./parts";

type Overview = Schemas["JudgeOverview"];
type Run = Schemas["JudgeRun"];
type Results = Schemas["JudgeResults"];
type Arm = Schemas["ArmMetrics"];
type Row = Schemas["Disagreement"];
type Estimate = Schemas["JudgeEstimate"];

const ARMS = ["luna", "jev_bridged", "jev_raw"] as const;
const ARM: Record<string, string> = { luna: "Luna", jev_bridged: "Jev · 영어 다리", jev_raw: "Jev · 한국어 원문" };
const ARM_NOTE: Record<string, string> = {
  luna: "gpt-6-luna, 한국어를 그대로 읽음", jev_bridged: "검사한 영어 번역만 읽음", jev_raw: "대조군 · 고정 표본",
};
const SHORT: Record<string, string> = { luna: "Luna", jev_bridged: "Jev", jev_raw: "Jev 원문" };
const PART: Record<string, string> = { calibration: "보정", held_out: "평가" };
const KIND: Record<string, string> = { link: "인용 하나", answer_claim: "답변 주장", claim: "필수 사실" };
const LABEL: Record<string, string> = {
  supporting: "지지", supported: "근거 있음", correct: "정확", unsupported: "근거 없음",
  incomplete_qualifier: "조건 누락", wrong_value: "값 틀림", missing: "빠짐", abstain: "보류",
};
const VERDICT: Record<string, { label: string; tone: Tone; text: string; line: string }> = {
  replaceable: { label: "교체 가능", tone: "ok", text: "text-ok", line: "미리 정한 세 조건을 모두 충족했습니다." },
  not_replaceable: { label: "교체 불가", tone: "bad", text: "text-bad", line: "충족하지 못한 조건이 있습니다." },
  inconclusive: { label: "판단 보류", tone: "warn", text: "text-warn", line: "판정한 항목이 규칙의 최소 개수에 못 미칩니다." },
};
const POSITIVE = new Set(["supporting", "supported", "correct"]);
const labelTone = (l: string | null | undefined): Tone => (!l ? "neutral" : POSITIVE.has(l) ? "ok" : "bad");
const pct = (x: number | null | undefined) => (x == null ? "-" : `${(x * 100).toFixed(1)}%`);
const sec = (x: number | null | undefined) => (x == null ? "-" : `${(x / 1000).toFixed(2)}초`);
const k3 = (x: number | null | undefined) => (x == null ? "-" : x.toFixed(3));

export function JudgeSection() {
  const [running, setRunning] = useState(false);
  const ov = usePoll("judge-progress", () => must(api.GET("/api/verify/judges/progress"), errorText), running ? 3000 : null);
  const runs = ov.data?.runs ?? [];
  const isRunning = runs.some((r) => r.running);
  if (isRunning !== running) setRunning(isRunning);
  // the newest held-out run; its results reload as it progresses and when it finishes
  const heldOut = [...runs].filter((r) => r.part === "held_out").sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const key = heldOut ? `${heldOut.run_id}:${heldOut.status}:${heldOut.progress.luna?.done}` : null;
  const res = usePoll(key && `judge-results:${key}`, () => must(api.GET("/api/verify/judges/results/{run_id}",
    { params: { path: { run_id: heldOut!.run_id } } }), errorText), null);
  if (ov.error) return <Notice tone="bad">{ov.error}</Notice>;
  if (!ov.data) return <Skeleton className="h-96 w-full" />;
  if (!ov.data.reference) return <Empty>판정 기준 세트가 없습니다. 소유자가 CLI로 judge-reference를 실행해야 합니다.</Empty>;
  const r = res.data;
  const scored = !!r && Object.keys(r.arms).length > 0;
  return (
    <div className="space-y-12">
      <Verdict ov={ov.data} results={r} run={heldOut} />
      {scored && <Comparison r={r} />}
      {scored && <Review runId={heldOut!.run_id} stamp={key!} />}
      <Operations ov={ov.data} runs={runs} open={!r?.verdict} onChanged={ov.reload} />
    </div>
  );
}

// ---------------------------------------------------------------- the answer

function Verdict({ ov, results, run }: { ov: Overview; results?: Results; run?: Run }) {
  const v = results?.verdict;
  const head = (title: string, cls: string, line: string) => (
    <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
      <div className="space-y-1">
        <p className="text-[13px] font-semibold text-muted-foreground">Jev로 Luna 판정을 바꿀 수 있나</p>
        <h3 id="judge-answer" className={cn("text-2xl font-bold", cls)}>{title}</h3>
        <p className="text-sm">{line}</p>
      </div>
      <p className="text-[13px] text-muted-foreground">
        기준 {ov.reference!.run_id} 독립 검토 · 평가용 {ov.split?.held_out ?? "-"}개 · 규칙 {String(v?.rule.version ?? "replacement-rule-1")}
      </p>
    </div>
  );
  if (!v) {
    return (
      <section aria-labelledby="judge-answer" className="space-y-6 rounded-2xl border p-4 sm:p-6">
        {head(run?.running ? "평가 실행 중" : "아직 판정 전", "", run?.running ? "모든 항목을 판정하면 미리 정한 규칙으로 판정합니다."
          : run ? "평가 실행이 끝나지 않았습니다. 아래 실행 관리에서 다시 추정하고 실행하면 남은 항목만 이어서 합니다."
            : "아래 실행 관리에서 보정 → 평가 순서로 실행하세요.")}
        {run?.running && <ArmProgress r={run} />}
      </section>
    );
  }
  const verdict = VERDICT[v.verdict];
  const l = results!.arms.luna, j = results!.arms.jev_bridged;
  const margin = Number(v.rule.kappa_margin ?? 0.05);
  const passed = Object.fromEntries(v.checks.map((c) => [c.condition, c.passed]));
  const tiles = v.verdict === "inconclusive" || !l || !j ? [] : [
    { key: "kappa", title: "카파", value: k3(j.kappa), need: l.kappa == null ? "-" : `${(l.kappa - margin).toFixed(3)} 이상`,
      note: `통과·불통과 일치도 · Luna ${k3(l.kappa)} − ${margin}` },
    { key: "false_accepts", title: "잘못 통과", value: `${j.false_accepts}건`, need: `${l.false_accepts}건 이하`,
      note: `기준상 불통과 ${j.reference_negatives}건 중 · Luna ${l.false_accepts}건` },
    { key: "coverage", title: "판정 비율", value: pct(j.coverage), need: `${pct(Number(v.rule.min_coverage ?? 0.9))} 이상`,
      note: `${j.judged}/${j.items}개 판정 · 나머지는 보류` },
  ];
  const negatives = j?.reference_negatives ?? 0;
  return (
    <section aria-labelledby="judge-answer" className="space-y-6 rounded-2xl border p-4 sm:p-6">
      {head(verdict.label, verdict.text, verdict.line)}
      {tiles.length > 0 ? (
        <ul className="grid gap-3 md:grid-cols-3">
          {tiles.map((t) => (
            <li key={t.key} className="space-y-2 rounded-xl bg-secondary/60 p-4">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[13px] font-semibold">{t.title}</span>
                <StatusBadge tone={passed[t.key] ? "ok" : passed[t.key] === false ? "bad" : "neutral"}>
                  {passed[t.key] ? "충족" : passed[t.key] === false ? "미충족" : "계산 안 함"}
                </StatusBadge>
              </div>
              <p className="flex flex-wrap items-baseline gap-x-2 tabular-nums">
                <span className="text-xl font-bold">{t.value}</span>
                <span className="text-sm text-muted-foreground">기준 {t.need}</span>
              </p>
              <p className="text-xs text-muted-foreground">{t.note}</p>
            </li>
          ))}
        </ul>
      ) : <Notice tone="warn">{v.deciding.map((c) => c.detail).join(" · ")}</Notice>}
      {negatives > 0 && negatives < 30 && (
        <p className="border-l-2 border-warn pl-3 text-sm text-muted-foreground">
          기준 세트에서 불통과로 검토된 항목이 {negatives}건뿐이라 카파와 잘못 통과는 한두 건에도 크게 움직입니다.
          이 결과는 &ldquo;이 기준에서 Luna보다 나쁘지 않다&rdquo;는 뜻이지, 검토자를 대신할 만큼 믿을 만하다는 뜻은 아닙니다.
        </p>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- three arms side by side

/** Agreement with its Wilson interval on a shared 60–100% axis, so the rows compare by eye. */
function Interval({ a }: { a: Arm }) {
  const [lo, hi] = a.agreement.wilson95 ?? [null, null];
  const rate = a.agreement.rate;
  const x = (v: number) => Math.max(0, Math.min(100, ((v - 0.6) / 0.4) * 100));
  return (
    <div className="space-y-1.5">
      <p className="tabular-nums"><span className="font-semibold">{pct(rate)}</span>
        <span className="text-xs text-muted-foreground"> {a.agreement.numerator}/{a.agreement.denominator}</span></p>
      {lo != null && hi != null && rate != null && (
        <div className="relative h-1.5 w-36 rounded-full bg-secondary" role="img" aria-label={`95% 구간 ${pct(lo)}–${pct(hi)}`}>
          <span className="absolute inset-y-0 rounded-full bg-primary/30" style={{ left: `${x(lo)}%`, width: `${x(hi) - x(lo)}%` }} />
          <span className="absolute top-1/2 size-2.5 -translate-1/2 rounded-full bg-primary" style={{ left: `${x(rate)}%` }} />
        </div>
      )}
      {lo != null && <p className="text-xs tabular-nums text-muted-foreground">{pct(lo)}–{pct(hi)}</p>}
    </div>
  );
}

function cost(a: Arm, arm: string) {
  if (a.usd.priced) return <>{usd(a.usd.settled_micro_usd, 4)}<span className="block text-xs text-muted-foreground">{a.usd.calls}회</span></>;
  const bridge = arm === "jev_bridged" && a.bridge_usd ? ` · 번역 ${usd(a.bridge_usd.settled_micro_usd, 4)}` : "";
  return <>가격 미제공<span className="block text-xs text-muted-foreground">{a.usd.calls}회{bridge}</span></>;
}

/** The metric cells of one arm, shared by the wide table and the narrow cards. */
function cells(a: Arm, k: string): [string, React.ReactNode][] {
  return [
    ["판정 비율", <span key="v">{pct(a.coverage)}<span className="block text-xs text-muted-foreground">{a.judged}/{a.items}</span></span>],
    ["기준과 일치 · 95%", <Interval key="v" a={a} />],
    ["카파", <span key="v" className="font-semibold">{k3(a.kappa)}</span>],
    ["잘못 통과", <span key="v"><span className={cn("font-semibold", a.false_accepts > 0 && "text-bad")}>{a.false_accepts}</span>
      <span className="text-xs text-muted-foreground"> / {a.reference_negatives}</span></span>],
    ["비용", cost(a, k)],
    ["지연 p50", <span key="v" className="whitespace-nowrap">{sec(a.latency_ms.p50)}<span className="block text-xs text-muted-foreground">p95 {sec(a.latency_ms.p95)}</span></span>],
  ];
}

function ArmName({ k, a }: { k: string; a: Arm }) {
  return (
    <>
      <span className="block font-semibold">{ARM[k]}</span>
      <span className="block text-xs font-normal text-muted-foreground">{ARM_NOTE[k]}{k === "jev_raw" ? ` ${a.items}개` : ""}</span>
    </>
  );
}

function Comparison({ r }: { r: Results }) {
  const arms = ARMS.filter((k) => r.arms[k]);
  return (
    <Section title="세 판정 모델" aside={<span className="text-[13px] text-muted-foreground">평가용 절반 · 임계값 {r.thresholds_sha256?.slice(0, 8) ?? "-"}</span>}>
      <div className="hidden md:block">
        <Table caption="Luna, 영어 다리 Jev, 한국어 원문 Jev의 평가용 지표" head={["판정 모델", ...cells(r.arms.luna ?? r.arms[arms[0]], arms[0]).map(([h]) => h)]}>
          {arms.map((k) => (
            <tr key={k}>
              <th scope="row" className={cn(td, "min-w-44 text-left")}><ArmName k={k} a={r.arms[k]} /></th>
              {cells(r.arms[k], k).map(([h, v]) => <td key={h} className={cn(td, "tabular-nums")}>{v}</td>)}
            </tr>
          ))}
        </Table>
      </div>
      <ul className="space-y-3 md:hidden">
        {arms.map((k) => (
          <li key={k} className="space-y-3 rounded-xl border p-4">
            <p><ArmName k={k} a={r.arms[k]} /></p>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm tabular-nums">
              {cells(r.arms[k], k).map(([h, v]) => <div key={h} className="min-w-0"><dt className="text-xs text-muted-foreground">{h}</dt><dd>{v}</dd></div>)}
            </dl>
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">
        카파는 통과(지지·근거 있음·정확)와 불통과로 나눠 셉니다. 잘못 통과는 기준상 불통과를 통과시킨 건수입니다.
        보류(불확실 구간, 호출 실패, 번역 불가)는 오답이 아니라 미판정입니다. 값 비교는 코드가 먼저 판정합니다.
      </p>
      <details className="rounded-xl border">
        <summary className="min-h-11 cursor-pointer px-4 py-3 text-sm font-semibold outline-none focus-visible:ring-3 focus-visible:ring-ring/50">항목 종류별 혼동 행렬</summary>
        <div className="grid gap-6 border-t p-4 xl:grid-cols-3">
          {arms.map((k) => <Confusion key={k} arm={k} a={r.arms[k]} />)}
        </div>
      </details>
    </Section>
  );
}

function Confusion({ arm, a }: { arm: string; a: Arm }) {
  return (
    <div className="min-w-0 space-y-2">
      <p className="text-sm font-semibold">{ARM[arm]}</p>
      {Object.entries(a.confusion).map(([kind, refs]) => {
        const cols = [...new Set(Object.values(refs).flatMap((c) => Object.keys(c)))].sort();
        return (
          <Table key={kind} caption={`${ARM[arm]} ${KIND[kind]} 혼동 행렬`} head={[`${KIND[kind] ?? kind} · 기준＼판정`, ...cols.map((c) => LABEL[c] ?? c)]}>
            {Object.entries(refs).map(([ref, cells]) => (
              <tr key={ref}>
                <th scope="row" className={cn(td, "text-left font-semibold")}>{LABEL[ref] ?? ref}</th>
                {cols.map((c) => <td key={c} className={cn(td, "tabular-nums", c === ref && "font-bold")}>{cells[c] ?? 0}</td>)}
              </tr>
            ))}
          </Table>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- runs, plan and start (folded once there is a verdict)

function Operations({ ov, runs, open, onChanged }: { ov: Overview; runs: Run[]; open: boolean; onChanged: () => void }) {
  const fitted = runs.some((r) => r.part === "calibration" && r.thresholds_fitted);
  const sorted = [...runs].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const summary = sorted.length ? sorted.map((r) => `${PART[r.part]} ${r.running ? "실행 중" : r.status === "complete" ? "완료" : "일부"}`).join(" · ") : "실행 없음";
  return (
    <details open={open} className="rounded-2xl border">
      <summary className="flex min-h-14 cursor-pointer flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3 outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
        <span className="text-lg font-bold">실행 관리</span>
        <span className="text-sm text-muted-foreground">{summary}</span>
        <span className="text-[13px] text-muted-foreground md:ml-auto">
          기준 {ov.reference!.items}개 · 보정 {ov.split?.calibration ?? "-"} / 평가 {ov.split?.held_out ?? "-"} · 원문 대조 표본 {ov.split?.raw_sample ?? "-"}
        </span>
      </summary>
      <div className="space-y-6 border-t p-5">
        {sorted.length > 0 && <ul className="divide-y rounded-xl border">{sorted.map((r) => <RunRow key={r.run_id} r={r} />)}</ul>}
        <Plan fitted={fitted} running={runs.some((r) => r.running)} onStarted={onChanged} />
      </div>
    </details>
  );
}

function ArmProgress({ r }: { r: Run }) {
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      {ARMS.map((k) => {
        const p = r.progress[k];
        return (
          <div key={k} className="space-y-1">
            <div className="flex justify-between text-xs"><span>{ARM[k]}</span><span className="tabular-nums">{p?.done ?? 0}/{p?.total ?? 0}</span></div>
            <Progress value={p?.total ? (100 * p.done) / p.total : 0} aria-label={`${ARM[k]} 진행`} />
          </div>
        );
      })}
    </div>
  );
}

function RunRow({ r }: { r: Run }) {
  const tone: Tone = r.running ? "info" : r.status === "complete" ? "ok" : "warn";
  return (
    <li className="space-y-3 p-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-sm font-semibold">{PART[r.part]}</span>
        <StatusBadge tone={tone}>{r.running ? "실행 중" : r.status === "complete" ? "완료" : "일부 완료"}</StatusBadge>
        {r.part === "calibration" && r.thresholds_fitted && <span className="text-xs text-ok">임계값 맞춤 완료</span>}
        <span className="font-mono text-xs text-muted-foreground">{r.run_id}</span>
        <span className="text-xs text-muted-foreground sm:ml-auto">{stamp(r.created_at)} · 번역 묶음 {r.translated_batches}개</span>
      </div>
      {r.running || r.status !== "complete" ? <ArmProgress r={r} /> : (
        <p className="text-xs tabular-nums text-muted-foreground">
          {ARMS.map((k) => `${ARM[k]} ${r.progress[k]?.done ?? 0}/${r.progress[k]?.total ?? 0}`).join(" · ")}
        </p>
      )}
      {(r.stop_reason || r.error) && !r.running && <Notice tone="warn">중단 사유: {r.stop_reason ?? r.error} · 같은 부분을 다시 추정하고 실행하면 남은 항목만 이어서 합니다.</Notice>}
    </li>
  );
}

function Plan({ fitted, running, onStarted }: { fitted: boolean; running: boolean; onStarted: () => void }) {
  const [part, setPart] = useState<"calibration" | "held_out">(fitted ? "held_out" : "calibration");
  const [est, setEst] = useState<Estimate | null>(null);
  const [agree, setAgree] = useState(false);
  const [state, setState] = useState<{ busy?: boolean; error?: string }>({});
  const plan = async () => {
    setState({ busy: true });
    const { data, error } = await api.POST("/api/verify/judges/plan", { body: { part } });
    setState(error ? { error: errorText(error) } : {});
    setEst(data ?? null);
    setAgree(false);
  };
  const start = async () => {
    if (!est) return;
    setState({ busy: true });
    const { error } = await api.POST("/api/verify/judges/start", { body: { estimate_id: est.estimate_id } });
    if (error) return setState({ error: errorText(error) });
    setEst(null);
    setState({});
    onStarted();
  };
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <Field id="judge-part" label="실행할 부분">
          <select id="judge-part" value={part} onChange={(e) => { setPart(e.target.value as typeof part); setEst(null); }} className={cn(field, "block h-11 sm:w-64")}>
            <option value="calibration">1. 보정 — Jev 임계값 맞춤</option>
            <option value="held_out" disabled={!fitted}>2. 평가 — 화면에 보고{fitted ? "" : " (보정 먼저)"}</option>
          </select>
        </Field>
        <Button variant="outline" size="lg" onClick={plan} disabled={state.busy || running}>비용 추정 <span className="text-ok">무료</span></Button>
      </div>
      <p className="text-xs text-muted-foreground">{running ? "실행 중인 부분이 끝나면 다시 추정할 수 있습니다." : "추정은 호출 없이 남은 번역·판정 호출의 최대 비용만 계산합니다. 이미 끝난 항목은 다시 부르지 않습니다."}</p>
      {est && (
        <div className="space-y-4 rounded-xl bg-secondary/60 p-4">
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3 lg:grid-cols-6">
            {[["항목", `${est.items}개`], ["번역 호출", `${est.translation.attempts}회 (${est.translation.segments ?? 0}조각)`],
              ["Luna 판정 호출", `${est.judge.attempts}회`], ["Jev 호출", `${est.jev.calls}회 · 가격 미제공`],
              ["최대 비용", usd(est.max_micro_usd, 4)], ["judge_eval 남음", est.envelope_remaining_micro_usd == null ? "배정 안 됨" : usd(est.envelope_remaining_micro_usd, 4)]].map(([k, v]) => (
              <div key={k}><dt className="text-xs text-muted-foreground">{k}</dt><dd className="font-semibold tabular-nums">{v}</dd></div>
            ))}
          </dl>
          {!est.translation.attempts && !est.judge.attempts && !est.jev.calls ? <Notice tone="ok">이 부분은 모든 항목을 판정했습니다. 다시 실행할 호출이 없습니다.</Notice>
            : !est.fits ? <Notice tone="bad">실행할 수 없습니다: {est.blocked}</Notice> : (
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex min-h-11 items-center gap-2 text-sm">
                <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} className="size-4 accent-primary" />
                이 추정으로 실행합니다 (최대 {usd(est.max_micro_usd, 4)}, 공유 예산 judge_eval에서 차감)
              </label>
              <Button size="lg" onClick={start} disabled={!agree || state.busy}>{PART[est.part]} 실행 · 유료</Button>
            </div>
          )}
        </div>
      )}
      {state.error && <Notice tone="bad">{state.error}</Notice>}
    </div>
  );
}

// ---------------------------------------------------------------- disagreements: a list beside one opened item

const FILTERS: Record<string, { label: string; test: (r: Row) => boolean }> = {
  all: { label: "전체", test: () => true },
  false_accept: { label: "기준상 불통과를 통과시킴", test: (r) => !POSITIVE.has(r.reference) && ARMS.some((k) => POSITIVE.has(r.arms[k]?.label ?? "")) },
  luna: { label: "Luna ≠ 기준", test: (r) => !!r.arms.luna && r.arms.luna.label !== r.reference },
  jev: { label: "Jev(영어) ≠ 기준", test: (r) => !!r.arms.jev_bridged && r.arms.jev_bridged.label !== r.reference },
  between: { label: "Luna ≠ Jev(영어)", test: (r) => r.arms.luna?.label !== r.arms.jev_bridged?.label },
  abstain: { label: "Jev 보류", test: (r) => !!r.arms.jev_bridged && r.arms.jev_bridged.label == null },
};

function Review({ runId, stamp: key }: { runId: string; stamp: string }) {
  const rows = usePoll(`judge-disagreements:${key}`, () => must(api.GET("/api/verify/judges/disagreements/{run_id}",
    { params: { path: { run_id: runId } } }), errorText), null);
  const [kind, setKind] = useState("all");
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const visible = useMemo(() => (rows.data ?? []).filter((r) => (kind === "all" || r.kind === kind) && FILTERS[filter].test(r)
    && JSON.stringify([r.korean, r.english]).toLocaleLowerCase().includes(query.toLocaleLowerCase())), [rows.data, kind, filter, query]);
  const cur = visible.find((r) => r.blind_id === open) ?? visible[0];
  return (
    <Section title="판정이 엇갈린 항목" aside={<span className="text-[13px] text-muted-foreground">{rows.data ? `세 판정 중 하나라도 기준과 다르거나 서로 다른 ${rows.data.length}건` : ""}</span>}>
      {rows.error ? <Notice tone="bad">{rows.error}</Notice> : !rows.data ? <Skeleton className="h-64 w-full" /> : (
        <div className="grid gap-6 xl:grid-cols-[340px_minmax(0,1fr)]">
          <nav aria-label="엇갈린 항목 목록" className="min-w-0 xl:sticky xl:top-20 xl:self-start">
            <div className="overflow-hidden rounded-xl border border-input">
              <div className="space-y-3 border-b p-4">
                <Field id="dis-filter" label="엇갈림">
                  <select id="dis-filter" value={filter} onChange={(e) => setFilter(e.target.value)} className={cn(field, "h-11")}>
                    {Object.entries(FILTERS).map(([k, f]) => <option key={k} value={k}>{f.label} · {rows.data!.filter(f.test).length}건</option>)}
                  </select>
                </Field>
                <div className="grid grid-cols-2 gap-3">
                  <Field id="dis-kind" label="항목 종류">
                    <select id="dis-kind" value={kind} onChange={(e) => setKind(e.target.value)} className={cn(field, "h-11")}>
                      <option value="all">전체</option>{Object.entries(KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                    </select>
                  </Field>
                  <Field id="dis-query" label="문장에서 찾기">
                    <input id="dis-query" value={query} onChange={(e) => setQuery(e.target.value)} className={cn(field, "h-11")} />
                  </Field>
                </div>
                <p className="text-sm font-semibold">{visible.length}건</p>
              </div>
              <ul className="max-h-[44dvh] divide-y overflow-y-auto xl:max-h-[calc(100dvh-22rem)]">
                {visible.map((r) => <li key={r.blind_id} className="p-1"><ItemRow r={r} active={r === cur} onOpen={() => {
                  setOpen(r.blind_id);
                  // below xl the item opens under the list, out of view; bring it up
                  if (!matchMedia("(min-width: 80rem)").matches) document.getElementById("judge-item")?.scrollIntoView({ block: "start" });
                }} /></li>)}
              </ul>
              {!visible.length && <p className="p-4 text-sm text-muted-foreground">조건에 맞는 항목이 없습니다.</p>}
            </div>
          </nav>
          <div id="judge-item" className="min-w-0 scroll-mt-4">{cur ? <ItemDetail key={cur.blind_id} r={cur} /> : <Empty>조건에 맞는 항목이 없습니다.</Empty>}</div>
        </div>
      )}
    </Section>
  );
}

function ItemRow({ r, active, onOpen }: { r: Row; active: boolean; onOpen: () => void }) {
  return (
    <button type="button" onClick={onOpen} aria-current={active || undefined}
            className={cn("w-full space-y-2 rounded-lg border-l-4 px-3 py-3 text-left outline-none hover:bg-secondary/70 focus-visible:ring-3 focus-visible:ring-ring/50",
              active ? "border-primary bg-accent hover:bg-accent" : "border-transparent")}>
      <span className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>{KIND[r.kind]} · 기준 <span className="font-semibold text-foreground">{LABEL[r.reference] ?? r.reference}</span></span>
        <span className="font-mono">{r.blind_id.slice(0, 6)}</span>
      </span>
      <span className="line-clamp-2 block text-sm [overflow-wrap:anywhere]">{String((r.kind === "claim" ? r.korean.question : r.korean.claim) ?? "")}</span>
      <span className="flex flex-wrap gap-x-3 gap-y-1">
        {ARMS.filter((k) => r.arms[k]).map((k) => {
          const l = r.arms[k]!.label;
          return (
            <span key={k} className={cn("inline-flex items-center gap-1 text-xs", l === r.reference && "opacity-60")}>
              <span className="text-muted-foreground">{SHORT[k]}</span>
              <StatusBadge tone={labelTone(l)}>{LABEL[l ?? "abstain"] ?? l}</StatusBadge>
            </span>
          );
        })}
      </span>
    </button>
  );
}

const FIELDS: [string, string][] = [
  ["question", "질문"], ["claim", "주장"], ["required", "필수 사실"], ["conditions", "조건"],
  ["answer_summary", "답변 요약"], ["answer_claims", "답변 주장"], ["passages", "근거 인용"],
];
const FOLDED = new Set(["answer_claims", "passages"]);
const lines = (v: unknown) => (Array.isArray(v) ? v.map(String).filter(Boolean) : v ? [String(v)] : []);

function ItemDetail({ r }: { r: Row }) {
  const shown = FIELDS.filter(([k]) => lines(r.korean[k]).length);
  return (
    <article className="space-y-6 rounded-2xl border p-4 sm:p-6">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <StatusBadge tone="neutral">{KIND[r.kind]}</StatusBadge>
        <span className="text-sm">기준 판정</span>
        <StatusBadge tone={labelTone(r.reference)} size="md">{LABEL[r.reference] ?? r.reference}</StatusBadge>
        <span className="ml-auto font-mono text-xs text-muted-foreground">{r.blind_id}</span>
      </header>
      <ul className="grid gap-3 md:grid-cols-[repeat(auto-fit,minmax(12rem,1fr))]">
        {ARMS.filter((k) => r.arms[k]).map((k) => {
          const a = r.arms[k]!;
          const off = a.label !== r.reference;
          return (
            <li key={k} className={cn("space-y-2 rounded-xl border p-4", off && "border-bad/40")}>
              <p className="flex items-center justify-between gap-2 text-[13px] font-semibold">
                {ARM[k]}{off && <span className="text-xs font-normal text-bad">기준과 다름</span>}
              </p>
              <p className="flex flex-wrap items-center gap-2">
                <StatusBadge tone={labelTone(a.label)} size="md">{LABEL[a.label ?? "abstain"] ?? a.label}</StatusBadge>
                {a.probability != null && <span className="text-sm tabular-nums text-muted-foreground">지지 확률 {a.probability.toFixed(2)}</span>}
              </p>
              {a.source === "code" && <p className="text-xs text-muted-foreground">코드가 값으로 판정</p>}
              {a.abstain && <p className="text-xs text-muted-foreground">보류 사유 · {a.abstain}</p>}
              {a.reason && <p lang="en" className="text-sm">{a.reason}</p>}
            </li>
          );
        })}
      </ul>
      <div className="space-y-2">
        {r.english ? (
          <div className="hidden grid-cols-[6rem_minmax(0,1fr)_minmax(0,1fr)] gap-4 text-xs font-semibold text-muted-foreground md:grid">
            <span /><span>한국어 원문</span><span>영어 다리 · Jev가 읽은 글</span>
          </div>
        ) : <Notice tone="warn">영어 다리 없음 · {r.untranslatable}. 번역이 보호한 값을 바꾸거나 한글을 남겨 Jev에 보내지 않았고, 보류로 셉니다.</Notice>}
        <dl className="divide-y">
          {shown.map(([k, name]) => <Pair key={k} name={name} ko={lines(r.korean[k])} en={r.english ? lines(r.english[k]) : null} folded={FOLDED.has(k)} />)}
        </dl>
      </div>
    </article>
  );
}

/** One field in Korean and in the English Jev read, on the same row so a changed meaning shows side by side. */
function Pair({ name, ko, en, folded }: { name: string; ko: string[]; en: string[] | null; folded: boolean }) {
  const body = (xs: string[], lang?: string) => (
    <div lang={lang} className="min-w-0 space-y-2 text-sm leading-relaxed">
      {xs.map((x, i) => <p key={i} className="whitespace-pre-wrap [overflow-wrap:anywhere]">{x}</p>)}
    </div>
  );
  const cols = en ? "md:grid-cols-[6rem_minmax(0,1fr)_minmax(0,1fr)]" : "md:grid-cols-[6rem_minmax(0,1fr)]";
  if (folded) {
    return (
      <details className="py-1">
        <summary className="flex min-h-11 cursor-pointer items-center text-[13px] font-semibold outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
          {name} · {ko.length}개
        </summary>
        <div className={cn("grid gap-x-4 gap-y-2 pb-3", cols)}><span className="hidden md:block" />{body(ko)}{en && body(en, "en")}</div>
      </details>
    );
  }
  return (
    <div className={cn("grid gap-x-4 gap-y-2 py-3", cols)}>
      <dt className="text-[13px] font-semibold">{name}</dt>
      <dd className="min-w-0">{body(ko)}</dd>
      {en && <dd className="min-w-0">{body(en, "en")}</dd>}
    </div>
  );
}
