// Characterization of src/components/ask/answer-parts.tsx as it renders today: citation numbering, every answer
// state, the tables and the evidence pane. Snapshots pin markup and copy; the assertions pin behaviour.

import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { api } from "@/lib/api";
import {
  citationNumbers, citedInOrder, ConflictTable, docLabel, EvidenceBody, EvidenceDetail, FactsTable, firstCited,
  InventoryTable, Markers, MissingTable, NextAction, RequestFooter, Sentences, StateHead,
} from "@/components/ask/answer-parts";
import { answer, DOC_A, DOC_B, evidence, stubApi, view } from "./fixtures";

vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), api: { GET: vi.fn(), POST: vi.fn() } }));

const pair = { coverage: [{ doc_id: DOC_A }, { doc_id: DOC_B }] };
const items = [
  { code: "SFR-001", source_form: "SFR-001", name: "통합 로그인", kind: "detail" as const, evidence_id: "E5",
    location: { format: "pdf", pages: [40] } },
  { code: "PER-001", source_form: "PER_01", name: null, kind: "summary" as const, evidence_id: null,
    location: { table_ordinal: 54 } },
  { code: "SFR-002", source_form: "SFR-002", name: "권한 관리", kind: "detail" as const, evidence_id: "E6",
    location: { section_path: ["4장", "기능"] } },
  { code: "X", source_form: "", name: "기타 항목", kind: "detail" as const, evidence_id: "E1", location: {} },
];
const inventory = { completeness_text: "요구사항 4개를 모두 찾았습니다.", counts: { codes: 4, detail: 3, summary: 1 },
                    items, repeated_detail_codes: ["SFR-001"], summary_only: [] };

describe("citation order", () => {
  test("docLabel names a document only in a comparison", () => {
    expect(docLabel(answer(pair), DOC_B)).toBe("문서 2");
    expect(docLabel(answer(pair), "other")).toBe("");
    expect(docLabel(answer(), DOC_A)).toBe("");
  });

  test("citationNumbers and firstCited number IDs once, in first-cited order", () => {
    expect([...citationNumbers(["b", "a"])]).toEqual([["b", 1], ["a", 2]]);
    expect(firstCited([["b", "a"], ["a", "c"], []])).toEqual(["b", "a", "c"]);
    expect(firstCited([["b", "a"], ["c"]], (id) => id !== "a")).toEqual(["b", "c"]);
  });

  test("citedInOrder walks summary, sentences, conflicts, then requirements by group, keeping known evidence", () => {
    const a = answer({
      conflicts: [{ field: "amount_krw", alternatives: [{ value: 1, doc_id: DOC_A, evidence_ids: ["E4", "E2"] }] }],
      inventory, evidence: { E1: {}, E2: {}, E3: {}, E4: {}, E5: {}, E6: {} },
    });
    // E9 is cited by a sentence but not kept as evidence; SFR rows come before PER and 기타 rows
    expect(citedInOrder(a)).toEqual(["E2", "E1", "E3", "E4", "E5", "E6"]);
    expect(citedInOrder(answer({ inventory: { ...inventory, items: [items[3], items[2], items[0]] },
                                 evidence: { E1: {}, E5: {}, E6: {} }, summary_evidence_ids: [], claims: [] })))
      .toEqual(["E1", "E6", "E5"]);
  });
});

describe("markers and sentences", () => {
  test("openable markers are buttons that report the clicked evidence", () => {
    const onCite = vi.fn();
    const { container } = render(<Markers ids={["E2", "E1", "E2", "E9"]} cite={{ numbers: citationNumbers(["E1", "E2"]), active: "E1", onCite }} />);
    expect(container).toMatchSnapshot();
    fireEvent.click(screen.getByRole("button", { name: "근거 2 원문 보기" }));
    expect(onCite).toHaveBeenCalledWith("E2");
    expect(screen.getByRole("button", { name: "근거 1 원문 보기" }).getAttribute("aria-pressed")).toBe("true");
    expect(container.textContent).toBe("[2][1]");
  });

  test("unvalidated markers are plain numbers, and no known ID renders nothing", () => {
    const { container } = render(<Markers ids={["E1"]} cite={{ numbers: citationNumbers(["E1"]) }} />);
    expect(container).toMatchSnapshot();
    expect(render(<Markers ids={["E9"]} cite={{ numbers: new Map() }} />).container.innerHTML).toBe("");
  });

  test("a comparison groups sentences under 문서 1 and 문서 2 and badges inferences", () => {
    const a = answer({ ...pair, mode: "compare", claims: [
      { text: "A는 12개월", kind: "source_fact", doc_id: DOC_A, evidence_ids: ["E1"] },
      { text: "B는 6개월", kind: "inference", doc_id: DOC_B, evidence_ids: ["E2"] },
      { text: "A는 무상", kind: "source_fact", doc_id: DOC_A, evidence_ids: [] },
    ] });
    const { container } = render(<Sentences claims={a.claims} cite={{ numbers: citationNumbers(["E1", "E2"]) }}
                                            labelOf={(d) => docLabel(a, d)} />);
    expect(container).toMatchSnapshot();
    expect(screen.getAllByRole("heading").map((h) => h.textContent)).toEqual(["문서 1", "문서 2"]);
    expect(render(<Sentences claims={[]} cite={{ numbers: new Map() }} labelOf={() => ""} />).container.innerHTML).toBe("");
  });
});

