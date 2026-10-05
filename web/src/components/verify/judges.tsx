"use client";

// 판정 모델 비교. Who comes here: a verifier deciding whether the Jev judge can replace the gpt-6-luna judge.
// Reading order, picked by the owner on 2026-10-04 (결론 먼저 + 두 칸 검토): the rule's answer and its three
// conditions as value against threshold, then the three arms in one table, then the disagreements as a list beside
// one opened item (the same two-pane shape as 할 일), and last, folded, the runs with plan and start. The verdict
// comes from the held-out half; calibration only fits thresholds. The judge golden set (mutated correct answers)
// is a second scored set: its view leads with each judge's false-accept rate, then the rate per mutation type.

import { useMemo, useState } from "react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { must, usePoll } from "@/lib/use-poll";
import { stamp, type Tone, usd } from "@/lib/format";
import { Table, td } from "@/components/data-table";
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
const PART: Record<string, string> = { calibration: "보정", held_out: "평가", judge_set: "골든 세트" };
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
const pct = (x: number | null | undefined) => (x == null ? "-" : `${(x * 100).toFixed(1)}%`);
const sec = (x: number | null | undefined) => (x == null ? "-" : `${(x / 1000).toFixed(2)}초`);
const k3 = (x: number | null | undefined) => (x == null ? "-" : x.toFixed(3));

type Scored = "held_out" | "judge_set";

export function JudgeSection() {
  const [running, setRunning] = useState(false);
  const [set, setSet] = useState<Scored>("held_out");
  const ov = usePoll("judge-progress", () => must(api.GET("/api/verify/judges/progress"), errorText), running ? 3000 : null);
  const runs = ov.data?.runs ?? [];
  const isRunning = runs.some((r) => r.running);
  if (isRunning !== running) setRunning(isRunning);
  // the newest run on the chosen set; its results reload as it progresses and when it finishes
  const run = [...runs].filter((r) => r.part === set).sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const key = run ? `${run.run_id}:${run.status}:${run.progress.luna?.done}` : null;
  const res = usePoll(key && `judge-results:${key}`, () => must(api.GET("/api/verify/judges/results/{run_id}",
    { params: { path: { run_id: run!.run_id } } }), errorText), null);
  if (ov.error) return <Notice tone="bad">{ov.error}</Notice>;
  if (!ov.data) return <Skeleton className="h-96 w-full" />;
  if (!ov.data.reference) return <Empty>판정 기준 세트가 없습니다. 소유자가 CLI로 judge-reference를 실행해야 합니다.</Empty>;
  const r = res.data;
  const scored = !!r && Object.keys(r.arms).length > 0;
  const js = ov.data.judge_set;
  return (
    <div className="space-y-16">
      <div className="space-y-10">
        <div role="group" aria-label="판정 세트" className="flex flex-wrap gap-2">
          {([["held_out", `평가용 기준 · ${ov.data.split?.held_out ?? "-"}개`],
            ["judge_set", js ? `판정 골든 세트 · 오답 ${js.negatives} · 정답 ${js.positives}` : "판정 골든 세트 · 생성 전"]] as const).map(([k, label]) => (
            <button key={k} type="button" aria-pressed={set === k} onClick={() => setSet(k)}
                    className={cn("min-h-11 rounded-full px-4 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
                      set === k ? "bg-foreground font-semibold text-background" : "bg-secondary text-foreground hover:bg-secondary/70")}>
              {label}
            </button>
          ))}
        </div>
        {set === "held_out" ? <Verdict ov={ov.data} results={r} run={run} /> : <Mutations ov={ov.data} results={scored ? r : undefined} run={run} />}
      </div>
      {scored && set === "held_out" && <Comparison r={r} />}
      {scored && <Review key={set} runId={run!.run_id} stamp={key!} />}
      <Operations ov={ov.data} runs={runs} open={set === "held_out" ? !r?.verdict : !scored} onChanged={ov.reload} />
    </div>
  );
}

// ---------------------------------------------------------------- the judge golden set: false accepts per mutation

