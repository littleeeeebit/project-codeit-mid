// Characterization of the verify screens' longest components as they render today: the experiment matrix (sorting
// and an opened row), the fidelity detail, the second review, the maintenance run and the retrieval trace form and run.

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api, type Schemas } from "@/lib/api";
import { ExperimentsSection } from "@/components/verify/experiments";
import { FidelityDetail, type Source } from "@/components/verify/fidelity";
import { SecondReviewDetail, type Waiting } from "@/components/verify/gold";
import { MaintenanceSection } from "@/components/verify/maintenance";
import { CompareRuns, RunView, TraceForm } from "@/components/verify/trace";
import { doc, DOC_A, DOC_B, stubApi } from "./fixtures";

vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), api: { GET: vi.fn(), POST: vi.fn() } }));

const settle = () => act(async () => {});

type Row = Schemas["ExperimentRow"];

function row(index: number, over: Partial<Row> = {}): Row {
  return {
    index, name: `row-${index}`, label: null, status: "complete", active: false, failures: 0, run_id: `R-${index}`,
    reason: null, estimate: null, facts: { licence: "MIT", size_bytes: 2 ** 31 }, axes: { analyzer: "kiwi", retrieval: "hybrid" },
    values: { "dev.ndcg": 0.8, "dev.support": 0.5, "dev.p95_ms": 120, cost_usd: 0 }, ...over,
  };
}

const TABLE: Schemas["ExperimentTable"] = {
  matrix: "lexical", title: "lexical", created_at: "2026-10-01T00:00:00Z", populations: {}, needs_evidence_review: ["q7"],
  fixed: { embedding: "m3", fusion: "keyword_first:60" },
  columns: [{ key: "dev.ndcg", label: "ndcg", better: "high" }, { key: "dev.support", label: "support", better: "high" },
            { key: "dev.p95_ms", label: "p95", better: "low" }, { key: "cost_usd", label: "cost", better: null }],
  rows: [
    row(0, { axes: { analyzer: "whitespace", retrieval: "keyword" }, values: { "dev.ndcg": 0.61, "dev.support": 0.4, "dev.p95_ms": 80, cost_usd: 0 } }),
    row(1, { active: true, values: { "dev.ndcg": 0.83, "dev.support": 0.55, "dev.p95_ms": 140, cost_usd: 0.0123 } }),
    row(2, { axes: { analyzer: "kiwi", retrieval: "dense" }, status: "needs_approval", run_id: null, values: {},
             estimate: { total_micro_usd: 42000, corpus_tokens: 120000 }, reason: "승인 필요" }),
  ],
};

const QUESTIONS: Schemas["ExperimentQuestion"][] = [
  { id: "q1", population: "dev", question: "하자보수 기간은?", complete: false, critical: true, fallback: null, hit5: 0,
    missing: ["g1"], ndcg: 0.5, qualifier_loss: 1, type: "fact" },
  { id: "q2", population: "whole", question: null, complete: true, critical: false, fallback: null, hit5: 1, missing: [],
    ndcg: 0.9, qualifier_loss: 0, type: "fact" },
];

describe("experiments", () => {
  beforeEach(() => stubApi(api, {
    "GET /api/verify/experiments": () => ({
      active_run_id: "R-1", golden_counts: null, serving: "hybrid", tables: [TABLE],
      serving_detail: { mode: "hybrid", embedding: "m3", reranker: null, protect: 0, fallback: null },
    }),
    "GET /api/verify/experiments/{matrix}/{index}/questions": () => QUESTIONS,
  }));

  test("the matrix as it opens", async () => {
    render(<ExperimentsSection />);
    expect(await screen.findByRole("region", { name: "K0 · K1 비교표" })).toMatchSnapshot();
  });

  test("sorting by a column and opening a row", async () => {
    render(<ExperimentsSection />);
    const table = await screen.findByRole("table");
    fireEvent.click(within(table).getByRole("button", { name: /검색 p95/ }));
    expect([...table.querySelectorAll("tbody th")].map((th) => th.textContent)).toEqual([
      "K0 공백 분리 · 키워드", "K1 Kiwi 형태소 · 하이브리드서비스 중", "K1 Kiwi 형태소 · 밀집만"]);
    fireEvent.click(within(table).getByRole("button", { name: /검색 p95/ }));
    expect(within(table).getAllByRole("columnheader")[3].getAttribute("aria-sort")).toBe("descending");
    fireEvent.click(within(table).getByRole("button", { name: /K0 공백 분리/ }));
    await settle();
    expect(screen.getByRole("region", { name: "K0 공백 분리 · 키워드" })).toMatchSnapshot();
  });
});

