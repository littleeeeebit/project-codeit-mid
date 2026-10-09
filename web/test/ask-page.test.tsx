// Characterization of src/components/ask/ask-page.tsx as it behaves today: document selection and the modes it
// offers, what a question sends, the conversation (follow-ups, 새 대화, streaming, cancellation) and the history.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api, type RequestView } from "@/lib/api";
import { AskPage } from "@/components/ask/ask-page";
import { answer, doc, DOC_B, evidence, owned, stubApi, view } from "./fixtures";

vi.mock("@/lib/api", async (original) => ({ ...(await original<object>()), api: { GET: vi.fn(), POST: vi.fn() } }));

const DOCS = [
  doc(),
  doc({ doc_id: DOC_B, source_hash: "hash-b", title: "학사 행정 고도화", institution: null, amount_krw: 52_300_000,
        format: "hwp", review_status: "auto_flagged", conflicts: [{ field: "amount_krw" }, { field: "notice" }],
        resolutions: { notice: {} } }),
  doc({ doc_id: "doc-cccccccc", source_hash: "hash-c", title: "미색인 문서", indexed: false, unavailable_reason: null,
        review_status: "unreviewed" }),
];

type Calls = { ask: unknown[]; abandon: string[]; cancel: string[] };
let calls: Calls;
let status: Record<string, { attachable: boolean; view: RequestView }>;

function routes(over: Record<string, Parameters<typeof stubApi>[1][string]> = {}) {
  let n = 0;
  stubApi(api, {
    "GET /api/documents": () => DOCS,
    "POST /api/ask": (o) => { calls.ask.push(o.body); return owned(++n); },
    "GET /api/requests/{request_id}": (o) => status[o.params!.path!.request_id] ?? {
      attachable: true, view: view({ request_id: o.params!.path!.request_id }),
    },
    "GET /api/requests/{request_id}/evidence/{evidence_id}": (o) => evidence({ evidence_id: o.params!.path!.evidence_id }),
    "POST /api/requests/{request_id}/abandon": (o) => { calls.abandon.push(o.params!.path!.request_id); return {}; },
    "POST /api/requests/{request_id}/cancel": (o) => { calls.cancel.push(o.params!.path!.request_id); return {}; },
    "GET /api/requests": () => [view(), view({ request_id: "req-free-0001", mode: "inventory", question: "", result: null,
                                              status: "failed" })],
    ...over,
  });
}

async function pick(title: string) {
  fireEvent.click(await screen.findByRole("checkbox", { name: new RegExp(title) }));
}

async function ask(question?: string) {
  if (question !== undefined) fireEvent.change(screen.getByLabelText("질문"), { target: { value: question } });
  fireEvent.submit(document.querySelector("form:not([role=search])")!);
  await act(async () => {});
}

const main = () => screen.getByRole("main");

beforeEach(() => {
  calls = { ask: [], abandon: [], cancel: [] };
  status = {};
  routes();
});

