"""질문하기, 검증 and 데이터셋 만들기: a presentation-only Streamlit shell over `service`.

Every action calls one `service` function, and nothing else from the package is imported
(tests/test_ui_boundary.py). Layout, result states and colours are recorded in DESIGN.md. Paid work runs on the
service's background executors; these screens own only their current request and poll read-only status.

Every source or model string is rendered through `plain` (Markdown/HTML escaped), tables included, because
`st.table` cells are Markdown; no unsafe HTML is used.
"""

from __future__ import annotations

import json
import re
from datetime import date

from . import service

ERRORS = service.SHELL_ERRORS

STATUS_TEXT = {
    "answered": "답변",
    "insufficient_evidence": "근거 부족",
    "clarification_required": "확인 필요",
    "conflicting_evidence": "근거 충돌",
    "ingestion_unavailable": "원문 미수집",
    "budget_blocked": "사용 한도로 차단",
    "technical_error": "기술 오류",
    "in_progress": "처리 중",
    "cancelled": "취소됨",
    "interrupted": "중단됨",
}
# Badge colours repeat what the label already says (DESIGN.md section 4); every other state is gray.
STATUS_COLOR = {"answered": "green", "clarification_required": "blue", "insufficient_evidence": "orange",
                "conflicting_evidence": "orange", "budget_blocked": "red", "technical_error": "red"}
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
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~=$])")  # $ would start LaTeX


def plain(text: str | None) -> str:
    """Escape Markdown/HTML so untrusted text renders literally."""
    return _MD_SPECIAL.sub(r"\\\1", text or "").replace("\n", "  \n")


def badge(label: str, color: str = "gray") -> str:
    """An inline status badge. The label carries the meaning; the colour only repeats it."""
    return f":{color}-badge[{plain(label)}]"


def marked(text: str, cited: bool) -> str:
    """Escaped text; a cited span is highlighted line by line, since an inline mark cannot cross a line break."""
    if not cited:
        return plain(text)
    return "  \n".join(f":blue-background[{plain(line)}]" if line.strip() else "" for line in text.split("\n"))


def table(st, rows: list[dict]) -> None:
    """A static, screen-reader-readable table. `st.table` renders Markdown, so every string is escaped."""
    st.table([{k: plain(_cell(v)) for k, v in r.items()} for r in rows])


def _cell(v) -> str:
    """Cells are text: a mixed or empty column would otherwise turn into floats ("2,305.0000")."""
    if v is None:
        return "-"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    return str(v)


def rate_text(r: dict | None) -> str:
    if not r or not r.get("denominator"):
        return "해당 없음"
    return f"{r['rate']:.2f} ({r['numerator']}/{r['denominator']})"


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


# ---------------------------------------------------------------- shell

REQUEST_TEXT = {"queued": "대기 중", "running": "처리 중", "completed": "완료", "failed": "실패", "cancelled": "취소됨",
                "interrupted": "중단됨"}
BILLING_TEXT = {"none": "유료 호출 없음", "pending": "예약 중(최대 비용 보류)", "settled": "정산됨",
                "unknown": "비용 미확정(확인 전까지 보류)", "reconciled": "제공자 대조로 처리됨", "released": "예약 해제"}
WARNING_TEXT = {"cap_50": "운영 한도의 50%를 넘었습니다.", "cap_75": "운영 한도의 75%를 넘었습니다.",
                "cap_90": "운영 한도의 90%를 넘었습니다. 꼭 필요한 질문만 하세요.",
                "cap_exhausted": "운영 한도에 도달해 유료 답변이 차단됩니다. 검색과 원문 열람은 계속 됩니다.",
                "ahead_of_pace": "프로젝트 일정보다 사용 속도가 빠릅니다.",
                "unknown_billing": "확인되지 않은 호출 비용이 보류 중입니다(관리자 확인 필요)."}
FACT_FIELD = {"title": "사업명", "institution": "발주 기관", "notice": "공고 번호", "revision": "공고 차수",
              "amount_krw": "사업 금액", "published_at": "공개 일자", "bid_start": "입찰 시작", "bid_close": "입찰 마감"}
FACT_STATE = {"known": "기록됨", "unknown": "값 없음", "conflict": "충돌(원문 확인 필요)", "zero_review": "0원(검토 필요)",
              "resolved": "검토자 확인값"}
ASK_MODES = {"single": "근거 기반 답변 (유료)", "metadata": "기본 정보 (무료)", "inventory": "요구사항 목록 (무료)"}
PAIR_MODES = {"compare": "두 문서 비교 (유료)", "metadata": "기본 정보 비교 (무료)"}
SUBMIT_TEXT = {"single": "근거 기반 답변 받기 (유료 1회)", "compare": "비교 답변 받기 (유료 1회)",
               "metadata": "기본 정보 보기 (무료)", "inventory": "요구사항 목록 보기 (무료)"}


def dollars(micro: int | None) -> str:
    """Raw text, for table cells (which `table` escapes)."""
    return "미상" if micro is None else f"${micro / 1_000_000:,.4f}"


def usd(micro: int | None) -> str:
    """Markdown-ready, for captions, labels and messages."""
    return plain(dollars(micro))


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="입찰메이트 RFP 도우미", layout="wide")

    @st.cache_resource
    def resources():
        return service.app_resources()

    res = resources()
    page = st.navigation([
        st.Page(lambda: chat_page(st, res, principal), title="질문하기", url_path="ask", default=True),
        st.Page(lambda: verify_page(st, res, principal), title="검증", url_path="verify"),
        st.Page(lambda: dataset_page(st, res, principal), title="데이터셋 만들기", url_path="dataset")],
        position="top")
    principal = header(st, res)  # read by the page callables above when page.run() calls them
    page.run()


def header(st, res):
    """One row under the menu on every page: the visitor's name and the shared allowance. There is no login; the
    name only attributes requests and reviews (and lets the service refuse a drafter's own approval)."""
    left, right = st.columns([1, 3], vertical_alignment="bottom")
    name = left.text_input("이름 (사용·검토 기록용)", value="owner", max_chars=40, key="member_name")
    principal = service.visitor(name)
    with right:
        budget_strip(st, res, principal)
    return principal


def budget_strip(st, res, principal) -> None:
    @st.fragment(run_every=2)
    def _strip():  # read-only polling; never dispatches paid work
        try:
            snap = service.budget_snapshot(res, principal)
        except ERRORS:
            st.markdown(badge("최신 아님", "red") + " 사용량을 읽지 못했습니다. 유료 답변은 이 상태에서 시작되지 않습니다.")
            return
        st.progress(min(1.0, max(0.0, snap.spent_micro_usd / snap.allowance_micro_usd)),
                    text=f"공유 사용량 {usd(snap.spent_micro_usd)} / {usd(snap.allowance_micro_usd)} "
                         f"({snap.spent_percent:.1f}%) · 진행 중 예약 {usd(snap.pending_micro_usd)} · "
                         f"예약 가능 {usd(max(0, snap.available_micro_usd))}")
        notes = [badge(WARNING_TEXT.get(w, w), "red" if w in ("cap_90", "cap_exhausted") else "orange")
                 for w in service.visible_warnings(snap.warnings)]
        if not snap.paid_enabled:
            notes.append(badge("유료 답변 꺼짐: 검색과 원문 열람은 사용할 수 있습니다", "orange"))
        if snap.frozen_reason:
            notes.append(badge("비용 추정 점검이 필요해 유료 호출 중지", "red"))
        if notes:
            st.markdown(" ".join(notes))

    _strip()


# ---------------------------------------------------------------- 질문하기

MODE_TEXT = {"whitespace_bm25": "키워드(공백 분리)", "kiwi_bm25": "키워드(형태소 분석)", "dense": "의미 검색",
             "hybrid": "키워드와 의미 검색 결합", "hybrid_rerank": "키워드와 의미 검색 결합 후 재정렬"}
PAGE_SIZE = 10


def _set_owned(st, res, principal, owned: dict | None) -> None:
    old = st.session_state.get("owned")
    if old and (owned is None or old["request_id"] != owned["request_id"]):
        try:
            service.abandon_request(res, principal, old["request_id"])
        except ERRORS:
            pass
    st.session_state["owned"] = owned
    st.session_state.pop("evidence_open", None)


def chat_page(st, res, principal) -> None:
    st.title("질문하기")
    st.caption("제공된 과거 공고(2021-10 ~ 2025-02) 기준입니다. 현재 입찰 가능 여부는 이 자료로 판단할 수 없습니다.")
    with st.sidebar:
        _document_picker(st, res, principal)
    selected = list(st.session_state.get("selected", {}).values())
    if selected:
        _selected_docs(st, res, principal, selected)
        _ask_form(st, res, principal, selected)
    else:
        st.info("왼쪽 '문서 찾기'에서 문서를 하나 고르면 질문할 수 있고, 두 개를 고르면 비교할 수 있습니다.")
        _set_current(st, None)
    _answer_area(st, res, principal)
    _history(st, res, principal)