const SOURCE: Source = {
  source_hash: "hash-a", filename: "통합 정보시스템.hwp", review_status: "auto_flagged",
  metrics: { pages: 12, extraction_unmatched: 40, rendering_unmatched: 1234, image_pages: [3, 4], extracted_chars: 9000, rendered_chars: 9100 },
  findings: [
    { page: 3, side: "rendering", stretch: "누락 문구", stretches: [], text: null, digits: ["12"], unmatched_chars: 30 },
    { page: null, side: "extraction", stretches: ["가", "나"], text: "추출 원문 문단", digits: [], unmatched_chars: 8 },
  ],
};

describe("fidelity detail", () => {
  test("the first finding, the print toggle, then the second finding", () => {
    const { container } = render(<FidelityDetail s={SOURCE} onConfirmed={() => {}} />);
    expect(container).toMatchSnapshot("first finding");
    fireEvent.click(screen.getByRole("button", { name: "인쇄본 쪽 보기" }));
    expect(screen.getByRole("img").getAttribute("src")).toBe("/api/verify/fidelity/hash-a/pages/3");
    fireEvent.click(screen.getByRole("button", { name: "불일치 2 · 쪽 미상" }));
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("region", { name: "불일치 검토" })).toMatchSnapshot("second finding");
  });

  test("confirming sends the note", async () => {
    const sent: unknown[] = [];
    const confirmed = vi.fn();
    stubApi(api, { "POST /api/verify/fidelity/{source_hash}/confirm": (o) => { sent.push([o.params?.path, o.body]); return {}; } });
    render(<FidelityDetail s={SOURCE} onConfirmed={confirmed} />);
    fireEvent.change(screen.getByLabelText("메모 (선택)"), { target: { value: "3쪽 확인" } });
    fireEvent.submit(screen.getByRole("button", { name: "표시된 곳을 모두 원문과 대조했습니다" }).closest("form")!);
    await settle();
    expect(sent).toEqual([[{ source_hash: "hash-a" }, { note: "3쪽 확인" }]]);
    expect(confirmed).toHaveBeenCalledOnce();
    expect(screen.getByText("표본 대조 완료로 기록했습니다.")).toBeTruthy();
  });

  test("before the automatic comparison", () => {
    const { container } = render(<FidelityDetail s={{ ...SOURCE, metrics: null, findings: [], review_status: "unreviewed" }}
                                                 onConfirmed={() => {}} />);
    expect(container).toMatchSnapshot();
  });
});

const WAITING: Waiting = {
  candidate_id: "dev-amount-r1", question: "사업 금액은 얼마인가요?",
  required_claims: [{ claim_id: "c1", match: { type: "number", value: 130000000, unit: "원" }, critical_kind: "amount",
                      qualifiers: [], support_groups: ["g1", "g2"] },
                    { claim_id: null, match: { type: "text", patterns: "부가세 포함" }, critical_kind: null, qualifiers: [],
                      support_groups: ["g2"] }],
  evidence_groups: [{ group_id: "g1", doc_id: DOC_A, alternatives: [{ quote: "사업 금액 1억 3천만원" }, { quote: null }] } as never],
};

describe("second review", () => {
  test("renders the claims and evidence, refuses without a verdict and saves one", async () => {
    const sent: unknown[] = [];
    const done = vi.fn();
    stubApi(api, { "POST /api/gold/{candidate_id}/second-review": (o) => { sent.push([o.params?.path, o.body]); return {}; } });
    const { container } = render(<SecondReviewDetail w={WAITING} onDone={done} />);
    expect(container).toMatchSnapshot();
    const form = screen.getByRole("button", { name: "2차 검토 저장" }).closest("form")!;
    fireEvent.submit(form);
    expect(screen.getByText("원문과 대조한 결과를 고르세요.")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("동의하지 않음"));
    fireEvent.change(screen.getByLabelText("확인 내용 (원문 위치 포함)"), { target: { value: "12쪽 표" } });
    fireEvent.submit(form);
    await settle();
    expect(sent).toEqual([[{ candidate_id: "dev-amount-r1" }, { agreed: false, note: "12쪽 표" }]]);
    expect(done).toHaveBeenCalledOnce();
  });
});