describe("state head", () => {
  const cite = { numbers: citationNumbers(["E2", "E1"]) };
  test.each([
    ["answered", {}],
    ["insufficient_evidence", { summary: "근거가 부족합니다." }],
    ["technical_error", { summary: "잠시 후 다시 시도하세요.", error: "x".repeat(200) }],
    ["budget_blocked", { summary: "운영 한도에 도달했습니다." }],
    ["clarification_required", { summary: "어느 사업인가요?", next_action: "사업명을 알려 주세요." }],
    ["made_up_status", {}],
  ] as const)("%s", (status, over) => {
    const { container } = render(<StateHead answer={answer({ status, ...over })} large cite={cite} />);
    expect(container).toMatchSnapshot();
  });

  test("technical error shows the first 160 characters of the reason code", () => {
    render(<StateHead answer={answer({ status: "technical_error", error: "y".repeat(200) })} />);
    expect(screen.getByText(/^사유 코드:/).textContent).toBe(`사유 코드: ${"y".repeat(160)}`);
  });
});

describe("tables", () => {
  const facts = [
    { doc_id: DOC_A, field: "title", value: "통합 정보시스템", state: "known" as const, provenance: "csv" as const },
    { doc_id: DOC_A, field: "amount_krw", value: 130000000, state: "zero_review" as const, provenance: "csv" as const },
    { doc_id: DOC_A, field: "bid_close", value: { value: "2024-03-20T10:00:00", precision: "timestamp" },
      state: "resolved" as const, provenance: "resolution" as const },
    { doc_id: DOC_A, field: "institution", value: null, state: "unknown" as const, provenance: "csv" as const },
  ];

  test("facts for one document: value and state columns", () => {
    expect(render(<FactsTable answer={answer({ facts })} />).container).toMatchSnapshot();
  });

  test("facts for two documents: one column each, a missing fact as -", () => {
    const two = [...facts, { doc_id: DOC_B, field: "title", value: "B 사업", state: "conflict" as const, provenance: "csv" as const }];
    expect(render(<FactsTable answer={answer({ facts: two })} />).container).toMatchSnapshot();
  });

  test("conflicts list each alternative, with a document column in a comparison", () => {
    const conflicts = [{ field: "amount_krw", alternatives: [{ value: 100, doc_id: DOC_A, evidence_ids: ["E1"] },
                                                           { value: "200", doc_id: DOC_B, evidence_ids: ["E2"] }] }];
    const cite = { numbers: citationNumbers(["E1", "E2"]) };
    expect(render(<ConflictTable answer={answer({ conflicts })} cite={cite} />).container).toMatchSnapshot();
    expect(render(<ConflictTable answer={answer({ conflicts, ...pair })} cite={cite} />).container).toMatchSnapshot();
  });

  test("missing fields, with a document column in a comparison", () => {
    const missing_fields = [{ field: "bid_close", reason: "not_found_in_context", doc_id: DOC_B },
                            { field: "custom", reason: "custom_reason", doc_id: "other" }];
    expect(render(<MissingTable answer={answer({ missing_fields })} />).container).toMatchSnapshot();
    expect(render(<MissingTable answer={answer({ missing_fields, ...pair })} />).container).toMatchSnapshot();
  });

  test("empty tables render nothing", () => {
    for (const el of [<FactsTable key="f" answer={answer()} />, <MissingTable key="m" answer={answer()} />,
                      <ConflictTable key="c" answer={answer()} cite={{ numbers: new Map() }} />,
                      <InventoryTable key="i" answer={answer()} onCite={() => {}} />]) {
      expect(render(el).container.innerHTML).toBe("");
    }
  });

  test("requirements group by source-form prefix and open their evidence", () => {
    const onCite = vi.fn();
    const { container } = render(<InventoryTable answer={answer({ inventory })} onCite={onCite} />);
    expect(container).toMatchSnapshot();
    fireEvent.click(screen.getByRole("button", { name: "SFR-002 원문 보기" }));
    expect(onCite).toHaveBeenCalledWith("E6");
  });

  test("one group shows no group rows; completeness text hides when the summary carries it", () => {
    const one = { ...inventory, counts: {}, items: [items[0]], repeated_detail_codes: [] };
    const { container } = render(<InventoryTable answer={answer({ inventory: one, summary: "요구사항 4개를 모두 찾았습니다." })} onCite={() => {}} />);
    expect(container).toMatchSnapshot();
  });

  test("next action: warning tone when evidence is short, hidden for clarification", () => {
    expect(render(<NextAction answer={answer({ next_action: "원문을 확인하세요." })} />).container).toMatchSnapshot();
    expect(render(<NextAction answer={answer({ next_action: "원문 확인", status: "insufficient_evidence" })} />).container).toMatchSnapshot();
    expect(render(<NextAction answer={answer({ next_action: "x", status: "clarification_required" })} />).container.innerHTML).toBe("");
    expect(render(<NextAction answer={answer()} />).container.innerHTML).toBe("");
  });

  test("request footer", () => {
    expect(render(<RequestFooter view={view()} />).container.textContent)
      .toBe("요청 req-0000 · 완료 · 정산됨 · 정산 $0.0012");
    expect(render(<RequestFooter view={view({ status: "running", billing_state: "pending", reserved_micro_usd: 50000 })} />).container.textContent)
      .toBe("요청 req-0000 · 처리 중 · 예약 중(최대 비용 보류) · 정산 $0.0012 · 보류 $0.0500");
  });
});