const MUTATION: Record<string, string> = {
  amount: "금액을 바꿈", unit: "단위를 바꿈", qualifier: "부가세·조건어를 뒤집음", date: "날짜·기간을 옮김",
  negation: "부정을 넣음", dropped_condition: "조건을 뺌", wrong_evidence: "다른 근거를 인용", unmutated: "변형 안 한 정답",
};
const JUDGES = ["luna", "jev_bridged"] as const;
type Rate = Schemas["MutationRate"];

function sumRates(cells: Rate[]) {
  const passed = cells.reduce((n, c) => n + c.passed, 0), judged = cells.reduce((n, c) => n + c.judged, 0);
  return { passed, judged, rate: judged ? passed / judged : null };
}

function RateCell({ c, bad }: { c?: Rate; bad: boolean }) {
  if (!c) return <span className="text-muted-foreground">-</span>;
  const [lo, hi] = c.wilson95 ?? [null, null];
  return (
    <>
      <span className={cn("font-semibold", c.rate != null && c.rate > 0 && bad && "text-bad")}>{pct(c.rate)}</span>
      <span className="block text-xs text-muted-foreground">
        {c.passed}/{c.judged}{lo != null && <span className="hidden sm:inline"> · {pct(lo)}–{pct(hi)}</span>}
      </span>
    </>
  );
}

function Mutations({ ov, results, run }: { ov: Overview; results?: Results; run?: Run }) {
  const js = ov.judge_set;
  const question = <p className="text-sm font-medium text-muted-foreground">정답을 일부러 틀리게 바꾸면 판정 모델이 알아채나</p>;
  if (!js) return <Empty>판정 골든 세트가 없습니다. 소유자가 CLI로 judge-set을 실행해야 합니다.</Empty>;
  const m = results?.mutations ?? {};
  if (!results || !Object.keys(m).length) {
    return (
      <section aria-labelledby="judge-set-answer" className="space-y-6">
        <div className="space-y-2">
          {question}
          <h3 id="judge-set-answer" className="text-4xl font-bold tracking-tight">{run?.running ? "골든 세트 실행 중" : "아직 실행 전"}</h3>
          <p className="text-lg">{run?.running ? "모든 항목을 판정하면 변형 종류별 잘못 통과율을 보여 줍니다."
            : "아래 실행 관리에서 '판정 골든 세트'를 추정하고, 비용을 승인한 뒤 실행하세요."}</p>
        </div>
        {run?.running && <div className="max-w-3xl"><ArmProgress r={run} /></div>}
        <p className="text-sm text-muted-foreground">
          평가용 기준의 정답 {js.positives}개를 {Object.keys(js.by_type).length}가지 방식으로 바꿔 오답 {js.negatives}개를 만들었습니다. 개발용 RAG 세트와 따로 셉니다.
        </p>
      </section>
    );
  }
  const types = [...new Set(JUDGES.flatMap((k) => Object.keys(m[k] ?? {})))].sort((a, b) =>
    Number(a === "unmutated") - Number(b === "unmutated") || (js.by_type[b] ?? 0) - (js.by_type[a] ?? 0));
  const overall = Object.fromEntries(JUDGES.map((k) => [k, sumRates(Object.entries(m[k] ?? {}).filter(([t]) => t !== "unmutated").map(([, c]) => c))]));
  return (
    <section aria-labelledby="judge-set-answer" className="space-y-10">
      <div className="space-y-3">
        {question}
        <h3 id="judge-set-answer" className="sr-only">변형 종류별 잘못 통과율</h3>
        <dl className="grid gap-8 sm:grid-cols-2">
          {JUDGES.map((k) => (
            <div key={k} className="space-y-1">
              <dt className="text-sm text-muted-foreground">{ARM[k]} · 틀린 답을 통과시킨 비율</dt>
              <dd className="text-5xl font-bold tabular-nums">{pct(overall[k].rate)}</dd>
              <dd className="text-sm tabular-nums text-muted-foreground">
                판정한 오답 {overall[k].judged}개 중 {overall[k].passed}개 · 정답 통과 {pct(m[k]?.unmutated?.rate)}
              </dd>
            </div>
          ))}
        </dl>
      </div>
      <Table caption="변형 종류별로 두 판정 모델이 틀린 답을 통과시킨 비율" head={["변형", "항목", ...JUDGES.map((k) => `${SHORT[k]} 통과율`)]}>
        {types.map((t) => (
          <tr key={t} className={cn(t === "unmutated" && "border-t-2")}>
            <th scope="row" className={cn(td, "min-w-24 text-left font-medium")}>{MUTATION[t] ?? t}</th>
            <td className={cn(td, "tabular-nums")}>{m.luna?.[t]?.items ?? m.jev_bridged?.[t]?.items}</td>
            {JUDGES.map((k) => <td key={k} className={cn(td, "tabular-nums")}><RateCell c={m[k]?.[t]} bad={t !== "unmutated"} /></td>)}
          </tr>
        ))}
      </Table>
      <p className="max-w-3xl text-sm text-muted-foreground">
        오답 행은 판정 모델 스스로 통과로 판정한 비율(낮을수록 좋음)이고, 마지막 행은 변형 안 한 정답을 통과시킨 비율(높을수록 좋음)입니다.
        보류는 분모에서 뺍니다. 코드가 값으로 판정할 수 있는 항목({types.reduce((n, t) => n + (m.luna?.[t]?.code_settled ?? 0), 0)}건)도 판정 모델 자신의 답으로 셉니다.
        Jev는 영어 다리를 거친 판정입니다. 세트 {results.judge_set_sha256?.slice(0, 8) ?? "-"}.
      </p>
    </section>
  );
}