type Step = Schemas["MaintenanceStep"];
const step = (name: Step["name"], status: Step["status"], detail: Step["detail"] = {}, reason?: string): Step =>
  ({ name, status, detail, reason });

describe("maintenance", () => {
  test("a run stopped at the paid embedding, with its regression rows", async () => {
    stubApi(api, { "GET /api/verify/maintenance": () => ({
      backup_root: "D:/backups", running: false,
      run: {
        run_id: "M-1", actor: "owner", started_at: "2026-10-06T09:00:00Z", status: "needs_approval", stopped_at: "embedding",
        reason: "비용 승인 필요", provider_calls: 0, serving: { unchanged: true },
        steps: [
          step("backup", "reused", { taken_at: "2026-10-05T23:00:00Z" }), step("restore_check", "done", { checks: 9 }),
          step("ingest", "done", { documents: 100, changed: [{ filename: "a.hwp" }, { filename: "b.pdf" }, { filename: "c.pdf" }, { filename: "d.hwp" }] }),
          step("fidelity", "done", { checked: 2 }), step("keyword", "done", { index_version: "ix-0123456789abcdef", served_index_version: "ix-fedcba9876543210" }),
          step("embedding", "needs_approval", { estimate: { estimate_id: "E-9", total_micro_usd: 123456 } }),
          step("regression", "done", { rows: [{ name: "K1", dev_ndcg: 0.8123, dev_support: 0.5, whole_ndcg: null, needle_top5: 0.75, dev_critical: 2 }],
                                       needs_evidence_review: ["q1"] }),
          step("report", "pending"),
        ],
      },
    }) });
    const { container } = render(<MaintenanceSection />);
    await settle();
    expect(container).toMatchSnapshot();
  });

  test("before any run, and starting one", async () => {
    let started = 0;
    stubApi(api, {
      "GET /api/verify/maintenance": () => ({ backup_root: "D:/backups", running: false, run: null }),
      "POST /api/verify/maintenance/start": () => { started++; return { error: { detail: "이미 실행 중" } }; },
    });
    const { container } = render(<MaintenanceSection />);
    await settle();
    expect(container).toMatchSnapshot();
    fireEvent.click(screen.getByRole("button", { name: "유지보수 실행" }));
    await settle();
    expect(started).toBe(1);
    expect(screen.getByText("이미 실행 중")).toBeTruthy();
  });

  const live = (over: Partial<Schemas["MaintenanceRun"]> = {}): Schemas["MaintenanceRun"] => ({
    run_id: "M-2", actor: "owner", started_at: "2026-10-11T09:00:00Z", status: "running", timed_out: [],
    steps: [step("backup", "reused"), step("restore_check", "reused"),
            step("ingest", "running", { progress: { done: 59, total: 98, current: "케빈랩.hwp", current_started_at: "2026-10-11T09:01:30Z" } }),
            step("fidelity", "pending"), step("keyword", "pending"), step("embedding", "pending"), step("regression", "pending"), step("report", "pending")],
    ...over,
  });

  test("a live run shows n/total, the file being read, both elapsed times, and cancels", async () => {
    vi.useFakeTimers({ now: Date.parse("2026-10-11T09:02:05Z"), toFake: ["Date"] });
    let cancels = 0;
    stubApi(api, {
      "GET /api/verify/maintenance": () => ({ backup_root: "D:/backups", running: true, run: live() }),
      "POST /api/verify/maintenance/cancel": () => { cancels++; return { run_id: "M-2" }; },
    });
    render(<MaintenanceSection />);
    await settle();
    expect(screen.getByText("2:05 경과")).toBeTruthy();
    expect(screen.getByText("60 / 98 읽는 중 · 케빈랩.hwp · 이 파일 0:35")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "취소" }));
    await settle();
    expect(cancels).toBe(1);
    vi.useRealTimers();
  });

  test("a cancelled run names its step and who cancelled; timed-out originals are listed", async () => {
    const steps = live().steps.map((s) => (s.name === "ingest" ? { ...s, status: "cancelled" as const, reason: "cancelled by owner" } : s));
    stubApi(api, { "GET /api/verify/maintenance": () => ({ backup_root: "D:/backups", running: false, run: live({
      status: "cancelled", stopped_at: "ingest", cancelled_by: "owner", cancelled_at: "2026-10-11T09:03:00Z", steps, timed_out: ["느린.hwp"] }) }) });
    render(<MaintenanceSection />);
    await settle();
    expect(screen.getByRole("heading", { name: "매니페스트·수집에서 취소됨" })).toBeTruthy();
    expect(screen.getByText(/owner이\(가\) 2026-10-11 09:03에 취소했습니다/)).toBeTruthy();
    expect(screen.getByText("59 / 98까지 읽고 멈춤 · 케빈랩.hwp 읽던 중 취소")).toBeTruthy();
    expect(screen.getByText(/다음 실행에서 다시 읽습니다: 느린.hwp/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "취소" })).toBeNull();
  });

  test("a run with a timed-out original ends unverified and says so once, in Korean", async () => {
    const steps = live().steps.map((s) => ({ ...s, status: "done" as const,
      detail: s.name === "ingest" ? { documents: 98, changed: [], timed_out: ["느린.hwp"], time_limit_seconds: 120 } : {} }));
    stubApi(api, { "GET /api/verify/maintenance": () => ({ backup_root: "D:/backups", running: false, run: live({
      status: "unverified", reason: "1 original(s) did not finish parsing within 120 s", steps, timed_out: ["느린.hwp"] }) }) });
    render(<MaintenanceSection />);
    await settle();
    expect(screen.getByText(/시간 제한 안에 읽지 못한 원문 1개가 이전 추출로 남았습니다/)).toBeTruthy();
    expect(screen.queryByText(/did not finish parsing/)).toBeNull();
    expect(screen.getByText(/120초 안에 못 읽은 1개는 이전 추출 유지$/)).toBeTruthy();
  });
});

