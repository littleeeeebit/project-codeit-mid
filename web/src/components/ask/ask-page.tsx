"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, Download, X } from "lucide-react";
import { cn } from "cn";
import { api, type Doc, errorText, originalHref, type Owned } from "@/lib/api";
import { FIELD, label, REQUEST, REVIEW, REVIEW_WARNING, STATUS, usd, when, wonShort } from "@/lib/format";
import { useAnswerStream } from "@/lib/use-answer-stream";
import { must, usePoll } from "@/lib/use-poll";
import { StatusBadge } from "@/components/status-badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { citationNumbers, citedInOrder, EvidenceDetail, firstCited, Markers, Sentences } from "./answer-parts";
import { AnswerBody, AnswerView } from "./answer-view";
import { DocumentSearch } from "./document-search";

type Mode = "single" | "compare" | "corpus" | "metadata" | "inventory";
type Scope = "selected" | "all";
const ALL: { id: Mode; text: string; paid: boolean }[] = [{ id: "corpus", text: "전체 문서에서 답변", paid: true }];
const ONE: typeof ALL = [
  { id: "single", text: "근거 기반 답변", paid: true },
  { id: "metadata", text: "기본 정보", paid: false },
  { id: "inventory", text: "요구사항 목록", paid: false },
];
const TWO: typeof ONE = [
  { id: "compare", text: "두 문서 비교", paid: true },
  { id: "metadata", text: "기본 정보 비교", paid: false },
];
const SUBMIT: Record<Mode, string> = {
  single: "답변 받기", compare: "비교 답변 받기", corpus: "전체 문서에서 답변 받기", metadata: "기본 정보 보기",
  inventory: "요구사항 목록 보기",
};

/** One turn of the conversation: what was asked, in which mode, of which documents (in selection order). */
type Turn = Owned & { question: string; mode: Mode; docs: string[] };
/** The citation the evidence pane shows: which turn's request, which evidence, and its marker number. */
type Opened = { requestId: string; evidenceId: string; number: number; turn: number };

const UNFINISHED = ["queued", "running"];
const FREE_QUESTION: Partial<Record<Mode, string>> = { metadata: "기본 정보", inventory: "요구사항 목록" };

async function abandon(owned: Owned) {
  const { error, response } = await api.POST("/api/requests/{request_id}/abandon", {
    params: { path: { request_id: owned.request_id } }, keepalive: true,
  });
  if (!response.ok) throw new Error(errorText(error));
}