def _document_picker(st, res, principal) -> None:
    st.header("문서 찾기")
    with st.form("search"):
        query = st.text_input("검색어", placeholder="예: 학사정보시스템, 하자보수, SFR-001")
        inst = st.text_input("발주 기관")
        with st.expander("상세 조건"):
            amin = st.number_input("최소 금액(원)", min_value=0, value=None, step=10_000_000)
            amax = st.number_input("최대 금액(원)", min_value=0, value=None, step=10_000_000)
            close_from = st.date_input("입찰 마감 시작", value=None)
            close_to = st.date_input("입찰 마감 끝", value=None)
        if st.form_submit_button("검색", width="stretch"):
            st.session_state["result_limit"] = PAGE_SIZE
    try:
        items = service.find_documents(res, principal, query, inst, amin, amax,
                                       close_from.isoformat() if close_from else None,
                                       close_to.isoformat() if close_to else None)
    except ERRORS as exc:
        st.error(str(exc))
        return
    st.caption(f"{len(items)}건 · 질문할 수 있는 문서가 먼저 나옵니다. 최대 2개를 고르세요.")
    _result_cards(st, items)


def _doc_badges(i: dict) -> list[str]:
    if i["indexed"]:
        out = [badge("질문 가능", "green")]
    else:
        out = [badge("원문 수집 실패", "red") if i["parse_status"] == "quarantined" else badge("색인 전")]
    out.append(badge(REVIEW_TEXT[i["review_status"]],
                     "orange" if i["review_status"] in ("auto_flagged", "needs_recovery") else "gray"))
    conflict = sorted({c["field"] for c in i["conflicts"]} - set(i.get("resolutions") or {}))
    if conflict:
        out.append(badge("기록 충돌: " + ", ".join(FACT_FIELD.get(f, f) for f in conflict), "orange"))
    for note in i.get("filter_undecided") or []:
        field, _, state = note.partition(":")
        out.append(badge(f"{FACT_FIELD.get(field, field)} 조건 판단 불가({'충돌' if state == 'conflict' else '값 없음'})",
                         "orange"))
    return out


def _result_cards(st, items: list[dict]) -> None:
    selected = st.session_state.setdefault("selected", {})
    limit = st.session_state.get("result_limit", PAGE_SIZE)
    for i in items[:limit]:
        key = i["doc_id"]
        with st.container(border=True):
            gen = st.session_state.setdefault("pick_gen", {}).get(key, 0)  # a new widget after deselection
            checked = st.checkbox(plain(i["title"] or "제목 없음"), key=f"pick-{key}-{gen}", value=key in selected,
                                  disabled=key not in selected and len(selected) >= 2)
            st.markdown(" ".join(_doc_badges(i)))
            st.caption(f"{plain(i['institution'] or '기관 미상')} · {won(i['amount_krw'])} · "
                       f"공개 {when(i['published_at'])} · 마감 {when(i['bid_close'])} · {i['format'].upper()}")
            if checked and key not in selected:
                selected[key] = i
            elif not checked and key in selected:
                selected.pop(key)
    if len(items) > limit and st.button(f"더 보기 ({len(items) - limit}건 남음)", width="stretch"):
        st.session_state["result_limit"] = limit + PAGE_SIZE
        st.rerun()


def _selected_docs(st, res, principal, selected: list[dict]) -> None:
    for col, item in zip(st.columns(len(selected)), selected):
        with col.container(border=True):
            st.markdown(f"**{plain(item['title'])}**")
            st.caption(f"원문 버전 {item['source_hash'][:12]} · {item['format'].upper()} · "
                       f"{REVIEW_TEXT[item['review_status']]}")
            open_conflicts = [c["field"] for c in item["conflicts"] if c["field"] not in (item.get("resolutions") or {})]
            if open_conflicts:
                st.warning("같은 원문 파일에 연결된 다른 공고와 값이 다릅니다: "
                           + ", ".join(FACT_FIELD.get(f, f) for f in open_conflicts) + ". 원문과 공고를 확인하세요.")
            for f, resolved in (item.get("resolutions") or {}).items():
                st.caption(f"{FACT_FIELD.get(f, f)}: 검토자가 확인한 값 "
                           f"{plain(json.dumps(resolved['value'], ensure_ascii=False))} (근거: {plain(resolved['evidence'])})")
            if item["review_status"] in REVIEW_WARNING and item["indexed"]:
                st.warning(REVIEW_WARNING[item["review_status"]])
            if not item["indexed"]:
                st.error(item.get("unavailable_reason") or "이 문서는 아직 질문용 색인에 포함되지 않았습니다.")
            with st.container(horizontal=True):
                try:
                    dl = service.original_download(res, principal, item["doc_id"], item["source_hash"])
                    st.download_button("원문 파일 받기", dl.data, file_name=dl.filename, mime=dl.mime,
                                       key=f"dl-{item['doc_id']}")
                except ERRORS as exc:
                    st.caption(f"원문을 열 수 없습니다: {plain(str(exc))}")
                if st.button("선택 해제", key=f"unpick-{item['doc_id']}"):
                    st.session_state["selected"].pop(item["doc_id"], None)
                    gens = st.session_state.setdefault("pick_gen", {})
                    gens[item["doc_id"]] = gens.get(item["doc_id"], 0) + 1
                    st.rerun()


def _ask_form(st, res, principal, selected: list[dict]) -> None:
    modes = ASK_MODES if len(selected) == 1 else PAIR_MODES
    mode = st.segmented_control("질문 방식", list(modes), format_func=modes.get, default=next(iter(modes)),
                                required=True, key=f"mode-{len(selected)}")
    scope = [(i["doc_id"], i["source_hash"]) for i in selected]
    as_of = date.today().isoformat()
    owned = st.session_state.get("owned")
    busy = False
    if owned:
        try:
            busy = service.request_status(res, principal, owned["request_id"]).status in ("queued", "running")
        except ERRORS:
            busy = False
    current = st.session_state.get("current") or {}
    if current.get("scope") != scope or current.get("mode") != mode or current.get("as_of") != as_of:
        # A changed selection or mode: the previous request can no longer attach to this screen.
        _set_current(st, {"scope": scope, "mode": mode, "as_of": as_of, "question": ""})
    with st.form("ask"):
        question = ""
        if mode in ("single", "compare"):
            question = st.text_area("질문", max_chars=res.settings.question_max_characters, key=f"q-{mode}",
                                    placeholder="예: 두 사업의 하자보수 조건을 비교해 주세요." if mode == "compare"
                                    else "예: 하자보수 기간과 조건은 무엇인가요?")
        submitted = st.form_submit_button(SUBMIT_TEXT[mode], type="primary", disabled=busy)
    if busy:
        st.caption("이전 요청이 끝나면 다시 질문할 수 있습니다. 같은 요청은 한 번만 실행됩니다.")
    if submitted:
        if mode in ("single", "compare") and not question.strip():
            st.warning("질문을 입력하세요.")
            return
        try:
            owned = service.ask(res, principal, scope, question, mode, as_of)
        except ERRORS as exc:
            st.error(str(exc))
            return
        st.session_state["current"] = {"scope": scope, "mode": mode, "as_of": as_of, "question": question}
        _set_owned(st, res, principal, owned)
        st.rerun()


def _set_current(st, current: dict | None) -> None:
    previous = st.session_state.get("current")
    st.session_state["current"] = current
    if previous != current and st.session_state.get("owned"):
        st.session_state["pending_abandon"] = st.session_state["owned"]
        st.session_state["owned"] = None
        st.session_state.pop("evidence_open", None)


def _answer_area(st, res, principal) -> None:
    st.session_state.pop("shown_request", None)
    stale = st.session_state.pop("pending_abandon", None)
    if stale:
        try:
            service.abandon_request(res, principal, stale["request_id"])
        except ERRORS:
            pass
    owned = st.session_state.get("owned")
    current = st.session_state.get("current")
    if not owned or not current:
        return
    target = service.target_key(current["scope"], current["question"], current["mode"], current["as_of"])
    try:
        view = service.request_status(res, principal, owned["request_id"])
    except ERRORS as exc:
        st.error(str(exc))
        return
    active = view.status in ("queued", "running")

    @st.fragment(run_every=1 if active else None)
    def _panel():  # read-only polling of the owned request; never submits
        v = service.request_status(res, principal, owned["request_id"])
        if v.status in ("queued", "running"):
            with st.container(border=True):
                st.markdown(badge(REQUEST_TEXT[v.status]) + " 근거를 찾고 답변을 검증하는 중입니다.")
                st.caption(f"이 요청의 예약 최대 비용 {usd(v.reserved_micro_usd)}" if v.reserved_micro_usd
                           else "아직 비용 예약 전입니다(근거 검색 중).")
                if st.button("요청 취소", key=f"cancel-{v.request_id}"):
                    service.cancel_request(res, principal, v.request_id)
            return
        if active and st.session_state.get("finished_seen") != v.request_id:
            st.session_state["finished_seen"] = v.request_id
            st.rerun(scope="app")  # once: re-enable the form and stop polling
        if not service.may_attach(owned, target, v):
            st.caption("이 요청은 현재 선택과 다른 질문이어서 표시하지 않습니다. '내 최근 요청'에서 볼 수 있습니다.")
            return
        st.session_state["shown_request"] = v.request_id  # its evidence opens in this pane, not in the history
        if current["question"]:
            st.markdown(f"> {plain(current['question'])}")
        render_result(st, res, principal, v)

    _panel()


def render_result(st, res, principal, v) -> None:
    r = v.result
    if r is None:
        st.warning(f"{REQUEST_TEXT.get(v.status, v.status)}: 결과가 없습니다.")
        return
    if not r.evidence:  # basic information and blocked results cite nothing: no empty evidence pane
        render_answer(st, res, principal, r, v)
        return
    cols = st.columns([3, 2], gap="large")
    with cols[0]:
        render_answer(st, res, principal, r, v)
    with cols[1]:
        _evidence_panel(st, res, principal, r)


