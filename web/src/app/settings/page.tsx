"use client";

import { useState } from "react";
import { api, errorText, type Budget } from "@/lib/api";
import { usd } from "@/lib/format";
import { must, usePoll } from "@/lib/use-poll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function SettingsPage() {
  const current = usePoll("settings-budget", () => must(api.GET("/api/budget"), errorText), 2000);
  const [limit, setLimit] = useState("");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [saved, setSaved] = useState<Budget>();
  const data = current.data ?? saved;

  async function save(event: React.FormEvent) {
    event.preventDefault();
    // Decimal digits become integer micro-USD without binary floating-point rounding.
    const match = /^(\d+)(?:\.(\d{1,6}))?$/.exec(limit.trim());
    if (!match) { setMessage("한도는 소수점 여섯 자리 이하의 양수로 입력하세요."); return; }
    const exact = BigInt(match[1]) * BigInt(1000000) + BigInt((match[2] ?? "").padEnd(6, "0"));
    if (exact <= BigInt(0) || exact > BigInt("9000000000000000")) { setMessage("한도를 확인하세요."); return; }
    setSaving(true);
    setMessage("");
    try {
      const result = await api.PUT("/api/budget/limit", { body: { cap_micro_usd: Number(exact), reason: reason.trim() } });
      if (result.error) { setMessage(errorText(result.error)); return; }
      setSaved(result.data);
      setMessage("공유 사용 한도를 저장했습니다. 기존 사용량과 예약은 유지됩니다.");
    } catch { setMessage("서버에 연결하지 못했습니다."); }
    finally { setSaving(false); }
  }

  return (
    <main className="mx-auto w-full max-w-2xl space-y-6 px-6 py-10">
      <h1 className="text-2xl font-semibold">설정</h1>
      <section className="space-y-4 rounded-xl border p-6" aria-labelledby="budget-heading">
        <h2 id="budget-heading" className="text-lg font-semibold">API 사용 한도</h2>
        <p className="text-sm text-muted-foreground">이 서버를 사용하는 팀 전체에 적용되는 누적 달러 한도입니다. API 제공자의 계정 한도는 별도로 적용됩니다.</p>
        {data && <p className="text-sm">현재 한도 {usd(data.snapshot.cap_micro_usd, 6)} · 사용 {usd(data.snapshot.spent_micro_usd, 6)} · 예약 {usd(data.snapshot.pending_micro_usd, 6)}</p>}
        {current.error && <p role="alert">{current.error}</p>}
        <form onSubmit={save} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="budget-limit">새 한도 (USD)</Label>
            <Input id="budget-limit" inputMode="decimal" value={limit} onChange={(event) => setLimit(event.target.value)} required placeholder="10.00" />
          </div>
          <div className="space-y-2">
            <Label htmlFor="budget-reason">변경 사유</Label>
            <Input id="budget-reason" value={reason} onChange={(event) => setReason(event.target.value)} required maxLength={500} />
          </div>
          <p className="text-sm text-muted-foreground">기존 사용량과 미확정 예약보다 낮게 설정할 수 없습니다. 용도별 예산은 현재 비율로 조정되며 변경자와 사유가 기록됩니다.</p>
          <Button type="submit" disabled={saving || !reason.trim()}>{saving ? "저장 중…" : "한도 저장"}</Button>
          <p role="status" aria-live="polite">{message}</p>
        </form>
      </section>
    </main>
  );
}
