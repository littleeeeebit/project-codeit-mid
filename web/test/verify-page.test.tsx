// The 검증 page's menu (groups, labels, the default section and each section's purpose line) and 평가·릴리스: the
// release checks and the development finalists laid out against the pass lines the API serves, and a release
// manifest written before structured checks.

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api, type Schemas } from "@/lib/api";
import { EvaluationSection } from "@/components/verify/evaluation";
import { PURPOSE, VerifyPage } from "@/components/verify/verify-page";
import { stubApi } from "./fixtures";

vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), api: { GET: vi.fn(), POST: vi.fn() } }));

type Evaluation = Schemas["EvaluationOverview"];
type Check = Schemas["ReleaseCheck"];

// claim_correctness is 0.5 here, not the service's 0.9, so a pass at 60% proves the screen reads the API's line
const TARGETS: Schemas["Targets"] = {
  rates: { "single_hit@20": 0.9, "multi_complete@20": 0.8, claim_correctness: 0.5, citation_precision: 0.95, negative_handling: 0.9 },
  latency_ms: { retrieval_p95: 2000, answer_p95: 15000 },
};

function evaluation(over: Partial<Evaluation> = {}): Evaluation {
  return { dev_validation: null, dev_frozen: null, test: { rows: 0, frozen: false, current: false }, answer_runs: [],
           release: null, targets: TARGETS, ...over };
}

const rate = (numerator: number, denominator: number) => ({ numerator, denominator, rate: denominator ? numerator / denominator : null });

function overview(e: Evaluation = evaluation()): Schemas["VerifyOverview"] {
  return { evaluation: e, awaiting_second_review: [], build: "abc123" };
}

const show = async (e: Evaluation) => {
  const out = render(<EvaluationSection ov={overview(e)} onChanged={() => {}} />);
  await act(async () => {});
  return out;
};

/** The metric row whose title (the label before its pass line) is `title`. */
const row = (title: string) => screen.getByText((_, el) => el?.tagName === "P" && el.firstChild?.nodeType === Node.TEXT_NODE
  && el.firstChild.textContent!.trim() === title).closest("li")!;

describe("the 검증 menu", () => {
  beforeEach(() => stubApi(api, {
    "GET /api/verify/overview": () => overview(),
    "GET /api/verify/fidelity": () => [],
    "GET /api/verify/ingestion": () => [],
    "GET /api/verify/traces": () => [],
  }));

  test("groups by the verifier's questions, opens on 할 일, and states each section's purpose", async () => {
    render(<VerifyPage />);
    const select = await screen.findByRole("combobox", { name: "검증 영역 선택" });
    const groups = [...select.querySelectorAll("optgroup")].map((g) =>
      [g.label, [...g.querySelectorAll("option")].map((o) => o.textContent!.replace(/ · .*$/, ""))]);
    expect(groups).toEqual([
      ["내가 처리할 것", ["할 일"]],
      ["기준 통과 여부", ["평가·릴리스", "판정 모델 비교"]],
      ["실패 원인 찾기", ["검색 추적", "추적 비교"]],
      ["운영 · 소유자", ["검색 구성 비교", "유지보수"]],
      ["기록", ["데이터셋", "수집 상태", "수정 기록", "요청 기록"]],
    ]);
    const nav = screen.getByRole("navigation", { name: "검증 영역" });
    expect(within(nav).getByRole("button", { current: "page" }).textContent).toMatch(/^할 일/);
    expect(within(nav).getByRole("button", { name: /평가·릴리스/ }).textContent).toContain("판정 없음");
    expect(screen.getByText(PURPOSE.todo)).toBeTruthy();
    fireEvent.click(within(nav).getByRole("button", { name: "추적 비교" }));
    expect(screen.getByRole("heading", { name: "추적 비교" })).toBeTruthy();
    expect(screen.getByText(PURPOSE.compare)).toBeTruthy();
    expect(PURPOSE.experiments).toBe("검색·임베딩·리랭커 변형을 비교하고 서비스할 구성을 고릅니다 (소유자)");
  });
});