def render_answer(st, res, principal, r, v=None, ns: str = "cur") -> None:
    """One body per result state (DESIGN.md, Result states). `ns` namespaces the widget keys: the same request may
    be shown at once as the current answer, in the history and on the verification page."""
    label = STATUS_TEXT.get(r.status, r.status)
    if r.status == "technical_error":  # never rendered as a domain answer
        st.error(f"기술 오류: {plain(r.summary)}")
        if r.error:
            st.caption(f"사유 코드: {plain(r.error[:160])}")
    elif r.status == "budget_blocked":
        with st.container(border=True):
            st.markdown(badge(label, "red"))
            st.markdown(plain(r.summary))
            st.caption("검색, 기본 정보, 요구사항 목록, 원문 열람은 계속 무료로 쓸 수 있습니다.")
    else:
        st.markdown(badge(label, STATUS_COLOR.get(r.status, "gray")))
        if r.status == "clarification_required":
            st.info(plain(r.summary) + (f"  \n다음 확인: {plain(r.next_action)}" if r.next_action else ""))
        else:
            st.markdown(plain(r.summary))
    if r.facts:
        _render_facts(st, r.facts)
    if r.inventory:
        _render_inventory(st, r, ns)
    for i, claim in enumerate(r.claims, 1):
        kind = "추론" if claim["kind"] == "inference" else "원문 사실"
        doc = _doc_label(r, claim["doc_id"])
        st.markdown(f"**{i}.** {badge(kind)}" + (f" {badge(doc)}" if doc else "") + f" {plain(claim['text'])}")
        cited = [eid for eid in claim["evidence_ids"] if eid in r.evidence]
        if cited:
            with st.container(horizontal=True, gap="small"):
                for eid in cited:  # a callback runs before the rerun, so every pane renders the choice at once
                    st.button(f"근거 {eid}", key=f"ev-{ns}-{r.request_id}-{i}-{eid}", on_click=_open_evidence,
                              args=(st, r.request_id, eid), help="원문 인용을 오른쪽에 엽니다")
    if r.conflicts:
        st.markdown("**서로 다른 값**")
        table(st, [{"항목": FACT_FIELD.get(c["field"], c["field"]), "값": str(a["value"]),
                    "근거": ", ".join(a["evidence_ids"])} for c in r.conflicts for a in c["alternatives"]])
    if r.missing_fields:
        st.markdown("**확인되지 않은 정보**")
        table(st, [{"항목": FACT_FIELD.get(m["field"], m["field"]), "사유": MISSING_REASON.get(m["reason"], m["reason"]),
                    "문서": _doc_label(r, m["doc_id"]) or "-"} for m in r.missing_fields])
    if r.next_action and r.status != "clarification_required":
        (st.warning if r.status == "insufficient_evidence" else st.caption)(f"다음 확인: {plain(r.next_action)}")
    if v is not None:
        cost = (f"정산 {usd(v.settled_micro_usd)}" + (f" · 보류 {usd(v.reserved_micro_usd)}" if v.reserved_micro_usd
                                                     else ""))
        st.caption(f"요청 {r.request_id[:8]} · {REQUEST_TEXT.get(v.status, v.status)} · "
                   f"{BILLING_TEXT.get(v.billing_state, v.billing_state)} · {cost}")


def _open_evidence(st, request_id: str, evidence_id: str) -> None:
    st.session_state["evidence_open"] = (request_id, evidence_id)


def _doc_label(r, doc_id: str) -> str:
    """In a comparison, which selected document (1 or 2) a claim or limitation belongs to."""
    order = [c["doc_id"] for c in r.coverage]
    return f"문서 {order.index(doc_id) + 1}" if len(order) > 1 and doc_id in order else ""


def _render_facts(st, facts: list[dict]) -> None:
    rows = []
    for f in facts:
        value = f["value"]
        if f["field"] == "amount_krw":
            shown = won(value)
        elif isinstance(value, dict):
            shown = when(value)
        else:
            shown = "미상" if value is None else str(value)
        rows.append({"문서": f["doc_id"][:8], "항목": FACT_FIELD.get(f["field"], f["field"]), "값": shown,
                     "상태": FACT_STATE.get(f["state"], f["state"]),
                     "출처": "검토자 확인" if f["provenance"] == "resolution" else "CSV 기록"})
    table(st, rows)  # static cells: readable by assistive technology, unlike the canvas grid
    st.caption("CSV 기록은 원문과 다를 수 있습니다. 충돌·값 없음은 추정하지 않고 그대로 표시합니다.")


def _render_inventory(st, r, ns: str = "cur") -> None:
    inv = r.inventory
    if inv["completeness_text"] not in (r.summary or ""):  # the summary usually carries it already
        st.caption(plain(inv["completeness_text"]))
    rows = [{"근거": i["evidence_id"], "코드": i["source_form"], "구분": "상세" if i["kind"] == "detail" else "요약",
             "이름": i["name"] or "", "위치": location_text(i["location"]) if i["location"] else ""}
            for i in inv["items"]]
    key = f"inv-{ns}-{r.request_id}"

    def picked():
        chosen = st.session_state[key].selection.rows
        if chosen:
            _open_evidence(st, r.request_id, rows[chosen[0]]["근거"])

    st.dataframe(rows, hide_index=True, width="stretch", on_select=picked, selection_mode="single-row", key=key)
    if inv["repeated_detail_codes"]:
        st.caption("같은 코드의 상세 행이 여러 번 나옵니다: " + plain(", ".join(inv["repeated_detail_codes"])))


def _evidence_panel(st, res, principal, r, ns: str = "cur") -> None:
    opened = st.session_state.get("evidence_open")
    if not opened or opened[0] != r.request_id:
        if r.evidence:
            st.caption("근거 버튼을 누르면 원문 인용과 주변 내용이 여기에 열립니다.")
        return
    try:
        view = service.open_evidence(res, principal, r.request_id, opened[1])
    except ERRORS as exc:
        st.error(str(exc))
        return
    with st.container(border=True):
        st.header(f"근거 {view.evidence_id}")
        st.caption(f"{plain(view.title)} · {location_text(view.location)} · {REVIEW_TEXT.get(view.review_status, '')}")
        for w in view.warnings:
            st.warning(EVIDENCE_WARNING.get(w, w))
        for ctx in view.context or [{"text": view.quote, "cited": True}]:
            st.markdown(marked(ctx["text"], ctx["cited"]))
        st.caption("강조된 부분이 답변이 인용한 원문입니다.")
        if view.download_available:
            try:
                dl = service.original_download(res, principal, view.doc_id, view.source_hash)
                st.download_button("이 근거의 원문 파일 받기", dl.data, file_name=dl.filename, mime=dl.mime,
                                   key=f"evdl-{ns}-{r.request_id}-{view.evidence_id}")
            except ERRORS as exc:
                st.caption(plain(str(exc)))


EVIDENCE_WARNING = {"source_version_changed": "이 근거 이후 원문 파일 버전이 바뀌었습니다. 현재 원문과 다를 수 있습니다.",
                    "extraction_revised": "원문 추출이 다시 이루어졌습니다. 인용은 답변 당시의 추출본입니다.",
                    "source_unreviewed": REVIEW_WARNING["unreviewed"],
                    "source_auto_flagged": REVIEW_WARNING["auto_flagged"],
                    "source_needs_recovery": "이 문서는 복구가 필요한 상태입니다. 원문으로 확인하세요.",
                    "ocr_text": "그림 속 글자를 OCR로 읽은 근거입니다. 원본 그림을 확인하세요."}


def _history(st, res, principal) -> None:
    with st.expander("내 최근 요청"):
        try:
            views = service.list_requests(res, principal, limit=15)
        except ERRORS as exc:
            st.error(str(exc))
            return
        if not views:
            st.caption("아직 요청이 없습니다.")
            return
        by_id = {v.request_id: v for v in views}  # option values are request IDs: equal labels never merge

        def label(rid: str) -> str:
            v = by_id[rid]
            return (f"{v.created_at[:16].replace('T', ' ')} · {REQUEST_TEXT.get(v.status, v.status)} · "
                    f"{v.question[:40] or v.mode} · {rid[:8]}")

        # The chosen request ID is kept per typed name. A label carries the live status, so another request
        # finishing changes the options; the choice must survive that and is cleared only when it is gone.
        pick_key, widget_key = f"history-pick-{principal.member_id}", f"history-{principal.member_id}"
        picked = st.session_state.get(pick_key)
        if picked not in by_id:
            picked = st.session_state[pick_key] = None
        options = list(by_id)

        def remember() -> None:
            st.session_state[pick_key] = st.session_state.get(widget_key)

        choice = st.selectbox("요청", options, format_func=label, index=options.index(picked) if picked else None,
                              placeholder="다시 볼 요청을 고르세요", key=widget_key, on_change=remember)
        choice = choice if choice in by_id else picked
        if choice:
            v = by_id[choice]
            st.info("이전 요청입니다. 현재 화면의 질문에 대한 답변이 아닙니다. 범위: "
                    + ", ".join(f"{s['doc_id'][:8]}@{s['source_hash'][:8]}" for s in v.scope)
                    + f" · 기준일 {v.as_of} · 방식 {v.mode}")
            if v.question:
                st.markdown(f"> {plain(v.question)}")
            if v.result is not None:
                render_answer(st, res, principal, v.result, v, ns="hist")
                if st.session_state.get("shown_request") != v.request_id:  # not already open above
                    _evidence_panel(st, res, principal, v.result, ns="hist")


# ---------------------------------------------------------------- 검증


