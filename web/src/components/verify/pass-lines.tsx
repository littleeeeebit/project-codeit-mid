"use client";

// The two "does it pass" parts of 평가·릴리스 (evaluation.tsx), read against the same pass lines: the release
// verdict with a one-line reason and its checks as rows (value, n, pass line, 충족 / 미달 / 미측정), then the newest
// development answer run with each finalist in its own column. Run and config IDs, older runs and the blind-review
// CLI stay folded. Pass lines come from the overview API (release.TARGETS, LATENCY_TARGETS_MS); none is written here.

import { cn } from "cn";
import type { Schemas } from "@/lib/api";
import { label, MODE, RELEASE, stamp, type Tone, usd } from "@/lib/format";
import { StatusBadge } from "@/components/status-badge";
import { Empty, More, Notice } from "./parts";

type Evaluation = Schemas["VerifyOverview"]["evaluation"];
type Release = NonNullable<Evaluation["release"]>;
type Targets = Schemas["Targets"];
type Finalist = Schemas["FinalistScores"];
type Rate = Schemas["Rate"];
type Ok = boolean | null | undefined;
type Cell = { value?: string; ok: Ok; note?: string };

const pct = (x: number | null | undefined) => (x == null ? "-" : `${(x * 100).toFixed(1)}%`);
const sec = (ms: number | null | undefined) => (ms == null ? "-" : `${(ms / 1000).toFixed(1)}초`);
const atLeast = (x: number) => `${+(x * 100).toFixed(1)}% 이상`;
const under = (ms: number) => `${+(ms / 1000).toFixed(1)}초 미만`;

/** Korean label, one-line definition and pass line of every check a release manifest records, by its stable key. */
const CHECK: Record<string, { title: string; define: string; line: (t: Targets) => string; format?: (v: number) => string }> = {
  automated_invariants: { title: "자동 불변식 검사", line: () => "통과",
    define: "가짜 제공자로 돌리는 전체 자동 검사(check --phase all)가 지금 코드에서 통과했는지입니다." },
  critical_wrong: { title: "치명 오류", line: () => "없어야 함",
    define: "검토된 사례에서 마감일·금액·필수 조건·발주 기관을 틀리게 말한 답의 수입니다." },
  scope_leakage: { title: "선택 범위 밖 근거", line: () => "없어야 함",
    define: "고른 문서 밖의 주장이나 근거가 답과 검색 후보에 섞였는지입니다." },
  evidence_links: { title: "근거 링크 열림", line: () => "전부 열려야 함", format: pct,
    define: "답이 인용한 원문 링크가 실제 위치로 열리는 비율입니다." },
  admission_cap: { title: "사용 한도 지킴", line: () => "한도 안",
    define: "확정·보류 비용이 운영 한도 안이고 원장이 동결되지 않았는지입니다." },
  billing_resolved: { title: "미확정 비용", line: () => "없어야 함",
    define: "예약·전송 중·확인 불가 상태로 남은 유료 호출이 있는지입니다." },
  "single_hit@20": { title: "단일 근거 검색 적중", line: (t) => atLeast(t.rates["single_hit@20"]), format: pct,
    define: "근거가 하나인 질문에서 정답 근거가 상위 20개 안에 든 비율입니다." },
  "multi_complete@20": { title: "다중 근거 완전 회수", line: (t) => atLeast(t.rates["multi_complete@20"]), format: pct,
    define: "근거가 여럿인 질문에서 필요한 근거를 상위 20개 안에 모두 찾은 비율입니다." },
  claim_correctness: { title: "필수 주장 정확도", line: (t) => atLeast(t.rates.claim_correctness), format: pct,
    define: "골드가 요구한 사실(금액·날짜·조건)을 답이 정확히 말한 비율입니다." },
  citation_precision: { title: "인용 정밀도", line: (t) => atLeast(t.rates.citation_precision), format: pct,
    define: "답이 단 인용이 그 문장을 실제로 뒷받침하는 비율입니다. 사람이 판정하지 않은 인용은 뒷받침하지 않는 것으로 세므로, 미판정 인용이 0건이 될 때까지는 비관적인 하한입니다." },
  negative_handling: { title: "부정·모호 질문 처리", line: (t) => atLeast(t.rates.negative_handling), format: pct,
    define: "원문에 없는 내용이나 모호한 질문에 지어내지 않고 없음·확인 필요로 답한 비율입니다." },
  answer_p95: { title: "전체 답변 p95", line: (t) => under(t.latency_ms.answer_p95), format: sec,
    define: "질문부터 답변 완료까지 걸린 시간의 95번째 백분위입니다. 출시 판정은 여섯 명이 함께 쓸 때 잽니다." },
};

