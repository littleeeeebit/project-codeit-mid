// Characterization of src/components/verify/judges.tsx as it renders today: the replacement verdict in each state,
// the three arms, the judge golden set, the disagreement review and the plan-then-start flow.

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api, type Schemas } from "@/lib/api";
import { JudgeSection } from "@/components/verify/judges";
import { judgeRun, overview, results, stubApi } from "./fixtures";

vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), api: { GET: vi.fn(), POST: vi.fn() } }));

type Row = Schemas["Disagreement"];

const ROWS: Row[] = [
  { blind_id: "b1", kind: "link", reference: "unsupported", korean: { claim: "하자보수는 24개월이다", passages: ["원문 A", "원문 B"] },
    english: { claim: "Warranty is 24 months", passages: ["Source A", "Source B"] },
    arms: { luna: { label: "unsupported", source: "model" }, jev_bridged: { label: "supporting", source: "model", probability: 0.71,
                                                                            reason: "The passage states it." }, jev_raw: null } },
  { blind_id: "b2", kind: "claim", reference: "correct", korean: { question: "사업 금액은?", required: "1억 3천만원", conditions: ["부가세 포함"] },
    english: null, untranslatable: "protected value changed", mutation: { type: "amount", from: "1억 3천만원", to: "1억 원" },
    arms: { luna: { label: "wrong_value", source: "code" }, jev_bridged: { label: null, source: "model", abstain: "untranslatable: x" } } },
  ...Array.from({ length: 13 }, (_, i): Row => ({
    blind_id: `c${i}`, kind: "answer_claim", reference: "supported", korean: { claim: `답변 주장 ${i}` }, english: { claim: `claim ${i}` },
    arms: { luna: { label: "unsupported", source: "model" }, jev_bridged: { label: "supported", source: "model" } } })),
];

let calls: { plan: unknown[]; start: unknown[]; progress: number };

function routes(ov: Schemas["JudgeOverview"], res: Schemas["JudgeResults"] | null, over: Parameters<typeof stubApi>[1] = {}) {
  stubApi(api, {
    "GET /api/verify/judges/progress": () => { calls.progress++; return ov; },
    "GET /api/verify/judges/results/{run_id}": () => res,
    "GET /api/verify/judges/disagreements/{run_id}": () => ROWS,
    "POST /api/verify/judges/plan": (o) => { calls.plan.push(o.body); return estimate(); },
    "POST /api/verify/judges/start": (o) => { calls.start.push(o.body); return {}; },
    ...over,
  });
}

function estimate(over: Partial<Schemas["JudgeEstimate"]> = {}): Schemas["JudgeEstimate"] {
  return {
    estimate_id: "est-1", run_id: "J-held_out-0123456789ab", part: "held_out", items: 120, fits: true, blocked: null,
    translation: { attempts: 4, max_micro_usd: 20000, segments: 37 }, judge: { attempts: 120, max_micro_usd: 400000 },
    jev: { calls: 150, priced: false, note: "" }, max_micro_usd: 420000, envelope_remaining_micro_usd: 3_000_000,
    available_micro_usd: 3_000_000, paid_enabled: true, model: "gpt-6-luna", jev_model: "jev-1.13.0", raw_sample: 30,
    expires_at: "2026-10-07T10:00:00Z", ...over,
  };
}

async function show(ov = overview(), res: Schemas["JudgeResults"] | null = results(), over: Parameters<typeof stubApi>[1] = {}) {
  routes(ov, res, over);
  const out = render(<JudgeSection />);
  await act(async () => {});
  await act(async () => {});
  return out;
}

beforeEach(() => {
  calls = { plan: [], start: [], progress: 0 };
});