def verify_page(st, res, principal) -> None:
    st.title("검증")
    try:
        ov = service.evaluation_overview(res, principal)
        waiting = service.gold_awaiting_second_review(res, principal)
    except ERRORS as exc:
        st.error(str(exc))
        return
    _summary_tiles(st, ov, waiting)
    tabs = st.tabs(["검색 추적", "평가·릴리스", "골드·봉인 검토", "원문·수집"])
    with tabs[0]:
        _trace_tab(st, res, principal)
        st.divider()
        _compare_tab(st, res, principal)
        st.divider()
        _request_export(st, res, principal)
    with tabs[1]:
        _evaluation_tab(st, res, principal, ov)
    with tabs[2]:
        _gold_tab(st, res, principal, ov, waiting)
    with tabs[3]:
        _fidelity_tab(st, res, principal)
        st.divider()
        _ingestion_section(st, res, principal)
        st.divider()
        _corrections_tab(st, res, principal)
    st.caption(f"빌드 {service.build_head()}")  # the served code's commit, for verification evidence


RELEASE_COLOR = {"ready": "green", "limited": "orange", "blocked": "red"}


def _summary_tiles(st, ov: dict, waiting: list[dict]) -> None:
    release, v, frozen, test = ov["release"], ov["dev_validation"], ov["dev_frozen"], ov["test"]
    tiles = st.columns(4)
    with tiles[0].container(border=True):
        st.caption("최근 릴리스 판정")
        st.markdown(badge(RELEASE_TEXT.get(release["status"], release["status"]), RELEASE_COLOR.get(release["status"],
                                                                                                    "gray"))
                    if release else badge("판정 없음"))
        st.caption(release["release_id"] if release else "freeze-release 이후에 기록됩니다.")
    with tiles[1].container(border=True):
        st.caption("개발 데이터셋")
        if v:
            st.markdown(f"**{v['rows']}문항** " + (badge("gold", "green") if v["label"] == "gold"
                                                 else badge("파일럿(목표 미달)", "orange")))
            st.caption(("유효" if v["ok"] else f"오류 {len(v['errors'])}건") + " · "
                       + (("고정됨" if frozen["current"] else "고정 이후 바뀜") if frozen else "고정 전"))
        else:
            st.markdown(badge("검증 기록 없음"))
            st.caption("validate-gold 이후에 기록됩니다.")
    with tiles[2].container(border=True):
        st.caption("봉인 시험 세트")
        state = (badge("고정됨", "green") if test["current"] else badge("고정 이후 바뀜", "red")) if test["frozen"] \
            else badge("고정 전")
        st.markdown(f"**{test['rows']}문항** {state}")
        st.caption("문항과 라벨은 표시하지 않습니다.")
    with tiles[3].container(border=True):
        st.caption("2차 검토 대기")
        st.markdown(f"**{len(waiting)}건** " + (badge("확인 필요", "blue") if waiting else badge("없음")))
        st.caption("쟁점 표시된 승인 질문")


def _evaluation_tab(st, res, principal, ov: dict | None = None) -> None:
    """Development answer runs and the release decision. The sealed test set appears only as a row count and its
    freeze state; its questions and labels are never served here."""
    ov = ov or service.evaluation_overview(res, principal)
    test = ov["test"]
    st.header("개발 답변 평가")
    st.caption(f"개발(dev) 질문만 이 화면에서 실행합니다. 봉인 시험 세트 {test['rows']}문항은 "
               + ("고정됨" if test["frozen"] else "고정 전") + " 상태이며 봉인 평가는 소유자 CLI로만 실행합니다. "
               "점수는 표본 수와 함께 읽으세요.")
    runs = list(reversed(ov["answer_runs"]))
    if runs:
        table(st, [{"실행": run["run_id"], "설정": run["config"]["label"],
                    "상태": "실행 중" if run["running"] else STATUS_EVAL.get((run["scores"] or {}).get("status"),
                                                                       "점수 없음"),
                    "완료": f"{run['progress']['done']}/{run['progress']['total']}"} for run in runs])
    for run in runs:
        scores = run["scores"] or {}
        if run["running"]:
            _evaluation_progress(st, res, principal, run["run_id"])
        if scores.get("stop_reason"):
            st.warning(f"{run['run_id']} 중단 사유: {plain(scores['stop_reason'])}")
        rows = [{"후보": fid, "방식": MODE_TEXT.get(f["mode"], f["mode"]), "완료": f"{f['completed']}/{f['of']}",
                 "필수 주장 정확도": rate_text(f["required_claim_correctness"]), "치명 오류": len(f["critical_wrong"]),
                 "인용 정밀도(하한)": rate_text(f["citation_precision_lower_bound"]), "미판정 인용": f["links_unjudged"],
                 "부정 질문 처리": rate_text(f["negative_handling"]), "비용": dollars(f["cost"]["settled_micro_usd"]),
                 "p95(ms)": f["latency_ms"]["p95"]} for fid, f in (scores.get("finalists") or {}).items()]
        if rows:
            with st.expander(f"{run['run_id']} 후보별 점수", expanded=run is runs[0]):
                table(st, rows)
                st.caption("인용 정밀도 하한은 사람이 아직 판정하지 않은 인용을 지지하지 않는 것으로 셉니다. "
                           "블라인드 검토표: export-review → import-review (CLI).")
    est = st.session_state.get("eval_estimate")
    if st.button("비용 추정 (무료, 호출 없음)", key="eval-plan"):
        try:
            est = service.plan_answer_evaluation(res, principal)
            st.session_state["eval_estimate"] = est
        except ERRORS as exc:
            st.error(str(exc))
            est = None
    if est:
        table(st, [{"추정": est["estimate_id"], "후보": ", ".join(est["finalists"]), "남은 답변": est["rows_remaining"],
                    "최대 비용": dollars(est["max_micro_usd"]), "gold_eval 남음": dollars(est["envelope_remaining_micro_usd"]),
                    "유효 기한": est["expires_at"][:16]}])
        if not est["fits"]:
            st.error("최대 비용이 평가 예산 또는 운영 한도를 넘어 실행할 수 없습니다.")
        else:
            agree = st.checkbox(f"이 추정으로 개발 답변 평가를 실행합니다 (최대 {usd(est['max_micro_usd'])}, 공유 예산에서 차감)",
                                key=f"eval-agree-{est['estimate_id']}")
            if st.button("평가 실행 (유료)", type="primary", disabled=not agree, key=f"eval-run-{est['estimate_id']}"):
                try:
                    service.start_answer_evaluation(res, principal, est["estimate_id"])
                    st.session_state.pop("eval_estimate", None)
                    st.rerun()
                except ERRORS as exc:
                    st.error(str(exc))
    st.header("릴리스 판정")
    release = ov["release"]
    if not release:
        st.caption("기록된 릴리스 판정이 없습니다.")
        return
    st.markdown(badge(RELEASE_TEXT.get(release["status"], release["status"]), RELEASE_COLOR.get(release["status"], "gray"))
                + f" {plain(release['release_id'])}")
    if release.get("reasons"):
        table(st, [{"#": i, "사유": r} for i, r in enumerate(release["reasons"], 1)])


def _evaluation_progress(st, res, principal, run_id: str) -> None:
    @st.fragment(run_every=2)
    def _poll():
        run = next((r for r in service.evaluation_overview(res, principal)["answer_runs"] if r["run_id"] == run_id), None)
        if run is None or not run["running"]:
            st.rerun(scope="app")
        st.info(f"평가 실행 중 · 완료 {run['progress']['done']}/{run['progress']['total']}개 답변")

    _poll()


def _trace_tab(st, res, principal) -> None:
    st.header("검색 추적")
    try:
        items = service.trace_documents(res, principal)
        rows = service.trace_questions(res, principal)
    except ERRORS as exc:
        st.error(str(exc))
        return
    if not items:
        st.info("색인된 문서가 없습니다.")
        return
    labels = {f"{i['title']} · {i['institution']} · {i['doc_id'][:8]}": i for i in items}
    source = st.radio("질문 출처", ["직접 입력", "검토된 개발 질문"], horizontal=True, key="vsource",
                      disabled=not rows)
    if source == "검토된 개발 질문" and rows:
        row = st.selectbox("개발 질문", rows, format_func=lambda r: f"{r.get('question_id') or r.get('id')} · {r.get('question', '')[:60]}")
        question = row.get("question", "")
        doc_ids = [s["doc_id"] for s in row.get("scope") or []] or row.get("doc_ids") or [row.get("doc_id")]
        default_docs = [k for k, i in labels.items() if i["doc_id"] in doc_ids]
        doc_key = f"vdocs-{row.get('question_id') or row.get('id')}"
        as_of = row.get("as_of_date") or row.get("as_of") or date.today().isoformat()
    else:
        question, default_docs = None, []
        doc_key, as_of = "vdocs", date.today().isoformat()
    # One form: a selection still being committed would otherwise swallow the button's first click.
    with st.form("trace", border=False):
        if question is None:
            question = st.text_input("질문", key="vq")
        docs = st.multiselect("문서 (최대 2개)", list(labels), default=default_docs[:2], max_selections=2, key=doc_key)
        mode = st.selectbox("검색 방식", service.trace_modes(res), format_func=lambda m: MODE_TEXT.get(m, m),
                            key="vmode")
        st.caption("의미 검색 방식은 캐시된 질의 벡터만 사용합니다(무료). 캐시에 없으면 키워드로 대체되며 예상 비용을 표시합니다.")
        submitted = st.form_submit_button("검색만 실행 (무료)", type="primary")
    if submitted and not (question.strip() and docs):
        st.warning("질문을 적고 문서를 하나 이상 고르세요.")
    elif submitted:
        try:
            run = service.run_trace(res, principal, question,
                                    [(labels[d]["doc_id"], labels[d]["source_hash"]) for d in docs], as_of, mode)
            st.session_state["vrun"] = run["run_id"]
        except ERRORS as exc:
            st.error(str(exc))
    run_id = st.session_state.get("vrun")
    if run_id:
        _render_run(st, res, principal, service.verifier_run(res, principal, run_id))


