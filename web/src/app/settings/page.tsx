"use client";

import { useState } from "react";
import { api, errorText, type Budget } from "@/lib/api";
import { usd } from "@/lib/format";
import { must, usePoll } from "@/lib/use-poll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

function ApiKeySection() {
  const status = usePoll("settings-api-key", () => must(api.GET("/api/settings/api-key"), errorText), 5000);
  const [key, setKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setMessage("");
    try {
      const result = await api.PUT("/api/settings/api-key", { body: { api_key: key } });
      if (result.error) { setMessage(errorText(result.error)); return; }
      setMessage("API 키를 확인하고 적용했습니다. 지금부터 유료 답변에 이 키를 씁니다.");
    } catch { setMessage("서버에 연결하지 못했습니다."); }
    finally { setKey(""); setSaving(false); }
  }

  const s = status.data;
  return (
    <section className="space-y-4 rounded-xl border p-6" aria-labelledby="api-key-heading">
      <h2 id="api-key-heading" className="text-lg font-semibold">OpenAI API 키</h2>
      {s && (s.configured
        ? <p className="text-sm">설정됨 · {s.set_by} · {s.set_at}</p>
        : <p className="text-sm font-medium text-destructive">설정되지 않았습니다. 키를 입력해야 질문에 답할 수 있습니다.</p>)}
      {status.error && <p role="alert">{status.error}</p>}
      <form onSubmit={save} className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="api-key">{s?.configured ? "새 키로 교체" : "키 입력"}</Label>
          <Input id="api-key" type="password" autoComplete="off" spellCheck={false} value={key}
                 onChange={(event) => setKey(event.target.value)} required placeholder="sk-…" />
        </div>
        <p className="text-sm text-muted-foreground">키는 서버 프로세스 메모리에만 있고 파일, 데이터베이스, 로그 어디에도 저장되지 않으며 화면에 다시 표시되지 않습니다. 서버가 다시 시작되면 다시 입력해야 합니다.</p>
        <Button type="submit" disabled={saving || !key.trim()}>{saving ? "확인 중…" : "키 확인 후 적용"}</Button>
        <p role="status" aria-live="polite">{message}</p>
      </form>
    </section>
  );
}

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
      <ApiKeySection />
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