describe("the verdict", () => {
  test("replaceable: three conditions met, the arms side by side, the review", async () => {
    const { container } = await show();
    expect(screen.getByRole("heading", { name: "교체 가능" })).toBeTruthy();
    expect(screen.getByText(/미리 정한 세 조건을 모두 충족했습니다\./).textContent)
      .toBe("미리 정한 세 조건을 모두 충족했습니다. 판정 속도는 Luna의 4배입니다.");
    expect(screen.getByText("이 답을 얼마나 믿을 수 있나 · 불통과 기준이 18건뿐")).toBeTruthy();
    expect(container).toMatchSnapshot();
  });

  test("not replaceable marks the failed condition", async () => {
    const r = results();
    r.verdict = { ...r.verdict!, verdict: "not_replaceable",
                  checks: r.verdict!.checks.map((c) => (c.condition === "kappa" ? { ...c, passed: false } : c)) };
    await show(overview(), r);
    expect(screen.getByRole("heading", { name: "교체 불가" })).toBeTruthy();
    const conditions = within(screen.getByRole("region", { name: "교체 불가" }));
    expect(conditions.getAllByText(/[✓✗]/).map((e) => e.textContent)).toEqual(["✗ 미충족", "✓ 충족", "✓ 충족"]);
    expect(screen.getByText("0.762 이상이어야 · Luna 0.812")).toBeTruthy();
  });

  test("inconclusive shows what decided it instead of the conditions", async () => {
    const r = results();
    r.verdict = { ...r.verdict!, verdict: "inconclusive",
                  deciding: [{ condition: "min_judged", detail: "Jev 판정 80개 < 100개", passed: false },
                             { condition: "x", detail: "보류 많음" }] };
    r.arms.jev_bridged = { ...r.arms.jev_bridged, reference_negatives: 40 };
    await show(overview(), r);
    expect(screen.getByRole("heading", { name: "판단 보류" })).toBeTruthy();
    expect(screen.getByText("Jev 판정 80개 < 100개 · 보류 많음")).toBeTruthy();
    expect(screen.getByText("규칙과 기준")).toBeTruthy();
  });

  test("before a verdict: running, unfinished and never run", async () => {
    const running = judgeRun({ running: true, status: "running", progress: { luna: { done: 30, total: 120 } } });
    const { unmount } = await show(overview({ runs: [running] }), results({ arms: {}, verdict: null }));
    expect(screen.getByRole("heading", { name: "평가 실행 중" })).toBeTruthy();
    expect(screen.getAllByText("30/120")).toHaveLength(2);  // the verdict's progress and the run row's
    expect(screen.getAllByText("보정 먼저", { exact: false })).toHaveLength(2);  // 평가 and 골든 세트 wait for calibration
    unmount();

    const partial = judgeRun({ status: "partial", stop_reason: "budget" });
    const second = await show(overview({ runs: [partial] }), results({ arms: {}, verdict: null }));
    expect(screen.getByText("평가 실행이 끝나지 않았습니다. 아래 실행 관리에서 남은 항목을 이어서 실행하세요.")).toBeTruthy();
    expect(screen.getByText(/^중단 사유: budget/)).toBeTruthy();
    second.unmount();

    await show(overview({ runs: [] }), null);
    expect(screen.getByRole("heading", { name: "아직 판정 전" })).toBeTruthy();
    expect(screen.getByText("실행 없음")).toBeTruthy();
    expect(document.querySelector("details[open]")).toBeTruthy();  // 실행 관리 opens while there is no verdict
  });

  test("no reference set, or the progress call refused", async () => {
    const { unmount } = await show(overview({ reference: null }), null);
    expect(screen.getByText("판정 기준 세트가 없습니다. 소유자가 CLI로 judge-reference를 실행해야 합니다.")).toBeTruthy();
    unmount();
    await show(overview(), null, { "GET /api/verify/judges/progress": () => ({ error: { detail: "권한이 없습니다." } }) });
    expect(screen.getByText("권한이 없습니다.")).toBeTruthy();
  });
});