// ---------------------------------------------------------------- the answer

/** A fold for what supports the page but is not the answer: small, quiet, closed by default. */
function More({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <details className="group">
      <summary className="inline-flex min-h-11 cursor-pointer list-none items-center gap-1.5 text-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 [&::-webkit-details-marker]:hidden">
        <span aria-hidden className="transition-transform group-open:rotate-90">›</span>{label}
      </summary>
      <div className="pt-2">{children}</div>
    </details>
  );
}

function Verdict({ ov, results, run }: { ov: Overview; results?: Results; run?: Run }) {
  const v = results?.verdict;
  const question = <p className="text-sm font-medium text-muted-foreground">Jev로 Luna 판정을 바꿀 수 있나</p>;
  if (!v) {
    return (
      <section aria-labelledby="judge-answer" className="space-y-6">
        <div className="space-y-2">
          {question}
          <h3 id="judge-answer" className="text-4xl font-bold tracking-tight">{run?.running ? "평가 실행 중" : "아직 판정 전"}</h3>
          <p className="text-lg">{run?.running ? "모든 항목을 판정하면 미리 정한 규칙으로 답합니다."
            : run ? "평가 실행이 끝나지 않았습니다. 아래 실행 관리에서 남은 항목을 이어서 실행하세요."
              : "아래 실행 관리에서 보정 → 평가 순서로 실행하세요."}</p>
        </div>
        {run?.running && <div className="max-w-3xl"><ArmProgress r={run} /></div>}
      </section>
    );
  }
  const verdict = VERDICT[v.verdict];
  const l = results!.arms.luna, j = results!.arms.jev_bridged;
  const margin = Number(v.rule.kappa_margin ?? 0.05);
  const passed = Object.fromEntries(v.checks.map((c) => [c.condition, c.passed]));
  const faster = l?.latency_ms.p50 && j?.latency_ms.p50 ? Math.round(l.latency_ms.p50 / j.latency_ms.p50) : null;
  const conditions = v.verdict === "inconclusive" || !l || !j ? [] : [
    { key: "kappa", title: "기준과의 일치도 (카파)", value: k3(j.kappa), need: l.kappa == null ? "-" : `${(l.kappa - margin).toFixed(3)} 이상이어야`, luna: k3(l.kappa) },
    { key: "false_accepts", title: "틀린 답을 통과시킨 수", value: `${j.false_accepts}건`, need: `${l.false_accepts}건 이하여야`, luna: `${l.false_accepts}건` },
    { key: "coverage", title: "판정한 비율", value: pct(j.coverage), need: `${pct(Number(v.rule.min_coverage ?? 0.9))} 이상이어야`, luna: pct(l.coverage) },
  ];
  const negatives = j?.reference_negatives ?? 0;
  return (
    <section aria-labelledby="judge-answer" className="space-y-10">
      <div className="space-y-3">
        {question}
        <h3 id="judge-answer" className={cn("text-5xl font-bold tracking-tight", verdict.text)}>{verdict.label}</h3>
        <p className="text-lg">{verdict.line}{v.verdict === "replaceable" && faster && faster > 1 ? ` 판정 속도는 Luna의 ${faster}배입니다.` : ""}</p>
      </div>
      {conditions.length > 0 ? (
        <dl className="grid gap-8 sm:grid-cols-3">
          {conditions.map((c) => (
            <div key={c.key} className="space-y-1">
              <dt className="text-sm text-muted-foreground">{c.title}</dt>
              <dd className="flex items-baseline gap-2">
                <span className="text-3xl font-bold tabular-nums">{c.value}</span>
                <span className={cn("text-sm font-semibold", passed[c.key] ? "text-ok" : "text-bad")}>{passed[c.key] ? "✓ 충족" : "✗ 미충족"}</span>
              </dd>
              <dd className="text-sm text-muted-foreground tabular-nums">{c.need} · Luna {c.luna}</dd>
            </div>
          ))}
        </dl>
      ) : <Notice tone="warn">{v.deciding.map((c) => c.detail).join(" · ")}</Notice>}
      <More label={negatives > 0 && negatives < 30 ? `이 답을 얼마나 믿을 수 있나 · 불통과 기준이 ${negatives}건뿐` : "규칙과 기준"}>
        <div className="max-w-3xl space-y-2 text-sm leading-relaxed text-muted-foreground">
          {negatives > 0 && negatives < 30 && <p>
            기준 세트에서 불통과로 검토된 항목이 {negatives}건뿐이라 카파와 잘못 통과는 한두 건에도 크게 움직입니다.
            이 결과는 &ldquo;이 기준에서 Luna보다 나쁘지 않다&rdquo;는 뜻이지, 검토자를 대신할 만큼 믿을 만하다는 뜻은 아닙니다.
          </p>}
          <p>규칙 {String(v.rule.version)}은 평가 실행 전에 코드와 docs/rag/judges.md에 정해 두었습니다. 세 조건을 모두 넘어야 교체 가능이고,
            어느 쪽이든 판정한 항목이 {String(v.rule.min_judged ?? 100)}개 미만이면 판단 보류입니다.</p>
          <p>기준: {ov.reference!.run_id}의 독립 검토 {ov.reference!.items}개 중 평가용 {ov.split?.held_out ?? "-"}개. 보정용 {ov.split?.calibration ?? "-"}개는 Jev 임계값을 맞추는 데만 썼습니다.</p>
        </div>
      </More>
    </section>
  );
}

