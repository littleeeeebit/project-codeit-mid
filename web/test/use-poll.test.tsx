// usePoll's ownership of a result: a failure under a new key never shows the old key's data, while a failed re-poll of
// the same key keeps what it last showed.

import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { usePoll } from "@/lib/use-poll";

afterEach(() => vi.useRealTimers());

test("a failure under a new key carries nothing from the old key", async () => {
  const load = vi.fn<(key: string) => Promise<string>>(async (key) => {
    if (key === "b") throw new Error("b failed");
    return `data of ${key}`;
  });
  const { result, rerender } = renderHook(({ k }) => usePoll(k, () => load(k), null), { initialProps: { k: "a" } });
  await act(async () => {});
  expect(result.current.data).toBe("data of a");
  rerender({ k: "b" });
  await act(async () => {});
  expect(result.current).toMatchObject({ error: "b failed", loading: false });
  expect(result.current.data).toBeUndefined();
});

test("a failed re-poll of the same key keeps its last data", async () => {
  vi.useFakeTimers();
  let calls = 0;
  const { result } = renderHook(() => usePoll("k", async () => {
    if (++calls > 1) throw new Error("blip");
    return "first";
  }, 1000));
  await act(async () => {});
  expect(result.current.data).toBe("first");
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(result.current).toMatchObject({ data: "first", error: "blip" });
});