describe("the judge golden set", () => {
  const mutations = {
    luna: { amount: rate(2, 30), date: rate(0, 25), unmutated: rate(19, 20) },
    jev_bridged: { amount: rate(6, 30), date: rate(1, 24), unmutated: rate(18, 20) },
  };
  function rate(passed: number, judged: number): Schemas["MutationRate"] {
    return { passed, judged, items: judged, code_settled: 1, measure: "false_accept", rate: judged ? passed / judged : null,
             wilson95: [0.01, 0.2] };
  }

  test("false accepts per mutation type, overall without unmutated", async () => {
    const run = judgeRun({ run_id: "J-judge_set-0123456789ab", part: "judge_set" });
    const { container } = await show(overview({ runs: [judgeRun(), run] }),
                                     results({ part: "judge_set", mutations, verdict: null, judge_set_sha256: "feedfacecafe" }));
    fireEvent.click(screen.getByRole("button", { name: /판정 골든 세트 · 오답 70 · 정답 20/ }));
    await act(async () => {});
    const answer = within(screen.getByRole("region", { name: "변형 종류별 잘못 통과율" }));
    expect(answer.getAllByRole("definition").map((d) => d.textContent)).toEqual([
      "3.6%", "판정한 오답 55개 중 2개 · 정답 통과 95.0%", "13.0%", "판정한 오답 54개 중 7개 · 정답 통과 90.0%"]);
    expect(answer.getAllByRole("rowheader").map((th) => th.textContent)).toEqual(["금액을 바꿈", "날짜·기간을 옮김", "변형 안 한 정답"]);
    expect(container).toMatchSnapshot();
  });

  test("not generated, or not yet run", async () => {
    const { unmount } = await show(overview({ judge_set: null }), results());
    fireEvent.click(screen.getByRole("button", { name: "판정 골든 세트 · 생성 전" }));
    expect(screen.getByText("판정 골든 세트가 없습니다. 소유자가 CLI로 judge-set을 실행해야 합니다.")).toBeTruthy();
    unmount();
    await show(overview(), results());
    fireEvent.click(screen.getByRole("button", { name: /판정 골든 세트/ }));
    await act(async () => {});
    expect(screen.getByRole("heading", { name: "아직 실행 전" })).toBeTruthy();
    expect(screen.getByText("평가용 기준의 정답 20개를 3가지 방식으로 바꿔 오답 70개를 만들었습니다. 개발용 RAG 세트와 따로 셉니다.")).toBeTruthy();
  });
});

describe("the disagreement review", () => {
  test("opens on false accepts, filters, searches and pages", async () => {
    await show();
    const review = within(screen.getByRole("region", { name: /판정이 엇갈린 15건/ }));
    expect(review.getAllByRole("button", { pressed: false }).concat(review.getAllByRole("button", { pressed: true }))
      .map((b) => b.textContent).sort()).toEqual(["Jev 보류 1", "Jev가 기준과 다름 2", "Luna가 기준과 다름 14", "전체 15",
                                                 "틀린 답을 통과시킴 1"].sort());
    expect(review.getByRole("button", { pressed: true }).textContent).toBe("틀린 답을 통과시킴 1");
    expect(review.getByRole("article")).toMatchSnapshot();

    fireEvent.click(review.getByRole("button", { name: /^전체/ }));
    const list = () => within(review.getByRole("navigation", { name: "엇갈린 항목 목록" }));
    expect(list().getAllByRole("listitem")).toHaveLength(12);
    fireEvent.click(list().getByRole("button", { name: "3건 더 보기 · 남은 3건" }));
    expect(list().getAllByRole("listitem")).toHaveLength(15);

    fireEvent.change(review.getByRole("combobox", { name: "항목 종류" }), { target: { value: "claim" } });
    expect(list().getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "필수 사실 · 1억 3천만원사업 금액은?금액을 바꿈 · 기준 정확 · Luna 값 틀림 · Jev 보류"]);
    expect(review.getByRole("article")).toMatchSnapshot();

    fireEvent.change(review.getByRole("combobox", { name: "항목 종류" }), { target: { value: "all" } });
    fireEvent.change(review.getByRole("textbox", { name: "문장에서 찾기" }), { target: { value: "CLAIM 1" } });
    expect(list().getAllByRole("listitem").map((li) => li.textContent?.split("기준")[0])).toEqual([
      "답변 주장 1", "답변 주장 10", "답변 주장 11", "답변 주장 12"]);
    fireEvent.click(list().getByRole("button", { name: /답변 주장 11/ }));
    expect(review.getByRole("article").textContent).toContain("답변 주장 11");
    fireEvent.change(review.getByRole("textbox", { name: "문장에서 찾기" }), { target: { value: "없는 문장" } });
    expect(review.getByText("조건에 맞는 항목이 없습니다.")).toBeTruthy();
  });
});