/** Why evidence short of sealed gold keeps the release from 출시 가능, by the manifest's evidence label. */
const EVIDENCE: Record<string, string> = {
  "sealed pilot": "근거가 봉인 시범 세트뿐이라 출시 가능으로 볼 수 없습니다",
  "development gold": "근거가 개발 골드뿐이라 출시 가능으로 볼 수 없습니다",
  "development pilot": "근거가 개발 시범 세트뿐이라 출시 가능으로 볼 수 없습니다",
  "no answer evaluation": "답변 평가가 없어 출시 가능으로 볼 수 없습니다",
  "no answer evaluation for the current candidate": "현재 후보의 답변 평가가 없어 출시 가능으로 볼 수 없습니다",
};
const TEXT: Record<Tone, string> = { ok: "text-ok", warn: "text-warn", bad: "text-bad", info: "text-info", neutral: "text-foreground" };

// ---------------------------------------------------------------- one row: what, its pass line, a value per column

function Mark({ ok }: { ok: Ok }) {
  return ok === true ? <span className="text-sm font-semibold text-ok">✓ 충족</span>
    : ok === false ? <span className="text-sm font-semibold text-bad">✗ 미달</span>
      : <span className="text-sm font-semibold text-muted-foreground">– 미측정</span>;
}

const GRID = "grid grid-cols-1 gap-x-8 gap-y-3 md:grid-cols-[minmax(14rem,1.4fr)_repeat(var(--n),minmax(0,1fr))]";
const cols = (n: number) => ({ "--n": n }) as React.CSSProperties;

function Row({ k, line, cells, names }: { k: string; line: string; cells: Cell[]; names?: string[] }) {
  const c = CHECK[k];
  return (
    <li className={cn(GRID, "py-5")} style={cols(cells.length)}>
      <div className="space-y-1">
        <p className="text-sm font-semibold">{c.title} <span className="font-normal text-muted-foreground">· 기준 {line}</span></p>
        <p className="max-w-xl text-[13px] leading-relaxed text-muted-foreground">{c.define}</p>
      </div>
      {cells.map((x, i) => (
        <div key={i} className="space-y-0.5">
          {names && <p className="text-xs text-muted-foreground md:hidden">{names[i]}</p>}
          <p className="flex flex-wrap items-baseline gap-x-2">
            {x.value && <span className="text-2xl font-bold tabular-nums">{x.value}</span>}
            <Mark ok={x.ok} />
          </p>
          {x.note && <p className="text-[13px] tabular-nums text-muted-foreground">{x.note}</p>}
        </div>
      ))}
    </li>
  );
}

// ---------------------------------------------------------------- the release verdict

function reasonLine(r: Release): string {
  if (r.status === "ready") return "모든 필수 검사와 품질 기준을 봉인된 골드 평가에서 통과했습니다.";
  if (!r.checks.length) return `기록된 사유 ${r.reasons.length}개가 있습니다.`;
  if (r.status === "blocked") return `필수 검사 ${r.checks.filter((c) => c.kind === "hard" && c.ok === false).length}개를 통과하지 못했습니다.`;
  const missed = r.checks.filter((c) => c.ok === false).length, open = r.checks.filter((c) => c.ok == null).length;
  const parts = [missed && `미달 ${missed}개`, open && `미측정 ${open}개`].filter(Boolean);
  const evidence = r.evidence_label && r.evidence_label !== "sealed gold"
    ? EVIDENCE[r.evidence_label] ?? `근거(${r.evidence_label})가 봉인 골드가 아니라 출시 가능으로 볼 수 없습니다` : null;
  return [parts.length ? `기준 ${parts.join(", ")}` : null, evidence].filter(Boolean).join(" · ") + ".";
}

