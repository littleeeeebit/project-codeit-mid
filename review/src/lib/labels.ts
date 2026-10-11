// Korean labels for the codes a run folder carries. The code stays available on hover (title) wherever it is shown;
// an unknown code falls back to itself so nothing is hidden. Question types and fact fields repeat web/src/lib/format.ts:
// the review app is a separate build and never imports from web/.

export const label = (map: Record<string, string>, key: string | null | undefined) => (key && map[key]) || key || "";

/** Gold question types. */
export const TYPE: Record<string, string> = {
  direct_fact: "직접 사실", semantic_paraphrase: "다른 표현", exact_identifier: "요구사항 코드", table_numeric: "표·숫자",
  multi_passage: "한 문서 여러 근거", cross_document: "두 문서 비교", missing_false_premise: "없음·잘못된 전제",
  revision_conflict: "차수·중복 충돌", metadata_direct: "기본 정보",
};

/** The answer validator's rules (src/rfp_assistant/gateway/generation.py): why an answer was refused before a user saw it. */
export const VALIDATION: Record<string, string> = {
  model_refusal: "모델이 답변을 거절함", output_truncated: "출력이 잘림", incomplete_output: "출력이 끝나지 않음",
  rewrite_schema_invalid: "질문 다시 쓰기 형식 오류", rewrite_length_invalid: "질문 다시 쓰기 길이 오류",
  schema_invalid: "답변 형식 오류", absence_not_listed: "'없음' 주장을 빠진 항목에 올리지 않음",
  conflict_without_conflicts: "충돌이라면서 충돌 항목이 없음", conflict_with_one_alternative: "충돌 값이 하나뿐",
  conflict_without_next_action: "충돌인데 다음 조치가 없음", unknown_evidence_id: "없는 근거 번호를 인용",
  evidence_scope_mismatch: "다른 문서의 근거를 인용", evidence_quote_mismatch: "인용문이 근거와 다름",
  claim_outside_scope: "고른 문서 밖의 주장", claim_without_evidence: "근거 없는 주장",
  conflict_without_evidence: "근거 없는 충돌 값", missing_field_outside_scope: "고른 문서 밖의 빠진 항목",
  source_absence_claimed_from_retrieval: "검색 결과만으로 원문에 없다고 단정",
  answered_without_claims: "주장 없이 답변으로 응답", comparison_side_missing: "비교할 문서 한쪽이 빠짐",
  summary_without_evidence: "근거 없는 요약",
};

/** Chunk kinds written by src/rfp_assistant/retrieval/chunking.py. */
export const CHUNK: Record<string, string> = {
  prose: "본문", table: "표", fixed: "고정 길이", requirement_detail: "요구사항 상세",
  requirement_summary: "요구사항 요약", image_text: "그림 속 글자",
};

export const FIELD: Record<string, string> = {
  title: "사업명", institution: "발주 기관", notice: "공고 번호", revision: "공고 차수", amount_krw: "사업 금액",
  published_at: "공개 일자", bid_start: "입찰 시작", bid_close: "입찰 마감",
};

export const FACT_STATE: Record<string, string> = {
  known: "기록됨", unknown: "값 없음", conflict: "충돌(원문 확인 필요)", zero_review: "0원(검토 필요)",
  resolved: "검토자 확인값",
};