describe("평가·릴리스", () => {
  const checks: Check[] = [
    { key: "critical_wrong", kind: "hard", ok: true, evidence: "0 observed wrong" },
    { key: "admission_cap", kind: "hard", ok: true, evidence: "committed $0.1 of cap $5" },
    { key: "claim_correctness", kind: "quality", ok: true, evidence: "{...}", value: 0.6, denominator: 10 },
    { key: "citation_precision", kind: "quality", ok: false, evidence: "{...}", value: 0.2, denominator: 27, unjudged: 20 },
    { key: "answer_p95", kind: "quality", ok: null, evidence: "no real latency sample" },
  ];

  test("the verdict leads, then each check as value, n, pass line and 충족 / 미달 / 미측정", async () => {
    await show(evaluation({ release: { release_id: "draft-2026-10-07", status: "limited", evidence_label: "development pilot",
                                       generated_at: "2026-10-07T05:00:00Z", reasons: ["x"], checks } }));
    expect(screen.getByRole("heading", { name: "제한적 출시" })).toBeTruthy();
    expect(screen.getByText("기준 미달 1개, 미측정 1개 · 근거가 개발 시범 세트뿐이라 출시 가능으로 볼 수 없습니다.")).toBeTruthy();
    const claims = row("필수 주장 정확도");
    expect(claims.textContent).toContain("기준 50% 이상");  // the API's line, not the service default
    expect(claims.textContent).toContain("60.0%✓ 충족");
    expect(claims.textContent).toContain("n=10");
    const citations = row("인용 정밀도");
    expect(citations.textContent).toContain("20.0%✗ 미달");
    expect(citations.textContent).toContain("미판정 인용 20건");
    expect(citations.textContent).toContain("비관적인 하한");
    expect(row("전체 답변 p95").textContent).toContain("기준 15초 미만");
    expect(row("전체 답변 p95").textContent).toContain("미측정");
    expect((screen.getByText("draft-2026-10-07").closest("details") as HTMLDetailsElement).open).toBe(false);  // IDs stay folded
  });

  test("a manifest without checks still shows its verdict and lists its reasons", async () => {
    await show(evaluation({ release: { release_id: "F-old", status: "blocked", reasons: ["hard check failed: a", "hard check failed: b"], checks: [] } }));
    expect(screen.getByRole("heading", { name: "차단" })).toBeTruthy();
    expect(screen.getByText("기록된 사유 2개가 있습니다.")).toBeTruthy();
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual(["hard check failed: a", "hard check failed: b"]);
  });

  test("the newest development run, each finalist against the same pass lines", async () => {
    const finalist = (over: Partial<Schemas["FinalistScores"]>): Schemas["FinalistScores"] => ({
      mode: "hybrid", completed: 3, of: 3, required_claim_correctness: rate(6, 10), critical_wrong: [], critical_unresolved: [],
      claims_needing_review: 0, citation_precision_lower_bound: rate(0, 27), links_unjudged: 27, negative_handling: rate(0, 0),
      latency_ms: { p95: 27649.8, n: 3 }, ...over,
    });
    const run = (id: string, finalists: Record<string, Schemas["FinalistScores"]>): Schemas["AnswerRun"] => ({
      run_id: id, config: { label: "dev", config_id: id }, progress: { done: 3, total: 3 }, running: false,
      scores: { status: "complete", finalists },
    });
    await show(evaluation({ answer_runs: [  // the service orders runs oldest first
      run("A-older", { "H-1": finalist({}) }),
      run("A-newest", { "H-1": finalist({ critical_wrong: ["c1"] }), "H-2": finalist({ mode: "kiwi_bm25", critical_unresolved: ["c2"] }) }),
    ] }));
    expect(screen.getByText("후보 2 · 키워드(형태소 분석)", { selector: "span" })).toBeTruthy();  // the column head
    const critical = row("치명 오류");
    expect(critical.textContent).toContain("1건✗ 미달");
    expect(critical.textContent).toContain("0건– 미측정");
    expect(critical.textContent).toContain("판정 대기 1건");
    expect(row("필수 주장 정확도").textContent).toContain("6/10");
    expect(row("인용 정밀도").textContent).toContain("미판정 인용 27건");
    expect(row("부정·모호 질문 처리").textContent).toContain("표본 없음");
    expect(row("전체 답변 p95").textContent).toContain("27.6초✗ 미달");
    expect(screen.getByText("실행 정보 · 이전 실행 1개")).toBeTruthy();
  });
});
