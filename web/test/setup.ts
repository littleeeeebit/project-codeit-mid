import { cleanup } from "@testing-library/react";
import { afterEach, expect, vi } from "vitest";

// A snapshot prints one line per element: its tag, the attributes that carry meaning (roles, labels, states, values,
// links, ids) and its text, quoted so a trailing space ("원문 인용 " before its number) stays visible. Styling (class,
// style), the components' own data-* markers and icon drawings are left out: the snapshot pins what a reader and a
// screen reader get, not the classes that lay it out.
const STYLING = /^(class|style|data-slot|data-size|data-spacing|data-orientation|data-radix-collection-item)$/;

function lines(node: Node, pad: string): string[] {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ? [pad + JSON.stringify(node.textContent)] : [];
  if (!(node instanceof Element)) return [];
  const attrs = [...node.attributes].filter((a) => !STYLING.test(a.name))
    .map((a) => (a.value ? `${a.name}=${JSON.stringify(a.value)}` : a.name));
  const head = pad + [node.tagName.toLowerCase(), ...attrs].join(" ");
  if (node instanceof SVGElement) return [head];
  const kids = [...node.childNodes].filter((c) => c instanceof Element || (c.nodeType === Node.TEXT_NODE && c.textContent));
  if (kids.length === 1 && kids[0].nodeType === Node.TEXT_NODE) return [`${head} ${JSON.stringify(kids[0].textContent)}`];
  return [head, ...kids.flatMap((c) => lines(c, pad + "  "))];
}

expect.addSnapshotSerializer({
  test: (value) => value instanceof Element,
  serialize: (value: Element) => lines(value, "").join("\n"),
});

// jsdom has no layout: scrolling is a no-op and every media query reads as a narrow screen unless a test says so
Element.prototype.scrollIntoView = () => {};
window.matchMedia = vi.fn((query: string) => ({ matches: false, media: query } as MediaQueryList));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