// ---------------------------------------------------------------- three arms side by side

/** Agreement with its Wilson interval on a shared 60–100% axis, so the rows compare by eye. */
function Interval({ a }: { a: Arm }) {
  const [lo, hi] = a.agreement.wilson95 ?? [null, null];
  const rate = a.agreement.rate;
  const x = (v: number) => Math.max(0, Math.min(100, ((v - 0.6) / 0.4) * 100));
  if (lo == null || hi == null || rate == null) return null;
  return (
    <div className="relative mt-2 h-1 w-full max-w-36 rounded-full bg-secondary" role="img" aria-label={`95% 구간 ${pct(lo)}–${pct(hi)}`}>
      <span className="absolute inset-y-0 rounded-full bg-primary/30" style={{ left: `${x(lo)}%`, width: `${x(hi) - x(lo)}%` }} />
      <span className="absolute top-1/2 size-2 -translate-1/2 rounded-full bg-primary" style={{ left: `${x(rate)}%` }} />
    </div>
  );
}

const usdOrUnpriced = (a: Arm) => (a.usd.priced ? usd(a.usd.settled_micro_usd, 3) : "가격 미제공");

/** The four numbers a reader compares; everything else waits in the fold below. */
const KEY_METRICS: { title: string; value: (a: Arm) => React.ReactNode }[] = [
  { title: "기준과 일치", value: (a) => <>{pct(a.agreement.rate)}<Interval a={a} /></> },
  { title: "틀린 답 통과", value: (a) => <>{a.false_accepts}<span className="text-sm font-normal text-muted-foreground"> / {a.reference_negatives}건</span></> },
  { title: "한 건 판정 시간", value: (a) => sec(a.latency_ms.p50) },
  { title: "비용", value: usdOrUnpriced },
];
const COLS = "md:grid-cols-[minmax(13rem,1.3fr)_repeat(4,minmax(0,1fr))]";

