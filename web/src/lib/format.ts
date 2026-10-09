// Korean labels and value formatting shared by every screen. Only wording lives here; every decision about
// what may be shown is made by the service.

export type Tone = "ok" | "info" | "warn" | "bad" | "neutral";

export const STATUS: Record<string, { label: string; tone: Tone }> = {
  answered: { label: "답변", tone: "ok" },
  insufficient_evidence: { label: "근거 부족", tone: "warn" },
  clarification_required: { label: "확인 필요", tone: "info" },
  conflicting_evidence: { label: "근거 충돌", tone: "warn" },
  ingestion_unavailable: { label: "원문 미수집", tone: "neutral" },
  budget_blocked: { label: "사용 한도로 차단", tone: "bad" },
  technical_error: { label: "기술 오류", tone: "bad" },
  cancelled: { label: "취소됨", tone: "neutral" },
  interrupted: { label: "중단됨", tone: "neutral" },
};

export const REQUEST: Record<string, string> = {
  queued: "대기 중", running: "처리 중", completed: "완료", failed: "실패", cancelled: "취소됨", interrupted: "중단됨",
};

export const BILLING: Record<string, string> = {
  none: "유료 호출 없음", pending: "예약 중(최대 비용 보류)", settled: "정산됨",
  unknown: "비용 미확정(확인 전까지 보류)", reconciled: "제공자 대조로 처리됨", released: "예약 해제",
};

export const WARNING: Record<string, string> = {
  cap_50: "운영 한도의 50%를 넘었습니다.",
  cap_75: "운영 한도의 75%를 넘었습니다.",
  cap_90: "운영 한도의 90%를 넘었습니다. 꼭 필요한 질문만 하세요.",
  cap_exhausted: "운영 한도에 도달해 유료 답변이 차단됩니다. 검색과 원문 열람은 계속 됩니다.",
  ahead_of_pace: "프로젝트 일정보다 사용 속도가 빠릅니다.",
  unknown_billing: "확인되지 않은 호출 비용이 보류 중입니다(관리자 확인 필요).",
};

export const REVIEW: Record<string, string> = {
  unreviewed: "원문 대조 전", auto_verified: "자동 대조 통과", auto_flagged: "자동 대조: 확인 필요",
  sample_checked: "표본 대조 완료", reviewed: "검수 완료", needs_recovery: "복구 필요",
};

export const REVIEW_WARNING: Record<string, string> = {
  unreviewed: "이 문서의 추출 결과는 아직 원문 대조 전입니다. 표와 숫자는 원문 파일로 확인하세요.",
  auto_flagged: "자동 원문 대조에서 추출 결과와 원문이 다른 곳이 발견되었습니다. 표·수식·숫자는 원문 파일로 확인하세요.",
};

export const EVIDENCE_WARNING: Record<string, string> = {
  source_version_changed: "이 근거 이후 원문 파일 버전이 바뀌었습니다. 현재 원문과 다를 수 있습니다.",
  extraction_revised: "원문 추출이 다시 이루어졌습니다. 인용은 답변 당시의 추출본입니다.",
  source_unreviewed: "이 문서의 추출 결과는 아직 원문 대조 전입니다. 표와 숫자는 원문 파일로 확인하세요.",
  source_auto_flagged: "자동 원문 대조에서 차이가 발견된 문서입니다. 표·수식·숫자는 원문 파일로 확인하세요.",
  source_needs_recovery: "이 문서는 복구가 필요한 상태입니다. 원문으로 확인하세요.",
  ocr_text: "그림 속 글자를 OCR로 읽은 근거입니다. 원본 그림을 확인하세요.",
};

export const FIELD: Record<string, string> = {
  title: "사업명", institution: "발주 기관", notice: "공고 번호", revision: "공고 차수", amount_krw: "사업 금액",
  published_at: "공개 일자", bid_start: "입찰 시작", bid_close: "입찰 마감",
};

export const FACT_STATE: Record<string, string> = {
  known: "기록됨", unknown: "값 없음", conflict: "충돌(원문 확인 필요)", zero_review: "0원(검토 필요)",
  resolved: "검토자 확인값",
};

export const MISSING_REASON: Record<string, string> = {
  unknown_metadata: "메타데이터 없음", not_found_in_context: "검색된 근거에 없음", ingestion_unavailable: "원문 미수집",
  source_absence_verified: "원문에 없음(검증됨)", scope_ambiguous: "문서 범위 모호",
};

export const MODE: Record<string, string> = {
  whitespace_bm25: "키워드(공백 분리)", kiwi_bm25: "키워드(형태소 분석)", dense: "의미 검색",
  hybrid: "키워드+의미 결합", hybrid_rerank: "키워드+의미 결합 후 재정렬",
};

export const RELEASE: Record<string, { label: string; tone: Tone }> = {
  ready: { label: "출시 가능", tone: "ok" }, limited: { label: "제한적 출시", tone: "warn" },
  blocked: { label: "차단", tone: "bad" },
};

export const REVIEW_TONE: Record<string, Tone> = {
  unreviewed: "neutral", auto_verified: "ok", auto_flagged: "warn", sample_checked: "ok", reviewed: "ok",
  needs_recovery: "bad",
};