export function Verdict({ release, targets }: { release: Evaluation["release"]; targets: Targets }) {
  const question = <p className="text-sm font-medium text-muted-foreground">지금 후보를 출시해도 되나</p>;
  if (!release) {
    return (
      <section aria-labelledby="release-answer" className="space-y-2">
        {question}
        <h3 id="release-answer" className="text-4xl font-bold tracking-tight">판정 없음</h3>
        <p className="text-lg">기록된 릴리스 판정이 없습니다. 소유자가 release-report를 실행하면 기록됩니다.</p>
      </section>
    );
  }
  const r = RELEASE[release.status] ?? { label: release.status, tone: "neutral" as const };
  const groups = [
    { kind: "hard", title: "필수 검사", note: "하나라도 미달이면 차단" },
    { kind: "quality", title: "품질 기준", note: "미달이나 미측정이 있으면 제한적 출시" },
  ] as const;
  return (
    <section aria-labelledby="release-answer" className="space-y-10">
      <div className="space-y-3">
        {question}
        <h3 id="release-answer" className={cn("text-5xl font-bold tracking-tight", TEXT[r.tone])}>{r.label}</h3>
        <p className="text-lg">{reasonLine(release)}</p>
        <p className="text-[13px] text-muted-foreground">판정 시각 {stamp(release.generated_at)}</p>
      </div>
      {release.checks.length ? groups.map((g) => (
        <div key={g.kind}>
          <h4 className="text-sm font-semibold">{g.title} <span className="font-normal text-muted-foreground">· {g.note}</span></h4>
          <ul className="divide-y">
            {release.checks.filter((c) => c.kind === g.kind && CHECK[c.key]).map((c) => (
              <Row key={c.key} k={c.key} line={CHECK[c.key].line(targets)} cells={[{
                value: c.value == null ? undefined : CHECK[c.key].format?.(c.value), ok: c.ok,
                note: [c.denominator != null && `n=${c.denominator}`, c.unjudged != null && `미판정 인용 ${c.unjudged}건`]
                  .filter(Boolean).join(" · ") || undefined,
              }]} />
            ))}
          </ul>
        </div>
      )) : (
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">이 판정은 항목별 검사 기록이 생기기 전에 만들어져 사유 문장만 있습니다. release-report를 다시 실행하면 항목별로 보입니다.</p>
          {release.reasons.length > 0 && <ol className="list-decimal space-y-1 pl-5 text-sm">{release.reasons.map((x, i) => <li key={i}>{x}</li>)}</ol>}
        </div>
      )}
      <More label="판정 기록 원문">
        <div className="space-y-2 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">
          <p>릴리스 <span className="font-mono">{release.release_id}</span> · 근거 {release.evidence_label ?? "-"}</p>
          {release.checks.length > 0 && <ul className="space-y-1">{release.checks.map((c) => <li key={c.key}><span className="font-mono">{c.key}</span>: {c.evidence}</li>)}</ul>}
          {release.checks.length > 0 && release.reasons.length > 0 && <ol className="list-decimal space-y-1 pl-5">{release.reasons.map((x, i) => <li key={i}>{x}</li>)}</ol>}
        </div>
      </More>
    </section>
  );
}

// ---------------------------------------------------------------- the newest development answer run

function rateCell(r: Rate | null | undefined, line: number, extra?: string | false): Cell {
  const measured = !!r && !!r.denominator && r.rate != null;
  return { value: measured ? pct(r!.rate) : "-", ok: measured ? r!.rate! >= line : null,
           note: [measured ? `${r!.numerator}/${r!.denominator}` : "표본 없음", extra].filter(Boolean).join(" · ") };
}