describe("plan and start", () => {
  test("estimate, agree, then start the held-out part", async () => {
    await show();
    const ops = within(screen.getByText("실행 관리").closest("details")!);
    expect(ops.getByText("평가 완료 · 보정 완료")).toBeTruthy();  // newest run first
    expect((ops.getByRole("combobox", { name: "실행할 부분" }) as HTMLSelectElement).value).toBe("held_out");
    fireEvent.click(ops.getByRole("button", { name: "비용 추정 무료" }));
    await act(async () => {});
    expect(calls.plan).toEqual([{ part: "held_out" }]);
    expect(ops.getAllByRole("term").map((t) => `${t.textContent}=${t.nextSibling?.textContent}`)).toEqual([
      "항목=120개", "번역 호출=4회 (37조각)", "Luna 판정 호출=120회", "Jev 호출=150회 · 가격 미제공", "최대 비용=$0.4200",
      "judge_eval 남음=$3.0000"]);
    const start = ops.getByRole("button", { name: "평가 실행 · 유료" }) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    fireEvent.click(ops.getByRole("checkbox"));
    const before = calls.progress;
    fireEvent.click(start);
    await act(async () => {});
    expect(calls.start).toEqual([{ estimate_id: "est-1" }]);
    expect(calls.progress).toBe(before + 1);  // the overview reloads
    expect(ops.queryByRole("checkbox")).toBeNull();
  });

  test("before calibration only calibration can run; refusals and finished parts", async () => {
    const calibrating = judgeRun({ run_id: "J-calibration-0123456789ab", part: "calibration", thresholds_fitted: false });
    await show(overview({ runs: [calibrating] }), null, {
      "POST /api/verify/judges/plan": (o) => (o.body as { part: string }).part === "calibration"
        ? estimate({ part: "calibration", fits: false, blocked: "judge_eval 배정 부족", envelope_remaining_micro_usd: null })
        : estimate({ translation: { attempts: 0, max_micro_usd: 0 }, judge: { attempts: 0, max_micro_usd: 0 },
                     jev: { calls: 0, priced: false, note: "" } }),
    });
    const select = screen.getByRole("combobox", { name: "실행할 부분" }) as HTMLSelectElement;
    expect(select.value).toBe("calibration");
    expect([...select.options].map((o) => `${o.textContent}${o.disabled ? " (disabled)" : ""}`)).toEqual([
      "1. 보정 — Jev 임계값 맞춤", "2. 평가 — 화면에 보고 (보정 먼저) (disabled)", "3. 판정 골든 세트 — 변형 오답 (보정 먼저) (disabled)"]);
    fireEvent.click(screen.getByRole("button", { name: "비용 추정 무료" }));
    await act(async () => {});
    expect(screen.getByText("실행할 수 없습니다: judge_eval 배정 부족")).toBeTruthy();
    expect(screen.getByText("배정 안 됨")).toBeTruthy();
    fireEvent.change(select, { target: { value: "held_out" } });
    expect(screen.queryByText(/실행할 수 없습니다/)).toBeNull();  // changing the part drops the estimate
    fireEvent.click(screen.getByRole("button", { name: "비용 추정 무료" }));
    await act(async () => {});
    expect(screen.getByText("이 부분은 모든 항목을 판정했습니다. 다시 실행할 호출이 없습니다.")).toBeTruthy();
  });
});
