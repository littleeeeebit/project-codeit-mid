import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom has no layout: scrolling is a no-op and every media query reads as a narrow screen unless a test says so
Element.prototype.scrollIntoView = () => {};
window.matchMedia = vi.fn((query: string) => ({ matches: false, media: query } as MediaQueryList));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
