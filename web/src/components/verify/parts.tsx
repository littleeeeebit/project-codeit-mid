// Small pieces every 검증 section shares: form field styling, labelled fields, empty notes and count tiles.

import { cn } from "cn";

export const field = "w-full rounded-lg border border-input bg-background px-3 text-base sm:text-sm outline-none "
  + "focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-60";

export function Field({ id, label, hint, children }: { id: string; label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="text-[13px] font-semibold">{label}</label>
      {children}
      {hint && <p id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="rounded-xl border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">{children}</p>;
}

export function PageTitle({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <header className="space-y-1">
      <h2 className="text-lg font-bold tracking-tight">{title}</h2>
      {children && <p className="text-sm text-muted-foreground">{children}</p>}
    </header>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warn" | "bad" | "ok"; children: React.ReactNode }) {
  const cls = { info: "bg-info-bg text-info", warn: "bg-warn-bg text-warn", bad: "bg-bad-bg text-bad", ok: "bg-ok-bg text-ok" }[tone];
  return <p role={tone === "bad" ? "alert" : undefined} className={cn("rounded-xl px-4 py-3 text-sm font-medium", cls)}>{children}</p>;
}