describe("evidence", () => {
  test("numbered quote with warnings, surrounding paragraphs and download", () => {
    const { container } = render(<EvidenceBody ev={evidence({ warnings: ["ocr_text", "custom"] })} number={3} />);
    expect(container).toMatchSnapshot();
    fireEvent.click(screen.getByRole("button", { name: "앞뒤 문단 포함 전체 원문 보기" }));
    expect(screen.getByText("보수 비용은 계약자가 부담한다.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "주변 원문 접기" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("link").getAttribute("href")).toBe("/api/originals/doc-aaaaaaaa/hash-a");
  });

  test("an unnumbered quote without context, a long quote folded", () => {
    const long = evidence({ context: [], quote: "가".repeat(401), download_available: false, title: null,
                            location: { format: "hwp", section_path: ["1장"], table_ordinal: 3 } });
    const { container } = render(<EvidenceBody ev={long} />);
    expect(container).toMatchSnapshot();
    fireEvent.click(screen.getByRole("button", { name: "인용문 일부 표시 · 전체 펼치기" }));
    expect(screen.getByRole("button", { name: "긴 인용문 접기" })).toBeTruthy();
  });

  test("a quote of more than twelve lines is folded too", () => {
    render(<EvidenceBody ev={evidence({ context: [], quote: Array(13).fill("줄").join("\n") })} />);
    expect(screen.getByRole("button", { name: "인용문 일부 표시 · 전체 펼치기" })).toBeTruthy();
  });

  test("EvidenceDetail loads the evidence of its request, or shows the refusal", async () => {
    const calls: unknown[] = [];
    stubApi(api, { "GET /api/requests/{request_id}/evidence/{evidence_id}": (o) => {
      calls.push(o.params?.path);
      return o.params?.path?.evidence_id === "E1" ? evidence() : { error: { detail: "근거를 찾을 수 없습니다." } };
    } });
    expect(render(<EvidenceDetail requestId="r1" evidenceId={null} />).container.innerHTML).toBe("");
    const { container, unmount } = render(<EvidenceDetail requestId="r1" evidenceId="E1" number={1} />);
    expect(container).toMatchSnapshot();  // loading
    await act(async () => {});
    expect(screen.getByRole("heading").textContent).toBe("근거 1 원문 인용");
    unmount();
    render(<EvidenceDetail requestId="r1" evidenceId="E2" />);
    await act(async () => {});
    expect(screen.getByRole("alert").textContent).toBe("근거를 찾을 수 없습니다.");
    expect(calls).toEqual([{ request_id: "r1", evidence_id: "E1" }, { request_id: "r1", evidence_id: "E2" }]);
  });
});