const METRICS: { k: string; cell: (f: Finalist, t: Targets) => Cell }[] = [
  { k: "claim_correctness", cell: (f, t) => rateCell(f.required_claim_correctness, t.rates.claim_correctness,
    f.claims_needing_review > 0 && `검토 대기 ${f.claims_needing_review}건`) },
  { k: "critical_wrong", cell: (f) => !f.completed ? { value: "-", ok: null, note: "채점된 답변 없음" } : {  // empty lists prove nothing
    value: `${f.critical_wrong.length}건`, ok: f.critical_wrong.length ? false : f.critical_unresolved.length ? null : true,
    note: f.critical_unresolved.length ? `판정 대기 ${f.critical_unresolved.length}건` : undefined } },
  { k: "citation_precision", cell: (f, t) => rateCell(f.citation_precision_lower_bound, t.rates.citation_precision,
    `미판정 인용 ${f.links_unjudged}건`) },
  { k: "negative_handling", cell: (f, t) => rateCell(f.negative_handling, t.rates.negative_handling) },
  { k: "answer_p95", cell: (f, t) => ({ value: sec(f.latency_ms?.p95),
    ok: f.latency_ms?.p95 == null ? null : f.latency_ms.p95 < t.latency_ms.answer_p95,
    note: f.latency_ms?.n ? `n=${f.latency_ms.n} · 한 번에 한 건씩 잰 값` : "표본 없음" }) },
];

export function DevRun({ e }: { e: Evaluation }) {
  const run = e.answer_runs.at(-1);  // the service orders runs oldest first
  const older = e.answer_runs.slice(0, -1).reverse();
  const finalists = Object.entries(run?.scores?.finalists ?? {});
  const names = finalists.map(([, f], i) => (finalists.length > 1 ? `후보 ${i + 1} · ` : "") + label(MODE, f.mode));
  return (
    <section aria-labelledby="dev-run" className="space-y-4">
      <div className="space-y-1">
        <h3 id="dev-run" className="text-lg font-bold">최근 개발 답변 평가</h3>
        {run && <p className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          {run.running ? <StatusBadge tone="info">실행 중</StatusBadge> : run.scores?.status === "complete" ? <StatusBadge tone="ok">완료</StatusBadge>
            : run.scores ? <StatusBadge tone="warn">일부 완료</StatusBadge> : <StatusBadge tone="neutral">점수 없음</StatusBadge>}
          <span className="tabular-nums">개발(dev) 질문 답변 {run.progress.done}/{run.progress.total}</span>
          {finalists.length === 1 && <span>· {names[0]}</span>}
          <span>· 출시 판정과 같은 기준선이며, 표본 수와 함께 읽으세요</span>
        </p>}
      </div>
      {!run ? <Empty>아직 평가 실행이 없습니다.</Empty> : (
        <>
          {run.scores?.stop_reason && <Notice tone="warn">중단 사유: {run.scores.stop_reason}</Notice>}
          {finalists.length === 0 ? <p className="text-sm text-muted-foreground">{run.running ? "첫 답변이 채점되면 여기에 보입니다." : "점수가 없습니다."}</p> : (
            <div>
              {finalists.length > 1 && (
                <div className={cn(GRID, "hidden border-b pb-2 text-sm text-muted-foreground md:grid")} style={cols(finalists.length)}>
                  <span />{names.map((n) => <span key={n}>{n}</span>)}
                </div>
              )}
              <ul className="divide-y">
                {METRICS.map((m) => (
                  <Row key={m.k} k={m.k} line={CHECK[m.k].line(e.targets)} names={finalists.length > 1 ? names : undefined}
                       cells={finalists.map(([, f]) => m.cell(f, e.targets))} />
                ))}
              </ul>
            </div>
          )}
          <More label={older.length ? `실행 정보 · 이전 실행 ${older.length}개` : "실행 정보"}>
            <div className="space-y-3 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">
              <p>실행 <span className="font-mono">{run.run_id}</span> · 구성 {run.config.label ?? run.config.config_id ?? "-"}</p>
              {finalists.length > 0 && <ul className="space-y-1">{finalists.map(([id, f], i) => (
                <li key={id}>{names[i]}: <span className="font-mono">{id}</span> · 완료 {f.completed}/{f.of} · 비용 {usd(f.cost?.settled_micro_usd, 4)}</li>
              ))}</ul>}
              {older.length > 0 && <ul className="space-y-1">{older.map((r) => (
                <li key={r.run_id}><span className="font-mono">{r.run_id}</span> · {r.config.label ?? r.config.config_id ?? "-"} · {r.scores?.status ?? "점수 없음"} · {r.progress.done}/{r.progress.total}</li>
              ))}</ul>}
              <p>사람의 블라인드 검토: export-review로 검토표를 내보내고, 판정을 채운 뒤 import-review로 들입니다 (CLI).</p>
            </div>
          </More>
        </>
      )}
    </section>
  );
}