const SOURCES: Schemas["TraceSources"] = {
  modes: ["kiwi_bm25", "hybrid"],
  documents: [doc(), doc({ doc_id: DOC_B, source_hash: "hash-b", title: "학사 행정 고도화", institution: "서울대학교" }),
              doc({ doc_id: "doc-c", source_hash: "hash-c", title: "도서관 좌석 예약", institution: null })],
  questions: [{ id: "dev-1", question_id: null, question: "하자보수 기간은 얼마인가요?", scope: [{ doc_id: DOC_A }], doc_ids: [DOC_B],
                doc_id: null, as_of_date: "2024-05-01" }],
};

describe("trace form", () => {
  test("a dev question fills the form, documents are found and removed, and the trace is sent", async () => {
    const sent: unknown[] = [];
    const onRun = vi.fn();
    stubApi(api, {
      "GET /api/verify/trace-sources": () => SOURCES,
      "POST /api/verify/traces": (o) => { sent.push(o.body); return { run_id: "T-1" }; },
    });
    const { container } = render(<TraceForm onRun={onRun} />);
    await settle();
    fireEvent.change(screen.getByLabelText("검토된 개발 질문에서 가져오기 (선택)"), { target: { value: "0" } });
    expect(container).toMatchSnapshot("from a dev question");
    fireEvent.click(screen.getByRole("button", { name: "학사 행정 고도화 빼기" }));
    fireEvent.change(screen.getByLabelText(/^문서/), { target: { value: "도서관" } });
    expect(screen.getByRole("button", { name: "도서관 좌석 예약" })).toMatchSnapshot("a match");
    fireEvent.click(screen.getByRole("button", { name: "도서관 좌석 예약" }));
    fireEvent.change(screen.getByLabelText("검색 방식"), { target: { value: "hybrid" } });
    fireEvent.submit(container.querySelector("form")!);
    await settle();
    expect(sent).toEqual([{ question: "하자보수 기간은 얼마인가요?", mode: "hybrid", as_of: "2024-05-01",
                            scope: [{ doc_id: DOC_A, source_hash: "hash-a" }, { doc_id: "doc-c", source_hash: "hash-c" }] }]);
    expect(onRun).toHaveBeenCalledWith("T-1");
  });

  test("an empty form is refused before anything is sent", async () => {
    stubApi(api, { "GET /api/verify/trace-sources": () => SOURCES });
    const { container } = render(<TraceForm onRun={() => {}} />);
    await settle();
    fireEvent.submit(container.querySelector("form")!);
    expect(screen.getByText("질문을 적고 문서를 하나 이상 고르세요.")).toBeTruthy();
  });
});

