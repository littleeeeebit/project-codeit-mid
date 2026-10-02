"use client";

// Gold and sealed state as the verifier may see it: the development set's validation and freeze, the sealed set's
// size and freeze only (never its questions or labels), disputed rows awaiting a second review, recent decisions.

import { useState } from "react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { GOLD_TYPE, label, stamp } from "@/lib/format";
import { must, usePoll } from "@/lib/use-poll";
import { Section, Table, td } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Empty, Field, field, Notice } from "./parts";

type Overview = Schemas["VerifyOverview"];
export type Waiting = Schemas["SecondReview"];

const CRITICAL: Record<string, string> = { deadline: "마감", amount: "금액", mandatory_condition: "필수 조건", institution: "기관" };
const MATCH: Record<string, string> = { number: "숫자", date: "날짜", text: "문구" };

export function SealedBadge({ test }: { test: Overview["evaluation"]["test"] }) {
  if (!test.frozen) return <StatusBadge tone="neutral">고정 전</StatusBadge>;
  return test.current ? <StatusBadge tone="ok">고정됨</StatusBadge> : <StatusBadge tone="bad">고정 이후 바뀜</StatusBadge>;
}

export function DevBadge({ ov }: { ov: Overview["evaluation"] }) {
  const v = ov.dev_validation;
  if (!v) return <StatusBadge tone="neutral">검증 기록 없음</StatusBadge>;
  if (!v.ok) return <StatusBadge tone="bad">오류 {v.errors.length}건</StatusBadge>;
  return v.label === "gold" ? <StatusBadge tone="ok">gold</StatusBadge> : <StatusBadge tone="warn">파일럿(목표 미달)</StatusBadge>;
}

export function DatasetState({ ov }: { ov: Overview }) {
  const e = ov.evaluation;
  const v = e.dev_validation;
  const frozen = e.dev_frozen as { current?: boolean } | null;
  return (
    <div className="space-y-8">
      <Section title="개발 데이터셋" aside={<DevBadge ov={e} />}>
        {!v ? <Empty>개발 데이터셋 검증 기록이 없습니다. CLI: validate-gold --dataset dev</Empty> : (
          <>
            <p className="text-sm text-muted-foreground">
              {v.rows}문항 · {frozen ? (frozen.current ? "고정됨" : "고정 이후 바뀜") : "고정 전"} · 검증 {stamp(v.validated_at)}
            </p>
            <Table caption="유형별 목표" head={["유형", "문항", "목표", "충족"]}>
              {Object.entries(v.targets).map(([t, x]) => (
                <tr key={t}>
                  <td className={td}>{label(GOLD_TYPE, t)}</td>
                  <td className={cn(td, "tabular-nums")}>{x.rows}</td>
                  <td className={cn(td, "tabular-nums")}>{x.target}</td>
                  <td className={td}>{x.met ? <StatusBadge tone="ok">충족</StatusBadge> : <StatusBadge tone="warn">미달</StatusBadge>}</td>
                </tr>
              ))}
            </Table>
            {v.errors.length > 0 && (
              <ul className="space-y-1 text-sm text-bad">{v.errors.slice(0, 50).map((x, i) => <li key={i}>{x}</li>)}</ul>
            )}
          </>
        )}
      </Section>
      <Section title="봉인 시험 세트" aside={<SealedBadge test={e.test} />}>
        <p className="text-sm"><span className="font-semibold tabular-nums">{e.test.rows}문항</span>
          <span className="text-muted-foreground"> · 문항과 라벨은 이 화면과 데이터셋 만들기에 표시하지 않습니다. 봉인 검토와 평가는 소유자 CLI로만 합니다.</span></p>
      </Section>
      <RecentDecisions />
    </div>
  );
}

export function RecentDecisions() {
  const recent = usePoll("gold-recent", () => must(api.GET("/api/gold/recent"), errorText), null);
  const cats = usePoll("gold-cats", () => must(api.GET("/api/gold/reject-categories"), errorText), null);
  return (
    <Section title="최근 검토 결정">
      {recent.error && <Notice tone="bad">{recent.error}</Notice>}
      {recent.data?.length === 0 && <Empty>처리된 질문이 없습니다.</Empty>}
      {!!recent.data?.length && (
        <Table caption="최근 검토 결정" head={["질문", "결과", "검토자", "시각", "거절 사유"]}>
          {recent.data.map((r) => (
            <tr key={r.candidate_id}>
              <td className={cn(td, "font-mono text-xs")}>{r.candidate_id}</td>
              <td className={td}>{r.status === "approved" ? <StatusBadge tone="ok">승인</StatusBadge> : <StatusBadge tone="bad">거절</StatusBadge>}</td>
              <td className={td}>{r.decided_by ?? "-"}</td>
              <td className={cn(td, "whitespace-nowrap tabular-nums")}>{stamp(r.decided_at)}</td>
              <td className={td}>{r.categories.map((c) => label(cats.data ?? {}, c)).join(", ") || "-"}</td>
            </tr>
          ))}
        </Table>
      )}
    </Section>
  );
}

