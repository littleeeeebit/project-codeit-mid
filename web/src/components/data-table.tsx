// Tables and titled sections shared by every screen: a caption for screen readers, scoped column headers.

export const th = "px-3 py-3 text-left text-sm font-semibold text-foreground";
export const td = "px-3 py-3 align-top text-sm";

export function Table({ caption, head, children }: { caption: string; head: string[]; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-input bg-background">
      <table className="w-full border-collapse">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-secondary/70"><tr>{head.map((h) => <th key={h} scope="col" className={th}>{h}</th>)}</tr></thead>
        <tbody className="divide-y divide-input">{children}</tbody>
      </table>
    </div>
  );
}

export function Section({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h3 className="text-lg font-bold">{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  );
}
