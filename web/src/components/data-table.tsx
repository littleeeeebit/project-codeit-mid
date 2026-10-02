// Tables and titled sections shared by every screen: a caption for screen readers, scoped column headers.

export const th = "px-3 py-2 text-left text-[13px] font-semibold text-muted-foreground";
export const td = "px-3 py-2.5 align-top text-sm";

export function Table({ caption, head, children }: { caption: string; head: string[]; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border">
      <table className="w-full border-collapse">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-secondary/70"><tr>{head.map((h) => <th key={h} scope="col" className={th}>{h}</th>)}</tr></thead>
        <tbody className="divide-y">{children}</tbody>
      </table>
    </div>
  );
}

export function Section({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-[15px] font-bold">{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  );
}
