"use client";

// 판정 모델 비교. Who comes here: a verifier deciding whether the Jev judge can replace the gpt-6-luna judge. They
// read the verdict of the rule declared before the run and which condition decided it, compare the three arms side
// by side on the held-out half, run a part within the shared budget, and open the items where a judge and the
// independent reference disagree. Every number shown comes from the held-out half; calibration only fits thresholds.

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
const ARM: Record<string, string> = { luna: "Luna 판정", jev_bridged: "Jev · 영어 다리", jev_raw: "Jev · 한국어 원문" };
const PART: Record<string, string> = { calibration: "보정", held_out: "평가" };
const KIND: Record<string, string> = { link: "인용 하나", answer_claim: "답변 주장", claim: "필수 사실" };
const LABEL: Record<string, string> = {
  supporting: "지지", supported: "근거 있음", correct: "정확", unsupported: "근거 없음",
  incomplete_qualifier: "조건 누락", wrong_value: "값 틀림", missing: "빠짐", abstain: "판정 보류",
};
const VERDICT: Record<string, { label: string; tone: Tone }> = {
  replaceable: { label: "교체 가능", tone: "ok" }, not_replaceable: { label: "교체 불가", tone: "bad" },
  inconclusive: { label: "판단 보류", tone: "warn" },
};
const CONDITION: Record<string, string> = {
  kappa: "카파가 Luna보다 0.05 넘게 낮지 않음", false_accepts: "잘못 통과시킨 항목이 Luna보다 많지 않음",
  coverage: "Jev가 판정한 비율 90% 이상", min_judged: "두 판정 모두 평가 항목 100개 이상 판정",
};
const POSITIVE = new Set(["supporting", "supported", "correct"]);
const labelTone = (l: string | null | undefined): Tone => (!l ? "neutral" : POSITIVE.has(l) ? "ok" : "bad");
const pct = (x: number | null | undefined) => (x == null ? "-" : `${(x * 100).toFixed(1)}%`);
const ms = (x: number | null | undefined) => (x == null ? "-" : `${(x / 1000).toFixed(1)}초`);

export function JudgeSection() {
  const [running, setRunning] = useState(false);
  const ov = usePoll("judge-progress", () => must(api.GET("/api/verify/judges/progress"), errorText), running ? 3000 : null);
  const runs = ov.data?.runs ?? [];
  const isRunning = runs.some((r) => r.running);
  if (isRunning !== running) setRunning(isRunning);
  // the newest held-out run that has results; it reloads when a run finishes
  const heldOut = [...runs].filter((r) => r.part === "held_out").sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const key = heldOut ? `${heldOut.run_id}:${heldOut.status}:${heldOut.progress.luna?.done}` : null;
  const res = usePoll(key && `judge-results:${key}`, () => must(api.GET("/api/verify/judges/results/{run_id}",
    { params: { path: { run_id: heldOut!.run_id } } }), errorText), null);
  if (ov.error) return <Notice tone="bad">{ov.error}</Notice>;
  if (!ov.data) return <Skeleton className="h-96 w-full" />;
  if (!ov.data.reference) return <Empty>판정 기준 세트가 없습니다. 소유자가 CLI로 judge-reference를 실행해야 합니다.</Empty>;
  return (
    <div className="space-y-10">
      <Verdict results={res.data} running={!!heldOut?.running} />
      {res.data && Object.keys(res.data.arms).length > 0 && <Metrics r={res.data} />}
      <Runs ov={ov.data} runs={runs} onChanged={ov.reload} />
      {heldOut && res.data && <Disagreements runId={heldOut.run_id} stamp={key!} />}
    </div>
  );
}

// ---------------------------------------------------------------- verdict and metrics

