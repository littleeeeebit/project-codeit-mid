"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "cn";
import { api, type Budget, errorText } from "@/lib/api";
import { usd, WARNING } from "@/lib/format";
import { useMember, writeMember } from "@/lib/member";
import { must, usePoll } from "@/lib/use-poll";
import { StatusBadge } from "./status-badge";

const PAGES = [
  { href: "/", label: "질문하기" },
  { href: "/verify", label: "검증" },
  { href: "/dataset", label: "데이터셋 만들기" },
];

/** One bar on every page: the three pages, the visitor's name and the shared allowance. There is no login. */
export function AppHeader() {
  const path = usePathname();
  const budget = usePoll("budget", () => must(api.GET("/api/budget"), errorText), 2000);  // read-only
  return (
    <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur">
      <div className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-x-6 px-4 pt-2 sm:px-6 lg:h-14 lg:flex-nowrap lg:pt-0">
        <span className="text-[15px] font-bold tracking-tight">입찰메이트</span>
        <nav aria-label="페이지" className="order-last flex h-11 w-full items-stretch gap-1 lg:order-none lg:h-full lg:w-auto">
          {PAGES.map((p) => {
            const active = p.href === "/" ? path === "/" : path.startsWith(p.href);
            return (
              <Link key={p.href} href={p.href} aria-current={active ? "page" : undefined}
                    className={cn("relative flex items-center rounded-md px-3 text-[15px] font-semibold outline-none",
                      "focus-visible:ring-3 focus-visible:ring-ring/50",
                      active ? "text-foreground after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:bg-foreground"
                        : "text-muted-foreground hover:text-foreground")}>
                {p.label}
              </Link>
            );
          })}
        </nav>
        <div className="ml-auto flex items-center gap-3 sm:gap-5">
          <BudgetMeter data={budget.data} error={budget.error} />
          <MemberField />
        </div>
      </div>
      <BudgetWarnings data={budget.data} />
    </header>
  );
}

function MemberField() {
  const name = useMember();
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <label htmlFor="member">이름</label>
      <input id="member" value={name} maxLength={40} onChange={(e) => writeMember(e.target.value)}
             aria-describedby="member-hint"
             className="h-8 w-24 rounded-md border sm:w-28 border-input bg-background px-2 text-base sm:text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50" />
      <span id="member-hint" className="sr-only">사용·검토 기록에 남는 이름입니다. 로그인은 없습니다.</span>
    </div>
  );
}

function BudgetMeter({ data, error }: { data?: Budget; error?: string }) {
  if (error && !data) return <StatusBadge tone="bad">사용량 최신 아님</StatusBadge>;
  if (!data) return <span className="h-8 w-48" aria-hidden />;
  const s = data.snapshot;
  const pct = Math.min(100, Math.max(0, (s.spent_micro_usd / s.allowance_micro_usd) * 100));
  return (
    <div className="flex items-center gap-3" title={`진행 중 예약 ${usd(s.pending_micro_usd, 4)} · 예약 가능 ${usd(Math.max(0, s.available_micro_usd), 4)}`}>
      <div className="text-right leading-tight">
        <div className="hidden text-[13px] text-muted-foreground sm:block">공유 사용량</div>
        <div className="text-sm font-semibold tabular-nums">
          {usd(s.spent_micro_usd)} <span className="font-normal text-muted-foreground">/ {usd(s.allowance_micro_usd)}</span>
        </div>
      </div>
      <div role="meter" aria-label="공유 사용량" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}
           aria-valuetext={`${pct.toFixed(1)}% 사용`} className="hidden h-1.5 w-24 overflow-hidden rounded-full bg-secondary sm:block">
        <div className={cn("h-full rounded-full", pct >= 90 ? "bg-bad" : pct >= 75 ? "bg-warn" : "bg-primary")}
             style={{ width: `${pct}%` }} />
      </div>
      {error && <StatusBadge tone="bad">최신 아님</StatusBadge>}
    </div>
  );
}

function BudgetWarnings({ data }: { data?: Budget }) {
  if (!data) return null;
  const s = data.snapshot;
  const notes: { text: string; tone: "warn" | "bad" }[] = data.warnings.map((w) => ({
    text: WARNING[w] ?? w, tone: w === "cap_90" || w === "cap_exhausted" ? "bad" : "warn",
  }));
  if (!s.paid_enabled) notes.push({ text: "유료 답변이 꺼져 있습니다. 검색과 원문 열람은 사용할 수 있습니다.", tone: "warn" });
  if (s.frozen_reason) notes.push({ text: "비용 추정 점검이 필요해 유료 호출이 멈춰 있습니다.", tone: "bad" });
  if (!notes.length) return null;
  return (
    <div className="border-t bg-secondary/60">
      <div className="mx-auto flex max-w-[1440px] flex-wrap gap-2 px-6 py-2">
        {notes.map((n) => <StatusBadge key={n.text} tone={n.tone}>{n.text}</StatusBadge>)}
      </div>
    </div>
  );
}
