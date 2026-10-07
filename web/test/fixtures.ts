// Payloads shaped like the service's responses (src/lib/api-schema.d.ts), and a stand-in for `api` that answers
// each route from a table, so a screen renders exactly as it would against the service.

import { vi } from "vitest";
import type { Answer, Doc, Evidence, Owned, RequestView, Schemas } from "@/lib/api";

export const DOC_A = "doc-aaaaaaaa";
export const DOC_B = "doc-bbbbbbbb";

export function answer(over: Partial<Answer> = {}): Answer {
  return {
    request_id: "req-00000001-abcd", generation_id: "gen-1", mode: "single", status: "answered",
    billing_state: "settled", standalone_question: "", error: null, next_action: null, limitations: [],
    summary: "하자보수 기간은 12개월입니다.", summary_evidence_ids: ["E2", "E1"],
    claims: [
      { text: "계약 후 12개월 동안 하자를 보수합니다.", kind: "source_fact", doc_id: DOC_A, evidence_ids: ["E1", "E3"] },
      { text: "무상 보수로 보입니다.", kind: "inference", doc_id: DOC_A, evidence_ids: ["E9"] },
    ],
    conflicts: [], coverage: [{ doc_id: DOC_A }], facts: [], missing_fields: [], inventory: null,
    evidence: { E1: {}, E2: {}, E3: {} },
    ...over,
  };
}

export function view(over: Partial<RequestView> = {}): RequestView {
  return {
    request_id: "req-00000001-abcd", generation_id: "gen-1", member_id: "member", mode: "single",
    question: "하자보수 기간은?", status: "completed", billing_state: "settled", cancel_requested: false,
    created_at: "2026-10-07T09:30:12Z", updated_at: "2026-10-07T09:30:20Z", as_of: "2026-10-07",
    reserved_micro_usd: 0, settled_micro_usd: 1234, scope: [], result: answer(), ...over,
  };
}

export function evidence(over: Partial<Evidence> = {}): Evidence {
  return {
    evidence_id: "E1", doc_id: DOC_A, source_hash: "hash-a", source_format: "pdf", title: "통합 정보시스템 구축",
    quote: "하자보수 기간은 검수 후 12개월로 한다.", review_status: "auto_verified", download_available: true,
    location: { format: "pdf", pages: [12], page_label: "11", section_path: ["제3장", "하자보수"] }, warnings: [],
    context: [
      { element_id: "p1", text: "제3장 계약 조건", cited: false, location: {} },
      { element_id: "p2", text: "하자보수 기간은 검수 후 12개월로 한다.", cited: true, location: {} },
      { element_id: "p3", text: "보수 비용은 계약자가 부담한다.", cited: false, location: {} },
    ],
    ...over,
  };
}

export function doc(over: Partial<Doc> = {}): Doc {
  return {
    doc_id: DOC_A, source_hash: "hash-a", title: "통합 정보시스템 구축", institution: "한국교육학술정보원",
    amount_krw: 130_000_000, notice: "2024-001", format: "pdf", indexed: true, parse_status: "parsed",
    review_status: "auto_verified", published_at: { value: "2024-03-02", precision: "date" },
    bid_close: { value: "2024-03-20T10:00:00", precision: "timestamp" }, conflicts: [], resolutions: {},
    filter_undecided: [], snippet: null, unavailable_reason: null, ...over,
  };
}

export const owned = (n: number): Owned => ({ request_id: `req-0000000${n}-abcd`, generation_id: `gen-${n}`, target: "t" });

type Arm = Schemas["ArmMetrics"];