describe("selection and modes", () => {
  test("nothing selected: the empty state, and the documents to pick from", async () => {
    const { container } = render(<AskPage />);
    await screen.findByText(/3건 · 질문할 수 있는 문서가 먼저 나옵니다/);
    expect(container).toMatchSnapshot();
  });

  test("one document offers answer, basic information and requirements", async () => {
    render(<AskPage />);
    await pick("통합 정보시스템 구축");
    expect(within(main()).getByRole("radiogroup", { name: "질문 방식" }).textContent)
      .toBe("근거 기반 답변유료기본 정보무료요구사항 목록무료");
    expect(main()).toMatchSnapshot();
    expect(screen.getByRole("button", { name: "답변 받기 · 유료 1회" })).toBeTruthy();
  });

  test("two documents offer comparison; their warnings show on the cards", async () => {
    render(<AskPage />);
    await pick("통합 정보시스템 구축");
    await pick("학사 행정 고도화");
    expect(within(main()).getByRole("radiogroup", { name: "질문 방식" }).textContent).toBe("두 문서 비교유료기본 정보 비교무료");
    expect(within(main()).getAllByRole("listitem").map((li) => li.textContent)).toMatchSnapshot();
    // a third document cannot be picked while two are selected
    expect((screen.getByRole("checkbox", { name: /미색인 문서/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "학사 행정 고도화 선택 해제" }));
    expect(within(main()).getByRole("radiogroup", { name: "질문 방식" }).textContent).toBe("근거 기반 답변유료기본 정보무료요구사항 목록무료");
  });

  test("an unindexed document says so", async () => {
    render(<AskPage />);
    await pick("미색인 문서");
    expect(within(main()).getByText("이 문서는 아직 질문용 색인에 포함되지 않았습니다.")).toBeTruthy();
    expect(within(main()).queryByText(/원문 대조 전입니다/)).toBeNull();
  });

  test("all documents: one paid mode and no selection needed", async () => {
    render(<AskPage />);
    fireEvent.click(await screen.findByRole("radio", { name: "전체 문서" }));
    expect(within(main()).getByRole("radiogroup", { name: "질문 방식" }).textContent).toBe("전체 문서에서 답변유료");
    expect(screen.getByLabelText("질문").getAttribute("placeholder")).toBe("예: ○○기관 ○○ 구축 사업의 하자보수 기간은 얼마인가요?");
    await ask("하자보수 기간은?");
    expect(calls.ask).toEqual([{ scope: [], question: "하자보수 기간은?", mode: "corpus", previous_request_id: "" }]);
  });
});

describe("asking", () => {
  test("a paid question needs text", async () => {
    render(<AskPage />);
    await pick("통합 정보시스템 구축");
    await ask("   ");
    expect(screen.getByRole("alert").textContent).toBe("질문을 입력하세요.");
    expect(calls.ask).toEqual([]);
  });

  test("a refused question shows the service's message", async () => {
    routes({ "POST /api/ask": () => ({ error: { detail: "사용 한도에 도달했습니다." } }) });
    render(<AskPage />);
    await pick("통합 정보시스템 구축");
    await ask("하자보수 기간은?");
    expect(screen.getByRole("alert").textContent).toBe("사용 한도에 도달했습니다.");
  });

  test("an answer, its first citation opened, then a follow-up and 새 대화", async () => {
    render(<AskPage />);
    await pick("통합 정보시스템 구축");
    await ask(" 하자보수 기간은? ");
    expect(calls.ask).toEqual([{ scope: [{ doc_id: "doc-aaaaaaaa", source_hash: "hash-a" }], question: "하자보수 기간은?",
                                 mode: "single", previous_request_id: "" }]);
    await screen.findByRole("heading", { level: 3, name: "근거 1 원문 인용" });
    expect(main()).toMatchSnapshot();
    expect(screen.getByRole("button", { name: "이어서 질문 · 유료 2회" })).toBeTruthy();
    expect(screen.getByLabelText("질문").getAttribute("placeholder")).toBe("이어서 질문하세요");

    // E1 is cited by the summary and by a sentence: both markers read 2 and both show as open
    fireEvent.click(screen.getAllByRole("button", { name: "근거 2 원문 보기" })[0]);
    expect(screen.getByText("질문 1의 근거")).toBeTruthy();
    await screen.findByRole("heading", { level: 3, name: "근거 2 원문 인용" });
    expect(screen.getAllByRole("button", { name: "근거 2 원문 보기" }).map((b) => b.getAttribute("aria-pressed")))
      .toEqual(["true", "true"]);

    await ask("비용은 누가 내나요?");
    expect(calls.ask[1]).toEqual({ scope: [{ doc_id: "doc-aaaaaaaa", source_hash: "hash-a" }],
                                   question: "비용은 누가 내나요?", mode: "single", previous_request_id: "req-00000001-abcd" });
    await waitFor(() => expect(screen.getByText(/질문 2개/)).toBeTruthy());
    expect(screen.getAllByRole("article").map((a) => a.getAttribute("aria-label"))).toEqual(["질문 1", "질문 2"]);

    fireEvent.click(screen.getByRole("button", { name: "새 대화" }));
    await act(async () => {});
    expect(calls.abandon).toEqual(["req-00000002-abcd"]);
    expect(screen.queryAllByRole("article")).toEqual([]);
  });

  test("a free question sends no text and is labelled by its mode", async () => {
    status["req-00000001-abcd"] = { attachable: true, view: view({ mode: "metadata", result: answer({
      mode: "metadata", summary: "기본 정보입니다.", summary_evidence_ids: [], claims: [], evidence: {},
      facts: [{ doc_id: "doc-aaaaaaaa", field: "title", value: "통합 정보시스템 구축", state: "known", provenance: "csv" }],
    }) }) };
    render(<AskPage />);
    await pick("통합 정보시스템 구축");
    fireEvent.click(screen.getByText("기본 정보"));
    expect(screen.queryByLabelText("질문")).toBeNull();
    expect(screen.getByRole("button", { name: "기본 정보 보기" })).toBeTruthy();
    await ask();
    expect(calls.ask).toEqual([{ scope: [{ doc_id: "doc-aaaaaaaa", source_hash: "hash-a" }], question: "",
                                 mode: "metadata", previous_request_id: "" }]);
    await screen.findByText("기본 정보입니다.");
    expect(within(screen.getByRole("article")).getAllByText("기본 정보")[0].tagName).toBe("P");
    expect(screen.getByText("답변 문장 끝의 번호를 누르면 원문 인용과 앞뒤 문단, 원문 파일이 여기에 열립니다.")).toBeTruthy();
  });

  test("leaving the page abandons the latest turn", async () => {
    const { unmount } = render(<AskPage />);
    await pick("통합 정보시스템 구축");
    await ask("하자보수 기간은?");
    unmount();
    await act(async () => {});
    expect(calls.abandon).toEqual(["req-00000001-abcd"]);
  });
});

describe("a running turn", () => {
  const running = (over: Partial<RequestView> = {}) =>
    view({ status: "running", result: null, reserved_micro_usd: 25000, settled_micro_usd: 0, ...over });

  test("streams a provisional answer and can be cancelled", async () => {
    status["req-00000001-abcd"] = { attachable: true, view: running() };
    const streamed = { summary: "작성 중인 요약", summary_evidence_ids: ["E7"],
                       claims: [{ text: "첫 문장", kind: "source_fact", doc_id: "doc-aaaaaaaa", evidence_ids: ["E7", "E8"] }] };
    const fetch = vi.fn(async () => new Response(`data: ${JSON.stringify(streamed)}\n\n`));
    vi.stubGlobal("fetch", fetch);
    try {
      render(<AskPage />);
      await pick("통합 정보시스템 구축");
      await ask("하자보수 기간은?");
      await screen.findByText("작성 중인 요약");
      expect(fetch).toHaveBeenCalledWith("/api/requests/req-00000001-abcd/stream?generation_id=gen-1",
                                         expect.objectContaining({ headers: {} }));
      expect(screen.getByRole("article")).toMatchSnapshot();
      expect(screen.getByRole("button", { name: "이어서 질문 · 유료 2회" }).hasAttribute("disabled")).toBe(true);
      expect(screen.getByLabelText("질문").getAttribute("placeholder")).toBe("답변이 끝나면 이어서 질문할 수 있습니다.");
      fireEvent.click(screen.getByRole("button", { name: "요청 취소" }));
      await act(async () => {});
      expect(calls.cancel).toEqual(["req-00000001-abcd"]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  test("a revoked attachment says the result will not be shown", async () => {
    status["req-00000001-abcd"] = { attachable: false, view: running({ cancel_requested: true }) };
    render(<AskPage />);
    await pick("통합 정보시스템 구축");
    await ask("하자보수 기간은?");
    await screen.findByText("취소 요청됨");
    expect(screen.getByRole("article")).toMatchSnapshot();
  });

  test("a finished request that cannot attach points to the history", async () => {
    status["req-00000001-abcd"] = { attachable: false, view: view({ status: "cancelled" }) };
    render(<AskPage />);
    await pick("통합 정보시스템 구축");
    await ask("하자보수 기간은?");
    expect((await screen.findByText(/이 요청의 결과는 답변으로 표시하지 않습니다/)).textContent)
      .toBe("취소됨 · 이 요청의 결과는 답변으로 표시하지 않습니다. 아래 ‘내 최근 요청’에서 볼 수 있습니다.");
  });
});

test("history lists my recent requests and opens one", async () => {
  render(<AskPage />);
  fireEvent.click(await screen.findByRole("button", { name: /내 최근 요청/ }));
  const first = await screen.findByRole("button", { name: /하자보수 기간은\?/ });
  expect(screen.getAllByRole("button", { name: /요청 req-/ }).map((b) => b.textContent)).toEqual([
    "답변하자보수 기간은?요청 req-0000 · 2026-10-07 09:30", "실패요구사항 목록요청 req-free · 2026-10-07 09:30"]);
  fireEvent.click(first);
  await screen.findByText("하자보수 기간은 12개월입니다.");
  expect(first.getAttribute("aria-expanded")).toBe("true");
});