function Verdict({ results, running }: { results?: Results; running: boolean }) {
  const v = results?.verdict;
  const badge = v ? VERDICT[v.verdict] : null;
  return (
    <Section title="Jev로 바꿀 수 있나" aside={badge ? <StatusBadge size="md" tone={badge.tone}>{badge.label}</StatusBadge>
      : <StatusBadge size="md" tone="neutral">{running ? "평가 실행 중" : "판정 전"}</StatusBadge>}>
      {!v ? <Empty>{running ? "평가 실행 중입니다. 모든 항목을 판정하면 규칙대로 판정합니다."
        : results ? "평가 실행이 끝나지 않아 판정하지 않았습니다. 다시 추정하고 실행하면 남은 항목만 이어서 합니다."
          : "평가(보고용) 실행이 아직 없습니다. 보정 → 평가 순서로 실행하세요."}</Empty> : (
        <div className="space-y-4 rounded-2xl border p-5">
          <p className="text-base font-semibold">
            결정한 조건: {v.deciding.map((c) => CONDITION[c.condition] ?? c.condition).join(" · ")}
          </p>
          <ul className="space-y-2">
            {(v.verdict === "inconclusive" ? v.deciding : v.checks).map((c) => (
              <li key={c.condition} className="flex flex-wrap items-baseline gap-2 text-sm">
                <StatusBadge tone={c.passed ? "ok" : c.passed === false ? "bad" : "neutral"}>{c.passed ? "충족" : c.passed === false ? "미충족" : "계산 안 함"}</StatusBadge>
                <span className="font-medium">{CONDITION[c.condition] ?? c.condition}</span>
                <span className="font-mono text-xs text-muted-foreground">{c.detail}</span>
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted-foreground">
            규칙은 평가 실행 전에 코드와 문서(docs/rag/judges.md)에 정해 두었습니다: 카파 Luna−0.05 이상, 잘못 통과 Luna 이하, 판정 비율 90% 이상을 모두 만족해야 교체 가능, 어느 쪽이든 판정 항목이 100개 미만이면 판단 보류.
          </p>
        </div>
      )}
    </Section>
  );
}

function cost(a: Arm, arm: string) {
  const main = a.usd.priced ? `${usd(a.usd.settled_micro_usd, 4)} · ${a.usd.calls}회` : `가격 미제공 · ${a.usd.calls}회`;
  return arm === "jev_bridged" && a.bridge_usd ? `${main} + 번역 ${usd(a.bridge_usd.settled_micro_usd, 4)}` : main;
}

function Metrics({ r }: { r: Results }) {
  const arms = ARMS.filter((k) => r.arms[k]);
  const rows: [string, (a: Arm, k: string) => React.ReactNode][] = [
    ["판정한 항목", (a) => `${a.judged}/${a.items} (${pct(a.coverage)})`],
    ["기준과 일치 (Wilson 95%)", (a) => a.agreement.rate == null ? "해당 없음"
      : `${pct(a.agreement.rate)} (${a.agreement.numerator}/${a.agreement.denominator}) · ${pct(a.agreement.wilson95?.[0])}–${pct(a.agreement.wilson95?.[1])}`],
    ["카파 (통과·불통과)", (a) => a.kappa == null ? "정의 안 됨" : a.kappa.toFixed(3)],
    ["잘못 통과 / 기준상 불통과", (a) => <span className={cn(a.false_accepts > 0 && "font-bold text-bad")}>{a.false_accepts} / {a.reference_negatives}</span>],
    ["코드가 정한 값 판정", (a) => String(a.code_settled)],
    ["비용", cost],
    ["지연 p50 / p95", (a) => `${ms(a.latency_ms.p50)} / ${ms(a.latency_ms.p95)}`],
  ];
  return (
    <Section title="판정 모델별 결과" aside={<span className="text-[13px] text-muted-foreground">평가용 절반만 · {r.status === "complete" ? "완료" : "일부"} · 임계값 {r.thresholds_sha256?.slice(0, 8) ?? "-"}</span>}>
      <Table caption="Luna, 영어 다리 Jev, 한국어 원문 Jev의 지표" head={["지표", ...arms.map((k) => ARM[k])]}>
        {rows.map(([name, f]) => (
          <tr key={name}>
            <th scope="row" className={cn(td, "text-left font-semibold")}>{name}</th>
            {arms.map((k) => <td key={k} className={cn(td, "tabular-nums")}>{f(r.arms[k], k)}</td>)}
          </tr>
        ))}
      </Table>
      <p className="text-xs text-muted-foreground">한국어 원문 Jev는 평가용 절반에서 고정 표본 {r.arms.jev_raw?.items ?? 0}개만 판정한 대조군입니다. 판정 보류(불확실 구간, 호출 실패, 번역 불가)는 오답이 아니라 미판정으로 셉니다.</p>
      <details className="rounded-xl border">
        <summary className="min-h-11 cursor-pointer px-4 py-3 text-sm font-semibold outline-none focus-visible:ring-3 focus-visible:ring-ring/50">기준 판정 대비 혼동 행렬</summary>
        <div className="grid gap-4 border-t p-4 lg:grid-cols-3">
          {arms.map((k) => <Confusion key={k} arm={k} a={r.arms[k]} />)}
        </div>
      </details>
    </Section>
  );
}

function Confusion({ arm, a }: { arm: string; a: Arm }) {
  return (
    <div className="space-y-2">
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

// ---------------------------------------------------------------- runs, plan and start

function Runs({ ov, runs, onChanged }: { ov: Overview; runs: Run[]; onChanged: () => void }) {
  const fitted = runs.some((r) => r.part === "calibration" && r.thresholds_fitted);
  return (
    <Section title="실행" aside={<span className="text-[13px] text-muted-foreground">
      기준 {ov.reference!.run_id} · {ov.reference!.items}개 · 보정 {ov.split?.calibration ?? "-"} / 평가 {ov.split?.held_out ?? "-"} · 원문 대조 표본 {ov.split?.raw_sample ?? "-"}</span>}>
      {runs.length === 0 ? <Empty>아직 실행이 없습니다.</Empty> : (
        <ul className="space-y-3">
          {[...runs].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((r) => <RunRow key={r.run_id} r={r} />)}
        </ul>
      )}
      <Plan fitted={fitted} running={runs.some((r) => r.running)} onStarted={onChanged} />
    </Section>
  );
}

function RunRow({ r }: { r: Run }) {
  const tone: Tone = r.running ? "info" : r.status === "complete" ? "ok" : "warn";
  return (
    <li className="space-y-3 rounded-xl border p-4">
      <div className="flex flex-wrap items-center gap-3">
        <StatusBadge tone={r.part === "held_out" ? "info" : "neutral"}>{PART[r.part]}</StatusBadge>
        <span className="font-mono text-sm font-semibold">{r.run_id}</span>
        <StatusBadge tone={tone}>{r.running ? "실행 중" : r.status === "complete" ? "완료" : "일부 완료"}</StatusBadge>
        {r.part === "calibration" && r.thresholds_fitted && <span className="text-xs text-ok">임계값 맞춤 완료</span>}
        <span className="ml-auto text-xs text-muted-foreground">{stamp(r.created_at)} · 번역 묶음 {r.translated_batches}개</span>
      </div>
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
      {(r.stop_reason || r.error) && !r.running && <Notice tone="warn">중단 사유: {r.stop_reason ?? r.error} · 같은 순서로 다시 추정하고 실행하면 남은 항목만 이어서 합니다.</Notice>}
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
    <div className="space-y-4 rounded-2xl bg-secondary/60 p-4">
      <div className="flex flex-wrap items-end gap-3">
        <Field id="judge-part" label="실행할 부분">
          <select id="judge-part" value={part} onChange={(e) => { setPart(e.target.value as typeof part); setEst(null); }} className={cn(field, "block h-11 sm:w-64")}>
            <option value="calibration">1. 보정 — Jev 임계값 맞춤</option>
            <option value="held_out" disabled={!fitted}>2. 평가 — 화면에 보고{fitted ? "" : " (보정 먼저)"}</option>
          </select>
        </Field>
        <Button variant="outline" size="lg" onClick={plan} disabled={state.busy || running}>비용 추정 <span className="text-ok">무료</span></Button>
        <span className="text-xs text-muted-foreground">{running ? "실행 중인 부분이 끝나면 다시 추정할 수 있습니다." : "추정은 호출 없이 남은 번역·판정 호출의 최대 비용만 계산합니다."}</span>
      </div>
      {est && (
        <>
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3 lg:grid-cols-6">
            {[["항목", `${est.items}개`], ["번역 호출", `${est.translation.attempts}회 (${est.translation.segments ?? 0}조각)`],
              ["Luna 판정 호출", `${est.judge.attempts}회`], ["Jev 호출", `${est.jev.calls}회 · 가격 미제공`],
              ["최대 비용", usd(est.max_micro_usd, 4)], ["judge_eval 남음", est.envelope_remaining_micro_usd == null ? "배정 안 됨" : usd(est.envelope_remaining_micro_usd, 4)]].map(([k, v]) => (
              <div key={k}><dt className="text-xs text-muted-foreground">{k}</dt><dd className="font-semibold tabular-nums">{v}</dd></div>
            ))}
          </dl>
          {!est.fits ? <Notice tone="bad">실행할 수 없습니다: {est.blocked}</Notice> : (
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex min-h-11 items-center gap-2 text-sm">
                <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} className="size-4 accent-primary" />
                이 추정으로 실행합니다 (최대 {usd(est.max_micro_usd, 4)}, 공유 예산 judge_eval에서 차감)
              </label>
              <Button size="lg" onClick={start} disabled={!agree || state.busy}>{PART[est.part]} 실행 · 유료</Button>
            </div>
          )}
        </>
      )}
      {state.error && <Notice tone="bad">{state.error}</Notice>}
    </div>
  );
}

// ---------------------------------------------------------------- disagreements

const FILTERS: Record<string, { label: string; test: (r: Row) => boolean }> = {
  all: { label: "전체", test: () => true },
  luna: { label: "Luna ≠ 기준", test: (r) => !!r.arms.luna && r.arms.luna.label !== r.reference },
  jev: { label: "Jev(영어) ≠ 기준", test: (r) => !!r.arms.jev_bridged && r.arms.jev_bridged.label !== r.reference },
  between: { label: "Luna ≠ Jev(영어)", test: (r) => r.arms.luna?.label !== r.arms.jev_bridged?.label },
  false_accept: { label: "기준상 불통과를 통과시킴", test: (r) => !POSITIVE.has(r.reference) && ARMS.some((k) => POSITIVE.has(r.arms[k]?.label ?? "")) },
  abstain: { label: "Jev 판정 보류", test: (r) => !!r.arms.jev_bridged && r.arms.jev_bridged.label == null },
};

function Disagreements({ runId, stamp: key }: { runId: string; stamp: string }) {
  const rows = usePoll(`judge-disagreements:${key}`, () => must(api.GET("/api/verify/judges/disagreements/{run_id}",
    { params: { path: { run_id: runId } } }), errorText), null);
  const [kind, setKind] = useState("all");
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState(20);
  const visible = useMemo(() => (rows.data ?? []).filter((r) => (kind === "all" || r.kind === kind) && FILTERS[filter].test(r)
    && JSON.stringify(r.korean).toLocaleLowerCase().includes(query.toLocaleLowerCase())), [rows.data, kind, filter, query]);
  return (
    <Section title="판정이 엇갈린 항목" aside={<span className="text-[13px] text-muted-foreground">{rows.data ? `${visible.length} / ${rows.data.length}건` : ""}</span>}>
      {rows.error ? <Notice tone="bad">{rows.error}</Notice> : !rows.data ? <Skeleton className="h-64 w-full" /> : (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field id="dis-kind" label="항목 종류">
              <select id="dis-kind" value={kind} onChange={(e) => { setKind(e.target.value); setShown(20); }} className={cn(field, "h-11")}>
                <option value="all">전체</option>{Object.entries(KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </Field>
            <Field id="dis-filter" label="엇갈림">
              <select id="dis-filter" value={filter} onChange={(e) => { setFilter(e.target.value); setShown(20); }} className={cn(field, "h-11")}>
                {Object.entries(FILTERS).map(([k, f]) => <option key={k} value={k}>{f.label}</option>)}
              </select>
            </Field>
            <Field id="dis-query" label="원문에서 찾기">
              <input id="dis-query" value={query} onChange={(e) => { setQuery(e.target.value); setShown(20); }} className={cn(field, "h-11")} />
            </Field>
          </div>
          {visible.length === 0 ? <Empty>조건에 맞는 항목이 없습니다.</Empty> : (
            <ul className="space-y-4">{visible.slice(0, shown).map((r) => <DisagreementRow key={r.blind_id} r={r} />)}</ul>
          )}
          {visible.length > shown && <Button variant="outline" size="lg" onClick={() => setShown(shown + 20)}>20건 더 보기</Button>}
        </>
      )}
    </Section>
  );
}

function DisagreementRow({ r }: { r: Row }) {
  return (
    <li className="space-y-4 rounded-xl border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge tone="neutral">{KIND[r.kind]}</StatusBadge>
        <span className="text-sm">기준 판정</span>
        <StatusBadge tone={labelTone(r.reference)} size="md">{LABEL[r.reference] ?? r.reference}</StatusBadge>
        <span className="ml-auto font-mono text-xs text-muted-foreground">{r.blind_id}</span>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        {ARMS.map((k) => {
          const a = r.arms[k];
          return (
            <div key={k} className={cn("space-y-1 rounded-lg border p-3", a && a.label !== r.reference && "border-bad/50")}>
              <p className="text-xs font-semibold text-muted-foreground">{ARM[k]}</p>
              {!a ? <p className="text-sm text-muted-foreground">이 항목은 판정 대상이 아님</p> : (
                <>
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge tone={labelTone(a.label)}>{LABEL[a.label ?? "abstain"] ?? a.label}</StatusBadge>
                    {a.probability != null && <span className="text-xs tabular-nums">지지 확률 {a.probability.toFixed(2)}</span>}
                    {a.source === "code" && <span className="text-xs text-muted-foreground">코드 값 판정</span>}
                  </div>
                  {a.abstain && <p className="text-xs text-muted-foreground">보류 사유: {a.abstain}</p>}
                  {a.reason && <p className="text-sm">{a.reason}</p>}
                </>
              )}
            </div>
          );
        })}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Texts title="한국어 원문" t={r.korean} />
        {r.english ? <Texts title="영어 다리" t={r.english} lang="en" />
          : <Notice tone="warn">영어 다리 없음: {r.untranslatable} — Jev에 보내지 않고 판정 보류로 셉니다.</Notice>}
      </div>
    </li>
  );
}

const FIELD_NAME: Record<string, string> = {
  question: "질문", claim: "주장", required: "필수 사실", conditions: "조건", answer_summary: "답변 요약",
  answer_claims: "답변 주장", passages: "근거 인용",
};

function Texts({ title, t, lang }: { title: string; t: Record<string, unknown>; lang?: string }) {
  return (
    <div lang={lang} className="min-w-0 space-y-2 rounded-lg bg-secondary/50 p-3 text-sm">
      <p className="text-xs font-semibold text-muted-foreground">{title}</p>
      {Object.entries(t).map(([k, v]) => {
        const list = Array.isArray(v) ? (v as string[]).filter(Boolean) : v ? [String(v)] : [];
        if (!list.length) return null;
        const long = k === "passages" || k === "answer_claims";
        const body = list.map((x, i) => <p key={i} className="whitespace-pre-wrap [overflow-wrap:anywhere]">{x}</p>);
        return long ? (
          <details key={k}>
            <summary className="min-h-8 cursor-pointer font-semibold outline-none focus-visible:ring-3 focus-visible:ring-ring/50">{FIELD_NAME[k] ?? k} · {list.length}개</summary>
            <div className="mt-1 space-y-2 border-l-2 pl-3">{body}</div>
          </details>
        ) : (
          <div key={k}><p className="font-semibold">{FIELD_NAME[k] ?? k}</p>{body}</div>
        );
      })}
    </div>
  );
}