function Comparison({ r }: { r: Results }) {
  const arms = ARMS.filter((k) => r.arms[k]);
  return (
    <section aria-labelledby="judge-arms" className="space-y-4">
      <h3 id="judge-arms" className="text-lg font-bold">세 판정 모델</h3>
      <div className={cn("hidden gap-6 border-b pb-2 text-sm text-muted-foreground md:grid", COLS)}>
        <span />{KEY_METRICS.map((m) => <span key={m.title}>{m.title}</span>)}
      </div>
      <ul className="divide-y">
        {arms.map((k) => {
          const a = r.arms[k];
          return (
            <li key={k} className={cn("grid grid-cols-2 gap-x-6 gap-y-4 py-5", COLS, k === "jev_raw" && "text-muted-foreground")}>
              <div className="col-span-2 md:col-span-1">
                <p className={cn("text-base font-semibold", k !== "jev_raw" && "text-foreground")}>{ARM[k]}</p>
                <p className="text-sm text-muted-foreground">{ARM_NOTE[k]}{k === "jev_raw" ? ` ${a.items}개` : ""}</p>
              </div>
              {KEY_METRICS.map((m) => (
                <dl key={m.title} className="min-w-0">
                  <dt className="text-sm text-muted-foreground md:sr-only">{m.title}</dt>
                  <dd className="text-xl font-semibold tabular-nums">{m.value(a)}</dd>
                </dl>
              ))}
            </li>
          );
        })}
      </ul>
      <More label="모든 지표 · 혼동 행렬">
        <AllMetrics r={r} arms={arms} />
      </More>
    </section>
  );
}