export function WaitingRow({ w, active, onOpen }: { w: Waiting; active: boolean; onOpen: () => void }) {
  return (
    <button type="button" onClick={onOpen} aria-current={active || undefined}
            className={cn("flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left outline-none hover:bg-secondary/70 focus-visible:ring-3 focus-visible:ring-ring/50",
              active && "bg-accent hover:bg-accent")}>
      <span className="min-w-0 flex-1">
        <span className="line-clamp-1 text-sm font-medium">{w.question}</span>
        <span className="font-mono text-xs text-muted-foreground">{w.candidate_id}</span>
      </span>
      <StatusBadge tone="info">2차 검토</StatusBadge>
    </button>
  );
}

export function SecondReviewDetail({ w, onDone }: { w: Waiting; onDone: () => void }) {
  const [agreed, setAgreed] = useState<boolean | null>(null);
  const [note, setNote] = useState("");
  const [state, setState] = useState<{ busy?: boolean; error?: string }>({});
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (agreed === null) return setState({ error: "원문과 대조한 결과를 고르세요." });
    setState({ busy: true });
    const { error } = await api.POST("/api/gold/{candidate_id}/second-review", {
      params: { path: { candidate_id: w.candidate_id } }, body: { agreed, note } });
    if (error) return setState({ error: errorText(error) });
    onDone();
  };
  const id = `second-${w.candidate_id}`;
  return (
    <article className="space-y-6">
      <header className="space-y-2">
        <StatusBadge tone="info" size="md">2차 검토 대기</StatusBadge>
        <h3 className="text-lg font-bold leading-snug">{w.question}</h3>
        <p className="font-mono text-xs text-muted-foreground">{w.candidate_id}</p>
      </header>
      <Section title={`필수 주장 ${w.required_claims.length}개`}>
        <Table caption="필수 주장" head={["주장", "형식", "값", "중요도", "근거 그룹"]}>
          {w.required_claims.map((c, i) => (
            <tr key={c.claim_id ?? i}>
              <td className={cn(td, "font-mono text-xs")}>{c.claim_id}</td>
              <td className={td}>{label(MATCH, String(c.match.type ?? ""))}</td>
              <td className={td}>{Object.entries(c.match).filter(([k]) => k !== "type").map(([, v]) => String(v)).join(" · ")}</td>
              <td className={td}>{c.critical_kind ? <StatusBadge tone="warn">{label(CRITICAL, c.critical_kind)}</StatusBadge> : "보통"}</td>
              <td className={td}>{c.support_groups.join(", ")}</td>
            </tr>
          ))}
        </Table>
      </Section>
      <Section title="근거">
        <ul className="space-y-2">
          {w.evidence_groups.flatMap((g) => g.alternatives.map((a, i) => (
            <li key={`${g.group_id}-${i}`} className="rounded-xl border px-4 py-3 text-sm leading-6">
              <span className="mr-2 text-xs font-semibold text-muted-foreground">{g.group_id}</span>{a.quote ?? "-"}
            </li>
          )))}
        </ul>
      </Section>
      <form onSubmit={submit} className="space-y-4 rounded-2xl bg-secondary/60 p-4">
        <fieldset className="space-y-2">
          <legend className="text-[13px] font-semibold">원문과 대조한 결과</legend>
          <div className="flex gap-4 text-sm">
            {[["동의", true], ["동의하지 않음", false]].map(([t, v]) => (
              <label key={String(t)} className="flex items-center gap-2">
                <input type="radio" name={`${id}-verdict`} checked={agreed === v} onChange={() => setAgreed(v as boolean)} className="size-4 accent-primary" />{t}
              </label>
            ))}
          </div>
        </fieldset>
        <Field id={`${id}-note`} label="확인 내용 (원문 위치 포함)">
          <textarea id={`${id}-note`} rows={3} required value={note} onChange={(e) => setNote(e.target.value)} className={cn(field, "py-2")} />
        </Field>
        <Button type="submit" size="lg" disabled={state.busy}>2차 검토 기록</Button>
        {state.error && <Notice tone="bad">{state.error}</Notice>}
      </form>
    </article>
  );
}
