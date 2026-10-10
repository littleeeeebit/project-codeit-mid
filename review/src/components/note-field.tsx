"use client";

import { useState } from "react";

/** A per-question or per-document note: a link until used, so empty notes cost no space. */
export function NoteField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  if (!open && !value) {
    return (
      <button type="button" onClick={() => setOpen(true)} className="h-7 text-[14px] font-medium text-primary hover:underline">
        메모 남기기
      </button>
    );
  }
  return (
    <label className="block w-full max-w-[720px]">
      <span className="text-[13px] font-semibold text-muted">메모 (결정 파일에 함께 기록됩니다)</span>
      <textarea value={value} onChange={(e) => onChange(e.target.value)} rows={2} autoFocus={!value}
                className="mt-1 block w-full rounded-[10px] border border-line px-3 py-2 text-[14px] focus:border-primary" />
    </label>
  );
}
