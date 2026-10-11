"use client";

// Who comes here: the person doing error analysis. What for: see whether the accepted failure types cover what they
// keep finding. Breadth is how many types there are, depth how many answers each one rests on, and saturation how
// many types first appeared in the most recent reviewed answers: when none do, reading more answers adds few types.

import { useEffect, useState } from "react";
import type { Progress } from "@/lib/api";
import { getJson, SOURCE } from "@/lib/api";

/** "골드 12 · 실제 요청 3": a count split by where its answers came from. */
const bySource = (counts: Record<string, number>) =>
  Object.entries(counts).sort().map(([s, n]) => `${SOURCE[s] ?? s} ${n}`).join(" · ");
import { Badge, Metric, Metrics, Segmented } from "./ui";

const WINDOWS = ["10", "20", "50"] as const;

export function ProgressView({ openTaxonomy, openRun }: { openTaxonomy: () => void; openRun: (id: string) => void }) {
  const [last, setLast] = useState<(typeof WINDOWS)[number]>("20");
  const [p, setP] = useState<Progress | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    getJson<Progress>(`/api/taxonomy/progress?last=${last}`).then((v) => live && setP(v))
      .catch((e: Error) => live && setError(`진행 상황을 읽지 못했습니다: ${e.message}`));
    return () => { live = false; };
  }, [last]);

  if (!p) return error ? <p role="alert" className="text-bad">{error}</p> : <p className="text-muted">불러오는 중…</p>;
  const most = Math.max(1, ...p.types.map((t) => t.answers));
  const thin = p.types.filter((t) => t.thin).length;
  const settled = p.reviewed >= Number(last) && p.new_in_last === 0;
  return (
    <div className="max-w-[1080px]">
      <h1 className="text-[24px] font-bold">오류 분석 진행</h1>
      <p className="mt-1 max-w-[760px] text-[14px] text-muted">
        수락된 유형마다 그 유형 메모가 달린 답변 수를 사람이 검토한 답변 수로 나눕니다. 다음에 고칠 것을 고르는 근거이고,
        릴리스 기준이나 봉인된 평가 수치가 아닙니다. AI가 제안하고 승인한 사례는 따로 보이며 비율에 넣지 않습니다. AI 제안은
        모든 사례를 찾지 못합니다.
      </p>

      <Metrics>
        <Metric lead value={p.breadth} label="수락된 유형 (폭)" />
        <Metric value={p.reviewed} label={`사람이 검토한 답변${p.reviewed ? ` (${bySource(p.reviewed_by_source)})` : ""}`} />
        <Metric value={p.new_in_last} label={`최근 ${p.last}개 검토 답변에서 처음 나온 유형`}
                tone={p.new_in_last > 0 ? "text-warn" : ""} />
        <Metric value={thin} label={`사례 ${p.thin}개 미만 유형`} tone={thin ? "text-warn" : ""} />
        <Metric value={p.rereview} label="기준 추가 후 다시 볼 답변" tone={p.rereview ? "text-warn" : ""} />
      </Metrics>
      {p.rereview > 0 && (
        <p className="mt-3 text-[15px]">
          &apos;문제 없음&apos;으로 표시한 뒤에 유형이 생긴 답변 {p.rereview}개가 남았습니다. 실행을 열고 &apos;다시 볼 답변&apos;에서 봅니다:{" "}
          {p.rereview_runs.map((r) => (
            <button key={r} type="button" onClick={() => openRun(r)} className="mr-2 font-semibold text-primary hover:underline">{r}</button>
          ))}
        </p>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <span className="text-[14px] text-muted">최근 몇 개를 볼까요</span>
        <Segmented value={last} onChange={setLast} options={WINDOWS.map((w) => [w, `${w}개`])} />
      </div>
      <p className={`mt-3 text-[16px] font-semibold ${settled ? "text-ok" : "text-warn"}`}>
        {p.reviewed === 0 ? "아직 검토한 답변이 없습니다."
          : settled ? `최근 ${p.last}개에서 새 유형이 나오지 않았습니다. 유형이 거의 다 모였을 수 있습니다.`
            : p.new_in_last > 0 ? `최근 ${p.last}개에서 새 유형 ${p.new_in_last}개(${p.new_names.join(", ")})가 나왔습니다. 더 검토하면 유형이 늘 수 있습니다.`
              : `검토한 답변이 ${last}개보다 적어 포화를 말하기 이릅니다.`}
      </p>

      <section aria-labelledby="depth-title" className="mt-10">
        <h2 id="depth-title" className="text-[18px] font-bold">유형별 깊이 <span className="text-[14px] font-normal text-muted">· v{p.version}</span></h2>
        {p.types.length === 0 ? (
          <p className="mt-2 text-[15px] text-muted">수락된 유형이 없습니다.</p>
        ) : (
          <ol className="mt-2 divide-y divide-line">
            {p.types.map((t) => (
              <li key={t.id} className="grid grid-cols-[minmax(180px,260px)_1fr_auto] items-center gap-x-6 gap-y-1 py-4">
                <div className="min-w-0">
                  <div className="text-[16px] font-bold">{t.name}</div>
                  <div className="text-[13px] text-muted">{t.first ? `검토 순서 ${t.first}번째 답변에서 처음 나옴` : "남은 메모 없음"}</div>
                </div>
                <div>
                  <div className="h-2.5 rounded-full bg-surface">
                    <div className={`h-2.5 rounded-full ${t.thin ? "bg-warn" : "bg-primary"}`} style={{ width: `${(t.answers / most) * 100}%` }} />
                  </div>
                  <p className="mt-1 text-[14px] text-muted">{t.definition}</p>
                </div>
                <div className="text-right">
                  <div className="flex items-center justify-end gap-2">
                    {t.thin && <Badge tone="warn">사례 {p.thin}개 미만</Badge>}
                    <span className="text-[18px] font-bold tabular-nums">{t.answers}</span>
                    <span className="text-[14px] tabular-nums text-muted">/ {p.reviewed} 답변 (사람이 찾음)</span>
                  </div>
                  {t.answers > 0 && <div className="text-[13px] tabular-nums text-muted">{bySource(t.by_source)}</div>}
                  {(t.ai_answers > 0 || t.rereview > 0) && (
                    <div className="text-[13px] tabular-nums">
                      {t.ai_answers > 0 && <span className="text-primary">AI 제안 승인 +{t.ai_answers}</span>}
                      {t.ai_answers > 0 && t.rereview > 0 && " · "}
                      {t.rereview > 0 && <span className="text-warn">다시 볼 답변 {t.rereview}</span>}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      {(p.pending > 0 || p.unassigned > 0) && (
        <p className="mt-6 text-[15px]">
          {p.pending > 0 && <>결정을 기다리는 유형 {p.pending}개 · </>}
          {p.unassigned > 0 && <>어느 유형에도 없는 메모 {p.unassigned}개 · </>}
          <button type="button" onClick={openTaxonomy} className="font-semibold text-primary hover:underline">실패 유형에서 정리하기</button>
        </p>
      )}
    </div>
  );
}
