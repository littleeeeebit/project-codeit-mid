"""Consultant, verifier and question-review screens, the visitor name and the live budget widget.

Every source or model string is rendered through `plain` (Markdown/HTML escaped); no unsafe HTML is used.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict
from datetime import date

from . import auth, service
from .contracts import CAPABILITIES, AnswerRequest, DocRef, Principal
from .settings import load_settings

STATUS_TEXT = {
    "answered": "답변",
    "insufficient_evidence": "근거 부족",
    "clarification_required": "확인 필요",
    "conflicting_evidence": "근거 충돌",
    "ingestion_unavailable": "원문 미수집",
    "budget_blocked": "사용 한도로 차단",
    "technical_error": "기술 오류",
    "in_progress": "처리 중",
}
REVIEW_TEXT = {"unreviewed": "원문 대조 전", "auto_verified": "자동 대조 통과", "auto_flagged": "자동 대조: 확인 필요",
               "sample_checked": "표본 대조 완료", "reviewed": "검수 완료", "needs_recovery": "복구 필요"}
REVIEW_WARNING = {
    "unreviewed": "이 문서의 추출 결과는 아직 원문 대조 전입니다. 표와 숫자는 원문 파일로 확인하세요.",
    "auto_flagged": "자동 원문 대조에서 추출 결과와 원문이 다른 곳이 발견되었습니다(검증 › 원문 대조). "
                    "표·수식·숫자는 원문 파일로 확인하세요.",
}
MISSING_REASON = {"unknown_metadata": "메타데이터 없음", "not_found_in_context": "검색된 근거에 없음",
                  "ingestion_unavailable": "원문 미수집", "source_absence_verified": "원문에 없음(검증됨)",
                  "scope_ambiguous": "문서 범위 모호"}
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~=])")


def plain(text: str | None) -> str:
    """Escape Markdown/HTML so untrusted text renders literally."""
    return _MD_SPECIAL.sub(r"\\\1", text or "").replace("\n", "  \n")


def won(amount: int | None) -> str:
    return "미상" if amount is None else f"{amount:,}원"


def when(value: dict | None) -> str:
    if not value:
        return "미상"
    return value["value"][:16].replace("T", " ") if value["precision"] == "timestamp" else value["value"]


def location_text(loc: dict) -> str:
    path = " > ".join(loc.get("section_path") or [])
    if loc.get("format") == "image_ocr":
        where = "HWP 한컴 인쇄본" if loc.get("rendering") == "hancom_print" else "PDF"
        return (f"{where} {loc.get('page')}쪽 그림 · OCR 판독({loc.get('engine')}), 원본 그림 확인 필요"
                + (f" · {path}" if path else ""))
    if loc.get("format") in ("pdf", "hwp_print"):
        pages = loc.get("pages") or [loc.get("page")]
        label = f" (인쇄 쪽번호 {loc['page_label']})" if loc.get("page_label") else ""
        kind = "PDF" if loc["format"] == "pdf" else "HWP 한컴 인쇄본"
        return f"{kind} {', '.join(map(str, pages))}쪽{label}" + (f" · {path}" if path else "")
    parts = ["HWP"]
    if path:
        parts.append(path)
    if loc.get("table_ordinal"):
        parts.append(f"표 {loc['table_ordinal']}")
    return " · ".join(parts) + " (쪽 번호 없음: 원문 파일에서 확인)"


# ---------------------------------------------------------------- request ownership (pure, tested)


def target_key(scope: list[tuple[str, str]], question: str, mode: str, as_of: str) -> str:
    """Identity of what the screen currently asks: selected (doc_id, source_hash) pairs, question, mode, date."""
    data = {"scope": [list(x) for x in scope], "q": " ".join((question or "").split()), "mode": mode, "as_of": as_of}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def may_attach(owned: dict | None, current_target: str, view) -> bool:
    """A request's outcome may render on the current answer panel only while the screen still asks exactly what
    it asked: same request, generation and target, and not cancelled. Otherwise it stays history."""
    return bool(owned and view is not None and owned.get("target") == current_target
                and owned.get("request_id") == view.request_id and owned.get("generation_id") == view.generation_id
                and not view.cancel_requested and view.status != "cancelled")


# ---------------------------------------------------------------- shell


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="입찰메이트 RFP 도우미", layout="wide")
    settings = load_settings()

    @st.cache_resource
    def resources():
        return service.get_resources(settings)

    res = resources()
    # No login (owner decision, 2026-09-30): every visitor gets every screen. The name only attributes paid
    # requests and review decisions; it is not authentication.
    with st.sidebar:
        name = st.text_input("이름 (사용·검토 기록용)", value="owner", max_chars=40, key="member_name").strip()
    principal = Principal(name or "owner", frozenset(CAPABILITIES))
    pages = [st.Page(lambda: consultant_page(st, res, principal), title="컨설턴트", url_path="consultant", default=True),
             st.Page(lambda: verifier_page(st, res, principal), title="검증", url_path="verify"),
             st.Page(lambda: gold_review_page(st, res, principal), title="질문 검토", url_path="review")]
    with st.sidebar:
        budget_widget(st, res, principal)
    st.navigation(pages).run()


def budget_widget(st, res, principal) -> None:
    @st.fragment(run_every=2)
    def _widget():  # read-only polling; never dispatches paid work
        snap = service.budget_snapshot(res, principal)
        usd = lambda m: f"\\${m / 1_000_000:,.4f}"  # noqa: E731 - escaped: a bare $ starts LaTeX in Markdown
        st.subheader("공유 사용량")
        st.progress(min(1.0, snap.spent_micro_usd / snap.allowance_micro_usd),
                    text=f"사용 {usd(snap.spent_micro_usd)} / {usd(snap.allowance_micro_usd)} "
                         f"({snap.spent_percent:.2f}%)")
        st.caption(f"진행 중 예약 {usd(snap.pending_micro_usd)} · 운영 한도 {usd(snap.cap_micro_usd)} · "
                   f"예약 가능 {usd(max(0, snap.available_micro_usd))}")
        if not snap.paid_enabled:
            st.warning("유료 답변 생성이 꺼져 있습니다. 검색과 원문 열람은 사용할 수 있습니다.")
        if snap.frozen_reason:
            st.error("비용 추정 점검이 필요해 유료 호출이 중지되었습니다.")
        with st.expander("상세"):
            st.caption(f"입력 {snap.tokens['prompt']:,} · 출력 {snap.tokens['completion']:,} · "
                       f"캐시 {snap.tokens['cached']:,} 토큰")
            for member, m in snap.per_member.items():
                st.caption(f"{member}: {usd(m)}")
            st.caption(snap.tracking_scope)
            st.caption(f"마지막 제공자 대조: {snap.last_reconciliation or '없음'}")

    _widget()


# ---------------------------------------------------------------- consultant


def _results_table(st, items: list[dict], key: str):
    rows = [{"사업명": i["title"], "발주 기관(CSV)": i["institution"] or "미상", "사업 금액": won(i["amount_krw"]),
             "공개일": when(i["published_at"]), "입찰 마감": when(i["bid_close"]),
             "원문": ("질문 가능" if i["indexed"] else
                    ("수집 실패" if i["parse_status"] == "quarantined" else "색인 전")),
             "검수": REVIEW_TEXT[i["review_status"]]} for i in items]
    event = st.dataframe(rows, on_select="rerun", selection_mode="single-row", hide_index=True, key=key,
                         width="stretch",
                         column_config={"사업명": st.column_config.TextColumn(width="large")})
    sel = event.selection.rows if event and event.selection else []
    return items[sel[0]] if sel else None


MODE_TEXT = {"whitespace_bm25": "키워드(공백 분리)", "kiwi_bm25": "키워드(형태소 분석)", "dense": "의미 검색",
             "hybrid": "키워드와 의미 검색 결합", "hybrid_rerank": "키워드와 의미 검색 결합 후 재정렬"}


def consultant_page(st, res, principal) -> None:
    st.title("RFP 검색과 근거 기반 질문")
    st.caption("제공된 과거 공고(2021-10 ~ 2025-02) 기준입니다. 현재 입찰 가능 여부는 이 자료로 판단할 수 없습니다.")
    with st.form("search"):
        c1, c2 = st.columns([2, 1])
        query = c1.text_input("검색어", placeholder="예: 학사정보시스템, 하자보수, SFR-001")
        inst = c2.text_input("발주 기관")
        c3, c4, c5 = st.columns(3)
        amin = c3.number_input("최소 금액(원)", min_value=0, value=None, step=10_000_000)
        amax = c4.number_input("최대 금액(원)", min_value=0, value=None, step=10_000_000)
        parsed_only = c5.checkbox("원문 수집된 문서만")
        st.form_submit_button("검색")
    items = service.search_projects(res, principal, {"institution": inst, "amount_min": amin, "amount_max": amax,
                                                     "parsed_only": parsed_only}, query, limit=100)
    st.caption(f"{len(items)}건 · 금액/날짜 조건은 값이 없는 문서를 포함하지 않습니다.")
    picked = _results_table(st, items, "results")
    if picked:
        scope = (picked["doc_id"], picked["source_hash"])
        if st.session_state.get("scope") != scope:  # a new scope drops the old answer from this screen
            st.session_state["scope"] = scope
            st.session_state["scope_item"] = picked
            st.session_state.pop("answer", None)
    item = st.session_state.get("scope_item")
    if not item:
        st.info("목록에서 문서를 하나 선택하면 질문할 수 있습니다.")
        return
    st.divider()
    st.subheader(item["title"])
    st.caption(f"{item['institution'] or '기관 미상'} · {won(item['amount_krw'])} · 마감 {when(item['bid_close'])} · "
               f"{item['format'].upper()} · {REVIEW_TEXT[item['review_status']]}")
    if item.get("snippet"):
        st.markdown(plain(item["snippet"]))
    for c in item["conflicts"]:
        resolved = item.get("resolutions", {}).get(c["field"])
        note = (f" 검토자가 확인한 값: {plain(json.dumps(resolved['value'], ensure_ascii=False))} "
                f"(근거: {plain(resolved['evidence'])})" if resolved else " 원문과 공고를 확인하세요.")
        st.warning(f"같은 원문 파일에 연결된 다른 공고와 '{c['field']}' 값이 다릅니다.{note}")
    if item["review_status"] in REVIEW_WARNING and item["indexed"]:
        st.warning(REVIEW_WARNING[item["review_status"]])
    dl = service.original_download(res, principal, item["doc_id"], item["source_hash"])
    st.download_button("원문 파일 받기", dl.data, file_name=dl.filename, mime=dl.mime)
    if not item["indexed"]:
        st.error(item.get("unavailable_reason") or "이 문서는 아직 질문용 색인에 포함되지 않았습니다.")
        return
    st.caption(f"검색 방식: {MODE_TEXT.get(res.serving()['mode'], res.serving()['mode'])}")
    with st.form("ask"):
        question = st.text_area("질문", max_chars=res.settings.question_max_characters,
                                placeholder="예: 하자보수 기간과 조건은 무엇인가요?")
        submitted = st.form_submit_button("근거 기반 답변 받기 (유료 1회)")
    if submitted and question.strip():
        generation_id = str(uuid.uuid4())
        request = AnswerRequest(idempotency_key=str(uuid.uuid4()), generation_id=generation_id, question=question,
                                scope=[DocRef(*st.session_state["scope"])], as_of=date.today().isoformat())
        with st.spinner("근거를 찾고 답변을 검증하는 중입니다…"):
            try:
                result = service.answer(res, principal, request)
            except (service.ServiceError, auth.AuthError) as exc:
                st.error(str(exc))
                return
        st.session_state["answer"] = {"scope": st.session_state["scope"], "result": result}
    shown = st.session_state.get("answer")
    if shown and shown["scope"] == st.session_state["scope"]:
        render_answer(st, res, principal, shown["result"])


def render_answer(st, res, principal, r) -> None:
    label = STATUS_TEXT.get(r.status, r.status)
    box = {"answered": st.success, "technical_error": st.error, "budget_blocked": st.warning}.get(r.status, st.info)
    box(f"{label}: {r.summary}")
    if r.error and r.status in ("technical_error", "budget_blocked"):
        st.caption(f"사유 코드: {r.error[:120]}")
    for i, claim in enumerate(r.claims, 1):
        kind = "추론" if claim["kind"] == "inference" else "원문 사실"
        st.markdown(f"**{i}. [{kind}]** {plain(claim['text'])}")
        for eid in claim["evidence_ids"]:
            ev = r.evidence.get(eid)
            if ev is None:
                continue
            with st.expander(f"근거 {eid} · {location_text(ev['location'])}"):
                view = service.open_evidence(res, principal, r.request_id, eid)
                for ctx in view.context:
                    text = plain(ctx["text"])
                    st.markdown(f"**{text}**" if ctx["cited"] else text)
    if r.missing_fields:
        st.markdown("**확인되지 않은 정보**")
        for m in r.missing_fields:
            st.markdown(f"- {plain(m['field'])}: {MISSING_REASON.get(m['reason'], m['reason'])}")
    for c in r.conflicts:
        vals = " / ".join(f"{plain(a['value'])} ({', '.join(a['evidence_ids'])})" for a in c["alternatives"])
        st.warning(f"근거 충돌 — {c['field']}: {vals}")
    if r.next_action:
        st.caption(f"다음 확인: {r.next_action}")
    st.caption(f"요청 {r.request_id[:8]} · 비용 상태 {r.billing_state}")


# ---------------------------------------------------------------- verifier


def verifier_page(st, res, principal) -> None:
    auth.require(principal, "verifier")  # the service checks again on every call
    st.title("검증: 검색 경로와 근거 추적")
    tab_trace, tab_fidelity, tab_ingest = st.tabs(["검색 추적", "원문 대조 (HWP)", "수집 상태 (100건)"])
    with tab_fidelity:
        _fidelity_tab(st, res, principal)
    with tab_ingest:
        rows = service.ingestion_overview(res, principal)
        st.dataframe([{k: r[k] for k in ("filename", "format", "parse_status", "review_status", "reason_code")}
                      | {"warnings": ", ".join(r["warnings"])} for r in rows], hide_index=True,
                     width="stretch")
    with tab_trace:
        items = [i for i in service.search_projects(res, principal, {"parsed_only": True}, "", limit=100)
                 if i["indexed"]]
        if not items:
            st.info("색인된 문서가 없습니다.")
            return
        labels = {f"{i['title']} · {i['institution']}": i for i in items}
        choice = labels[st.selectbox("문서", list(labels))]
        question = st.text_input("질문", key="vq")
        if st.button("검색만 실행 (무료)") and question.strip():
            st.session_state["vtrace"] = {
                "key": (choice["doc_id"], question),
                "trace": service.verifier_trace(res, principal, question,
                                                [DocRef(choice["doc_id"], choice["source_hash"])],
                                                date.today().isoformat())}
        vt = st.session_state.get("vtrace")
        if not vt or vt["key"] != (choice["doc_id"], question):
            return
        t = vt["trace"]
        r = t["retrieval"]
        qe = r.get("query_embedding") or {}
        st.caption(f"운영 설정 {t['serving'].get('run_id') or '기본(키워드)'} · 요청 모드 {t['serving']['mode']} · "
                   f"대체 {r.get('fallback') or '없음'} · 의미 행렬 {r.get('dense_version') or '-'} · "
                   f"질의 벡터 {qe.get('cache', '-')}")
        st.caption(f"색인 {t['index_version']} ({t['review_scope']}) · 모드 {r['mode']} · "
                   f"소요 {r['timings_ms']['total']}ms · 근거 토큰 {r['evidence_tokens']} · "
                   f"최종 입력 추정 {t['input_tokens']} 토큰")
        st.markdown("**질의 토큰**: " + plain(" ".join(t["query_tokens"])))
        st.markdown("**제한 사항**: " + plain(", ".join(r["limitations"]) or "없음"))
        st.markdown("**후보 순위 (채널별: exact / bm25 / dense / rrf / rerank)**")
        st.dataframe(r["candidates"], hide_index=True, width="stretch")
        st.markdown("**선택된 근거**")
        for e in r["evidence"]:
            with st.expander(f"{e['evidence_id']} · {e['token_count']} 토큰 · {location_text(e['location'])}"):
                st.markdown(plain(e["quote"]))
                st.code(json.dumps({"chunk_id": e["chunk_id"], "element_ids": e["element_ids"],
                                    "location": e["location"]}, ensure_ascii=False, indent=1), language="json")
        if r["excluded"]:
            st.markdown("**제외된 후보**")
            st.dataframe(r["excluded"], hide_index=True)
        st.divider()
        est = t["estimate_micro_usd"] / 1_000_000
        agree = st.checkbox(f"유료 답변 생성을 1회 실행합니다 (최대 예상 \\${est:.6f})")
        if st.button("유료 답변 생성", disabled=not agree):
            result = service.answer(res, principal, AnswerRequest(
                idempotency_key=str(uuid.uuid4()), generation_id="verifier", question=question,
                scope=[DocRef(choice["doc_id"], choice["source_hash"])], as_of=date.today().isoformat()))
            render_answer(st, res, principal, result)
            with st.expander("결과 원본(JSON)"):
                st.code(json.dumps(asdict(result), ensure_ascii=False, indent=1), language="json")


# ---------------------------------------------------------------- dataset question review
# Who comes here: a verifier who did not draft the questions. What for: check each drafted question against
# the original, then approve it into the dataset or reject it with a reason for the drafting agent.

TYPE_TEXT = {"late_content": "뒷부분 내용", "table_fact": "표 속 사실", "numeric_qualifier": "숫자·조건",
             "repeated_code": "반복 요구사항 코드", "requirement_detail": "요구사항 상세", "condition": "조건",
             "missing_metadata": "메타데이터 누락", "provenance_conflict": "출처 충돌",
             "converter_unavailable": "변환 실패 문서", "not_in_source": "원문에 없음"}


def gold_review_page(st, res, principal) -> None:
    from .gold import REJECT_CATEGORIES

    auth.require(principal, "verifier")
    st.title("질문 검토")
    st.caption("초안 질문을 원문과 대조하세요. 승인하면 데이터셋에, 거절하면 사유와 함께 거절 위키에 기록됩니다.")
    if flash := st.session_state.pop("gold_flash", None):
        st.success(flash)
    q = service.gold_queue(res, principal)
    pending = q["pending"]
    st.caption(f"대기 {len(pending)}건")
    if not pending:
        st.info("검토할 질문이 없습니다.")
    else:
        labels = {f"{p['candidate_id']} · {TYPE_TEXT.get(p['type'], p['type'])} · {p['question'][:50]}":
                  p["candidate_id"] for p in pending}
        cid = labels[st.selectbox("검토할 질문", list(labels))]
        _gold_candidate(st, res, principal, service.gold_candidate(res, principal, cid), REJECT_CATEGORIES)
    if q["recent"]:
        with st.expander("최근 처리"):
            st.dataframe([{"질문": r["candidate_id"], "결과": "승인" if r["status"] == "approved" else "거절",
                           "검토자": r["decided_by"], "시각": r["decided_at"][:19].replace("T", " "),
                           "거절 사유": ", ".join(REJECT_CATEGORIES.get(c, c) for c in r["categories"])}
                          for r in q["recent"]], hide_index=True, width="stretch")


def _fidelity_tab(st, res, principal) -> None:
    st.caption("한컴 뷰어 인쇄본(PDF)의 글자와 추출 결과를 자동으로 양방향 대조합니다. "
               "사람은 아래에 표시된 곳만 원문 쪽 이미지와 비교하면 됩니다.")
    rows = service.fidelity_overview(res, principal)
    checked = [r for r in rows if r["metrics"]]
    counts = {k: sum(r["review_status"] == k for r in rows) for k in REVIEW_TEXT}
    st.markdown(" · ".join(f"{REVIEW_TEXT[k]} {n}" for k, n in counts.items() if n)
                + f" · 대조 결과 {len(checked)}/{len(rows)}")
    table = [{"파일": r["filename"], "상태": REVIEW_TEXT[r["review_status"]],
              "표시된 곳": len(r["findings"]) if r["metrics"] else None,
              "추출→원문 불일치(자)": r["metrics"]["extraction_unmatched"] if r["metrics"] else None,
              "원문→추출 불일치(자)": r["metrics"]["rendering_unmatched"] if r["metrics"] else None,
              "이미지 쪽": len(r["metrics"]["image_pages"]) if r["metrics"] else None} for r in rows]
    event = st.dataframe(table, on_select="rerun", selection_mode="single-row", hide_index=True, key="fid",
                         width="stretch")
    picked = event.selection.rows if event else []
    if not picked:
        return
    r = rows[picked[0]]
    if not r["metrics"]:
        st.info("아직 자동 대조 전입니다: `python -m rfp_assistant.cli fidelity run`")
        return
    m = r["metrics"]
    st.markdown(f"**{plain(r['filename'])}** · {m['pages']}쪽 · 추출 {m['extracted_chars']:,}자 중 불일치 "
                f"{m['extraction_unmatched']}자 · 인쇄본 {m['rendered_chars']:,}자 중 추출에 없는 글자 "
                f"{m['rendering_unmatched']}자")
    if m["image_pages"]:
        st.caption("그림이 있는 쪽(그림 속 글자는 양쪽 모두에 없음): " + ", ".join(map(str, m["image_pages"])))
    for i, f in enumerate(r["findings"]):
        side = "추출에만 있음/다름" if f["side"] == "extraction" else "인쇄본에만 있음(추출 누락 의심)"
        where = f"{f['page']}쪽" if f.get("page") else "쪽 미상"
        with st.expander(f"{i + 1}. {where} · {side} · {f['unmatched_chars']}자"
                         + (f" · 숫자 {', '.join(f['digits'])}" if f["digits"] else "")):
            st.markdown("불일치 구간(공백·문장부호 제외): " + plain(" / ".join(f.get("stretches") or [f["stretch"]]) or "-"))
            if f.get("text"):
                st.markdown("추출 원문: " + plain(f["text"]))
            if f.get("page") and st.toggle("인쇄본 쪽 보기", key=f"fidpg-{r['source_hash']}-{i}"):
                st.image(service.rendered_page_png(res, principal, r["source_hash"], f["page"]))
    if r["review_status"] in ("auto_verified", "auto_flagged"):
        with st.form(f"fidok-{r['source_hash']}"):
            note = st.text_input("메모 (선택)", max_chars=300)
            if st.form_submit_button("표시된 곳을 모두 원문과 대조했음 → 표본 대조 완료"):
                service.confirm_fidelity(res, principal, r["source_hash"], note)
                st.rerun()


def _gold_candidate(st, res, principal, c: dict, categories: dict) -> None:
    row, ctx = c["row"], c["context"]
    doc = ctx.get("document") or {}
    st.markdown(f"**유형** {TYPE_TEXT.get(row.get('type'), row.get('type'))} · **데이터셋** {plain(c['dataset'])} · "
                f"**초안** {plain(c['drafted_by'])}")
    st.markdown("**질문**")
    st.info(plain(row.get("question", "")))
    st.markdown("**기대 답변**")
    st.markdown(plain(row.get("expected_answer")))
    st.markdown(f"**문서** {plain(doc.get('title'))} · {plain(doc.get('filename'))} · "
                f"{(doc.get('format') or '').upper()} · {REVIEW_TEXT.get(doc.get('review_status'), '')}")
    if doc.get("review_status") in REVIEW_WARNING:
        st.warning(REVIEW_WARNING[doc["review_status"]])
    for err in c.get("current_errors") or []:  # e.g. a converter case whose document has since been recovered
        st.error(f"현재 원문 상태와 맞지 않습니다: {plain(err)}")
    if doc.get("unavailable_reason"):
        st.error(doc["unavailable_reason"])
    if doc.get("doc_id"):
        dl = service.original_download(res, principal, doc["doc_id"], row["source_hash"])
        st.download_button("원문 파일 받기", dl.data, file_name=dl.filename, mime=dl.mime, key=f"dl-{c['candidate_id']}")
    for i, ev in enumerate(ctx.get("evidence", []), 1):
        loc = location_text(ev["location"]) if ev["location"] else "위치 없음"
        st.markdown(f"**근거 {i}** · {loc}")
        st.markdown(f"> {plain(ev['quote'])}")
        with st.expander("추출된 원문 요소 전체"):
            st.markdown(plain(ev["text"]) if ev["text"] else "요소를 찾을 수 없습니다.")
    if "metadata" in ctx:
        st.markdown("**CSV 메타데이터**")
        st.json({"fields": ctx["metadata"], "conflicts": ctx["metadata_conflicts"]})
    st.divider()
    left, right = st.columns(2)
    if left.button("승인 → 데이터셋", type="primary", key=f"approve-{c['candidate_id']}"):
        _gold_decide(st, res, principal, c, "approve")
    with right.form(f"reject-{c['candidate_id']}", clear_on_submit=True):
        picked = st.multiselect("거절 사유", list(categories), format_func=categories.get)
        note = st.text_area("메모 (무엇이 틀렸는지 구체적으로)")
        if st.form_submit_button("거절 → 거절 위키"):
            _gold_decide(st, res, principal, c, "reject", picked, note)


def _gold_decide(st, res, principal, c: dict, decision: str, categories=None, note: str = "") -> None:
    try:
        service.gold_decide(res, principal, c["candidate_id"], decision, c["row_sha256"], categories, note)
    except (service.ServiceError, auth.AuthError) as exc:
        st.error(str(exc))
        return
    st.session_state["gold_flash"] = f"{c['candidate_id']}: {'승인' if decision == 'approve' else '거절'}했습니다."
    st.rerun()