function AllMetrics({ r, arms }: { r: Results; arms: readonly string[] }) {
  const rows: [string, (a: Arm, k: string) => React.ReactNode][] = [
    ["판정 비율", (a) => `${pct(a.coverage)} (${a.judged}/${a.items})`],
    ["기준과 일치 · Wilson 95%", (a) => `${pct(a.agreement.rate)} (${a.agreement.numerator}/${a.agreement.denominator}) · ${pct(a.agreement.wilson95?.[0])}–${pct(a.agreement.wilson95?.[1])}`],
    ["카파 (통과·불통과)", (a) => k3(a.kappa)],
    ["틀린 답 통과 / 기준상 불통과", (a) => `${a.false_accepts} / ${a.reference_negatives}`],
    ["코드가 값으로 판정", (a) => `${a.code_settled}건`],
    ["비용", (a, k) => `${usdOrUnpriced(a)} · ${a.usd.calls}회${k === "jev_bridged" && a.bridge_usd ? ` · 번역 ${usd(a.bridge_usd.settled_micro_usd, 4)}` : ""}`],
    ["지연 p50 / p95", (a) => `${sec(a.latency_ms.p50)} / ${sec(a.latency_ms.p95)}`],
  ];
  return (
    <div className="space-y-6">
      <Table caption="세 판정 모델의 평가용 지표 전체" head={["지표", ...arms.map((k) => ARM[k])]}>
        {rows.map(([name, f]) => (
          <tr key={name}>
            <th scope="row" className={cn(td, "text-left font-medium")}>{name}</th>
            {arms.map((k) => <td key={k} className={cn(td, "tabular-nums")}>{f(r.arms[k], k)}</td>)}
          </tr>
        ))}
      </Table>
      <p className="max-w-3xl text-sm text-muted-foreground">
        카파는 통과(지지·근거 있음·정확)와 불통과로 나눠 셉니다. 보류(불확실 구간, 호출 실패, 번역 불가)는 오답이 아니라 미판정입니다.
        지연은 한 번에 한 호출씩 잰 값입니다. 임계값 {r.thresholds_sha256?.slice(0, 8) ?? "-"}.
      </p>
      <div className="grid gap-6 xl:grid-cols-3">{arms.map((k) => <Confusion key={k} arm={k} a={r.arms[k]} />)}</div>
    </div>
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
    <details open={open} className="group border-t pt-4">
      <summary className="flex min-h-11 cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1 outline-none focus-visible:ring-3 focus-visible:ring-ring/50 [&::-webkit-details-marker]:hidden">
        <span aria-hidden className="text-muted-foreground transition-transform group-open:rotate-90">›</span>
        <span className="text-base font-semibold">실행 관리</span>
        <span className="text-sm text-muted-foreground">{summary}</span>
      </summary>
      <div className="space-y-6 pt-4">
        <p className="text-sm text-muted-foreground">
          기준 {ov.reference!.run_id} · {ov.reference!.items}개 · 보정 {ov.split?.calibration ?? "-"} / 평가 {ov.split?.held_out ?? "-"} · 원문 대조 표본 {ov.split?.raw_sample ?? "-"}
        </p>
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
  const [part, setPart] = useState<"calibration" | "held_out" | "judge_set">(fitted ? "held_out" : "calibration");
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
            <option value="judge_set" disabled={!fitted}>3. 판정 골든 세트 — 변형 오답{fitted ? "" : " (보정 먼저)"}</option>
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
  false_accept: { label: "틀린 답을 통과시킴", test: (r) => !POSITIVE.has(r.reference) && ARMS.some((k) => POSITIVE.has(r.arms[k]?.label ?? "")) },
  luna: { label: "Luna가 기준과 다름", test: (r) => !!r.arms.luna && r.arms.luna.label !== r.reference },
  jev: { label: "Jev가 기준과 다름", test: (r) => !!r.arms.jev_bridged && r.arms.jev_bridged.label !== r.reference },
  abstain: { label: "Jev 보류", test: (r) => !!r.arms.jev_bridged && r.arms.jev_bridged.label == null },
  all: { label: "전체", test: () => true },
};
const PAGE = 12;
const sentence = (r: Row) => String((r.kind === "claim" ? r.korean.question : r.korean.claim) ?? "");
const labelText = (l: string | null | undefined): string => (!l ? "text-muted-foreground" : POSITIVE.has(l) ? "text-ok" : "text-bad");

function Review({ runId, stamp: key }: { runId: string; stamp: string }) {
  const rows = usePoll(`judge-disagreements:${key}`, () => must(api.GET("/api/verify/judges/disagreements/{run_id}",
    { params: { path: { run_id: runId } } }), errorText), null);
  const [kind, setKind] = useState("all");
  // the failures that decide replacement come first; the full list is one tap away
  const [filter, setFilter] = useState("false_accept");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [shown, setShown] = useState(PAGE);
  const visible = useMemo(() => (rows.data ?? []).filter((r) => (kind === "all" || r.kind === kind) && FILTERS[filter].test(r)
    && JSON.stringify([r.korean, r.english]).toLocaleLowerCase().includes(query.toLocaleLowerCase())), [rows.data, kind, filter, query]);
  const cur = visible.find((r) => r.blind_id === open) ?? visible[0];
  const pick = (f: string) => { setFilter(f); setShown(PAGE); setOpen(null); };
  return (
    <section aria-labelledby="judge-review" className="space-y-5">
      <h3 id="judge-review" className="text-lg font-bold">판정이 엇갈린 {rows.data?.length ?? ""}건</h3>
      {rows.error ? <Notice tone="bad">{rows.error}</Notice> : !rows.data ? <Skeleton className="h-64 w-full" /> : (
        <>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
            <div role="group" aria-label="엇갈림 종류" className="flex flex-wrap gap-2">
              {Object.entries(FILTERS).map(([k, f]) => (
                <button key={k} type="button" aria-pressed={filter === k} onClick={() => pick(k)}
                        className={cn("min-h-9 rounded-full px-3.5 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
                          filter === k ? "bg-foreground font-semibold text-background" : "bg-secondary text-foreground hover:bg-secondary/70")}>
                  {f.label} <span className="tabular-nums opacity-70">{rows.data!.filter(f.test).length}</span>
                </button>
              ))}
            </div>
            <div className="flex gap-2 xl:ml-auto">
              <select aria-label="항목 종류" value={kind} onChange={(e) => { setKind(e.target.value); setShown(PAGE); }} className={cn(field, "h-9 w-32")}>
                <option value="all">모든 종류</option>{Object.entries(KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
              <input aria-label="문장에서 찾기" placeholder="문장에서 찾기" value={query} onChange={(e) => { setQuery(e.target.value); setShown(PAGE); }} className={cn(field, "h-9 w-44")} />
            </div>
          </div>
          {!visible.length ? <Empty>조건에 맞는 항목이 없습니다.</Empty> : (
            <div className="grid gap-8 xl:grid-cols-[360px_minmax(0,1fr)]">
              <nav aria-label="엇갈린 항목 목록" className="min-w-0">
                <ul className="space-y-1">
                  {visible.slice(0, shown).map((r) => <li key={r.blind_id}><ItemRow r={r} active={r === cur} onOpen={() => {
                    setOpen(r.blind_id);
                    // below xl the item opens under the list, out of view; bring it up
                    if (!matchMedia("(min-width: 80rem)").matches) document.getElementById("judge-item")?.scrollIntoView({ block: "start" });
                  }} /></li>)}
                </ul>
                {visible.length > shown && (
                  <button type="button" onClick={() => setShown(shown + PAGE)} className="mt-2 min-h-11 px-3 text-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
                    {Math.min(PAGE, visible.length - shown)}건 더 보기 · 남은 {visible.length - shown}건
                  </button>
                )}
              </nav>
              <div id="judge-item" className="min-w-0 scroll-mt-4 xl:sticky xl:top-6 xl:max-h-[calc(100dvh-3rem)] xl:self-start xl:overflow-y-auto">
                {cur && <ItemDetail key={cur.blind_id} r={cur} />}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function ItemRow({ r, active, onOpen }: { r: Row; active: boolean; onOpen: () => void }) {
  return (
    <button type="button" onClick={onOpen} aria-current={active || undefined}
            className={cn("w-full space-y-1.5 rounded-lg px-3 py-3 text-left outline-none hover:bg-secondary/70 focus-visible:ring-3 focus-visible:ring-ring/50",
              active && "bg-accent hover:bg-accent")}>
      {r.kind === "claim" ? (
        <>
          <span className="line-clamp-2 text-sm [overflow-wrap:anywhere]">필수 사실 · {String(r.korean.required ?? "")}</span>
          <span className="line-clamp-1 text-xs text-muted-foreground [overflow-wrap:anywhere]">{sentence(r)}</span>
        </>
      ) : <span className="line-clamp-2 text-sm [overflow-wrap:anywhere]">{sentence(r)}</span>}
      <span className="block text-xs text-muted-foreground">
        {r.mutation && <>{MUTATION[String(r.mutation.type)] ?? String(r.mutation.type)} · </>}
        기준 <span className={cn("font-semibold", labelText(r.reference))}>{LABEL[r.reference] ?? r.reference}</span>
        {ARMS.filter((k) => r.arms[k] && r.arms[k]!.label !== r.reference).map((k) => {
          const l = r.arms[k]!.label;
          return <span key={k}> · {SHORT[k]} <span className={cn("font-semibold", labelText(l))}>{LABEL[l ?? "abstain"] ?? l}</span></span>;
        })}
      </span>
    </button>
  );
}

const FIELDS: [string, string][] = [
  ["question", "질문"], ["claim", "주장"], ["required", "필수 사실"], ["conditions", "조건"],
  ["answer_summary", "답변 요약"], ["answer_claims", "답변 주장"], ["passages", "근거 인용"],
];
const FOLDED = new Set(["answer_summary", "answer_claims", "passages"]);
const lines = (v: unknown) => (Array.isArray(v) ? v.map(String).filter(Boolean) : v ? [String(v)] : []);

function ItemDetail({ r }: { r: Row }) {
  const shown = FIELDS.filter(([k]) => lines(r.korean[k]).length);
  return (
    <article className="space-y-8 xl:border-l xl:pl-8">
      <header className="space-y-2">
        <p className="text-sm text-muted-foreground">
          {KIND[r.kind]} · 기준 판정 <span className={cn("font-semibold", labelText(r.reference))}>{LABEL[r.reference] ?? r.reference}</span>
        </p>
        <p className="text-lg font-semibold leading-snug [overflow-wrap:anywhere]">{sentence(r)}</p>
        {r.kind === "claim" && r.korean.required ? <p className="text-sm">필수 사실 · {String(r.korean.required)}</p> : null}
        {r.mutation && (
          <p className="text-sm">
            <span className="font-semibold">{MUTATION[String(r.mutation.type)] ?? String(r.mutation.type)}</span>
            {r.mutation.from != null && <> · <span className="line-through">{String(r.mutation.from)}</span>{r.mutation.to ? <> → {String(r.mutation.to)}</> : " (뺌)"}</>}
          </p>
        )}
      </header>
      <ul className="grid gap-6 sm:grid-cols-[repeat(auto-fit,minmax(11rem,1fr))]">
        {ARMS.filter((k) => r.arms[k]).map((k) => {
          const a = r.arms[k]!;
          const off = a.label !== r.reference;
          return (
            <li key={k} className="space-y-1">
              <p className="text-sm text-muted-foreground">{ARM[k]}</p>
              <p className="flex flex-wrap items-baseline gap-x-2">
                <span className={cn("text-base font-bold", labelText(a.label))}>{LABEL[a.label ?? "abstain"] ?? a.label}</span>
                {off && <span className="text-xs text-muted-foreground">≠ 기준</span>}
                {a.probability != null && <span className="text-xs tabular-nums text-muted-foreground">지지 확률 {a.probability.toFixed(2)}</span>}
              </p>
              {a.source === "code" && <p className="text-xs text-muted-foreground">코드가 값으로 판정</p>}
              {a.abstain && <p className="text-xs text-muted-foreground">{a.abstain.startsWith("untranslatable") ? "영어로 옮기지 못해 보내지 않음" : a.abstain}</p>}
              {a.reason && <p lang="en" className="text-sm leading-relaxed text-muted-foreground">{a.reason}</p>}
            </li>
          );
        })}
      </ul>
      <div className="space-y-1">
        <p className="text-sm font-semibold">원문과 Jev가 읽은 영어</p>
        {!r.english && <p className="text-sm text-warn">영어 다리 없음 · {r.untranslatable}. 번역이 보호한 값을 바꾸거나 한글을 남겨 Jev에 보내지 않았습니다.</p>}
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
  const cols = en ? "md:grid-cols-[5rem_minmax(0,1fr)_minmax(0,1fr)]" : "md:grid-cols-[5rem_minmax(0,1fr)]";
  if (folded) {
    return (
      <details className="group py-1">
        <summary className="flex min-h-11 cursor-pointer list-none items-center gap-1.5 text-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 [&::-webkit-details-marker]:hidden">
          <span aria-hidden className="transition-transform group-open:rotate-90">›</span>{name} {ko.length}개
        </summary>
        <div className={cn("grid gap-x-6 gap-y-2 pb-3", cols)}><span className="hidden md:block" />{body(ko)}{en && body(en, "en")}</div>
      </details>
    );
  }
  return (
    <div className={cn("grid gap-x-6 gap-y-2 py-3", cols)}>
      <dt className="text-sm text-muted-foreground">{name}</dt>
      <dd className="min-w-0">{body(ko)}</dd>
      {en && <dd className="min-w-0">{body(en, "en")}</dd>}
    </div>
  );
}
