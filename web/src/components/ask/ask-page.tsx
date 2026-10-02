"use client";

import { useState } from "react";
import { ChevronDown, Download, X } from "lucide-react";
import { cn } from "cn";
import { api, type Doc, errorText, originalHref, type Owned, type RequestView } from "@/lib/api";
import { FIELD, label, REQUEST, REVIEW, REVIEW_WARNING, STATUS, usd, when, wonShort } from "@/lib/format";
import { readMember } from "@/lib/member";
import { must, usePoll } from "@/lib/use-poll";
import { StatusBadge } from "@/components/status-badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { AnswerView } from "./answer-view";
import { DocumentSearch } from "./document-search";

type Mode = "single" | "compare" | "metadata" | "inventory";
const ONE: { id: Mode; text: string; paid: boolean }[] = [
  { id: "single", text: "근거 기반 답변", paid: true },
  { id: "metadata", text: "기본 정보", paid: false },
  { id: "inventory", text: "요구사항 목록", paid: false },
];
const TWO: typeof ONE = [
  { id: "compare", text: "두 문서 비교", paid: true },
  { id: "metadata", text: "기본 정보 비교", paid: false },
];
const SUBMIT: Record<Mode, string> = {
  single: "답변 받기", compare: "비교 답변 받기", metadata: "기본 정보 보기", inventory: "요구사항 목록 보기",
};

/** The one request this screen owns, with the name it was asked under (taken once, at submit). */
type Ownership = Owned & { member: string };

export function AskPage() {
  const [selected, setSelected] = useState<Doc[]>([]);
  const [mode, setMode] = useState<Mode>("single");
  const [question, setQuestion] = useState("");
  const [owned, setOwned] = useState<Ownership | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const modes = selected.length === 2 ? TWO : ONE;
  const current = modes.find((m) => m.id === mode) ?? modes[0];

  // A changed selection or mode means the screen no longer asks what the owned request asked: let it go.
  const release = () => {
    if (owned) {
      void api.POST("/api/requests/{request_id}/abandon", { params: { path: { request_id: owned.request_id } } });
      setOwned(null);
    }
  };
  const toggle = (d: Doc) => {
    release();
    setSelected((s) => (s.some((x) => x.doc_id === d.doc_id) ? s.filter((x) => x.doc_id !== d.doc_id)
      : s.length < 2 ? [...s, d] : s));
  };
  const status = usePoll(owned ? `${owned.request_id}/${owned.generation_id}` : null, () => must(
    api.GET("/api/requests/{request_id}", {
      params: { path: { request_id: owned!.request_id }, query: { generation_id: owned!.generation_id, target: owned!.target } },
    }), errorText), 1000, (d) => !["queued", "running"].includes(d.view.status));
  const busy = !!owned && (!status.data || ["queued", "running"].includes(status.data.view.status));
  const historyKey = `${owned?.request_id ?? ""}:${busy}`;  // reload the history when the owned request finishes

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitError(null);
    const q = current.paid ? question.trim() : "";
    if (current.paid && !q) return setSubmitError("질문을 입력하세요.");
    const member = readMember();
    const { data, error } = await api.POST("/api/ask", {
      body: { scope: selected.map((d) => ({ doc_id: d.doc_id, source_hash: d.source_hash })), question: q, mode: current.id },
    });
    if (error || !data) return setSubmitError(errorText(error));
    release();
    setOwned({ ...data, member });
  };

  return (
    <div className="mx-auto grid w-full max-w-[1440px] flex-1 lg:grid-cols-[360px_minmax(0,1fr)]">
      <aside className="border-r lg:sticky lg:top-14 lg:h-[calc(100dvh-3.5rem)]">
        <DocumentSearch selected={selected} onToggle={toggle} />
      </aside>
      <main className="min-w-0 space-y-8 px-6 py-8 lg:px-10">
        <header className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight">질문하기</h1>
          <p className="text-sm text-muted-foreground">제공된 과거 공고(2021-10 ~ 2025-02) 기준입니다. 현재 입찰 가능 여부는 이 자료로 판단할 수 없습니다.</p>
        </header>

        {selected.length === 0 ? (
          <div className="rounded-2xl border border-dashed p-10 text-center">
            <p className="text-[17px] font-semibold">왼쪽에서 문서를 고르세요</p>
            <p className="mt-1 text-sm text-muted-foreground">하나를 고르면 질문할 수 있고, 두 개를 고르면 비교할 수 있습니다.</p>
          </div>
        ) : (
          <>
            <ul className="grid gap-3 md:grid-cols-2">
              {selected.map((d, i) => <SelectedDoc key={d.doc_id} doc={d} index={selected.length > 1 ? i + 1 : 0} onRemove={() => toggle(d)} />)}
            </ul>
            <form onSubmit={submit} className="space-y-4 rounded-2xl border p-5 shadow-[0_1px_2px_rgb(0_0_0/0.04)]">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <ToggleGroup type="single" value={current.id} aria-label="질문 방식" variant="outline" spacing={0}
                             className="grid w-full auto-cols-fr grid-flow-col sm:flex sm:w-fit"
                             onValueChange={(v) => { if (v) { release(); setMode(v as Mode); } }}>
                  {modes.map((m) => (
                    <ToggleGroupItem key={m.id} value={m.id} className="h-auto min-h-9 flex-wrap px-2 py-1 text-sm whitespace-normal sm:h-9 sm:flex-nowrap sm:px-3 sm:py-0 sm:whitespace-nowrap">
                      {m.text}<span className={cn("ml-1.5 text-xs", m.paid ? "text-warn" : "text-ok")}>{m.paid ? "유료" : "무료"}</span>
                    </ToggleGroupItem>
                  ))}
                </ToggleGroup>
              </div>
              {current.paid && (
                <div>
                  <label htmlFor="question" className="sr-only">질문</label>
                  <textarea id="question" rows={3} maxLength={2000} value={question} disabled={busy}
                            onChange={(e) => setQuestion(e.target.value)}
                            placeholder={current.id === "compare" ? "예: 두 사업의 하자보수 조건을 비교해 주세요." : "예: 하자보수 기간과 조건은 무엇인가요?"}
                            className="w-full resize-y rounded-xl border border-input bg-background p-3 text-[16px] leading-7 outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-60" />
                </div>
              )}
              <div className="flex flex-wrap items-center gap-3">
                <button type="submit" disabled={busy}
                        className="h-11 rounded-xl bg-primary px-5 text-[15px] font-semibold text-primary-foreground outline-none hover:bg-primary/90 focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50">
                  {SUBMIT[current.id]}{current.paid ? " · 유료 1회" : ""}
                </button>
                {busy && <span className="text-sm text-muted-foreground">이전 요청이 끝나면 다시 질문할 수 있습니다.</span>}
                {submitError && <span role="alert" className="text-sm font-medium text-bad">{submitError}</span>}
              </div>
            </form>
          </>
        )}

        {owned && <OwnedAnswer owned={owned} status={status} />}
        <History refresh={historyKey} />
      </main>
    </div>
  );
}