const RUN: Schemas["TraceRun"] = {
  run_id: "T-1", question: "하자보수 기간은?", member_id: "verifier", created_at: "2026-10-06T10:11:12Z", as_of: "2024-05-01",
  scope: [{ doc_id: DOC_A }, { doc_id: DOC_B }], codes: ["SFR-001"], query_tokens: ["하자", "보수", "기간"],
  review_scope: "includes_unreviewed", index_version: "ix-1", input_tokens: 900, estimate_micro_usd: 3100, generation_block: null,
  config: { config_id: "cfg-1", mode: "hybrid", model: "m", prompt_version: "p", serving_mode: "hybrid", effective_limits: {} },
  coverage: [{ doc_id: DOC_A, evidence: 2 }, { doc_id: DOC_B, evidence: 0, limitation: "근거 없음" }],
  docs: [{ doc_id: DOC_A, filename: "a.pdf", parse_status: "parsed", review_status: "auto_verified" },
         { doc_id: DOC_B, filename: null, parse_status: "quarantined", review_status: "unreviewed", reason_code: "broken" }],
  retrieval: {
    mode: "kiwi_bm25", fallback: "hybrid->kiwi_bm25:query_vector_unavailable", evidence_tokens: 40, ranking: [], timings_ms: { total: 37 },
    limitations: [`${DOC_A.slice(0, 8)}:code_not_found:SFR-001`, "idf:whole_index", "scope_not_indexed:doc-9",
                  `${DOC_A.slice(0, 8)}:linked_evidence_missing:chunk-77:token_budget`],
    candidates: [{ channel: "bm25", rank: 1, score: 7.5, chunk_id: "chunk-0123456789abcdef" },
                 { channel: "dense", rank: 2, score: null, chunk_id: "chunk-fedcba9876543210" }],
    excluded: [{ chunk_id: "chunk-aaaaaaaaaaaaaaaa", reason: "token_budget" }],
    evidence: [{ evidence_id: "E1", doc_id: DOC_A, source_hash: "hash-a", extraction_id: "x1", chunk_id: "chunk-0123456789abcdef",
                 element_ids: ["e1", "e2"], quote: "하자보수 기간은 12개월로 한다.", token_count: 20,
                 location: { format: "pdf", pages: [12], section_path: ["제3장"] } }],
  },
};

describe("trace fallback", () => {
  const stage3 = async (fallback: string) => {
    stubApi(api, { "GET /api/verify/traces/{run_id}": () => ({ ...RUN, retrieval: { ...RUN.retrieval, fallback } }) });
    render(<RunView runId="T-1" />);
    await settle();
    fireEvent.click(within(screen.getByRole("list", { name: "검색 단계" })).getByRole("button", { name: /채널 순위/ }));
    return screen.getByText("대체 실행").nextElementSibling!;
  };

  test("a reranker failure keeps its exception type and message", async () => {
    const shown = await stage3("hybrid_rerank->hybrid:reranker_error:RuntimeError: CUDA out of memory");
    expect(shown.textContent).toBe("키워드+의미 결합 후 재정렬 → 키워드+의미 결합 · 재정렬 모델 오류: RuntimeError: CUDA out of memory");
    expect(shown.getAttribute("title")).toBe("hybrid_rerank->hybrid:reranker_error:RuntimeError: CUDA out of memory");
  });

  test("a fallback in no known shape is shown as written", async () => {
    expect((await stage3("query vector not cached")).textContent).toBe("query vector not cached");
  });
});

