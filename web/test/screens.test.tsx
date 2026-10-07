// Characterization of the dataset review queue, the settings page and the document search as they render today.

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api, type Schemas } from "@/lib/api";
import SettingsPage from "@/app/settings/page";
import { DocumentSearch } from "@/components/ask/document-search";
import { ColumnsQueue } from "@/components/dataset/review";
import { doc, DOC_A, DOC_B, stubApi } from "./fixtures";

vi.mock("@/lib/api", async (original) => ({
  ...(await original<object>()), api: { GET: vi.fn(), POST: vi.fn(), PUT: vi.fn() },
}));
vi.mock("@/components/sign-in", () => ({ useMe: () => ({ name: "person-a" }) }));

const settle = () => act(async () => {});

const CANDIDATE: Schemas["CandidateReview"] = {
  candidate_id: "dev-amount-r1", row_sha256: "sha", question: "사업 금액은 얼마인가요?", type: "fact", dataset: "dev",
  gold: true, answerability: "answerable", expected_status: "answered", as_of_date: "2024-05-01", drafted_by: "agent-a",
  requested_by: "person-a", expected_answer: "1억 3천만원", difficulty_reason: "표 안의 값", current_errors: ["g1: 원문이 바뀜"],
  required_claims: [{ claim_id: "c1", match: { type: "number", value: 130000000, unit: "원" }, critical_kind: "amount",
                      qualifiers: [["부가세", "VAT"], ["포함"]], support_groups: ["g1"] }],
  documents: [
    { doc_id: DOC_A, source_hash: "hash-a", found: true, title: "통합 정보시스템 구축", filename: "a.pdf", format: "pdf",
      review_status: "auto_verified", pdf_pages: [{ group_id: "g1", pdf_pages: [12, 13] }], unavailable_reason: null },
    { doc_id: null, source_hash: null, found: false, pdf_pages: [] },
  ],
  negative_validation: { searched: "전체", terms: ["금액"] },
  metadata: [{ field: "amount_krw", value: 130000000 }],
  spans: [
    { label: "g1 · 근거 1", cited_found: true, missing: false, quote: "1억 3천만원", location: { format: "pdf", pages: [12] },
      segments: [{ text: "사업 금액은 ", cited: false }, { text: "1억 3천만원", cited: true }] },
    { label: "g1 · 근거 2", cited_found: false, missing: true, quote: "없는 인용", location: null, segments: [] },
  ],
};

