"use client";

// The Linear-style grouped menu with counts the user picked for 검증 (DESIGN.md), shared by 데이터셋 만들기: one area
// at a time on the right, each menu entry with the number that tells whether it needs a look.

import { cn } from "cn";

export type MenuGroup<K extends string> = { title: string; items: { k: K; count?: React.ReactNode; urgent?: boolean }[] };

export function SideMenu<K extends string>({ title, groups, titles, section, onSection, children }: {
  title: string; groups: MenuGroup<K>[]; titles: Record<K, string>; section: K; onSection: (k: K) => void;
  children: React.ReactNode;
}) {
  return (
    <div className="grid gap-8 lg:grid-cols-[220px_minmax(0,1fr)]">
      <nav aria-label={`${title} 영역`} className="space-y-5 lg:sticky lg:top-20 lg:self-start">
        <h1 className="px-2 text-2xl font-bold tracking-tight">{title}</h1>
        {groups.map((g) => (
          <div key={g.title} className="space-y-1">
            <p className="px-3 pb-2 text-sm font-semibold">{g.title}</p>
            {g.items.map(({ k, count, urgent }) => (
              <button key={k} type="button" aria-current={section === k ? "page" : undefined} onClick={() => onSection(k)}
                      className={cn("flex min-h-11 w-full items-center justify-between gap-2 rounded-lg border-l-4 px-3 py-2 text-left text-sm font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
                        section === k ? "border-primary bg-accent font-semibold text-primary" : "border-transparent text-foreground hover:bg-secondary")}>
                {titles[k]}
                {count !== undefined && <span className={cn("text-xs tabular-nums", urgent ? "font-bold text-warn" : "text-muted-foreground")}>{count}</span>}
              </button>
            ))}
          </div>
        ))}
      </nav>
      <main className="min-w-0 space-y-6">{children}</main>
    </div>
  );
}