def _stage(st, col, n: int, title: str, value: str, note: str = "") -> None:
    """One tile of the five-stage strip; `note` is already-escaped Markdown."""
    with col.container(border=True):
        st.caption(f"{n}. {title}")
        st.markdown(f"**{plain(value)}**")
        if note:
            st.caption(note)


def _render_run(st, res, principal, t: dict) -> None:
    r = t["retrieval"]
    qe = r.get("query_embedding") or {}
    st.subheader(f"실행 {t['run_id']}")
    st.caption(f"설정 {t['config']['config_id']} · {t['created_at'][:19].replace('T', ' ')} · {plain(t['member_id'])}"
               f" · 기준일 {t.get('as_of') or '-'}")
    cols = st.columns(5)
    parsed = sum(d["parse_status"] == "parsed" for d in t["docs"])
    checked = sum(d["review_status"] not in ("unreviewed", "needs_recovery") for d in t["docs"])
    _stage(st, cols[0], 1, "수집·검수", f"{parsed}/{len(t['docs'])} 수집됨", f"원문 대조 {checked}/{len(t['docs'])}")
    _stage(st, cols[1], 2, "범위·분석", f"질의 토큰 {len(t['query_tokens'])}개", f"색인 {t['index_version'] or '-'}")
    _stage(st, cols[2], 3, "채널 순위", MODE_TEXT.get(r["mode"], r["mode"]),
           "대체 " + plain(r.get("fallback") or "없음") + f" · 질의 벡터 {plain(qe.get('cache', '-'))}")
    _stage(st, cols[3], 4, "선택된 근거", f"{len(r['evidence'])}개 · {r['evidence_tokens']} 토큰",
           f"입력 추정 {t['input_tokens']} 토큰 · {r['timings_ms'].get('total')}ms")
    _stage(st, cols[4], 5, "답변 생성", "미실행" if f"vgen-{t['run_id']}" not in st.session_state else "요청됨",
           f"최대 예상 {usd(t['estimate_micro_usd'])}")
    with st.expander("1. 수집·검수 상태"):
        table(st, [{"파일": d["filename"], "수집": d["parse_status"],
                    "원문 대조": REVIEW_TEXT.get(d["review_status"], d["review_status"]),
                    "사유": d["reason_code"] or "-"} for d in t["docs"]])
    with st.expander("2. 범위·분석"):
        table(st, [{"항목": "색인", "값": f"{t['index_version']} ({t['review_scope']})"},
                   {"항목": "질의 토큰", "값": " ".join(t["query_tokens"]) or "-"},
                   {"항목": "요구사항 코드", "값": ", ".join(t["codes"]) or "없음"}])
    with st.expander(f"3. 채널 순위 · 후보 {len(r['candidates'])}개"):
        st.caption(f"요청 방식 {MODE_TEXT.get(t['config']['mode'], t['config']['mode'])} → 실제 "
                   f"{MODE_TEXT.get(r['mode'], r['mode'])}. 점수는 각 채널 내부 순위용 값이며 답변 신뢰도가 아닙니다.")
        if qe.get("cache") == "miss":
            st.warning(f"질의 벡터가 캐시에 없어 대체 검색으로 고정했습니다. 일반 질문이면 질의 임베딩 "
                       f"{usd(t.get('query_embedding_estimate_micro_usd'))}가 추가되지만, 아래 생성은 이 고정 근거를 "
                       "쓰므로 임베딩을 하지 않습니다.")
        table(st, [{"채널": c["channel"], "순위": c["rank"], "점수": "-" if c["score"] is None else c["score"],
                    "조각": c["chunk_id"][:12]} for c in r["candidates"]])
    st.markdown(f"**4. 선택된 근거 {len(r['evidence'])}개**")
    if t.get("coverage"):
        table(st, [{"문서": c["doc_id"][:8], "근거": c["evidence"], "제한": c.get("limitation") or "-"}
                   for c in t["coverage"]])
    for e in r["evidence"]:
        with st.expander(f"{e['evidence_id']} · {location_text(e['location'])} · {e['token_count']} 토큰"):
            st.markdown(plain(e["quote"]))
            st.caption(f"조각 {plain(e['chunk_id'])} · 추출 {plain(e['extraction_id'][:12])} · 요소 "
                       + plain(", ".join(e["element_ids"])))
    if r["limitations"]:
        st.markdown(badge(f"제한 사항 {len(r['limitations'])}건", "orange"))
        table(st, [{"#": i, "제한 사항": x} for i, x in enumerate(r["limitations"], 1)])
    else:
        st.markdown(badge("제한 사항 없음", "green"))
    if r["excluded"]:
        with st.expander(f"제외된 후보 {len(r['excluded'])}개"):
            table(st, [{"조각": x["chunk_id"][:12], "사유": x["reason"]} for x in r["excluded"]])
    st.download_button("실행 내보내기(JSON)", json.dumps(service.export_verifier_run(res, principal, t["run_id"]),
                                                      ensure_ascii=False, indent=1),
                       file_name=f"{t['run_id']}.json", mime="application/json", key=f"vexp-{t['run_id']}")
    st.markdown("**5. 답변 생성 (별도 유료 작업)**")
    est = t["estimate_micro_usd"]
    gen_key = f"vgen-{t['run_id']}"
    if est is None:
        st.warning("현재 요금표에 없는 모델이라 비용을 추정할 수 없어 유료 생성을 막습니다.")
        return
    if gen_key not in st.session_state:
        try:  # generation runs with this run's frozen mode and limits; an outdated configuration is not offered
            service.resolve_verifier_config(res, t["config"]["config_id"])
        except ERRORS as exc:
            st.warning(str(exc))
            return
    st.caption(f"다시 검색하지 않고 4단계의 고정 근거 {len(r['evidence'])}개와 기준일 {t.get('as_of') or '-'}로 "
               "실행합니다. 예약은 최대 예상 비용을 넘지 않으며, 넘게 되면 호출 없이 거절됩니다.")
    agree = st.checkbox(f"이 범위로 유료 답변 생성을 1회 실행합니다 (최대 예상 {usd(est)})", key=f"vagree-{t['run_id']}")
    if st.button("유료 답변 생성", disabled=not agree, key=f"vbtn-{t['run_id']}") and gen_key not in st.session_state:
        try:
            st.session_state[gen_key] = service.generate_from_run(res, principal, t)
        except ERRORS as exc:
            st.error(str(exc))
    if gen_key in st.session_state:
        _verifier_request(st, res, principal, st.session_state[gen_key])


def _verifier_request(st, res, principal, request_id: str) -> None:
    view = service.request_status(res, principal, request_id)

    @st.fragment(run_every=1 if view.status in ("queued", "running") else None)
    def _poll():
        v = service.request_status(res, principal, request_id)
        if v.status in ("queued", "running"):
            with st.container(border=True):
                st.markdown(badge(REQUEST_TEXT[v.status]) + f" 예약 {usd(v.reserved_micro_usd)}")
            return
        if view.status in ("queued", "running") and st.session_state.get("vfinished") != request_id:
            st.session_state["vfinished"] = request_id
            st.rerun(scope="app")
        render_answer(st, res, principal, v.result, v, ns="ver")

    _poll()


def _request_export(st, res, principal) -> None:
    """Request records (redacted) for any member, as JSON files; removed from 질문하기 in the redesign."""
    with st.expander("요청 기록 내보내기"):
        try:
            rows = service.list_requests(res, principal, all_members=True)
        except ERRORS as exc:
            st.error(str(exc))
            return
        if not rows:
            st.caption("요청 기록이 없습니다.")
            return
        labels = {f"{r.request_id[:8]} · {r.member_id} · {REQUEST_TEXT.get(r.status, r.status)} · "
                  f"{(r.question or '')[:40]}": r.request_id for r in rows}
        rid = labels[st.selectbox("요청", list(labels), key="rexp-pick")]
        st.download_button("요청 기록(JSON) 받기", json.dumps(service.export_request(res, principal, rid),
                                                         ensure_ascii=False, indent=1),
                           file_name=f"request-{rid}.json", mime="application/json", key=f"rexp-{rid}")