function SelectedDoc({ doc, index, onRemove }: { doc: Doc; index: number; onRemove: () => void }) {
  const conflicts = [...new Set(doc.conflicts.map((c) => String(c.field)))].filter((f) => !(f in doc.resolutions));
  return (
    <li className="space-y-3 rounded-2xl bg-secondary/60 p-4">
      <div className="flex items-start gap-3">
        {index > 0 && <span className="rounded-md bg-foreground px-1.5 py-0.5 text-xs font-bold text-background">문서 {index}</span>}
        <div className="min-w-0 flex-1">
          <p className="line-clamp-2 font-semibold leading-snug">{doc.title}</p>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {doc.institution || "기관 미상"} · {wonShort(doc.amount_krw)} · 공개 {when(doc.published_at)} · {doc.format.toUpperCase()} · {label(REVIEW, doc.review_status)}
          </p>
        </div>
        <button type="button" onClick={onRemove} aria-label={`${doc.title} 선택 해제`}
                className="rounded-md p-1 text-muted-foreground outline-none hover:bg-background hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
          <X className="size-4" aria-hidden />
        </button>
      </div>
      {conflicts.length > 0 && <p className="text-[13px] text-warn">같은 원문에 연결된 다른 공고와 값이 다릅니다: {conflicts.map((f) => label(FIELD, f)).join(", ")}</p>}
      {doc.indexed && REVIEW_WARNING[doc.review_status] && <p className="text-[13px] text-warn">{REVIEW_WARNING[doc.review_status]}</p>}
      {!doc.indexed && <p className="text-[13px] font-medium text-bad">{doc.unavailable_reason || "이 문서는 아직 질문용 색인에 포함되지 않았습니다."}</p>}
      <a href={originalHref(doc.doc_id, doc.source_hash)}
         className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-background px-2.5 text-[13px] font-semibold outline-none hover:bg-background/70 focus-visible:ring-3 focus-visible:ring-ring/50">
        <Download className="size-3.5" aria-hidden />원문 파일 받기
      </a>
    </li>
  );
}

