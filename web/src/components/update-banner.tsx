"use client";

import { useEffect, useState } from "react";
import { accountHeaders, api, errorText, type Schemas } from "@/lib/api";
import { must, usePoll } from "@/lib/use-poll";
import { Button } from "@/components/ui/button";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { StatusBadge } from "./status-badge";

type UpdateStatus = Schemas["UpdateStatus"];
type UpdateResult = Schemas["UpdateResult"];

const FAILED = ["failed", "rolled_back", "rollback_failed", "interrupted"];
const SHOW_FAILURE_MS = 24 * 3600 * 1000;  // a failed update stays visible to whoever comes next, for a day
const GIVE_UP_MS = 15 * 60 * 1000;

const short = (sha?: string | null) => (sha ? sha.slice(0, 7) : "?");

/** The header's update strip: a newer GitHub main, the 업데이트 button (faded while paid work is open), the
 *  confirmation, and the wait while the server updates and restarts (runbook 3.2). Every signed-in member sees it. */
export function UpdateBanner() {
  const status = usePoll("update", () => must(api.GET("/api/update/status"), errorText), 15000);  // read-only
  const [confirming, setConfirming] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const data = status.data;

  const watching = sent || !!data?.in_progress;
  const outcome = useUpdateWait(watching);

  if (!data?.configured) return null;
  const failure = recentFailure(data.last_result);
  if (!data.available && !watching && !failure) return null;

  const send = async () => {
    setError(null);
    const { data: next, error: refused } = await api.POST("/api/update");
    if (next) setSent(true);
    else setError(errorText(refused));
    status.reload();
  };

  const blocked = data.paid_work.open;
  return (
    <div className="border-t bg-info-bg/60" data-testid="update-banner">
      <div className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-x-3 gap-y-2 px-6 py-2 text-sm">
        {watching && !outcome ? (
          <>
            <StatusBadge tone="info">업데이트 중</StatusBadge>
            <span role="status">서버를 main의 최신 커밋으로 바꾸고 다시 시작하고 있습니다. 응답이 돌아오면 이 화면을 새로 고칩니다.</span>
          </>
        ) : outcome ? (
          <>
            <StatusBadge tone={outcome.tone}>업데이트 결과</StatusBadge>
            <span role="status">{outcome.text}</span>
          </>
        ) : data.available ? (
          <>
            <StatusBadge tone="info">새 버전</StatusBadge>
            <span>
              main에 새 커밋 {data.ahead_by}개가 있습니다 (실행 중 {short(data.running_commit)} → 최신 {short(data.latest_commit)}).
            </span>
            <details className="group">
              <summary className="cursor-pointer text-muted-foreground hover:text-foreground">바뀐 내용</summary>
              <ul className="mt-1 list-disc space-y-0.5 pl-5">
                {data.commits.map((c) => <li key={c.sha}><code className="text-xs">{short(c.sha)}</code> {c.title}</li>)}
              </ul>
            </details>
            <Button size="sm" disabled={blocked} aria-describedby={blocked ? "update-blocked" : undefined}
                    onClick={() => setConfirming(true)}>
              업데이트
            </Button>
            {blocked && <span id="update-blocked" className="text-muted-foreground">{data.paid_work.reason}</span>}
            {error && <span role="alert" className="text-bad">{error}</span>}
          </>
        ) : null}
        {failure && !watching && !outcome && <FailureNote result={failure} />}
      </div>
      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogTitle>지금 업데이트할까요?</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div>
              <p>서버를 main의 최신 커밋({short(data.latest_commit)})으로 바꾸고 다시 시작합니다. 몇 분 걸립니다.</p>
              <p className="font-semibold text-foreground">
                다시 시작하면 지금 접속한 모든 팀원이 로그아웃되고, 설정에서 입력한 OpenAI API 키가 지워져 다시 입력해야 합니다.
              </p>
              <p>새 버전이 응답하지 않으면 자동으로 이전 버전으로 되돌립니다.</p>
            </div>
          </AlertDialogDescription>
          <AlertDialogFooter>
            <AlertDialogCancel>취소</AlertDialogCancel>
            <AlertDialogAction onClick={send}>업데이트</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function recentFailure(result?: UpdateResult | null): UpdateResult | null {
  if (!result || !FAILED.includes(result.state)) return null;
  const at = Date.parse(result.finished_at ?? result.started_at ?? "");
  return Number.isNaN(at) || Date.now() - at < SHOW_FAILURE_MS ? result : null;
}

function FailureNote({ result }: { result: UpdateResult }) {
  const what = result.state === "failed" ? "지난 업데이트를 하지 못했습니다 (바뀐 것 없음)"
    : result.state === "rolled_back" ? "지난 업데이트가 실패해 이전 버전으로 되돌렸습니다"
    : result.state === "interrupted" ? "지난 업데이트가 끝나지 않았습니다"
    : "지난 업데이트와 되돌리기가 모두 실패했습니다";
  return (
    <span role="status" className="flex flex-wrap items-center gap-2">
      <StatusBadge tone={result.state === "failed" ? "warn" : "bad"}>{what}</StatusBadge>
      <span className="text-muted-foreground">
        {short(result.from_commit)} → {short(result.to_commit)}: {result.message}
      </span>
    </span>
  );
}

/** While an update runs: polls the status without the session middleware, so the restart's lost session does not
 *  bounce the page to sign-in mid-wait. The page reloads once a restarted server answers. An updater that finished
 *  without restarting (refused, already current) leaves its result here instead. The request marker keeps
 *  `in_progress` true from the moment the request is accepted, so a result read after it is this run's. */
function useUpdateWait(active: boolean): { tone: "warn" | "bad"; text: string } | null {
  const [outcome, setOutcome] = useState<{ tone: "warn" | "bad"; text: string } | null>(null);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    let wentDown = false;
    const started = Date.now();
    const tick = async () => {
      let next: UpdateStatus | null = null;
      try {
        const response = await fetch("/api/update/status", { headers: accountHeaders(), cache: "no-store" });
        if (response.status === 401 || response.status === 409) return window.location.reload();  // restarted
        if (response.ok) next = await response.json();
        else wentDown = true;
      } catch {
        wentDown = true;  // the server is restarting
      }
      if (!alive) return;
      if (next && wentDown) return window.location.reload();
      const result = next?.last_result;
      if (next && !next.in_progress && result && result.state !== "running") {
        if (result.state === "succeeded") return window.location.reload();
        setOutcome(result.state === "up_to_date" ? { tone: "warn", text: "이미 최신 main을 실행하고 있습니다." }
          : { tone: result.state === "failed" ? "warn" : "bad", text: result.message ?? result.state });
        return;
      }
      if (Date.now() - started > GIVE_UP_MS) {
        setOutcome({ tone: "bad", text: "서버가 15분 동안 다시 응답하지 않았습니다. 잠시 뒤 새로 고쳐 보세요." });
        return;
      }
      setTimeout(() => { if (alive) tick(); }, 2000);
    };
    tick();
    return () => { alive = false; };
  }, [active]);
  return outcome;
}