describe("trace comparison", () => {
  const SUMMARIES = [{ run_id: "T-2", member_id: "v", config_id: "cfg-1", question: "하자보수 기간은?", created_at: "2026-10-06T11:00:00Z" },
                     { run_id: "T-1", member_id: "v", config_id: "cfg-1", question: "하자보수 기간은?", created_at: "2026-10-06T10:00:00Z" }];
  const PACKED = "chunk-0123456789abcdef";
  const comparison = () => ({ same_question: true, same_scope: true, config_changes: {}, evidence_only_a: [], evidence_only_b: [],
    evidence_common: ["x1:e1,e2"], top5_a: [PACKED], top5_b: [PACKED], evidence_tokens: [40, 40], input_tokens: [900, 900], total_ms: [37, 37] });
  const rankOne = () => within(screen.getByRole("table", { name: "상위 5개 순위" })).getAllByRole("row")[1];

  test("a ranked chunk keeps its ID while the runs load, then reads as its place", async () => {
    let release = () => {};
    const loaded = new Promise<void>((r) => { release = r; });
    stubApi(api, { "GET /api/verify/compare": comparison,
                   "GET /api/verify/traces/{run_id}": async (o) => { await loaded; return { ...RUN, run_id: String(o.params?.path?.run_id) }; } });
    render(<CompareRuns runs={SUMMARIES} />);
    await settle();
    expect(rankOne().textContent).toBe(`1${PACKED.slice(0, 12)}${PACKED.slice(0, 12)}`);
    expect(screen.queryByText(/근거로 (담기지 않은|안 담음)/)).toBeNull();
    release();
    await settle();
    expect(rankOne().textContent).toBe("1E1 · 문서 1 · 12쪽E1 · 문서 1 · 12쪽");
  });

  test("after switching runs, a failed load never shows the previous run's evidence", async () => {
    const runs = [...SUMMARIES, { ...SUMMARIES[1], run_id: "T-3", created_at: "2026-10-06T09:00:00Z" }];
    stubApi(api, { "GET /api/verify/compare": comparison,
                   "GET /api/verify/traces/{run_id}": (o) => o.params?.path?.run_id === "T-3" ? { error: { detail: "실행을 찾을 수 없습니다." } } : RUN });
    render(<CompareRuns runs={runs} />);
    await settle();
    expect(rankOne().textContent).toBe("1E1 · 문서 1 · 12쪽E1 · 문서 1 · 12쪽");  // A is T-1, which packed the chunk
    fireEvent.change(screen.getByLabelText("실행 A"), { target: { value: "T-3" } });
    await settle();
    expect(screen.getByText(/실행 A 정보를 불러오지 못해/)).toBeTruthy();
    expect(rankOne().textContent).toBe(`1${PACKED.slice(0, 12)}E1 · 문서 1 · 12쪽`);
  });

  test("a comparison that fails after switching runs shows no table from the previous pair", async () => {
    const runs = [...SUMMARIES, { ...SUMMARIES[1], run_id: "T-3", created_at: "2026-10-06T09:00:00Z" }];
    stubApi(api, { "GET /api/verify/compare": (o) => o.params?.query?.a === "T-3" ? { error: { detail: "비교할 수 없습니다." } } : comparison(),
                   "GET /api/verify/traces/{run_id}": () => RUN });
    render(<CompareRuns runs={runs} />);
    await settle();
    fireEvent.change(screen.getByLabelText("실행 A"), { target: { value: "T-3" } });
    await settle();
    expect(screen.getByText("비교할 수 없습니다.")).toBeTruthy();
    expect(screen.queryByRole("table", { name: "상위 5개 순위" })).toBeNull();
  });

  test("a run that fails to load says so and never reads as unpacked", async () => {
    stubApi(api, { "GET /api/verify/compare": comparison,
                   "GET /api/verify/traces/{run_id}": (o) => o.params?.path?.run_id === "T-1" ? { error: { detail: "실행을 찾을 수 없습니다." } } : RUN });
    render(<CompareRuns runs={SUMMARIES} />);
    await settle();
    expect(screen.getByText(/실행 A 정보를 불러오지 못해/)).toBeTruthy();
    expect(rankOne().textContent).toBe(`1${PACKED.slice(0, 12)}E1 · 문서 1 · 12쪽`);
    expect(screen.queryByText(/근거로 (담기지 않은|안 담음)/)).toBeNull();
  });
});

describe("trace run", () => {
  test("every stage of a frozen run", async () => {
    stubApi(api, { "GET /api/verify/traces/{run_id}": () => RUN });
    const { container } = render(<RunView runId="T-1" />);
    await settle();
    const article = container.querySelector("article")!;
    expect(article).toMatchSnapshot("stage 4");
    for (const [n, name] of [[1, "수집·검수"], [2, "범위·분석"], [3, "채널 순위"]] as const) {
      fireEvent.click(within(screen.getByRole("list", { name: "검색 단계" })).getByRole("button", { name: new RegExp(name) }));
      expect(article.querySelector(":scope > div.space-y-2")).toMatchSnapshot(`stage ${n}`);
    }
  });
});
