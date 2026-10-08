import { render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { api, type Schemas } from "@/lib/api";
import { ExperimentsSection } from "@/components/verify/experiments";
import { stubApi } from "./fixtures";

vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), api: { GET: vi.fn(), POST: vi.fn() } }));

test.each([false, true, undefined])("comparison completion %s controls the verdict and preserves recorded retrieval", async (complete) => {
  const table: Schemas["ExperimentTable"] = {
    matrix: "answer-embedding", title: "answers", created_at: "2026-10-08", populations: {}, needs_evidence_review: [],
    fixed: { reasoning_effort: "minimal", max_output_tokens: 2000, answer_questions: 35 },
    columns: [{ key: "answer.pass_rate", label: "pass rate", better: "high" },
              { key: "retrieval.mode", label: "retrieval", better: null },
              { key: "retrieval.depth", label: "depth", better: null }],
    rows: [0, 1].map((index) => ({ index, name: index ? "candidate" : "K1", label: null,
      status: "complete", active: false, failures: 0, run_id: `R-${index}`, reason: null, estimate: null, facts: {},
      axes: { embedding: index ? "candidate" : "K1" },
      values: { "answer.pass_rate": index, "retrieval.mode": "kiwi_bm25", "retrieval.depth": index ? 10 : 50 } })),
    conclusion: { complete, best: ["candidate"], best_pass_rate: 1, baseline: "K1", alpha: 0.05,
                  significant: [{ name: "candidate", pass_diff: 1, p_holm: 0.0312 }] },
  };
  stubApi(api, { "GET /api/verify/experiments": () => ({ active_run_id: null, serving: "keyword",
    serving_detail: { mode: "kiwi_bm25", embedding: null, reranker: null, protect: 0, fallback: null },
    golden_counts: null, tables: [table] }) });
  render(<ExperimentsSection />);
  const grid = await screen.findByRole("table");
  const candidate = within(grid).getAllByRole("row")[2];
  expect(within(candidate).getByText("10")).toBeTruthy();
  expect(within(candidate).getByText("키워드 Kiwi BM25")).toBeTruthy();
  if (complete === false) {
    expect(screen.getByText("비교가 완료되지 않아 순위와 유의성 결론을 내릴 수 없습니다.")).toBeTruthy();
    expect(screen.queryByText(/유의하게 높음/)).toBeNull();
    expect(screen.queryByText("가장 높은 답변 통과율")).toBeNull();
  } else {
    expect(screen.getByText(/유의하게 높음/)).toBeTruthy();
  }
});
