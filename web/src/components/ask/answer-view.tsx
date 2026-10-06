"use client";

// The answer as sentences that each end with numbered citation markers (DESIGN.md, 질문하기 대화). A marker opens
// its quote, neighbouring text and the original in an evidence pane: the conversation's shared pane, or the pane
// AnswerView keeps beside one answer in the history and on 검증.

import { useState } from "react";
import { cn } from "cn";
import type { Answer, RequestView } from "@/lib/api";
import {
  type Cite, citationNumbers, citedInOrder, ConflictTable, docLabel, EvidenceDetail, FactsTable, InventoryTable,
  MissingTable, NextAction, RequestFooter, Sentences, StateHead,
} from "./answer-parts";

export function AnswerBody({ view, answer, cite }: { view: RequestView; answer: Answer; cite: Cite }) {
  return (
    <div className="space-y-6">
      <StateHead answer={answer} cite={cite} />
      <Sentences claims={answer.claims} cite={cite}
                 labelOf={(d) => (answer.mode === "compare" ? docLabel(answer, d) : "")} />
      <FactsTable answer={answer} />
      <InventoryTable answer={answer} onCite={(id) => cite.onCite?.(id)} />
      <ConflictTable answer={answer} cite={cite} />
      <MissingTable answer={answer} />
      <NextAction answer={answer} />
      <RequestFooter view={view} />
    </div>
  );
}

export function AnswerView({ view, answer }: { view: RequestView; answer: Answer }) {
  const order = citedInOrder(answer);
  const [open, setOpen] = useState<string | null>(order[0] ?? null);
  const numbers = citationNumbers(order);
  return (
    <div className={cn("grid gap-8", order.length && "lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]")}>
      <AnswerBody view={view} answer={answer} cite={{ numbers, active: open, onCite: setOpen }} />
      {order.length > 0 && (  // basic information cites nothing: no empty pane
        <aside aria-label="근거" className="lg:sticky lg:top-20 lg:self-start">
          <div className="rounded-2xl border border-input bg-background p-5">
            <EvidenceDetail requestId={answer.request_id} evidenceId={open} number={open ? numbers.get(open) : undefined} />
          </div>
        </aside>
      )}
    </div>
  );
}
