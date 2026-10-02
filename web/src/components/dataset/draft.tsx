"use client";

// 초안 만들기: pick development documents and the source passages a question must stand on, describe each question
// slot, then price the gpt-6-luna drafting for free and run it only with consent. Drafting never approves anything;
// valid drafts go to the review queue with one button.

import { useMemo, useState } from "react";
import { Search, X } from "lucide-react";
import { cn } from "cn";
import { api, errorText, type Schemas } from "@/lib/api";
import { DRAFT_STATUS, DRAFT_TYPES, GOLD_TYPE, label, locationShort, locationText, stamp, usd } from "@/lib/format";
import { must, usePoll } from "@/lib/use-poll";
import { Section, Table, td } from "@/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Empty, Field, field, Notice } from "@/components/verify/parts";

type Doc = Schemas["DraftDocument"];
type Slot = Schemas["Slot"];
type Estimate = Schemas["DraftEstimate"];
const ROWS = 200;

const docName = (d: Doc) => d.title || d.filename || d.doc_id.slice(0, 8);

export function DraftBuilder({ onStarted }: { onStarted: () => void }) {
  const docs = usePoll("draft-docs", () => must(api.GET("/api/drafting/documents"), errorText), null);
  const [picked, setPicked] = useState<Doc[]>([]);
  const [sources, setSources] = useState<{ doc_id: string; element_id: string }[]>([]);
  const [slots, setSlots] = useState<Slot[]>([]);
  const toggleDoc = (d: Doc) => {
    const on = picked.some((x) => x.doc_id === d.doc_id);
    setPicked(on ? picked.filter((x) => x.doc_id !== d.doc_id) : [...picked, d]);
    if (on) setSources(sources.filter((s) => s.doc_id !== d.doc_id));
  };

  if (docs.error) return <Notice tone="bad">{docs.error}</Notice>;
  if (!docs.data) return <Skeleton className="h-64 w-full" />;
  if (!docs.data.length) return <Empty>초안에 쓸 개발용 문서가 없습니다. 문서 계열 배정(assign-families) 뒤에 다시 여세요.</Empty>;
  return (
    <div className="space-y-10">
      <Step n={1} title="문서와 원문 구절" note="개발(dev) 문서만 나옵니다. 봉인 평가 문서는 초안에 쓸 수 없습니다.">
        <DocPicker docs={docs.data} picked={picked} onToggle={toggleDoc} />
        {picked.map((d) => (
          <PassagePicker key={d.doc_id} doc={d} label={picked.length > 1 ? `문서 ${picked.indexOf(d) + 1}` : ""}
                         chosen={sources.filter((s) => s.doc_id === d.doc_id).map((s) => s.element_id)}
                         onToggle={(el) => setSources(sources.some((s) => s.element_id === el && s.doc_id === d.doc_id)
                           ? sources.filter((s) => !(s.element_id === el && s.doc_id === d.doc_id))
                           : [...sources, { doc_id: d.doc_id, element_id: el }])} />
        ))}
      </Step>
      <Step n={2} title="질문 유형과 의도" note={`고른 구절 ${sources.length}개로 질문 자리를 만듭니다. 자리마다 초안이 하나 나옵니다.`}>
        <SlotForm docs={picked} sources={sources} onAdd={(s) => setSlots([...slots, s])} />
        {slots.length > 0 && (
          <Table caption="질문 자리" head={["유형", "의도", "문서", "구절", ""]}>
            {slots.map((s) => (
              <tr key={s.question_id}>
                <td className={cn(td, "whitespace-nowrap")}>{label(GOLD_TYPE, s.question_type)}</td>
                <td className={td}>{s.intent}</td>
                <td className={td}>{s.doc_ids.map((id) => docName(docs.data!.find((d) => d.doc_id === id) ?? { doc_id: id } as Doc)).join(" / ")}</td>
                <td className={cn(td, "tabular-nums")}>{s.sources.length}</td>
                <td className={td}>
                  <Button variant="ghost" size="sm" onClick={() => setSlots(slots.filter((x) => x !== s))} aria-label={`${s.intent} 자리 빼기`}>빼기</Button>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Step>
      <Step n={3} title="최대 비용과 생성">
        {slots.length ? <Generate key={slots.map((s) => s.question_id).join()} slots={slots} onStarted={() => { setSlots([]); onStarted(); }} />
          : <p className="text-sm text-muted-foreground">질문 자리를 하나 이상 추가하면 비용을 추정할 수 있습니다.</p>}
      </Step>
    </div>
  );
}

function Step({ n, title, note, children }: { n: number; title: string; note?: string; children: React.ReactNode }) {
  return (
    <section className="space-y-4">
      <div className="flex items-start gap-3">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-foreground text-sm font-bold text-background">{n}</span>
        <div>
          <h3 className="text-[17px] font-bold">{title}</h3>
          {note && <p className="text-sm text-muted-foreground">{note}</p>}
        </div>
      </div>
      <div className="space-y-4 pl-10">{children}</div>
    </section>
  );
}

function DocPicker({ docs, picked, onToggle }: { docs: Doc[]; picked: Doc[]; onToggle: (d: Doc) => void }) {
  const [q, setQ] = useState("");
  const matches = useMemo(() => q.trim()
    ? docs.filter((d) => `${docName(d)} ${d.institution ?? ""}`.includes(q.trim()) && !picked.includes(d)).slice(0, 6) : [], [docs, q, picked]);
  return (
    <div className="space-y-2">
      <Field id="draft-doc" label={`문서 (최대 2개, 두 개면 비교 질문) · 개발용 ${docs.length}개`} hint="사업명이나 기관명 일부를 입력하세요.">
        <input id="draft-doc" value={q} onChange={(e) => setQ(e.target.value)} disabled={picked.length >= 2} autoComplete="off"
               aria-describedby="draft-doc-hint" className={cn(field, "h-10")} />
      </Field>
      {matches.length > 0 && (
        <ul className="divide-y rounded-lg border">
          {matches.map((d) => (
            <li key={d.doc_id}>
              <button type="button" onClick={() => { onToggle(d); setQ(""); }}
                      className="w-full px-3 py-2 text-left text-sm outline-none hover:bg-secondary/70 focus-visible:bg-accent">
                <span className="line-clamp-1 font-medium">{docName(d)}</span>
                <span className="text-xs text-muted-foreground">{d.institution}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {picked.map((d, i) => (
        <p key={d.doc_id} className="flex items-center gap-2 rounded-lg bg-secondary/70 px-3 py-2 text-sm">
          {picked.length > 1 && <span className="rounded bg-foreground px-1.5 text-xs font-bold text-background">문서 {i + 1}</span>}
          <span className="line-clamp-1 flex-1 font-medium">{docName(d)}</span>
          <button type="button" aria-label={`${docName(d)} 빼기`} onClick={() => onToggle(d)}
                  className="rounded p-0.5 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
            <X className="size-4" aria-hidden />
          </button>
        </p>
      ))}
    </div>
  );
}

function PassagePicker({ doc, label: tag, chosen, onToggle }: { doc: Doc; label: string; chosen: string[]; onToggle: (el: string) => void }) {
  const [typed, setTyped] = useState("");
  const [phrase, setPhrase] = useState("");
  const els = usePoll(`els-${doc.doc_id}-${phrase}`, () => must(api.GET("/api/drafting/documents/{doc_id}/elements", {
    params: { path: { doc_id: doc.doc_id }, query: { contains: phrase } } }), errorText), null);
  const id = `find-${doc.doc_id}`;
  return (
    <div className="space-y-3 rounded-2xl border p-4">
      <p className="text-sm font-semibold">{tag && <span className="mr-2 rounded bg-foreground px-1.5 text-xs font-bold text-background">{tag}</span>}{docName(doc)}</p>
      <form onSubmit={(e) => { e.preventDefault(); setPhrase(typed); }} className="flex items-end gap-2">
        <div className="flex-1"><Field id={id} label="구절 찾기">
          <input id={id} value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="예: 계약 기간, 하자보수" className={cn(field, "h-10")} />
        </Field></div>
        <Button type="submit" variant="outline" size="lg" className="h-10"><Search aria-hidden />찾기</Button>
      </form>
      {els.error && <Notice tone="bad">{els.error}</Notice>}
      {!els.data ? <Skeleton className="h-40 w-full" /> : (
        <>
          <p className="text-xs text-muted-foreground">
            원문 요소 {els.data.length}개{els.data.length > ROWS ? ` 중 앞 ${ROWS}개` : ""} · 고름 {chosen.length}개. 질문의 근거가 될 구절을 고르세요.
          </p>
          <ul className="max-h-96 divide-y overflow-y-auto rounded-lg border">
            {els.data.slice(0, ROWS).map((e) => {
              const on = chosen.includes(e.element_id);
              return (
                <li key={e.element_id}>
                  <label className={cn("flex cursor-pointer gap-3 px-3 py-2 text-sm hover:bg-secondary/60", on && "bg-accent hover:bg-accent")}>
                    <input type="checkbox" checked={on} onChange={() => onToggle(e.element_id)} className="mt-1 size-4 shrink-0 accent-primary" />
                    <span className="w-20 shrink-0 text-xs text-muted-foreground" title={locationText(e.location)}>{locationShort(e.location)}</span>
                    <span className="line-clamp-3 flex-1 leading-6">{e.text}</span>
                  </label>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </div>
  );
}

function SlotForm({ docs, sources, onAdd }: { docs: Doc[]; sources: { doc_id: string; element_id: string }[]; onAdd: (s: Slot) => void }) {
  const [type, setType] = useState<string>(DRAFT_TYPES[0]);
  const [intent, setIntent] = useState("");
  const [error, setError] = useState<string | null>(null);
  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    const { data, error: err } = await api.POST("/api/drafting/slots", { body: {
      question_type: type, intent, doc_ids: docs.map((d) => d.doc_id), sources } });
    if (err || !data) return setError(errorText(err));
    setError(null);
    setIntent("");
    onAdd(data);
  };
  return (
    <form onSubmit={add} className="grid items-end gap-3 md:grid-cols-[14rem_1fr_auto]">
      <Field id="slot-type" label="질문 유형">
        <select id="slot-type" value={type} onChange={(e) => setType(e.target.value)} className={cn(field, "h-10")}>
          {DRAFT_TYPES.map((t) => <option key={t} value={t}>{label(GOLD_TYPE, t)}</option>)}
        </select>
      </Field>
      <Field id="slot-intent" label="의도 (무엇을 묻는 질문인지)">
        <input id="slot-intent" value={intent} onChange={(e) => setIntent(e.target.value)} placeholder="예: 하자보수 기간과 시작 시점" className={cn(field, "h-10")} />
      </Field>
      <Button type="submit" size="lg" variant="secondary" className="h-10">질문 자리 추가</Button>
      {error && <p role="alert" className="text-sm font-medium text-bad md:col-span-3">{error}</p>}
    </form>
  );
}

function Generate({ slots, onStarted }: { slots: Slot[]; onStarted: () => void }) {
  const [est, setEst] = useState<Estimate | null>(null);
  const [agree, setAgree] = useState(false);
  const [state, setState] = useState<{ busy?: boolean; error?: string }>({});
  const plan = async () => {
    setState({ busy: true });
    const { data, error } = await api.POST("/api/drafting/plan", { body: { slots } });
    setState(error ? { error: errorText(error) } : {});
    setEst(data ?? null);
  };
  const start = async () => {
    setState({ busy: true });
    const { error } = await api.POST("/api/drafting/start", { body: { slots, consented_max_micro_usd: est!.max_micro_usd } });
    if (error) return setState({ error: errorText(error) });
    onStarted();
  };
  return (
    <div className="space-y-3">
      <Button variant="outline" size="lg" onClick={plan} disabled={state.busy}>최대 비용 추정 <span className="text-ok">무료</span></Button>
      {est && (
        <>
          <dl className="grid grid-cols-2 gap-3 rounded-2xl bg-secondary/60 p-4 text-sm sm:grid-cols-4">
            {[["질문 자리 · 호출", `${slots.length}개 · ${est.calls}회`], ["최대 비용", usd(est.max_micro_usd, 4)],
              ["평가 예산(gold_eval) 남음", usd(est.envelope_remaining_micro_usd, 4)], ["예약 가능", usd(est.available_micro_usd, 4)]].map(([k, v]) => (
              <div key={k}><dt className="text-xs text-muted-foreground">{k}</dt><dd className="font-semibold tabular-nums">{v}</dd></div>
            ))}
          </dl>
          {!est.paid_enabled ? <Notice tone="bad">유료 호출이 꺼져 있어 초안을 만들 수 없습니다.</Notice>
            : !est.fits ? <Notice tone="bad">최대 비용이 평가 예산(gold_eval) 또는 운영 한도를 넘습니다. 질문 자리를 줄이세요.</Notice> : (
              <div className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} className="size-4 accent-primary" />
                  질문 자리 {slots.length}개로 gpt-6-luna 초안 생성을 실행합니다 (최대 {usd(est.max_micro_usd, 4)}, 공유 예산에서 차감)
                </label>
                <Button size="lg" onClick={start} disabled={!agree || state.busy}>초안 생성 · 유료</Button>
              </div>
            )}
        </>
      )}
      {state.error && <Notice tone="bad">{state.error}</Notice>}
    </div>
  );
}

export function DraftRuns({ runs, onChanged }: { runs: Schemas["DraftRun"][]; onChanged: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const submit = async (runId: string) => {
    const { error: e } = await api.POST("/api/drafting/runs/{run_id}/submit", { params: { path: { run_id: runId } } });
    setError(e ? errorText(e) : null);
    onChanged();
  };
  if (!runs.length) return <Empty>아직 초안 생성 기록이 없습니다.</Empty>;
  return (
    <div className="space-y-3">
      {error && <Notice tone="bad">{error}</Notice>}
      {runs.map((r) => {
        const s = DRAFT_STATUS[r.status];
        return (
          <article key={r.run_id} className="space-y-3 rounded-2xl border p-4">
            <header className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-sm font-semibold">{r.run_id}</span>
              <StatusBadge tone={s.tone}>{s.label}</StatusBadge>
              {r.submitted && <StatusBadge tone="ok">검토 대기열에 올림</StatusBadge>}
              {r.status === "completed" && r.rows.length > 0 && !r.submitted && (
                <Button size="sm" className="ml-auto" onClick={() => submit(r.run_id)}>검토 대기열에 올리기</Button>
              )}
            </header>
            <p className="text-xs text-muted-foreground tabular-nums">
              요청 {r.requested_by ?? "-"} · {stamp(r.created_at)} · 질문 자리 {r.slots}개 · 동의한 최대 {usd(r.max_micro_usd, 4)}
              {r.receipt && ` · 유효 ${r.receipt.rows} · 무효 ${r.receipt.invalid} · 정산 ${usd(r.receipt.settled_micro_usd, 4)}`}
            </p>
            {r.error && <Notice tone="bad">{r.error}</Notice>}
            {r.rows.length > 0 && (
              <Table caption={`${r.run_id} 유효한 초안`} head={["유형", "질문", "필수 주장"]}>
                {r.rows.map((x) => (
                  <tr key={x.question_id}>
                    <td className={cn(td, "whitespace-nowrap")}>{label(GOLD_TYPE, x.question_type)}</td>
                    <td className={td}>{x.question}</td>
                    <td className={cn(td, "tabular-nums")}>{x.required_claims.length}</td>
                  </tr>
                ))}
              </Table>
            )}
            {r.invalid.length > 0 && (
              <Section title={`무효 초안 ${r.invalid.length}개`}>
                <ul className="space-y-1 text-sm text-bad">{r.invalid.map((x) => <li key={x.question_id}>{x.question_id}: {x.reason}</li>)}</ul>
              </Section>
            )}
          </article>
        );
      })}
    </div>
  );
}
