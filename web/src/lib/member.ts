"use client";

import { useSyncExternalStore } from "react";

// The visitor's typed name (attribution, not authentication), kept in this browser.
const KEY = "rfp-member";
const DEFAULT = "owner";
const listeners = new Set<() => void>();

export function readMember(): string {
  if (typeof window === "undefined") return DEFAULT;
  return window.localStorage.getItem(KEY)?.trim() || DEFAULT;
}

export function writeMember(name: string): void {
  window.localStorage.setItem(KEY, name.slice(0, 40));
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useMember(): string {
  return useSyncExternalStore(subscribe, readMember, () => DEFAULT);
}
