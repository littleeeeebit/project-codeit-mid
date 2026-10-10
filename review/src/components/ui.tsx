import type { Candidate } from "@/lib/api";
import { hostLabel, short } from "@/lib/api";

const TONES = {
  ok: "bg-ok-bg text-ok",
  warn: "bg-warn-bg text-warn",
  bad: "bg-bad-bg text-bad",
  info: "bg-primary-bg text-primary",
  neutral: "bg-neutral-bg text-neutral",
} as const;

export function Badge({ tone = "neutral", children }: { tone?: keyof typeof TONES; children: React.ReactNode }) {
  return (
    <span className={`inline-flex h-6 shrink-0 items-center rounded-md px-2 text-[13px] font-semibold ${TONES[tone]}`}>
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: Candidate["status"] }) {
  if (status === "complete") return <Badge tone="ok">완료</Badge>;
  if (status === "refused") return <Badge tone="warn">실행 거부</Badge>;
  if (status === "stopped") return <Badge tone="warn">중간에 멈춤</Badge>;
  return <Badge tone="bad">실패</Badge>;
}

export function Segmented<T extends string>({ value, onChange, options }: {
  value: T; onChange: (v: T) => void; options: [T, string][];
}) {
  return (
    <div role="radiogroup" className="inline-flex rounded-[10px] bg-surface p-1">
      {options.map(([v, label]) => (
        <button key={v} type="button" role="radio" aria-checked={value === v} onClick={() => onChange(v)}
                className={`h-8 rounded-md px-3 text-[14px] font-medium ${value === v ? "bg-white text-ink shadow-sm" : "text-muted hover:text-ink"}`}>
          {label}
        </button>
      ))}
    </div>
  );
}

/** A stopped paid candidate keeps what it finished: shown with its reason, never as a complete result. */
export function Stopped({ c }: { c: Candidate }) {
  if (c.status !== "stopped") return null;
  return (
    <p className="mt-2 rounded-md bg-warn-bg px-2 py-1 text-[13px] text-warn">
      중간에 멈춤: {c.reason ?? "이유가 기록되지 않았습니다"} · 다시 이어서 실행하면 남은 것만 지불합니다
    </p>
  );
}

export function LedgerLine({ ledger }: { ledger?: Candidate["ledger"] }) {
  if (!ledger) return null;
  return (
    <p className="mt-2 text-[13px] text-muted" title={ledger.target}>
      공유 원장 기록 ${(ledger.settled_micro_usd / 1e6).toFixed(4)} · 시도 {ledger.attempts}건
      {ledger.open > 0 && <span className="text-bad"> · 정산 안 됨 {ledger.open}건</span>}
    </p>
  );
}

/** One candidate's identity, the same block over every column so the eye finds the same candidate everywhere. */
export function CandidateHead({ c }: { c: Candidate }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2">
        <span className="truncate text-[15px] font-bold" title={c.ref}>{c.label}</span>
        {c.role === "baseline" && <Badge tone="info">기준</Badge>}
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[13px] text-muted">
        <code title={c.commit}>{short(c.commit)}</code>
        <span>{hostLabel(c.host)}</span>
        {c.variant && <span title={JSON.stringify(c.variant)}>설정 변경</span>}
      </div>
    </div>
  );
}

export function Failure({ c }: { c: Candidate }) {
  return (
    <div className="mt-3 rounded-[10px] bg-bad-bg px-3 py-2 text-[14px] text-bad">
      <div className="font-semibold">{c.status === "refused" ? "이 기기에서 실행하지 않음" : "결과 없음"}</div>
      <p className="mt-1 break-words">{c.reason ?? "이유가 기록되지 않았습니다"}</p>
    </div>
  );
}

export const columns = (n: number) => ({ gridTemplateColumns: `repeat(${n}, minmax(280px, 1fr))` });