export function arm(over: Partial<Arm> = {}): Arm {
  return {
    items: 120, judged: 114, coverage: 0.95, kappa: 0.812, false_accepts: 2, reference_negatives: 18, code_settled: 7,
    abstentions: {}, agreement: { numerator: 105, denominator: 114, rate: 0.9211, wilson95: [0.8566, 0.958] },
    latency_ms: { n: 114, p50: 2400, p95: 5100 }, usd: { calls: 120, priced: true, settled_micro_usd: 412_000, source: "ledger" },
    bridge_usd: null, confusion: { link: { supporting: { supporting: 50, unsupported: 2 }, unsupported: { unsupported: 9 } } },
    ...over,
  };
}

export function judgeRun(over: Partial<Schemas["JudgeRun"]> = {}): Schemas["JudgeRun"] {
  return {
    run_id: "J-held_out-0123456789ab", part: "held_out", created_at: "2026-10-05T08:00:00Z", running: false,
    status: "complete", thresholds_fitted: false, translated_batches: 4,
    progress: { luna: { done: 120, total: 120 }, jev_bridged: { done: 120, total: 120 }, jev_raw: { done: 30, total: 30 } },
    ...over,
  };
}

export function overview(over: Partial<Schemas["JudgeOverview"]> = {}): Schemas["JudgeOverview"] {
  return {
    reference: { run_id: "A-ref", items: 240, labels: {}, reference_sha256: "r" },
    split: { calibration: 120, held_out: 120, raw_sample: 30, seed: 7, split_sha256: "s" },
    judge_set: { items: 90, negatives: 70, positives: 20, by_type: { amount: 30, date: 25, negation: 15 }, by_kind: {},
                 set_sha256: "js", version: "1" },
    runs: [judgeRun({ run_id: "J-calibration-0123456789ab", part: "calibration", thresholds_fitted: true,
                      created_at: "2026-10-04T08:00:00Z" }), judgeRun()],
    ...over,
  };
}

export function results(over: Partial<Schemas["JudgeResults"]> = {}): Schemas["JudgeResults"] {
  return {
    run_id: "J-held_out-0123456789ab", part: "held_out", status: "complete", config_hashes: {}, mutations: {},
    progress: {}, thresholds_sha256: "abcdef0123", judge_set_sha256: null,
    arms: { luna: arm(), jev_bridged: arm({ kappa: 0.79, false_accepts: 1, latency_ms: { n: 114, p50: 600, p95: 900 },
                                             usd: { calls: 120, priced: false, source: "none" },
                                             bridge_usd: { calls: 4, priced: true, settled_micro_usd: 21_000, source: "ledger" } }),
            jev_raw: arm({ items: 30, judged: 25, coverage: 0.8333, kappa: 0.4 }) },
    verdict: {
      verdict: "replaceable", rule: { version: "r1", kappa_margin: 0.05, min_coverage: 0.9, min_judged: 100 },
      checks: [{ condition: "kappa", passed: true, detail: "" }, { condition: "false_accepts", passed: true, detail: "" },
               { condition: "coverage", passed: true, detail: "" }],
      deciding: [],
    },
    ...over,
  };
}

type Route = (opts: { params?: { path?: Record<string, string>; query?: Record<string, unknown> }; body?: unknown }) => unknown;

/** `api.GET`/`api.POST` (and `api.PUT` when mocked) answering from `routes` by method and path template; a missing route fails the test. A
 *  route returning `{ error }` is a refusal, anything else is the response body. */
export function stubApi(target: { GET: unknown; POST: unknown; PUT?: unknown }, routes: Record<string, Route>) {
  for (const method of ["GET", "POST", "PUT"] as const) {
    if (!target[method]) continue;
    vi.mocked(target[method] as (...a: unknown[]) => unknown).mockImplementation(async (path: unknown, opts: unknown) => {
      const route = routes[`${method} ${path}`];
      if (!route) throw new Error(`unexpected ${method} ${path}`);
      const out = await route((opts ?? {}) as Parameters<Route>[0]);
      if (out && typeof out === "object" && "error" in out) {
        return { error: (out as { error: unknown }).error, response: { ok: false, status: 409 } };
      }
      return { data: out, response: { ok: true, status: 200 } };
    });
  }
}
