"use client";

// Design harness chrome (variant skill, picker.md): deliberately outside the design system. Deleted with the
// variants once the user picks one.

import { useEffect } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import "./variant-picker.css";

export function useVariant<T extends string>(ids: readonly T[]): T {
  const v = useSearchParams().get("variant");
  return (ids as readonly string[]).includes(v ?? "") ? (v as T) : ids[0];
}

export function VariantPicker({ variants }: { variants: readonly { id: string; name: string }[] }) {
  const router = useRouter();
  const path = usePathname();
  const params = useSearchParams();
  const active = useVariant(variants.map((v) => v.id));

  useEffect(() => {
    const go = (id: string) => {
      const next = new URLSearchParams(params.toString());
      next.set("variant", id);
      router.replace(`${path}?${next}`, { scroll: false });
    };
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName))) return;
      const i = variants.findIndex((v) => v.id === active);
      if (e.key === "ArrowRight") go(variants[(i + 1) % variants.length].id);
      else if (e.key === "ArrowLeft") go(variants[(i - 1 + variants.length) % variants.length].id);
      else if (/^[1-9]$/.test(e.key) && variants[Number(e.key) - 1]) go(variants[Number(e.key) - 1].id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, params, path, router, variants]);

  const select = (id: string) => {
    const next = new URLSearchParams(params.toString());
    next.set("variant", id);
    router.replace(`${path}?${next}`, { scroll: false });
  };

  return (
    <nav className="variant-picker" aria-label="Variants">
      {variants.map((v) => (
        <button key={v.id} type="button" data-variant={v.id} aria-current={v.id === active ? "true" : undefined}
                onClick={() => select(v.id)}>
          {v.name}
        </button>
      ))}
    </nav>
  );
}
