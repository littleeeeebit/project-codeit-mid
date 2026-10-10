"use client";

import { useState } from "react";
import type { Candidate, Decision, RunView, Saved } from "@/lib/api";
import { postJson, short } from "@/lib/api";

const NONE = "__none__";

export function DecisionPanel({ view, notes, initial, onSaved }: {
  view: RunView;
  notes: Record<string, string>;
  initial: Decision | null;
  onSaved: () => void;
}) {
  const candidates = view.candidates.filter((c) => c.role === "candidate");
  const [chosen, setChosen] = useState<string>(initial ? (initial.chosen ?? NONE) : "");
  const [reasons, setReasons] = useState<Record<string, string>>(
    Object.fromEntries((initial?.candidates ?? []).map((c) => [c.id, c.reason ?? ""])));
  const [saved, setSaved] = useState<Saved | null>(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const missing = candidates.filter((c) => c.status === "complete" && c.id !== chosen && !(reasons[c.id] ?? "").trim());
  const noteCount = Object.values(notes).filter((v) => v.trim()).length;

  async function save() {
    setError("");
    setCopied(false);
    try {
      setSaved(await postJson<Saved>(`/api/runs/${view.run_id}/decision`, JSON.stringify({
        chosen: chosen === NONE ? null : chosen, reasons, notes,
      })));
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function copy() {
    if (!saved) return;
    await navigator.clipboard.writeText(saved.prompt);
    setCopied(true);
  }

  return (
    <section id="decision" aria-labelledby="decision-title" className="mt-14 border-t-2 border-ink pt-6">
      <h2 id="decision-title" className="text-[22px] font-bold">결정</h2>
      <p className="mt-1 text-[14px] text-muted">
        후보 하나를 고르거나 고르지 않습니다. 고르지 않은 후보마다 이유를 적으면 결정 파일과 에이전트용 문장이 만들어집니다.
      </p>
      <fieldset className="mt-5">
        <legend className="text-[15px] font-semibold">남길 후보</legend>
        <div className="mt-2 flex flex-col gap-1">
          {candidates.map((c) => <Choice key={c.id} c={c} chosen={chosen} setChosen={setChosen} />)}
          <label className="flex h-10 items-center gap-3 rounded-[10px] px-2 hover:bg-surface">
            <input type="radio" name="chosen" checked={chosen === NONE} onChange={() => setChosen(NONE)} className="size-4" />
            <span className="text-[15px] font-medium">선택하지 않음 · 기준 유지</span>
          </label>
        </div>
      </fieldset>

      {chosen && (
        <div className="mt-6 flex max-w-[860px] flex-col gap-4">
          {candidates.filter((c) => c.id !== chosen).map((c) => (
            <label key={c.id} className="block">
              <span className="text-[15px] font-semibold">{c.label}을(를) 고르지 않은 이유</span>
              {c.status !== "complete" && <span className="ml-2 text-[13px] text-muted">실패한 후보는 비워두면 실패 이유가 기록됩니다</span>}
              <textarea value={reasons[c.id] ?? ""} rows={2}
                        onChange={(e) => setReasons({ ...reasons, [c.id]: e.target.value })}
                        className="mt-1 block w-full rounded-[10px] border border-line px-3 py-2 text-[15px] focus:border-primary" />
            </label>
          ))}
          <div className="flex flex-wrap items-center gap-4">
            <button type="button" onClick={save} disabled={missing.length > 0}
                    className="h-11 rounded-[10px] bg-primary px-5 text-[15px] font-semibold text-white disabled:cursor-not-allowed disabled:bg-neutral-bg disabled:text-muted">
              결정 파일 쓰기
            </button>
            <span className="text-[14px] text-muted">
              {missing.length > 0 ? `이유가 비어 있는 후보 ${missing.length}개` : `메모 ${noteCount}개가 함께 기록됩니다`}
            </span>
          </div>
          {error && <p role="alert" className="text-[14px] text-bad">{error}</p>}
        </div>
      )}

      {saved && (
        <div className="mt-6 max-w-[860px] rounded-[10px] bg-ok-bg px-4 py-3">
          <div className="text-[15px] font-semibold text-ok">결정을 기록했습니다</div>
          <code className="mt-1 block break-all text-[13px]">{saved.markdown}</code>
          <p className="mt-2 text-[14px]">{saved.prompt}</p>
          <button type="button" onClick={copy} className="mt-3 h-10 rounded-[10px] bg-ink px-4 text-[14px] font-semibold text-white">
            {copied ? "복사했습니다" : "에이전트용으로 복사"}
          </button>
        </div>
      )}
    </section>
  );
}

function Choice({ c, chosen, setChosen }: { c: Candidate; chosen: string; setChosen: (v: string) => void }) {
  const usable = c.status === "complete";
  return (
    <label className={`flex min-h-10 items-center gap-3 rounded-[10px] px-2 ${usable ? "hover:bg-surface" : "text-muted"}`}>
      <input type="radio" name="chosen" disabled={!usable} checked={chosen === c.id} onChange={() => setChosen(c.id)} className="size-4" />
      <span className="text-[15px] font-medium">{c.label}</span>
      <code className="text-[13px] text-muted">{short(c.commit)}</code>
      {!usable && <span className="text-[13px]">결과가 없어 고를 수 없음</span>}
    </label>
  );
}