describe("dataset review queue", () => {
  test("the draft beside its spans, and a rejection with reasons", async () => {
    const sent: unknown[] = [];
    const decided = vi.fn();
    stubApi(api, {
      "GET /api/gold/{candidate_id}": () => CANDIDATE,
      "GET /api/gold/reject-categories": () => ({ too_easy: "너무 쉬움", wrong_value: "값 틀림" }),
      "POST /api/gold/{candidate_id}/decide": (o) => { sent.push([o.params?.path, o.body]); return {}; },
    });
    const pending = [{ candidate_id: "dev-amount-r1", question: "사업 금액은 얼마인가요?", type: "fact" },
                     { candidate_id: "dev-date-r1", question: "마감일은?", type: "fact" }] as Schemas["Pending"][];
    const { container } = render(<ColumnsQueue pending={pending} onDecided={decided} />);
    await settle();
    expect(container).toMatchSnapshot();
    expect((screen.getByRole("button", { name: "승인 → 데이터셋" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/^메모/), { target: { value: "12쪽 표와 다름" } });
    fireEvent.click(screen.getByRole("button", { name: "값 틀림" }));
    fireEvent.click(screen.getByLabelText(/쟁점 있음/));
    fireEvent.click(screen.getByRole("button", { name: "거절 → 거절 위키" }));
    await settle();
    expect(sent).toEqual([[{ candidate_id: "dev-amount-r1" }, { decision: "reject", expected_sha: "sha", note: "12쪽 표와 다름",
                                                               categories: ["wrong_value"], original_inspected: false, disputed: true }]]);
    expect(decided).toHaveBeenCalledWith("dev-amount-r1: 거절했습니다.");
  });
});

describe("settings", () => {
  let puts: unknown[];
  beforeEach(() => {
    puts = [];
    stubApi(api, {
      "GET /api/settings/api-key": () => ({ configured: true, model: "gpt-5-mini", models: ["gpt-6-luna", "gpt-5-mini", "o9"],
                                            set_by: "person-a", set_at: "2026-10-07 09:00" }),
      "GET /api/budget": () => ({ warnings: [], snapshot: { cap_micro_usd: 5_000_000, spent_micro_usd: 1_234_567, pending_micro_usd: 10 } }),
      "PUT /api/settings/model": (o) => { puts.push(["model", o.body]); return {}; },
      "PUT /api/settings/api-key": (o) => { puts.push(["key", o.body]); return { error: { detail: "OpenAI가 이 키를 거부했습니다." } }; },
      "PUT /api/budget/limit": (o) => { puts.push(["limit", o.body]); return { warnings: [], snapshot: { cap_micro_usd: 7_500_000,
                                                                                                         spent_micro_usd: 1, pending_micro_usd: 0 } }; },
    });
  });

  test("the page as it opens", async () => {
    const { container } = render(<SettingsPage />);
    await settle();
    expect(container).toMatchSnapshot();
  });

  test("a model change, a refused key and a new limit", async () => {
    render(<SettingsPage />);
    await settle();
    const keys = screen.getByRole("region", { name: "내 OpenAI API 키와 답변 모델" });
    fireEvent.change(within(keys).getByLabelText("답변 모델"), { target: { value: "o9" } });
    fireEvent.submit(within(keys).getByRole("button", { name: "모델 변경" }).closest("form")!);
    await settle();
    expect(within(keys).getByRole("status").textContent).toBe("내 계정의 답변 모델을 o9(으)로 바꿨습니다.");
    fireEvent.change(within(keys).getByLabelText(/새 키로 교체/), { target: { value: "sk-x" } });
    expect(within(keys).getByRole("button", { name: "키 확인 후 적용" })).toBeTruthy();
    fireEvent.submit(within(keys).getByRole("button", { name: "키 확인 후 적용" }).closest("form")!);
    await settle();
    expect(within(keys).getByRole("status").textContent).toBe("OpenAI가 이 키를 거부했습니다.");
    const budget = screen.getByRole("region", { name: "API 사용 한도" });
    fireEvent.change(within(budget).getByLabelText("새 한도 (USD)"), { target: { value: "7.5" } });
    fireEvent.change(within(budget).getByLabelText("변경 사유"), { target: { value: "증액" } });
    fireEvent.submit(within(budget).getByRole("button", { name: "한도 저장" }).closest("form")!);
    await settle();
    expect(puts).toEqual([["model", { model: "o9" }], ["key", { api_key: "sk-x", model: "gpt-5-mini" }],
                          ["limit", { cap_micro_usd: 7_500_000, reason: "증액" }]]);
  });
});

describe("document search", () => {
  const DOCS = [doc(), ...Array.from({ length: 13 }, (_, i) => doc({
    doc_id: i ? `doc-${i}` : DOC_B, source_hash: `hash-${i}`, title: i % 2 ? null : `문서 ${i}`, institution: null,
    amount_krw: null, bid_close: null, indexed: i % 3 !== 0, parse_status: i === 3 ? "quarantined" : "parsed",
    review_status: i === 1 ? "auto_flagged" : "sample_checked", conflicts: i === 2 ? [{ field: "amount_krw" }] : [],
    filter_undecided: i === 4 ? ["amount_krw:conflict", "bid_close:missing"] : [],
  }))];
  let queries: unknown[];
  beforeEach(() => {
    queries = [];
    stubApi(api, { "GET /api/documents": (o) => { queries.push(o.params?.query); return DOCS; } });
  });

  test("the results, a full selection and the next page", async () => {
    const toggle = vi.fn();
    const { container } = render(<DocumentSearch selected={[DOCS[0], DOCS[1]]} onToggle={toggle} />);
    await settle();
    expect(container).toMatchSnapshot();
    fireEvent.click(screen.getByRole("button", { name: "더 보기 (2건 남음)" }));
    expect(screen.getAllByRole("checkbox")).toHaveLength(14);
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(toggle).toHaveBeenCalledWith(DOCS[0]);
  });

  test("the filters it sends", async () => {
    render(<DocumentSearch selected={[]} onToggle={() => {}} />);
    await settle();
    fireEvent.change(screen.getByLabelText("검색어"), { target: { value: "하자" } });
    fireEvent.change(screen.getByLabelText("발주 기관"), { target: { value: "교육" } });
    fireEvent.click(screen.getByRole("button", { name: /상세 조건/ }));
    fireEvent.change(screen.getByLabelText("최소 금액(원)"), { target: { value: "1,000만" } });
    fireEvent.change(screen.getByLabelText("마감 끝"), { target: { value: "2024-12-31" } });
    expect(screen.getByRole("search")).toMatchSnapshot();
    fireEvent.submit(screen.getByRole("search"));
    await settle();
    expect(queries).toEqual([{ query: "", institution: "" },
                             { query: "하자", institution: "교육", amount_min: 1000, closing_to: "2024-12-31" }]);
  });
});
