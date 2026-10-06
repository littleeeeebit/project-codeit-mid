import { cleanup } from "@testing-library/react";
import { afterEach, expect, vi } from "vitest";

// A text node ending in a space ("원문 인용 " before its number) prints as a line ending in a space; show it as ␠ so
// the snapshot keeps it and the repository's whitespace check (`git diff --check`) stays clean.
let printing = false;
expect.addSnapshotSerializer({
  test: (value) => !printing && value instanceof Element,
  serialize(value, config, indentation, depth, refs, printer) {
    printing = true;
    try {
      return printer(value, config, indentation, depth, refs).replace(/ +$/gm, (s) => "␠".repeat(s.length));
    } finally {
      printing = false;
    }
  },
});

// jsdom has no layout: scrolling is a no-op and every media query reads as a narrow screen unless a test says so
Element.prototype.scrollIntoView = () => {};
window.matchMedia = vi.fn((query: string) => ({ matches: false, media: query } as MediaQueryList));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
