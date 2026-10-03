"use client";

// The answer layout the user picked on 2026-10-02 (DESIGN.md, 질문하기): numbered claims with citation chips on
// the left, the opened quote in a sticky pane on the right.

import { useState } from "react";
import { cn } from "cn";
import type { Answer, RequestView } from "@/lib/api";
import {
  Chips, citedInOrder, ConflictTable, docLabel, EvidenceDetail, FactsTable, InventoryTable, MissingTable, NextAction,
  RequestFooter, StateHead,
} from "./answer-parts";
import { StatusBadge } from "@/components/status-badge";

export function AnswerView({ view, answer }: { view: RequestView; answer: Answer }) {
  const [open, setOpen] = useState<string | null>(citedInOrder(answer)[0] ?? null);
  const hasEvidence = Object.keys(answer.evidence).length > 0;  // basic information cites nothing: no empty pane
  return (
    <div className={cn("grid gap-8", hasEvidence && "lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]")}>
      <div className="space-y-6">
        <StateHead answer={answer} />
        {answer.claims.length > 0 && (
          <ol className="space-y-3">
            {answer.claims.map((c, i) => (
              <li key={i} className="flex gap-3">
                <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full bg-secondary text-xs font-bold tabular-nums">{i + 1}</span>
                <div className="space-y-1.5">
                  <p className="text-base">{c.text}</p>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {c.kind === "inference" ? <StatusBadge tone="warn">추론</StatusBadge> : <StatusBadge tone="neutral">원문 사실</StatusBadge>}
                    {docLabel(answer, c.doc_id) && <StatusBadge tone="neutral">{docLabel(answer, c.doc_id)}</StatusBadge>}
                    <Chips answer={answer} ids={c.evidence_ids} onCite={setOpen} active={open} />
                  </div>
                </div>
              </li>
            ))}
          </ol>
        )}
        <FactsTable answer={answer} />
        <InventoryTable answer={answer} onCite={setOpen} />
        <ConflictTable answer={answer} onCite={setOpen} />
        <MissingTable answer={answer} />
        <NextAction answer={answer} />
        <RequestFooter view={view} />
      </div>
      {hasEvidence && (
        <aside aria-label="근거" className="lg:sticky lg:top-20 lg:self-start">
          <div className="rounded-2xl bg-secondary/60 p-5">
            {open ? <EvidenceDetail requestId={answer.request_id} evidenceId={open} />
              : <p className="text-sm text-muted-foreground">근거 번호를 누르면 원문 인용과 주변 내용이 여기에 열립니다.</p>}
          </div>
        </aside>
      )}
    </div>
  );
}