export function AskPage() {
  const [selected, setSelected] = useState<Doc[]>([]);
  const [scope, setScope] = useState<Scope>("selected");
  const [mode, setMode] = useState<Mode>("single");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [states, setStates] = useState<Record<string, string>>({});  // request_id -> execution status, as polled
  const [opened, setOpened] = useState<Opened | null>(null);
  const [sheet, setSheet] = useState(false);  // below the lg breakpoint the evidence pane is a bottom sheet
  const pane = useRef<HTMLElement>(null);
  const owner = useRef<Owned | null>(null);  // the latest turn: abandoned when the conversation is left
  const pending = useRef<{ valid: boolean } | null>(null);
  const mounted = useRef(true);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const modes = scope === "all" ? ALL : selected.length === 2 ? TWO : ONE;
  const current = modes.find((m) => m.id === mode) ?? modes[0];
  const last = turns.at(-1);
  const lastStatus = last ? states[last.request_id] : undefined;

  // Leaving the conversation (new documents, scope or 새 대화) also invalidates an initial POST that has not
  // returned its ownership record yet. Finished turns stay in 내 최근 요청.
  const release = useCallback(() => {
    if (pending.current) pending.current.valid = false;
    const previous = owner.current;
    owner.current = null;
    setTurns([]);
    setOpened(null);
    setSheet(false);
    if (previous) {
      void abandon(previous).catch((error) => {
        if (mounted.current) setSubmitError(`이전 요청을 중단하지 못했습니다: ${error.message}`);
        else console.error("Could not abandon the queued request", error);
      });
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      release();
    };
  }, [release]);
  const toggle = (d: Doc) => {
    release();
    setScope("selected");
    setSelected((s) => (s.some((x) => x.doc_id === d.doc_id) ? s.filter((x) => x.doc_id !== d.doc_id)
      : s.length < 2 ? [...s, d] : s));
  };
  // Only the latest turn can be unfinished: the input waits for it, and the next turn continues from it.
  const busy = submitting || (!!last && (!lastStatus || UNFINISHED.includes(lastStatus)));
  const historyKey = `${last?.request_id ?? ""}:${busy}`;  // reload the history when the latest turn finishes
  const follow = !!last;
  const onStatus = useCallback((id: string, s: string) => setStates((m) => (m[id] === s ? m : { ...m, [id]: s })), []);
  const cite = useCallback((o: Opened, show: boolean) => {
    setOpened(o);
    if (show && !window.matchMedia("(min-width: 1024px)").matches) setSheet(true);  // wide screens show the pane
  }, []);
  useEffect(() => {  // the narrow sheet takes focus when it opens, and Escape closes it
    if (!sheet || window.matchMedia("(min-width: 1024px)").matches) return;
    pane.current?.focus();
    const close = (e: KeyboardEvent) => e.key === "Escape" && setSheet(false);
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [sheet, opened]);

  const modeGroup = (
    <ToggleGroup type="single" value={current.id} aria-label="질문 방식" variant="outline" spacing={0}
                 className="grid w-full auto-cols-fr grid-flow-col sm:flex sm:w-fit"
                 onValueChange={(v) => { if (v) setMode(v as Mode); }}>
      {modes.map((m) => (
        <ToggleGroupItem key={m.id} value={m.id}
                         className={cn("h-auto flex-wrap px-2 py-1 whitespace-normal sm:flex-nowrap sm:px-3 sm:py-0 sm:whitespace-nowrap",
                           follow ? "min-h-8 text-[13px] sm:h-8" : "min-h-9 text-sm sm:h-9")}>
          {m.text}<span className={cn("ml-1.5 text-xs", m.paid ? "text-warn" : "text-ok")}>{m.paid ? "유료" : "무료"}</span>
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (pending.current || busy) return;
    setSubmitError(null);
    const q = current.paid ? question.trim() : "";
    if (current.paid && !q) return setSubmitError("질문을 입력하세요.");
    const docs = scope === "all" ? [] : selected;
    const attempt = { valid: true };
    pending.current = attempt;
    setSubmitting(true);
    try {
      const { data: request, error } = await api.POST("/api/ask", {
        body: {
          scope: docs.map((d) => ({ doc_id: d.doc_id, source_hash: d.source_hash })),
          question: q, mode: current.id, previous_request_id: last?.request_id ?? "",
        },
      });
      if (error || !request) throw new Error(errorText(error));
      if (mounted.current && attempt.valid) {
        owner.current = request;
        setTurns((t) => [...t, { ...request, question: q, mode: current.id, docs: docs.map((d) => d.doc_id) }]);
        setQuestion("");
      } else {
        await abandon(request);
      }
    } catch (error) {
      if (mounted.current) setSubmitError(error instanceof Error ? error.message : errorText(error));
      else console.error("Could not finish request ownership cleanup", error);
    } finally {
      if (pending.current === attempt) pending.current = null;
      if (mounted.current) setSubmitting(false);
    }
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

        <ToggleGroup type="single" value={scope} aria-label="질문 범위" variant="outline" spacing={0} className="w-fit"
                     onValueChange={(v) => { if (v) { release(); setScope(v as Scope); } }}>
          <ToggleGroupItem value="selected" className="h-9 px-3 text-sm">선택한 문서</ToggleGroupItem>
          <ToggleGroupItem value="all" className="h-9 px-3 text-sm">전체 문서</ToggleGroupItem>
        </ToggleGroup>

        {scope === "selected" && selected.length === 0 ? (
          <div className="rounded-2xl border border-dashed p-10 text-center">
            <p className="text-lg font-semibold">질문할 문서를 고르세요</p>
            <p className="mt-1 text-sm text-muted-foreground">하나를 고르면 질문할 수 있고, 두 개를 고르면 비교할 수 있습니다. 문서를 모르면 &lsquo;전체 문서&rsquo;로 질문하세요.</p>
          </div>
        ) : (
          <>
            {scope === "all" ? (
              <p className="rounded-2xl bg-secondary/60 p-4 text-sm text-muted-foreground">
                색인된 모든 원문에서 근거를 찾습니다. 문서를 고르지 않아도 됩니다. 사업명이나 기관명을 질문에 넣으면 정확한 문서를 찾기 쉽습니다.
              </p>
            ) : (
              <ul className="grid gap-3 md:grid-cols-2">
                {selected.map((d, i) => <SelectedDoc key={d.doc_id} doc={d} index={selected.length > 1 ? i + 1 : 0} onRemove={() => toggle(d)} />)}
              </ul>
            )}
            <div className={cn("grid gap-8", follow && "lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]")}>
              <div className="min-w-0 space-y-8">
                {follow && (
                  <div className="flex items-center justify-between gap-3 border-b pb-3">
                    <h2 className="text-lg font-bold">대화 <span className="ml-1 text-sm font-medium text-muted-foreground">질문 {turns.length}개</span></h2>
                    <button type="button" onClick={release}
                            className="h-8 rounded-lg border px-3 text-sm font-semibold outline-none hover:bg-secondary focus-visible:ring-3 focus-visible:ring-ring/50">
                      새 대화
                    </button>
                  </div>
                )}
                {turns.map((t, i) => (
                  <TurnView key={t.request_id} turn={t} index={i} latest={i === turns.length - 1} opened={opened}
                            onCite={cite} onStatus={onStatus} />
                ))}
                {/* In a conversation the composer sticks to the bottom, so it stays small: question first, then
                    the modes and the button on one row, and the busy hint in the disabled field. */}
                <form onSubmit={submit}
                      className={cn("rounded-2xl border bg-background", follow
                        ? "sticky bottom-0 z-30 space-y-2 p-3 shadow-[0_-6px_20px_rgb(0_0_0/0.06)]" : "space-y-3 p-4 shadow-[0_1px_2px_rgb(0_0_0/0.04)]")}>
                  {!follow && modeGroup}
                  {current.paid && (
                    <div>
                      <label htmlFor="question" className="sr-only">질문</label>
                      <textarea id="question" rows={follow ? 1 : 3} maxLength={2000} value={question} disabled={busy}
                                onChange={(e) => setQuestion(e.target.value)}
                                placeholder={busy ? "답변이 끝나면 이어서 질문할 수 있습니다."
                                  : follow ? "이어서 질문하세요"
                                  : current.id === "compare" ? "예: 두 사업의 하자보수 조건을 비교해 주세요."
                                  : current.id === "corpus" ? "예: ○○기관 ○○ 구축 사업의 하자보수 기간은 얼마인가요?" : "예: 하자보수 기간과 조건은 무엇인가요?"}
                                className={cn("w-full rounded-xl border border-input bg-background p-3 text-[16px] leading-7 outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-60",
                                  follow ? "max-h-40 resize-none [field-sizing:content]" : "resize-y")} />
                    </div>
                  )}
                  <div className="flex flex-wrap items-center gap-3">
                    {follow && modeGroup}
                    <button type="submit" disabled={busy}
                            className={cn("rounded-xl bg-primary px-5 text-[15px] font-semibold text-primary-foreground outline-none hover:bg-primary/90 focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50",
                              follow ? "ml-auto h-10" : "h-11")}>
                      {/* a follow-up pays for rewriting it into a standalone question, then for the answer */}
                      {follow && current.paid ? "이어서 질문 · 유료 2회" : `${SUBMIT[current.id]}${current.paid ? " · 유료 1회" : ""}`}
                    </button>
                    {submitError && <span role="alert" className="text-sm font-medium text-bad">{submitError}</span>}
                  </div>
                </form>
              </div>
              {follow && (
                <aside ref={pane} tabIndex={-1} aria-label="대화 근거"
                       className={cn("fixed inset-x-0 bottom-0 z-40 max-h-[80dvh] overflow-y-auto rounded-t-2xl border-t bg-background p-5 shadow-[0_-8px_30px_rgb(0_0_0/0.15)] outline-none",
                         "lg:sticky lg:inset-auto lg:top-20 lg:z-auto lg:max-h-[calc(100dvh-6rem)] lg:self-start lg:rounded-2xl lg:border lg:shadow-none",
                         !sheet && "hidden lg:block")}>
                  <div className="mb-3 flex min-h-8 items-center justify-between gap-3">
                    <p className="text-[13px] text-muted-foreground">{opened ? `질문 ${opened.turn + 1}의 근거` : "근거"}</p>
                    <button type="button" onClick={() => setSheet(false)} aria-label="근거 닫기"
                            className="rounded-md p-1.5 text-muted-foreground outline-none hover:bg-secondary hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 lg:hidden">
                      <X className="size-5" aria-hidden />
                    </button>
                  </div>
                  {opened ? <EvidenceDetail key={`${opened.requestId}/${opened.evidenceId}`} requestId={opened.requestId}
                                            evidenceId={opened.evidenceId} number={opened.number} />
                    : <p className="text-sm text-muted-foreground">답변 문장 끝의 번호를 누르면 원문 인용과 앞뒤 문단, 원문 파일이 여기에 열립니다.</p>}
                </aside>
              )}
            </div>
          </>
        )}

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
          <p className="text-base font-semibold">{doc.title}</p>
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

/** One turn: the question, then its answer. Each turn polls only its own request with its own generation and
 *  target, so a late result renders only where it was asked; the answer streams in while the request runs. */
function TurnView({ turn, index, latest, opened, onCite, onStatus }: {
  turn: Turn; index: number; latest: boolean; opened: Opened | null;
  onCite: (o: Opened, show: boolean) => void; onStatus: (requestId: string, status: string) => void;
}) {
  const [cancelError, setCancelError] = useState<string | null>(null);
  const status = usePoll(`${turn.request_id}/${turn.generation_id}`, () => must(
    api.GET("/api/requests/{request_id}", {
      params: { path: { request_id: turn.request_id }, query: { generation_id: turn.generation_id, target: turn.target } },
    }), errorText), 1000, (d) => !UNFINISHED.includes(d.view.status));
  const v = status.data?.view;
  const live = !v || UNFINISHED.includes(v.status);
  // A cancellation (or a lost generation) revokes attachment while the provider may still run: the stream and its
  // provisional text stop at once, and the poll keeps following execution and billing until the request finishes.
  const revoked = !!status.data && !status.data.attachable;
  const streamed = useAnswerStream(live && !revoked && !FREE_QUESTION[turn.mode] ? turn : null, status.reload);
  const answer = v && !live && status.data!.attachable ? v.result : null;
  const order = answer ? citedInOrder(answer) : [];
  const numbers = citationNumbers(order);
  const execution = v?.status;
  useEffect(() => {
    if (execution) onStatus(turn.request_id, execution);
  }, [execution, onStatus, turn.request_id]);
  const article = useRef<HTMLElement>(null);
  useEffect(() => {  // a new turn moves its question to the top, so the answer streams in above the composer
    article.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, []);
  const first = latest ? order[0] : undefined;
  useEffect(() => {  // the latest answer opens its first citation in the pane; the narrow sheet stays closed
    if (first) onCite({ requestId: turn.request_id, evidenceId: first, number: 1, turn: index }, false);
  }, [first, index, onCite, turn.request_id]);

  const cancel = async () => {
    setCancelError(null);
    try {
      const { error, response } = await api.POST("/api/requests/{request_id}/cancel", {
        params: { path: { request_id: turn.request_id } },
      });
      if (!response.ok) throw new Error(errorText(error));
      status.reload();
    } catch (error) {
      setCancelError(error instanceof Error ? error.message : errorText(error));
    }
  };

  return (
    <article ref={article} aria-label={`질문 ${index + 1}`} className="scroll-mt-20 space-y-4">
      <div className="flex justify-end">
        <div className="max-w-[85%] space-y-1 rounded-2xl rounded-br-md bg-secondary px-4 py-3">
          <p className="text-base [overflow-wrap:anywhere]">{turn.question || FREE_QUESTION[turn.mode]}</p>
          {answer?.standalone_question && (
            <p className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]">검색에 쓴 질문 · {answer.standalone_question}</p>
          )}
        </div>
      </div>
      <section aria-label={latest ? "답변" : `질문 ${index + 1}의 답변`} aria-busy={live}>
        {status.error && !status.data ? <p role="alert" className="text-sm text-bad">{status.error}</p>
          : live ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-3" aria-live="polite">
                <StatusBadge tone="neutral" size="md">
                  {revoked && v?.cancel_requested ? "취소 요청됨" : streamed ? "작성 중 · 검증 전" : v ? REQUEST[v.status] : "요청 중"}
                </StatusBadge>
                <span className="text-sm text-muted-foreground">
                  {revoked ? "이 요청의 결과는 답변으로 표시하지 않습니다. 진행 중인 호출이 끝나면 비용을 정산합니다."
                    : v?.reserved_micro_usd ? `이 요청의 예약 최대 비용 ${usd(v.reserved_micro_usd, 4)}` : "근거를 찾고 있습니다."}
                </span>
                {v && !revoked && (
                  <button type="button" onClick={cancel}
                          className="ml-auto h-8 rounded-lg border px-3 text-sm font-semibold outline-none hover:bg-secondary focus-visible:ring-3 focus-visible:ring-ring/50">
                    요청 취소
                  </button>
                )}
              </div>
              {cancelError && <p role="alert" className="text-sm text-bad">{cancelError}</p>}
              {revoked ? null : streamed ? <Provisional streamed={streamed} docs={turn.docs} />
                : <div className="space-y-2"><Skeleton className="h-5 w-3/4" /><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-5/6" /></div>}
            </div>
          ) : !status.data!.attachable ? (
            <p className="text-sm text-muted-foreground">{label(REQUEST, v!.status)} · 이 요청의 결과는 답변으로 표시하지 않습니다. 아래 &lsquo;내 최근 요청&rsquo;에서 볼 수 있습니다.</p>
          ) : answer ? (
            <AnswerBody view={v!} answer={answer} cite={{
              numbers, active: opened?.requestId === turn.request_id ? opened.evidenceId : null,
              onCite: (id) => onCite({ requestId: turn.request_id, evidenceId: id, number: numbers.get(id)!, turn: index }, true),
            }} />
          ) : <p className="text-sm text-muted-foreground">{label(REQUEST, v!.status)}: 결과가 없습니다.</p>}
      </section>
    </article>
  );
}

/** The answer as the model writes it, before validation: marked provisional, its markers not yet openable. The
 *  validated answer or its failure state replaces it when the request finishes. */
function Provisional({ streamed, docs }: { streamed: NonNullable<ReturnType<typeof useAnswerStream>>; docs: string[] }) {
  const cite = {
    numbers: citationNumbers(firstCited([streamed.summary_evidence_ids, ...streamed.claims.map((c) => c.evidence_ids)])),
  };
  return (
    <div className="space-y-4 border-l-2 border-dashed border-input pl-4">
      <p className="text-[13px] text-muted-foreground">검증 전 임시 답변입니다. 근거 검증이 끝나면 확정된 답변이나 실패 상태로 바뀝니다.</p>
      {streamed.summary && (
        <p className="text-lg leading-relaxed font-semibold [overflow-wrap:anywhere]">
          {streamed.summary}<Markers ids={streamed.summary_evidence_ids} cite={cite} />
        </p>
      )}
      <Sentences claims={streamed.claims} cite={cite}
                 labelOf={(d) => (docs.length > 1 && docs.includes(d) ? `문서 ${docs.indexOf(d) + 1}` : "")} />
    </div>
  );
}

function History({ refresh }: { refresh: string }) {
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState<string | null>(null);
  const { data } = usePoll(open ? `history-${refresh}` : null, () => must(api.GET("/api/requests", {
    params: { query: { limit: 20 } },
  }), errorText), null);
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="border-t pt-6">
      <CollapsibleTrigger className="group flex min-h-9 items-center gap-2 rounded text-lg font-bold outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
        내 최근 요청 <ChevronDown className="size-4 transition-transform group-data-[state=open]:rotate-180" aria-hidden />
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-4 pt-4">
        {!data && <Skeleton className="h-10 w-full" />}
        {data?.length === 0 && <p className="text-sm text-muted-foreground">아직 요청이 없습니다.</p>}
        {data?.map((r) => {
          const s = r.result ? STATUS[r.result.status] : null;
          const isOpen = shown === r.request_id;
          return (
            <div key={r.request_id} className="rounded-xl border">
              <button type="button" aria-expanded={isOpen} onClick={() => setShown(isOpen ? null : r.request_id)}
                      className="flex w-full flex-wrap items-center gap-3 rounded-xl px-4 py-4 text-left outline-none hover:bg-secondary/50 focus-visible:ring-3 focus-visible:ring-ring/50">
                <StatusBadge tone={s?.tone ?? "neutral"}>{s?.label ?? REQUEST[r.status]}</StatusBadge>
                <span className="min-w-0 flex-1 text-base font-semibold [overflow-wrap:anywhere]">{r.question || ({ metadata: "기본 정보", inventory: "요구사항 목록" } as Record<string, string>)[r.mode] || r.mode}</span>
                <span className="w-full text-[13px] text-muted-foreground tabular-nums sm:w-auto">요청 {r.request_id.slice(0, 8)} · {r.created_at.slice(0, 16).replace("T", " ")}</span>
              </button>
              {isOpen && r.result && <div className="border-t p-5"><AnswerView view={r} answer={r.result} /></div>}
            </div>
          );
        })}
      </CollapsibleContent>
    </Collapsible>
  );
}