/** Question types of gold rows, then of the earlier pilot rows still in the review queue. */
export const GOLD_TYPE: Record<string, string> = {
  direct_fact: "직접 사실", semantic_paraphrase: "다른 표현", exact_identifier: "요구사항 코드", table_numeric: "표·숫자",
  multi_passage: "한 문서 여러 근거", cross_document: "두 문서 비교", missing_false_premise: "없음·잘못된 전제",
  revision_conflict: "차수·중복 충돌", metadata_direct: "기본 정보",
  late_content: "뒷부분 내용", table_fact: "표 속 사실", numeric_qualifier: "숫자·조건", repeated_code: "반복 요구사항 코드",
  requirement_detail: "요구사항 상세", condition: "조건", missing_metadata: "메타데이터 누락",
  provenance_conflict: "출처 충돌", converter_unavailable: "변환 실패 문서", not_in_source: "원문에 없음",
};

/** The types a drafting slot can ask for, in the order the form offers them. */
export const DRAFT_TYPES = ["direct_fact", "semantic_paraphrase", "exact_identifier", "table_numeric", "multi_passage",
  "cross_document", "missing_false_premise"] as const;

export const ANSWERABILITY: Record<string, string> = {
  answerable: "답변 가능", unanswerable: "원문에 없음", ambiguous: "모호함", conflicting: "충돌",
};

export const DRAFT_STATUS: Record<string, { label: string; tone: Tone }> = {
  running: { label: "생성 중", tone: "info" }, completed: { label: "완료", tone: "ok" }, failed: { label: "실패", tone: "bad" },
  interrupted: { label: "중단됨", tone: "neutral" },
};

/** 0.83 (5/6); an empty denominator is 'not applicable', never 100 %. */
export function rate(r: { numerator: number; denominator: number; rate: number | null } | null | undefined): string {
  return !r || !r.denominator || r.rate == null ? "해당 없음" : `${r.rate.toFixed(2)} (${r.numerator}/${r.denominator})`;
}

export const stamp = (iso: string | null | undefined) => (iso ? iso.slice(0, 16).replace("T", " ") : "-");

export const label = (map: Record<string, string>, key: string | null | undefined) =>
  (key && map[key]) || key || "";

export function won(amount: number | null | undefined): string {
  return amount == null ? "미상" : `${amount.toLocaleString("ko-KR")}원`;
}

/** 130,000,000원 → 1억 3,000만원: the way Korean readers scan amounts. */
export function wonShort(amount: number | null | undefined): string {
  if (amount == null) return "금액 미상";
  const eok = Math.floor(amount / 100_000_000);
  const man = Math.round((amount % 100_000_000) / 10_000);
  if (!eok) return `${man.toLocaleString("ko-KR")}만원`;
  return man ? `${eok}억 ${man.toLocaleString("ko-KR")}만원` : `${eok}억원`;
}

export function when(value: { value: string; precision: string } | null | undefined): string {
  if (!value) return "미상";
  return value.precision === "timestamp" ? value.value.slice(0, 16).replace("T", " ") : value.value;
}

export function usd(micro: number | null | undefined, digits = 2): string {
  return micro == null ? "미상" : `$${(micro / 1_000_000).toFixed(digits)}`;
}

type Location = Record<string, unknown>;

/** Just the place within the document (표 54, 12쪽), for table cells; `locationText` gives the full sentence. */
export function locationShort(loc: Location | null | undefined): string {
  if (!loc) return "-";
  if (loc.format === "pdf" || loc.format === "hwp_print" || loc.format === "image_ocr") {
    const pages = (loc.pages as number[] | undefined) ?? [loc.page as number];
    return `${pages.join(", ")}쪽`;
  }
  if (loc.table_ordinal) return `표 ${loc.table_ordinal}`;
  const path = (loc.section_path as string[] | undefined) ?? [];
  return path[path.length - 1] ?? "-";
}

export function locationText(loc: Location | null | undefined): string {
  if (!loc) return "위치 없음";
  const path = ((loc.section_path as string[] | undefined) ?? []).join(" > ");
  if (loc.format === "image_ocr") {
    const where = loc.rendering === "hancom_print" ? "HWP 한컴 인쇄본" : "PDF";
    return `${where} ${loc.page}쪽 그림 · OCR 판독` + (path ? ` · ${path}` : "");
  }
  if (loc.format === "pdf" || loc.format === "hwp_print") {
    const pages = (loc.pages as number[] | undefined) ?? [loc.page as number];
    const printed = loc.page_label ? ` (인쇄 쪽번호 ${loc.page_label})` : "";
    const kind = loc.format === "pdf" ? "PDF" : "HWP 한컴 인쇄본";
    return `${kind} ${pages.join(", ")}쪽${printed}` + (path ? ` · ${path}` : "");
  }
  // hwp_loader: parsed by the HWP loader when pyhwp failed; no sections or tables, only the loader's element order.
  const parts = [loc.format === "hwp_loader" ? `HWP 보조 파서 요소 ${String(loc.path ?? "").replace("loader/e", "")}` : "HWP"];
  if (path) parts.push(path);
  if (loc.table_ordinal) parts.push(`표 ${loc.table_ordinal}`);
  return parts.join(" · ") + " (쪽 번호 없음: 원문 파일에서 확인)";
}