def _compare_tab(st, res, principal) -> None:
    st.header("실행 비교")
    runs = service.verifier_runs(res, principal)
    if len(runs) < 2:
        st.info("비교하려면 검색 추적 실행이 두 개 이상 필요합니다.")
        return
    labels = {f"{r['run_id']} · {r['config_id']} · {r['question'][:40]} · {r['created_at'][:16]}": r["run_id"]
              for r in runs}
    c1, c2 = st.columns(2)
    a = c1.selectbox("실행 A", list(labels), index=1, key="cmpa")
    b = c2.selectbox("실행 B", list(labels), index=0, key="cmpb")
    if a == b:
        st.caption("서로 다른 실행을 고르세요.")
        return
    d = service.compare_verifier_runs(res, principal, labels[a], labels[b])
    if not (d["same_question"] and d["same_scope"]):
        st.warning("질문 또는 문서 범위가 다릅니다. 설정 효과만 비교하려면 같은 질문과 범위를 쓰세요.")
    changes = [{"항목": "설정 " + k, "A": json.dumps(va, ensure_ascii=False), "B": json.dumps(vb, ensure_ascii=False)}
               for k, (va, vb) in sorted(d["config_changes"].items())]
    table(st, changes + [
        {"항목": "근거 토큰", "A": d["evidence_tokens"][0], "B": d["evidence_tokens"][1]},
        {"항목": "입력 추정 토큰", "A": d["input_tokens"][0], "B": d["input_tokens"][1]},
        {"항목": "소요(ms)", "A": d["total_ms"][0], "B": d["total_ms"][1]},
        {"항목": "공통 근거", "A": len(d["evidence_common"]), "B": len(d["evidence_common"])},
        {"항목": "한쪽에만 있는 근거", "A": len(d["evidence_only_a"]), "B": len(d["evidence_only_b"])}])
    if not changes:
        st.caption("설정 차이 없음")
    with st.expander("상위 5개 순위와 근거 차이"):
        n = max(len(d["top5_a"]), len(d["top5_b"]))
        table(st, [{"순위": i + 1, "A": (d["top5_a"][i:i + 1] or ["-"])[0][:12],
                    "B": (d["top5_b"][i:i + 1] or ["-"])[0][:12]} for i in range(n)])
        table(st, [{"근거": k, "있는 쪽": side} for side, ks in (("A만", d["evidence_only_a"]),
                                                              ("B만", d["evidence_only_b"])) for k in ks]
              or [{"근거": "-", "있는 쪽": "차이 없음"}])


def _ingestion_section(st, res, principal) -> None:
    st.header("수집 상태")
    rows = service.ingestion_overview(res, principal)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["parse_status"]] = counts.get(r["parse_status"], 0) + 1
    st.markdown(" ".join(badge(f"{k} {n}", "green" if k == "parsed" else "orange") for k, n in sorted(counts.items())))
    st.dataframe([{"파일": r["filename"], "형식": r["format"], "수집": r["parse_status"],
                   "원문 대조": REVIEW_TEXT.get(r["review_status"], r["review_status"]), "사유": r["reason_code"],
                   "경고": ", ".join(r["warnings"])} for r in rows], hide_index=True, width="stretch")


def _corrections_tab(st, res, principal) -> None:
    st.header("수정 기록")
    st.caption("수정 기록은 추가만 됩니다. 기존 데이터셋·실행·봉인된 평가 라벨은 바뀌지 않습니다.")
    runs = service.verifier_runs(res, principal)
    if runs:
        run_id = st.selectbox("대상 검증 실행", [r["run_id"] for r in runs], key="corr-run")
        run = service.verifier_run(res, principal, run_id)
        evidence = run["retrieval"]["evidence"]
        if evidence:
            ev = st.selectbox("원문 근거", evidence, format_func=lambda e: f"{e['evidence_id']} · "
                              f"{location_text(e['location'])}", key="corr-ev")
            element_id = st.selectbox("원문 요소", ev["element_ids"], key="corr-el")
            with st.form("correction", clear_on_submit=True):
                reason = st.text_input("사유")
                quote = st.text_area("원문 인용(그대로)")
                proposal = st.text_area("제안 내용(JSON)", value="{}")
                if st.form_submit_button("수정 기록 추가"):
                    try:
                        service.record_correction(
                            res, principal, reason=reason, quote=quote, proposal=json.loads(proposal or "{}"),
                            run_id=run_id, evidence={"doc_id": ev["doc_id"], "source_hash": ev["source_hash"],
                                                     "extraction_id": ev["extraction_id"], "element_id": element_id})
                        st.success("기록했습니다.")
                    except (*ERRORS, ValueError) as exc:
                        st.error(str(exc))
    rows = service.list_corrections(res, principal)
    if rows:
        table(st, [{"시각": r["created_at"][:19].replace("T", " "), "검토자": r["reviewer"], "사유": r["reason"],
                    "실행": r["run_id"] or "-", "요청": (r["request_id"] or "-")[:8], "인용": r["quote"][:60]}
                   for r in rows])


# ---------------------------------------------------------------- labels shared by 검증 and 데이터셋 만들기

GOLD_TYPE_TEXT = {"direct_fact": "직접 사실", "semantic_paraphrase": "다른 표현", "exact_identifier": "요구사항 코드",
                  "table_numeric": "표·숫자", "multi_passage": "한 문서 여러 근거", "cross_document": "두 문서 비교",
                  "missing_false_premise": "없음·잘못된 전제", "revision_conflict": "차수·중복 충돌",
                  "metadata_direct": "기본 정보"}
STATUS_EVAL = {"complete": "완료", "partial": "일부 완료"}
MATCH_TEXT = {"number": "숫자", "date": "날짜", "text": "문구"}
CRITICAL_TEXT = {"deadline": "중요: 마감", "amount": "중요: 금액", "mandatory_condition": "중요: 필수 조건",
                 "institution": "중요: 기관"}
RELEASE_TEXT = {"ready": "출시 가능", "limited": "제한적 출시", "blocked": "차단"}
ANSWERABILITY_TEXT = {"answerable": "답변 가능", "unanswerable": "원문에 없음", "ambiguous": "모호함", "conflicting": "충돌"}
TYPE_TEXT = {"late_content": "뒷부분 내용", "table_fact": "표 속 사실", "numeric_qualifier": "숫자·조건",
             "repeated_code": "반복 요구사항 코드", "requirement_detail": "요구사항 상세", "condition": "조건",
             "missing_metadata": "메타데이터 누락", "provenance_conflict": "출처 충돌",
             "converter_unavailable": "변환 실패 문서", "not_in_source": "원문에 없음"}


def _claims_rows(claims: list[dict]) -> list[dict]:
    return [{"주장": x.get("claim_id"),
             "형식": MATCH_TEXT.get((x.get("match") or {}).get("type"), (x.get("match") or {}).get("type")),
             "값": json.dumps({k: v for k, v in (x.get("match") or {}).items() if k != "type"}, ensure_ascii=False),
             "단서(하나 이상 표기)": " / ".join("|".join(q) for q in x.get("qualifiers") or []) or "-",
             "중요도": CRITICAL_TEXT.get(x.get("critical_kind"), "보통"),
             "근거 그룹": ", ".join(x.get("support_groups") or [])} for x in claims]


def _decided_table(st, recent: list[dict]) -> None:
    if not recent:
        st.caption("처리된 질문이 없습니다.")
        return
    table(st, [{"질문": r["candidate_id"], "결과": "승인" if r["status"] == "approved" else "거절",
                "검토자": r["decided_by"], "시각": (r["decided_at"] or "")[:19].replace("T", " "),
                "거절 사유": ", ".join(service.REJECT_CATEGORIES.get(c, c) for c in r["categories"]) or "-"}
               for r in recent])


def _gold_tab(st, res, principal, ov: dict, waiting: list[dict]) -> None:
    v, frozen, test = ov["dev_validation"], ov["dev_frozen"], ov["test"]
    st.header("개발 데이터셋")
    if v:
        state = badge("유효", "green") if v["ok"] else badge(f"오류 {len(v['errors'])}건", "red")
        label = badge("gold", "green") if v["label"] == "gold" else badge("파일럿(목표 미달)", "orange")
        fixed = (badge("고정됨", "green") if frozen["current"] else badge("고정 이후 바뀜", "red")) if frozen \
            else badge("고정 전")
        st.markdown(f"**{v['rows']}문항** {state} {label} {fixed}")
        st.caption(f"검증 {v['validated_at'][:16].replace('T', ' ')}")
        table(st, [{"유형": GOLD_TYPE_TEXT.get(t, t), "문항": x["rows"], "목표": x["target"],
                    "충족": "예" if x["met"] else "아니요"} for t, x in v["targets"].items()])
        if v["errors"]:
            with st.expander(f"검증 오류 {len(v['errors'])}건"):
                table(st, [{"#": i, "오류": e} for i, e in enumerate(v["errors"][:50], 1)])
    else:
        st.info("개발 데이터셋 검증 기록이 없습니다. CLI: validate-gold --dataset dev")
    st.header("봉인 시험 세트")
    state = (badge("고정됨", "green") if test["current"] else badge("고정 이후 바뀜", "red")) if test["frozen"] \
        else badge("고정 전")
    st.markdown(f"**{test['rows']}문항** {state}")
    st.caption("봉인 문항과 라벨은 이 화면과 데이터셋 만들기에 표시하지 않습니다. 봉인 검토와 평가는 소유자 CLI로만 합니다.")
    st.header(f"2차 검토 대기 {len(waiting)}건")
    if not waiting:
        st.caption("쟁점 표시된 승인 질문 중 2차 검토를 기다리는 것이 없습니다.")
    else:
        labels = {f"{r['candidate_id']} · {r['question'][:50]}": r for r in waiting}
        r = labels[st.selectbox("2차 검토할 질문", list(labels), key="second-pick")]
        st.markdown(f"> {plain(r['question'])}")
        table(st, _claims_rows(r.get("required_claims") or []) or [{"주장": "-"}])
        table(st, [{"근거 그룹": g.get("group_id"), "문서": (g.get("doc_id") or "")[:8], "인용": alt.get("quote") or "-"}
                   for g in r.get("evidence_groups") or [] for alt in g.get("alternatives") or []]
              or [{"근거 그룹": "-"}])
        with st.form(f"second-{r['candidate_id']}", clear_on_submit=True):
            verdict = st.radio("원문과 대조한 결과", ["동의", "동의하지 않음"], horizontal=True)
            note = st.text_area("확인 내용 (원문 위치 포함)")
            if st.form_submit_button("2차 검토 기록"):
                try:
                    service.gold_second_review(res, principal, r["candidate_id"], verdict == "동의", note)
                    st.session_state["gold_flash"] = f"{r['candidate_id']}: 2차 검토를 기록했습니다."
                    st.rerun()
                except ERRORS as exc:
                    st.error(str(exc))
    if flash := st.session_state.pop("gold_flash", None):
        st.success(flash)
    st.header("최근 검토 결정")
    try:
        _decided_table(st, service.gold_queue(res, principal)["recent"])
    except ERRORS as exc:
        st.error(str(exc))


