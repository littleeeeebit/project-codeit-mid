import { cn } from "cn";
import type { Tone } from "@/lib/format";

const TONE: Record<Tone, string> = {
  ok: "bg-ok-bg text-ok",
  info: "bg-info-bg text-info",
  warn: "bg-warn-bg text-warn",
  bad: "bg-bad-bg text-bad",
  neutral: "bg-neutral-bg text-neutral",
};

const DOT: Record<Tone, string> = {
  ok: "bg-ok", info: "bg-info", warn: "bg-warn", bad: "bg-bad", neutral: "bg-neutral",
};

/** A state spelled out in words; the colour only repeats what the label says. */
export function StatusBadge({ tone, children, className, size = "sm" }: {
  tone: Tone; children: React.ReactNode; className?: string; size?: "sm" | "md";
}) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full font-semibold whitespace-nowrap",
      size === "md" ? "px-3 py-1 text-sm" : "px-2 py-0.5 text-xs", TONE[tone], className)}>
      <span aria-hidden className={cn("size-1.5 rounded-full", DOT[tone])} />
      {children}
    </span>
  );
}