function OwnedAnswer({ owned, status }: { owned: Ownership; status: ReturnType<typeof usePoll<{ view: RequestView; attachable: boolean }>> }) {
  if (status.error && !status.data) return <p role="alert" className="text-sm text-bad">{status.error}</p>;
  const v = status.data?.view;
  if (!v || ["queued", "running"].includes(v.status)) {
    return (
      <section aria-live="polite" aria-busy="true" className="space-y-4 rounded-2xl border p-6">
        <div className="flex items-center gap-3">
          <StatusBadge tone="neutral" size="md">{v ? REQUEST[v.status] : "요청 중"}</StatusBadge>
          <span className="text-sm text-muted-foreground">
            {v?.reserved_micro_usd ? `이 요청의 예약 최대 비용 ${usd(v.reserved_micro_usd, 4)}` : "근거를 찾고 답변을 검증하는 중입니다."}
          </span>
          {v && (
            <button type="button" className="ml-auto h-8 rounded-lg border px-3 text-sm font-semibold outline-none hover:bg-secondary focus-visible:ring-3 focus-visible:ring-ring/50"
                    onClick={() => api.POST("/api/requests/{request_id}/cancel", { params: { path: { request_id: owned.request_id } } })}>
              요청 취소
            </button>
          )}
        </div>
        <div className="space-y-2"><Skeleton className="h-5 w-3/4" /><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-5/6" /></div>
      </section>
    );
  }
  if (!status.data!.attachable) {
    return <p className="text-sm text-muted-foreground">이 요청은 지금 선택과 다른 질문이어서 표시하지 않습니다. 아래 &lsquo;내 최근 요청&rsquo;에서 볼 수 있습니다.</p>;
  }
  return (
    <section aria-label="답변" className="space-y-4">
      {v.question && <p className="rounded-xl bg-secondary/60 px-4 py-3 text-[15px]"><span className="mr-2 font-semibold text-muted-foreground">질문</span>{v.question}</p>}
      {v.result ? <AnswerView view={v} answer={v.result} />
        : <p className="text-sm text-muted-foreground">{REQUEST[v.status]}: 결과가 없습니다.</p>}
    </section>
  );
}

function History({ refresh }: { refresh: string }) {
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState<string | null>(null);
  const { data } = usePoll(open ? `history-${refresh}` : null, () => must(api.GET("/api/requests", { params: { query: { limit: 20 } } }), errorText), null);
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="border-t pt-6">
      <CollapsibleTrigger className="group flex items-center gap-1.5 rounded text-[15px] font-bold outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
        내 최근 요청 <ChevronDown className="size-4 transition-transform group-data-[state=open]:rotate-180" aria-hidden />
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-2 pt-4">
        {!data && <Skeleton className="h-10 w-full" />}
        {data?.length === 0 && <p className="text-sm text-muted-foreground">아직 요청이 없습니다.</p>}
        {data?.map((r) => {
          const s = r.result ? STATUS[r.result.status] : null;
          const isOpen = shown === r.request_id;
          return (
            <div key={r.request_id} className="rounded-xl border">
              <button type="button" aria-expanded={isOpen} onClick={() => setShown(isOpen ? null : r.request_id)}
                      className="flex w-full items-center gap-3 rounded-xl px-4 py-3 text-left outline-none hover:bg-secondary/50 focus-visible:ring-3 focus-visible:ring-ring/50">
                <StatusBadge tone={s?.tone ?? "neutral"}>{s?.label ?? REQUEST[r.status]}</StatusBadge>
                <span className="min-w-0 flex-1 truncate text-sm">{r.question || ({ metadata: "기본 정보", inventory: "요구사항 목록" } as Record<string, string>)[r.mode] || r.mode}</span>
                <span className="text-[13px] text-muted-foreground tabular-nums">요청 {r.request_id.slice(0, 8)} · {r.created_at.slice(0, 16).replace("T", " ")}</span>
              </button>
              {isOpen && r.result && <div className="border-t p-5"><AnswerView view={r} answer={r.result} /></div>}
            </div>
          );
        })}
      </CollapsibleContent>
    </Collapsible>
  );
}