def _fidelity_tab(st, res, principal) -> None:
    st.header("원문 대조")
    st.caption("한컴 뷰어 인쇄본(PDF)의 글자와 추출 결과를 자동으로 양방향 대조합니다. "
               "사람은 아래에 표시된 곳만 원문 쪽 이미지와 비교하면 됩니다.")
    rows = service.fidelity_overview(res, principal)
    checked = [r for r in rows if r["metrics"]]
    counts = {k: sum(r["review_status"] == k for r in rows) for k in REVIEW_TEXT}
    st.markdown(" ".join(badge(f"{REVIEW_TEXT[k]} {n}", "orange" if k in REVIEW_WARNING else "green")
                         for k, n in counts.items() if n) + f" · 대조 결과 {len(checked)}/{len(rows)}")
    cells = [{"파일": r["filename"], "상태": REVIEW_TEXT[r["review_status"]],
              "표시된 곳": len(r["findings"]) if r["metrics"] else None,
              "추출→원문 불일치(자)": r["metrics"]["extraction_unmatched"] if r["metrics"] else None,
              "원문→추출 불일치(자)": r["metrics"]["rendering_unmatched"] if r["metrics"] else None,
              "이미지 쪽": len(r["metrics"]["image_pages"]) if r["metrics"] else None} for r in rows]
    st.caption("행을 고르면 표시된 곳이 아래에 열립니다.")
    event = st.dataframe(cells, on_select="rerun", selection_mode="single-row", hide_index=True, key="fid",
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


# ---------------------------------------------------------------- 데이터셋 만들기
# Who comes here: a verifier building the development gold set. Drafts come from development-split documents
# only; someone other than whoever started the drafting approves or rejects each draft, always with a note.

DRAFT_TYPE_ORDER = ("direct_fact", "semantic_paraphrase", "exact_identifier", "table_numeric", "multi_passage",
                    "cross_document", "missing_false_premise")
DRAFT_STATUS = {"running": ("생성 중", "blue"), "completed": ("완료", "green"), "failed": ("실패", "red"),
                "interrupted": ("중단됨", "gray")}
ELEMENT_ROWS = 200


def dataset_page(st, res, principal) -> None:
    st.title("데이터셋 만들기")
    st.caption("개발(dev) 문서로만 질문 초안을 만듭니다. gpt-6-luna가 고른 원문 구절에 묶어 초안을 쓰고, 초안 생성을 "
               "요청하지 않은 다른 검토자가 메모와 함께 승인하거나 거절합니다.")
    if flash := st.session_state.pop("gold_flash", None):
        st.success(flash)
    try:
        q = service.gold_queue(res, principal)
    except ERRORS as exc:
        st.error(str(exc))
        return
    tabs = st.tabs(["초안 만들기", f"검토 대기 {len(q['pending'])}건", "처리 기록"])
    with tabs[0]:
        _draft_tab(st, res, principal)
    with tabs[1]:
        _review_tab(st, res, principal, q["pending"])
    with tabs[2]:
        _decided_table(st, q["recent"])


def _draft_tab(st, res, principal) -> None:
    try:
        docs = service.draft_documents(res, principal)
    except ERRORS as exc:
        st.error(str(exc))
        return
    if not docs:
        st.info("초안에 쓸 개발용 문서가 없습니다. 문서 계열 배정(assign-families) 뒤에 다시 여세요.")
        return
    titles = {d["doc_id"]: d["title"] or d["filename"] or d["doc_id"][:8] for d in docs}
    labels = {f"{titles[d['doc_id']]} · {d['institution'] or '-'} · {d['doc_id'][:8]}": d for d in docs}
    slots = st.session_state.setdefault("draft_slots", [])

    st.header("① 문서와 원문 구절")
    picked = st.multiselect("문서 (최대 2개, 두 개면 비교 질문)", list(labels), max_selections=2, key="draft-docs")
    sources = []
    for name in picked:
        d = labels[name]
        phrase = st.text_input(f"구절 찾기: {titles[d['doc_id']]}", key=f"draft-find-{d['doc_id']}",
                               placeholder="예: 계약 기간, 하자보수")
        try:
            els = service.draft_elements(res, principal, d["doc_id"], phrase)
        except ERRORS as exc:
            st.error(str(exc))
            continue
        st.caption(f"원문 요소 {len(els)}개" + (f" 중 앞 {ELEMENT_ROWS}개" if len(els) > ELEMENT_ROWS else "")
                   + ". 질문의 근거가 될 행을 고르세요(여러 개 가능).")
        els = els[:ELEMENT_ROWS]
        event = st.dataframe([{"위치": location_text(e["location"]), "종류": e["kind"], "내용": (e["text"] or "")[:200]}
                              for e in els], on_select="rerun", selection_mode="multi-row", hide_index=True,
                             key=f"draft-el-{d['doc_id']}-{phrase}", width="stretch")
        sources += [{"doc_id": d["doc_id"], "element_id": els[i]["element_id"]}
                    for i in (event.selection.rows if event else [])]
    st.caption(f"고른 구절 {len(sources)}개")

    st.header("② 질문 유형과 의도")
    with st.form("draft-slot", clear_on_submit=True):
        qtype = st.selectbox("질문 유형", DRAFT_TYPE_ORDER, format_func=lambda t: GOLD_TYPE_TEXT.get(t, t))
        intent = st.text_input("의도 (무엇을 묻는 질문인지)", placeholder="예: 하자보수 기간과 시작 시점")
        if st.form_submit_button("질문 자리 추가"):
            try:
                slots.append(service.draft_slot(qtype, intent, [labels[n]["doc_id"] for n in picked], sources))
            except ERRORS as exc:
                st.error(str(exc))
    if slots:
        event = st.dataframe([{"질문 자리": s["question_id"], "유형": GOLD_TYPE_TEXT.get(s["question_type"]),
                               "의도": s["intent"], "문서": " / ".join(titles.get(x, x[:8]) for x in s["doc_ids"]),
                               "구절": len(s["sources"])} for s in slots],
                             on_select="rerun", selection_mode="multi-row", hide_index=True, key="draft-slot-table",
                             width="stretch")
        chosen = event.selection.rows if event else []
        if st.button(f"고른 자리 빼기 ({len(chosen)}개)", disabled=not chosen, key="draft-slot-remove"):
            st.session_state["draft_slots"] = [s for i, s in enumerate(slots) if i not in chosen]
            st.rerun()
    else:
        st.caption("문서와 구절을 고른 뒤 유형과 의도를 적어 질문 자리를 추가하세요.")

    st.header("③ 최대 비용과 생성")
    _draft_cost(st, res, principal, slots)
    st.header("초안 생성 기록")
    _draft_runs(st, res, principal)


def _draft_cost(st, res, principal, slots: list[dict]) -> None:
    if not slots:
        st.caption("질문 자리를 하나 이상 추가하면 비용을 추정할 수 있습니다.")
        return
    ids = [s["question_id"] for s in slots]
    est = st.session_state.get("draft_estimate")
    if est and est["ids"] != ids:  # the slots changed after the estimate
        est = None
    if st.button("최대 비용 추정 (무료, 호출 없음)", key="draft-plan"):
        try:
            est = {"ids": ids, **service.plan_drafting(res, principal, slots)}
            st.session_state["draft_estimate"] = est
        except ERRORS as exc:
            st.error(str(exc))
            est = None
    if not est:
        return
    table(st, [{"질문 자리": len(ids), "호출": est["calls"], "최대 비용": dollars(est["max_micro_usd"]),
                "gold_eval 남음": dollars(est["envelope_remaining_micro_usd"]),
                "예약 가능": dollars(est["available_micro_usd"])}])
    if not est["paid_enabled"]:
        st.error("유료 호출이 꺼져 있어 초안을 만들 수 없습니다.")
        return
    if not est["fits"]:
        st.error("최대 비용이 평가 예산(gold_eval) 또는 운영 한도를 넘습니다. 질문 자리를 줄이세요.")
        return
    agree = st.checkbox(f"질문 자리 {len(ids)}개로 gpt-6-luna 초안 생성을 실행합니다 (최대 {usd(est['max_micro_usd'])}, "
                        "공유 예산에서 차감)", key=f"draft-agree-{ids[0]}-{ids[-1]}-{len(ids)}")
    if st.button("초안 생성 (유료)", type="primary", disabled=not agree, key="draft-run"):
        try:
            run_id = service.start_drafting(res, principal, slots, est["max_micro_usd"])
        except ERRORS as exc:
            st.error(str(exc))
            return
        st.session_state["draft_slots"] = []
        st.session_state.pop("draft_estimate", None)
        st.session_state["gold_flash"] = f"{run_id}: 초안 생성을 시작했습니다."
        st.rerun()


def _draft_runs(st, res, principal) -> None:
    try:
        runs = service.drafting_runs(res, principal)
    except ERRORS as exc:
        st.error(str(exc))
        return
    if not runs:
        st.caption("아직 초안 생성 기록이 없습니다.")
        return
    if any(r["status"] == "running" for r in runs):
        @st.fragment(run_every=2)
        def _poll():  # read-only; reruns the page once when the run ends
            if not any(r["status"] == "running" for r in service.drafting_runs(res, principal)):
                st.rerun(scope="app")
            st.caption("초안 생성 중입니다. 2초마다 상태를 확인합니다.")

        _poll()
    for run in runs:
        label, color = DRAFT_STATUS[run["status"]]
        receipt = run["receipt"] or {}
        with st.container(border=True):
            st.markdown(f"**{run['run_id']}** {badge(label, color)}"
                        + (" " + badge("검토 대기열에 올림", "green") if run["submitted"] else ""))
            st.caption(f"요청 {plain(run['requested_by'] or '-')} · {(run['created_at'] or '')[:16].replace('T', ' ')}"
                       f" · 질문 자리 {run['slots']}개 · 동의한 최대 {usd(run['max_micro_usd'])}"
                       + (f" · 유효 {receipt['rows']} · 무효 {receipt['invalid']} · 정산 "
                          f"{usd(receipt['settled_micro_usd'])}" if receipt else ""))
            if run["error"]:
                st.error(run["error"])
            if run["rows"]:
                with st.expander(f"유효한 초안 {len(run['rows'])}개"):
                    table(st, [{"질문 ID": r["question_id"], "유형": GOLD_TYPE_TEXT.get(r["question_type"]),
                                "질문": r["question"], "필수 주장": len(r["required_claims"])} for r in run["rows"]])
            if run["invalid"]:
                with st.expander(f"무효 초안 {len(run['invalid'])}개"):
                    table(st, [{"질문 ID": x["question_id"], "사유": x["reason"]} for x in run["invalid"]])
            if run["status"] == "completed" and run["rows"] and not run["submitted"]:
                if st.button("검토 대기열에 올리기", key=f"draft-submit-{run['run_id']}"):
                    try:
                        service.submit_drafts(res, principal, run["run_id"])
                    except ERRORS as exc:
                        st.error(str(exc))
                    else:
                        st.session_state["gold_flash"] = f"{run['run_id']}: 유효한 초안을 검토 대기열에 올렸습니다."
                        st.rerun()


def _review_tab(st, res, principal, pending: list[dict]) -> None:
    if not pending:
        st.info("검토할 초안이 없습니다.")
        return
    labels = {f"{p['candidate_id']} · {GOLD_TYPE_TEXT.get(p['type']) or TYPE_TEXT.get(p['type'], p['type'])} · "
              f"{(p['question'] or '')[:50]}": p["candidate_id"] for p in pending}
    cid = labels[st.selectbox("검토할 초안", list(labels), key="review-pick")]
    try:
        c = service.gold_candidate(res, principal, cid)
    except ERRORS as exc:
        st.error(str(exc))
        return
    left, right = st.columns([3, 2], gap="large")
    with left:
        _candidate_card(st, res, principal, c)
    with right:
        _candidate_sources(st, c)
    _decide_form(st, res, principal, c)


def _candidate_card(st, res, principal, c: dict) -> None:
    row, ctx = c["row"], c["context"]
    kind = row.get("question_type") or row.get("type")
    st.header("초안")
    st.info(plain(row.get("question", "")))
    if isinstance(row.get("expected_answer"), str) and row["expected_answer"].strip():
        st.markdown("**초안 답변**")
        st.markdown(plain(row["expected_answer"]))
    if row.get("difficulty_reason"):
        st.caption("난이도 이유: " + plain(row["difficulty_reason"]))
    meta = [{"항목": "유형", "값": GOLD_TYPE_TEXT.get(kind) or TYPE_TEXT.get(kind, kind)},
            {"항목": "데이터셋", "값": c["dataset"]}, {"항목": "초안 작성", "값": c["drafted_by"]},
            {"항목": "초안 생성 요청", "값": (c.get("requested_by") or "-") + " (이 사람은 승인할 수 없음)"
             if c.get("requested_by") else "-"}]
    if c["gold"]:
        meta += [{"항목": "답변 가능성", "값": ANSWERABILITY_TEXT.get(row.get("answerability"), row.get("answerability"))},
                 {"항목": "기대 상태", "값": STATUS_TEXT.get(row.get("expected_status"), row.get("expected_status"))},
                 {"항목": "기준일", "값": row.get("as_of_date") or "-"}]
    table(st, meta)
    for err in c.get("current_errors") or []:
        st.error(f"현재 원문 상태와 맞지 않습니다: {plain(err)}")
    if row.get("required_claims"):
        st.markdown("**필수 주장** (답변이 반드시 맞혀야 하는 값과 단서)")
        table(st, _claims_rows(row["required_claims"]))
    scope = row.get("scope") if isinstance(row.get("scope"), list) else [
        {"doc_id": row.get("doc_id"), "source_hash": row.get("source_hash")}]
    for i, (doc, sc) in enumerate(zip(ctx.get("documents") or [ctx.get("document")], scope)):
        if doc is None:
            st.error("문서를 찾을 수 없습니다.")
            continue
        st.markdown(f"**문서 {i + 1}** {plain(doc.get('title'))} "
                    + badge(REVIEW_TEXT.get(doc.get("review_status"), "원문 대조 미상"),
                            "orange" if doc.get("review_status") in REVIEW_WARNING else "gray"))
        st.caption(f"{plain(doc.get('filename'))} · {(doc.get('format') or '').upper()}")
        for loc in row.get("review_locations") or []:
            if isinstance(loc, dict) and loc.get("doc_id") == sc["doc_id"] and loc.get("source_hash") == sc["source_hash"]:
                pages = loc.get("pdf_pages")
                if isinstance(pages, list) and pages and all(type(p) is int and p > 0 for p in pages):
                    st.caption(f"근거 {plain(loc.get('group_id'))} · PDF 파일 {', '.join(map(str, pages))}쪽 "
                               "(파일 페이지 기준; 인쇄된 쪽 번호와 다를 수 있습니다)")
        if doc.get("unavailable_reason"):
            st.error(doc["unavailable_reason"])
        try:
            dl = service.original_download(res, principal, doc["doc_id"], sc["source_hash"])
            st.download_button("원문 파일 받기", dl.data, file_name=dl.filename, mime=dl.mime,
                               key=f"dl-{c['candidate_id']}-{i}")
        except ERRORS as exc:
            st.caption(plain(str(exc)))
    if row.get("negative_validation"):
        st.markdown("**부재 검증 기록**")
        table(st, [{"항목": k, "값": v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}
                   for k, v in row["negative_validation"].items()])
    if "metadata" in ctx:
        st.markdown("**CSV 메타데이터**")
        meta_rows = ctx["metadata"].items() if not c["gold"] else [
            (f"{d[:8]} {f}", v) for d, fields in ctx["metadata"].items() for f, v in fields.items()]
        table(st, [{"필드": k, "값": json.dumps(v, ensure_ascii=False)} for k, v in meta_rows]
              + [{"필드": f"충돌: {x.get('field')}", "값": json.dumps(x, ensure_ascii=False)}
                 for x in ctx.get("metadata_conflicts") or []])


def _candidate_sources(st, c: dict) -> None:
    st.header("원문 구절")
    st.caption("파란 바탕이 초안이 인용한 구절입니다.")
    for s in service.candidate_spans(c):
        with st.container(border=True):
            st.markdown(f"**{plain(s['label'])}** " + (badge("인용 위치 확인", "green") if s["cited_found"]
                                                       else badge("인용 구절을 찾지 못함", "red")))
            st.caption(location_text(s["location"]) if s["location"] else "위치 없음")
            if s["missing"]:
                st.error("원문 요소를 찾을 수 없습니다.")
                continue
            for seg in s["segments"]:
                st.markdown(marked(seg["text"].strip("\n"), seg["cited"]))
            if not s["cited_found"] and s["quote"]:
                st.markdown("초안 인용: " + plain(s["quote"]))


def _decide_form(st, res, principal, c: dict) -> None:
    with st.form(f"decide-{c['candidate_id']}"):
        st.header("결정")
        note = st.text_area("메모 (필수: 확인한 원문 위치, 또는 무엇이 틀렸는지)")
        inspected = disputed = False
        if c["gold"]:
            inspected = st.checkbox("원문 파일에서 질문·값·단위·조건·근거를 직접 확인했습니다")
            disputed = st.checkbox("쟁점 있음: 마감·금액·기관·필수 조건을 다른 사람이 한 번 더 확인해야 합니다")
        reasons = st.multiselect("거절 사유 (거절할 때)", list(service.REJECT_CATEGORIES),
                                 format_func=service.REJECT_CATEGORIES.get)
        a, b = st.columns(2)
        approve = a.form_submit_button("승인 → 데이터셋", type="primary", width="stretch")
        reject = b.form_submit_button("거절 → 거절 위키", width="stretch")
    if not (approve or reject):
        return
    decision = "approve" if approve else "reject"
    try:
        service.gold_decide(res, principal, c["candidate_id"], decision, c["row_sha256"],
                            reasons if reject else None, note, original_inspected=inspected, disputed=disputed)
    except ERRORS as exc:
        st.error(str(exc))
        return
    st.session_state["gold_flash"] = f"{c['candidate_id']}: {'승인' if approve else '거절'}했습니다."
    st.rerun()
