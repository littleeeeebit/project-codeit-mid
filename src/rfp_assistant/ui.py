"""Consultant, verifier, question-review and budget-administration screens, the visitor name and the live budget widget.

Paid work runs on the service's background executor; these screens own only their current request and poll
read-only status. Layout and state ownership are described in DESIGN.md.

Every source or model string is rendered through `plain` (Markdown/HTML escaped); no unsafe HTML is used.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import subprocess
import uuid
from datetime import date

from . import auth, budget, service
from .contracts import AnswerRequest, DocRef
from .settings import REPO_ROOT, load_settings

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
ASK_MODES = {"single": "근거 기반 답변 (유료 1회)", "metadata": "기본 정보 (무료)", "inventory": "요구사항 목록 (무료)"}
PAIR_MODES = {"compare": "두 문서 비교 (유료 1회)", "metadata": "기본 정보 비교 (무료)"}


def visible_warnings(warnings: list[str]) -> list[str]:
    """Only the highest cap level (exhaustion replaces them all), then the other warnings."""
    caps = [w for w in warnings if w.startswith("cap_")]
    top = ["cap_exhausted"] if "cap_exhausted" in caps else caps[-1:]
    return top + [w for w in warnings if not w.startswith("cap_")]


def usd(micro: int | None) -> str:
    return "미상" if micro is None else f"\\${micro / 1_000_000:,.4f}"  # escaped: a bare $ starts LaTeX


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="입찰메이트 RFP 도우미", layout="wide")
    settings = load_settings()

    @st.cache_resource
    def resources():
        return service.get_resources(settings)

    res = resources()
    # No login (owner decision): every visitor gets every screen. The name only attributes paid requests,
    # review decisions and owner actions; it is not authentication.
    with st.sidebar:
        name = st.text_input("이름 (사용·검토 기록용)", value="owner", max_chars=40, key="member_name")
        st.caption("로그인 없이 사용합니다. 이름은 요청·검토·관리 작업의 기록용입니다.")
        st.caption(f"빌드 {build_head()}")  # the served code's commit, for verification evidence
    principal = auth.visitor(name)
    pages = [st.Page(lambda: consultant_page(st, res, principal), title="컨설턴트", url_path="consultant", default=True),
             st.Page(lambda: verifier_page(st, res, principal), title="검증", url_path="verify"),
             st.Page(lambda: gold_review_page(st, res, principal), title="질문 검토", url_path="review"),
             st.Page(lambda: admin_page(st, res, principal), title="사용량 관리", url_path="admin")]
    with st.sidebar:
        budget_widget(st, res, principal)
    st.navigation(pages).run()


@functools.lru_cache(maxsize=1)
def build_head() -> str:
    """The commit this server process loaded (read once at first render), or `unknown` outside a checkout."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    head = out.stdout.strip()
    return head if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", head) else "unknown"


def budget_widget(st, res, principal) -> None:
    @st.fragment(run_every=2)
    def _widget():  # read-only polling; never dispatches paid work
        st.subheader("공유 사용량")
        try:
            snap = service.budget_snapshot(res, principal)
        except (service.ServiceError, auth.AuthError) as exc:
            st.error("사용량을 읽지 못했습니다(최신 아님). 유료 답변은 이 상태에서 시작되지 않습니다.")
            st.caption(plain(str(exc)[:120]))
            return
        st.progress(min(1.0, max(0.0, snap.spent_micro_usd / snap.allowance_micro_usd)),
                    text=f"사용 {usd(snap.spent_micro_usd)} / {usd(snap.allowance_micro_usd)} "
                         f"({snap.spent_percent:.2f}%)")
        st.caption(f"진행 중 예약 {usd(snap.pending_micro_usd)} · 운영 한도 {usd(snap.cap_micro_usd)} 중 "
                   f"{snap.cap_percent:.1f}% 사용·예약 · 예약 가능 {usd(max(0, snap.available_micro_usd))}")
        for w in visible_warnings(snap.warnings):
            (st.error if w in ("cap_90", "cap_exhausted") else st.warning)(WARNING_TEXT.get(w, w))
        if not snap.paid_enabled:
            st.warning("유료 답변 생성이 꺼져 있습니다. 검색과 원문 열람은 사용할 수 있습니다.")
        if snap.frozen_reason:
            st.error("비용 추정 점검이 필요해 유료 호출이 중지되었습니다.")
        with st.expander("상세"):
            st.caption(f"입력 {snap.tokens['prompt']:,} · 출력 {snap.tokens['completion']:,} · "
                       f"캐시 {snap.tokens['cached']:,} 토큰 · 미확정 {usd(snap.unknown_micro_usd)}")
            if snap.pacing:
                st.caption(f"일정 {snap.project_start} ~ {snap.project_end} · 경과 {snap.pacing['elapsed_fraction']:.0%}"
                           f" · 일정상 기준 {usd(snap.pacing['expected_micro_usd'])} · 남은 {snap.pacing['days_left']}일")
            for member, m in snap.per_member.items():
                st.caption(f"{plain(member)}: {usd(m)}")
            st.caption(snap.tracking_scope)
            st.caption(f"마지막 제공자 대조: {snap.last_reconciliation or '없음'} · 읽은 시각 {snap.read_at[11:19]} UTC")

    _widget()


# ---------------------------------------------------------------- consultant

MODE_TEXT = {"whitespace_bm25": "키워드(공백 분리)", "kiwi_bm25": "키워드(형태소 분석)", "dense": "의미 검색",
             "hybrid": "키워드와 의미 검색 결합", "hybrid_rerank": "키워드와 의미 검색 결합 후 재정렬"}
PAGE_SIZE = 10


def _abandon(res, principal, owned: dict) -> None:
    """The screen no longer owns this request: cancel it only while it is still queued (never dispatched)."""
    view = service.request_status(res, principal, owned["request_id"])
    if view.status == "queued":
        service.cancel_request(res, principal, owned["request_id"])


def _set_owned(st, res, principal, owned: dict | None) -> None:
    old = st.session_state.get("owned")
    if old and (owned is None or old["request_id"] != owned["request_id"]):
        try:
            _abandon(res, principal, old)
        except (service.ServiceError, auth.AuthError):
            pass
    st.session_state["owned"] = owned
    st.session_state.pop("evidence_open", None)


def consultant_page(st, res, principal) -> None:
    st.title("RFP 검색과 근거 기반 질문")
    top = st.columns([3, 1])
    top[0].caption("제공된 과거 공고(2021-10 ~ 2025-02) 기준입니다. 현재 입찰 가능 여부는 이 자료로 판단할 수 없습니다.")
    as_of = top[1].date_input("기준일", value=date.today(), key="as_of").isoformat()
    with st.form("search"):
        c1, c2 = st.columns([2, 1])
        query = c1.text_input("검색어", placeholder="예: 학사정보시스템, 하자보수, SFR-001")
        inst = c2.text_input("발주 기관")
        c3, c4, c5, c6 = st.columns(4)
        amin = c3.number_input("최소 금액(원)", min_value=0, value=None, step=10_000_000)
        amax = c4.number_input("최대 금액(원)", min_value=0, value=None, step=10_000_000)
        close_from = c5.date_input("입찰 마감 시작", value=None)
        close_to = c6.date_input("입찰 마감 끝", value=None)
        c7, c8 = st.columns(2)
        parsed_only = c7.checkbox("원문 수집된 문서만")
        include_unknown = c8.checkbox("금액·마감일이 없거나 충돌하는 문서도 표시")
        if st.form_submit_button("검색"):
            st.session_state["result_limit"] = PAGE_SIZE
    items = service.search_projects(res, principal, {
        "institution": inst, "amount_min": amin, "amount_max": amax, "parsed_only": parsed_only,
        "closing_from": close_from.isoformat() if close_from else None,
        "closing_to": close_to.isoformat() if close_to else None, "include_unknown": include_unknown},
        query, limit=100)
    st.caption(f"{len(items)}건 · 금액/마감일 조건은 값이 없거나 충돌하는 문서를 기본으로 제외합니다.")
    left, right = st.columns([3, 2], gap="large")
    with left:
        _result_cards(st, items)
    with right:
        _selection_panel(st, res, principal, as_of)
    _answer_area(st, res, principal)
    _history(st, res, principal)


def _result_cards(st, items: list[dict]) -> None:
    selected = st.session_state.setdefault("selected", {})
    limit = st.session_state.get("result_limit", PAGE_SIZE)
    for i in items[:limit]:
        key = i["doc_id"]
        with st.container(border=True):
            st.markdown(f"**{plain(i['title'])}**")
            conflict = {c["field"] for c in i["conflicts"]} - set(i.get("resolutions") or {})
            inst = plain(i["institution"] or "기관 미상") + (" (같은 원문에 다른 기관 기록 있음)" if "institution" in conflict
                                                           else "")
            status = ("질문 가능" if i["indexed"] else ("원문 수집 실패" if i["parse_status"] == "quarantined"
                                                       else "색인 전"))
            st.caption(f"발주 기관(CSV) {inst} · {won(i['amount_krw'])} · 공개 {when(i['published_at'])} · "
                       f"마감 {when(i['bid_close'])} · {i['format'].upper()} · {status} · "
                       f"{REVIEW_TEXT[i['review_status']]}")
            for note in i.get("filter_undecided") or []:
                field, _, state = note.partition(":")
                st.caption(f"⚠ {FACT_FIELD.get(field, field)} 값이 {'충돌' if state == 'conflict' else '없어'} 조건 "
                           "충족 여부를 알 수 없습니다.")
            gen = st.session_state.setdefault("pick_gen", {}).get(key, 0)  # a new widget after panel deselection
            checked = st.checkbox("선택", key=f"pick-{key}-{gen}", value=key in selected,
                                  disabled=key not in selected and len(selected) >= 2)
            if checked and key not in selected:
                selected[key] = i
            elif not checked and key in selected:
                selected.pop(key)
    if len(items) > limit and st.button(f"더 보기 ({len(items) - limit}건 남음)"):
        st.session_state["result_limit"] = limit + PAGE_SIZE
        st.rerun()


def _selection_panel(st, res, principal, as_of: str) -> None:
    selected = list(st.session_state.get("selected", {}).values())
    if not selected:
        st.info("왼쪽 목록에서 문서를 하나 고르면 질문할 수 있고, 두 개를 고르면 비교할 수 있습니다.")
        _set_current(st, None)
        return
    st.subheader("선택한 문서")
    for item in selected:
        with st.container(border=True):
            st.markdown(f"**{plain(item['title'])}**")
            st.caption(f"원문 버전 {item['source_hash'][:12]} · {item['format'].upper()} · "
                       f"{REVIEW_TEXT[item['review_status']]} · 기준일 {as_of}")
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
            row = st.columns(2)
            try:
                dl = service.original_download(res, principal, item["doc_id"], item["source_hash"])
                row[0].download_button("원문 파일 받기", dl.data, file_name=dl.filename, mime=dl.mime,
                                       key=f"dl-{item['doc_id']}")
            except (service.ServiceError, auth.AuthError) as exc:
                row[0].caption(f"원문을 열 수 없습니다: {plain(str(exc))}")
            if row[1].button("선택 해제", key=f"unpick-{item['doc_id']}"):
                st.session_state["selected"].pop(item["doc_id"], None)
                gens = st.session_state.setdefault("pick_gen", {})
                gens[item["doc_id"]] = gens.get(item["doc_id"], 0) + 1
                st.rerun()
    modes = ASK_MODES if len(selected) == 1 else PAIR_MODES
    mode = st.radio("질문 방식", list(modes), format_func=modes.get, key=f"mode-{len(selected)}", horizontal=True)
    scope = [(i["doc_id"], i["source_hash"]) for i in selected]
    owned = st.session_state.get("owned")
    busy = False
    if owned:
        try:
            busy = service.request_status(res, principal, owned["request_id"]).status in ("queued", "running")
        except (service.ServiceError, auth.AuthError):
            busy = False
    with st.form("ask"):
        question = ""
        if mode in ("single", "compare"):
            question = st.text_area("질문", max_chars=res.settings.question_max_characters, key=f"q-{mode}",
                                    placeholder="예: 하자보수 기간과 조건은 무엇인가요?" if mode != "compare"
                                    else "예: 두 사업의 하자보수 조건을 비교해 주세요.")
        label = {"single": "근거 기반 답변 받기 (유료 1회)", "compare": "비교 답변 받기 (유료 1회)",
                 "metadata": "기본 정보 보기 (무료)", "inventory": "요구사항 목록 보기 (무료)"}[mode]
        submitted = st.form_submit_button(label, type="primary", disabled=busy)
    if busy:
        st.caption("이전 요청이 끝나면 다시 질문할 수 있습니다. 같은 요청은 한 번만 실행됩니다.")
    current = st.session_state.get("current") or {}
    if current.get("scope") != scope or current.get("mode") != mode or current.get("as_of") != as_of:
        # A changed selection, mode or date: the previous request can no longer attach to this screen.
        _set_current(st, {"scope": scope, "mode": mode, "as_of": as_of, "question": ""})
        current = st.session_state["current"]
    if submitted:
        if mode in ("single", "compare") and not question.strip():
            st.warning("질문을 입력하세요.")
            return
        generation_id = str(uuid.uuid4())
        request = AnswerRequest(idempotency_key=generation_id, generation_id=generation_id, question=question,
                                scope=[DocRef(*x) for x in scope], mode=mode, as_of=as_of)
        try:
            request_id = service.submit_answer(res, principal, request)
        except (service.ServiceError, auth.AuthError) as exc:
            st.error(str(exc))
            return
        current = {"scope": scope, "mode": mode, "as_of": as_of, "question": question}
        st.session_state["current"] = current
        _set_owned(st, res, principal, {"request_id": request_id, "generation_id": generation_id,
                                        "target": target_key(scope, question, mode, as_of)})
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
            _abandon(res, principal, stale)
        except (service.ServiceError, auth.AuthError):
            pass
    owned = st.session_state.get("owned")
    current = st.session_state.get("current")
    if not owned or not current:
        return
    target = target_key(current["scope"], current["question"], current["mode"], current["as_of"])
    try:
        view = service.request_status(res, principal, owned["request_id"])
    except (service.ServiceError, auth.AuthError) as exc:
        st.error(str(exc))
        return
    active = view.status in ("queued", "running")

    @st.fragment(run_every=1 if active else None)
    def _panel():  # read-only polling of the owned request; never submits
        v = service.request_status(res, principal, owned["request_id"])
        if v.status in ("queued", "running"):
            cost = (f"이 요청 예약 최대 비용 {usd(v.reserved_micro_usd)}" if v.reserved_micro_usd
                    else "아직 비용 예약 전(근거 검색 중)")
            st.info(f"{REQUEST_TEXT[v.status]}: 근거를 찾고 답변을 검증하는 중입니다. {cost}")
            if st.button("요청 취소", key=f"cancel-{v.request_id}"):
                service.cancel_request(res, principal, v.request_id)
            return
        if active and st.session_state.get("finished_seen") != v.request_id:
            st.session_state["finished_seen"] = v.request_id
            st.rerun(scope="app")  # once: re-enable the form and stop polling
        if not may_attach(owned, target, v):
            st.caption("이 요청은 현재 선택과 다른 질문이어서 표시하지 않습니다. '내 최근 요청'에서 볼 수 있습니다.")
            return
        st.divider()
        st.session_state["shown_request"] = v.request_id  # its evidence opens in this panel, not in the history
        render_result(st, res, principal, v)

    _panel()


def render_result(st, res, principal, v) -> None:
    r = v.result
    if r is None:
        st.warning(f"{REQUEST_TEXT.get(v.status, v.status)}: 결과가 없습니다.")
        return
    cols = st.columns([3, 2], gap="large")
    with cols[0]:
        render_answer(st, res, principal, r, v)
    with cols[1]:
        _evidence_panel(st, res, principal, r)


def render_answer(st, res, principal, r, v=None, ns: str = "cur") -> None:
    """`ns` namespaces the widget keys: the same request may be shown at once as the current answer, in the
    history and on the verifier page."""
    label = STATUS_TEXT.get(r.status, r.status)
    if r.status == "technical_error":
        st.error(f"{label}: {r.summary}")
        if r.error:
            st.caption(f"사유 코드: {plain(r.error[:160])}")
    elif r.status == "budget_blocked":
        st.warning(f"{label}: {r.summary}")
    elif r.status == "answered":
        st.success(f"{label}: {plain(r.summary)}")
    else:
        st.info(f"{label}: {plain(r.summary)}")
    if r.facts:
        _render_facts(st, r.facts)
    if r.inventory:
        _render_inventory(st, r, ns)
    for i, claim in enumerate(r.claims, 1):
        kind = "추론" if claim["kind"] == "inference" else "원문 사실"
        doc = _doc_label(r, claim["doc_id"])
        st.markdown(f"**{i}. [{kind}]** {plain(claim['text'])}" + (f"  \n{plain(doc)}" if doc else ""))
        buttons = st.columns(max(1, len(claim["evidence_ids"])))
        for col, eid in zip(buttons, claim["evidence_ids"]):
            if eid in r.evidence:  # a callback runs before the rerun, so every panel renders the choice at once
                col.button(f"근거 {eid}", key=f"ev-{ns}-{r.request_id}-{i}-{eid}", on_click=_open_evidence,
                           args=(st, r.request_id, eid))
    if r.missing_fields:
        st.markdown("**확인되지 않은 정보**")
        for m in r.missing_fields:
            doc = _doc_label(r, m["doc_id"])
            st.markdown(f"- {plain(FACT_FIELD.get(m['field'], m['field']))}: "
                        f"{MISSING_REASON.get(m['reason'], m['reason'])}" + (f" ({plain(doc)})" if doc else ""))
    for c in r.conflicts:
        vals = " / ".join(f"{plain(a['value'])} ({', '.join(a['evidence_ids'])})" for a in c["alternatives"])
        st.warning(f"근거 충돌 — {plain(c['field'])}: {vals}")
    if r.next_action:
        st.caption(f"다음 확인: {plain(r.next_action)}")
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
    st.table(rows)  # static cells: readable by assistive technology, unlike the canvas grid
    st.caption("CSV 기록은 원문과 다를 수 있습니다. 충돌·값 없음은 추정하지 않고 그대로 표시합니다.")


def _render_inventory(st, r, ns: str = "cur") -> None:
    inv = r.inventory
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
    except (service.ServiceError, auth.AuthError) as exc:
        st.error(str(exc))
        return
    with st.container(border=True):
        st.markdown(f"**근거 {view.evidence_id}** · {plain(view.title)}")
        st.caption(location_text(view.location) + f" · {REVIEW_TEXT.get(view.review_status, '')}")
        for w in view.warnings:
            st.warning(EVIDENCE_WARNING.get(w, w))
        for ctx in view.context or [{"text": view.quote, "cited": True}]:
            text = plain(ctx["text"])
            st.markdown(f"**{text}**" if ctx["cited"] else text)
        if view.download_available:
            try:
                dl = service.original_download(res, principal, view.doc_id, view.source_hash)
                st.download_button("이 근거의 원문 파일 받기", dl.data, file_name=dl.filename, mime=dl.mime,
                                   key=f"evdl-{ns}-{r.request_id}-{view.evidence_id}")
            except (service.ServiceError, auth.AuthError) as exc:
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
        except (service.ServiceError, auth.AuthError) as exc:
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
            if v.result is not None:
                render_answer(st, res, principal, v.result, v, ns="hist")
                if st.session_state.get("shown_request") != v.request_id:  # not already open above
                    _evidence_panel(st, res, principal, v.result, ns="hist")
                st.download_button("요청 기록 내보내기(JSON)", json.dumps(
                    service.export_request(res, principal, v.request_id), ensure_ascii=False, indent=1),
                    file_name=f"request-{v.request_id[:8]}.json", mime="application/json",
                    key=f"exp-{v.request_id}")


# ---------------------------------------------------------------- verifier


def verifier_page(st, res, principal) -> None:
    principal = auth.require(principal, "verifier")  # the service checks again
    st.title("검증: 검색 경로와 근거 추적")
    tabs = st.tabs(["검색 추적", "실행 비교", "수정 기록", "원문 대조 (HWP)", "수집 상태 (100건)"])
    with tabs[0]:
        _trace_tab(st, res, principal)
    with tabs[1]:
        _compare_tab(st, res, principal)
    with tabs[2]:
        _corrections_tab(st, res, principal)
    with tabs[3]:
        _fidelity_tab(st, res, principal)
    with tabs[4]:
        rows = service.ingestion_overview(res, principal)
        st.dataframe([{k: r[k] for k in ("filename", "format", "parse_status", "review_status", "reason_code")}
                      | {"warnings": ", ".join(r["warnings"])} for r in rows], hide_index=True, width="stretch")


def _trace_tab(st, res, principal) -> None:
    items = [i for i in service.search_projects(res, principal, {"parsed_only": True}, "", limit=100) if i["indexed"]]
    if not items:
        st.info("색인된 문서가 없습니다.")
        return
    labels = {f"{i['title']} · {i['institution']} · {i['doc_id'][:8]}": i for i in items}
    rows = service.dataset_rows(res, principal, "dev-pilot")
    source = st.radio("질문", ["직접 입력", "검토된 개발 질문"], horizontal=True, key="vsource",
                      disabled=not rows)
    if source == "검토된 개발 질문" and rows:
        row = st.selectbox("개발 질문", rows, format_func=lambda r: f"{r.get('id')} · {r.get('question', '')[:60]}")
        question = row.get("question", "")
        default_docs = [k for k, i in labels.items() if i["doc_id"] in (row.get("doc_ids") or [row.get("doc_id")])]
    else:
        question = st.text_input("질문", key="vq")
        default_docs = []
    docs = st.multiselect("문서 (최대 2개)", list(labels), default=default_docs[:2], max_selections=2, key="vdocs")
    c1, c2, c3 = st.columns(3)
    serving = res.serving()
    modes = list(dict.fromkeys([serving["mode"], "kiwi_bm25", "whitespace_bm25", "dense", "hybrid", "hybrid_rerank"]))
    mode = c1.selectbox("검색 방식", modes, format_func=lambda m: MODE_TEXT.get(m, m), key="vmode")
    pair = len(docs) == 2  # a balanced comparison needs one unit per document
    units = c2.number_input("근거 최대 개수", 2 if pair else 1, res.settings.evidence_max_units,
                            res.settings.evidence_max_units,
                            help="두 문서 비교는 문서마다 1개 이상이 필요해 최소 2개입니다." if pair else None)
    target = c3.number_input("근거 목표 토큰", 200, res.settings.evidence_max_tokens,
                             res.settings.evidence_target_tokens, step=100)
    limits = {}
    if units != res.settings.evidence_max_units:
        limits["evidence_max_units"] = int(units)
    if target != res.settings.evidence_target_tokens:
        limits["evidence_target_tokens"] = int(target)
    if mode in ("dense", "hybrid", "hybrid_rerank"):
        st.caption("의미 검색은 캐시된 질의 벡터만 사용합니다(무료). 캐시에 없으면 키워드로 대체되며 예상 비용을 표시합니다.")
    if st.button("검색만 실행 (무료)", type="primary") and question.strip() and docs:
        try:
            run = service.verifier_trace(res, principal, question,
                                         [DocRef(labels[d]["doc_id"], labels[d]["source_hash"]) for d in docs],
                                         date.today().isoformat(),
                                         mode=None if mode == serving["mode"] else mode, limits=limits or None)
            st.session_state["vrun"] = run["run_id"]
        except (service.ServiceError, auth.AuthError) as exc:
            st.error(str(exc))
    run_id = st.session_state.get("vrun")
    if run_id:
        _render_run(st, res, principal, service.verifier_run(res, principal, run_id))


def _render_run(st, res, principal, t: dict) -> None:
    r = t["retrieval"]
    qe = r.get("query_embedding") or {}
    st.markdown(f"**실행 {t['run_id']}** · 설정 {t['config']['config_id']} · {t['created_at'][:19]} · "
                f"{plain(t['member_id'])}")
    st.caption("1. 수집·검수 상태")
    st.dataframe([{k: d[k] for k in ("filename", "parse_status", "review_status", "reason_code")} for d in t["docs"]],
                 hide_index=True, width="stretch")
    st.caption(f"2. 범위·분석 · 색인 {t['index_version']} ({t['review_scope']}) · 질의 토큰: "
               + plain(" ".join(t["query_tokens"])) + (f" · 코드 {', '.join(t['codes'])}" if t["codes"] else ""))
    st.caption(f"3. 채널 순위 · 요청 모드 {t['config']['mode']} → 실제 {r['mode']} · 대체 {r.get('fallback') or '없음'} · "
               f"질의 벡터 {qe.get('cache', '-')}"
               + (f" (캐시 없음 → 대체 검색으로 고정. 일반 질문이면 질의 임베딩 "
                  f"{usd(t.get('query_embedding_estimate_micro_usd'))}가 추가되지만, 아래 생성은 이 고정 근거를 쓰므로 "
                  "임베딩을 하지 않습니다)" if qe.get("cache") == "miss" else ""))
    st.caption("점수는 각 채널 내부 순위용 값이며 답변 신뢰도가 아닙니다.")
    st.dataframe(r["candidates"], hide_index=True, width="stretch")
    st.caption(f"4. 선택된 근거 {len(r['evidence'])}개 · 근거 토큰 {r['evidence_tokens']} · 최종 입력 추정 "
               f"{t['input_tokens']} 토큰 · 소요 {r['timings_ms'].get('total')}ms")
    if t.get("coverage"):
        st.caption("문서별 범위: " + ", ".join(f"{c['doc_id'][:8]}={c['evidence']}개" + (
            f"({c['limitation']})" if c.get("limitation") else "") for c in t["coverage"]))
    for e in r["evidence"]:
        with st.expander(f"{e['evidence_id']} · {e['token_count']} 토큰 · {location_text(e['location'])}"):
            st.markdown(plain(e["quote"]))
            st.code(json.dumps({"chunk_id": e["chunk_id"], "extraction_id": e["extraction_id"],
                                "element_ids": e["element_ids"], "location": e["location"]}, ensure_ascii=False,
                               indent=1), language="json")
    st.markdown("**제한 사항**: " + plain(", ".join(r["limitations"]) or "없음"))
    if r["excluded"]:
        with st.expander("제외된 후보"):
            st.dataframe(r["excluded"], hide_index=True)
    st.download_button("실행 내보내기(JSON)", json.dumps(service.export_verifier_run(res, principal, t["run_id"]),
                                                      ensure_ascii=False, indent=1),
                       file_name=f"{t['run_id']}.json", mime="application/json", key=f"vexp-{t['run_id']}")
    st.divider()
    est = t["estimate_micro_usd"]
    st.markdown("**5. 답변 생성 (별도 유료 작업)**")
    gen_key = f"vgen-{t['run_id']}"
    if est is None:
        st.warning("현재 요금표에 없는 모델이라 비용을 추정할 수 없어 유료 생성을 막습니다.")
        return
    if gen_key not in st.session_state:
        try:  # generation runs with this run's frozen mode and limits; an outdated configuration is not offered
            service.resolve_verifier_config(res, t["config"]["config_id"])
        except service.ServiceError as exc:
            st.warning(str(exc))
            return
    st.caption(f"생성은 다시 검색하지 않고 위 4단계의 고정 근거 {len(r['evidence'])}개와 기준일 "
               f"{t.get('as_of') or '-'}로 실행합니다(설정 {t['config']['config_id']}). 예약은 아래 최대 예상 비용을 "
               "넘지 않으며, 넘게 되면 호출 없이 거절됩니다.")
    agree = st.checkbox(f"이 범위로 유료 답변 생성을 1회 실행합니다 (최대 예상 {usd(est)})", key=f"vagree-{t['run_id']}")
    if st.button("유료 답변 생성", disabled=not agree, key=f"vbtn-{t['run_id']}") and gen_key not in st.session_state:
        scope = [DocRef(s["doc_id"], s["source_hash"]) for s in t["scope"]]
        try:
            st.session_state[gen_key] = service.submit_answer(res, principal, AnswerRequest(
                idempotency_key=gen_key, generation_id=gen_key, question=t["question"], scope=scope,
                mode="compare" if len(scope) == 2 else "single", as_of=t.get("as_of") or date.today().isoformat(),
                config_id=t["config"]["config_id"], verifier_run_id=t["run_id"]))
        except (service.ServiceError, auth.AuthError) as exc:
            st.error(str(exc))
    if gen_key in st.session_state:
        _verifier_request(st, res, principal, st.session_state[gen_key])


def _verifier_request(st, res, principal, request_id: str) -> None:
    view = service.request_status(res, principal, request_id)

    @st.fragment(run_every=1 if view.status in ("queued", "running") else None)
    def _poll():
        v = service.request_status(res, principal, request_id)
        if v.status in ("queued", "running"):
            st.info(f"{REQUEST_TEXT[v.status]} · 예약 {usd(v.reserved_micro_usd)}")
            return
        if view.status in ("queued", "running") and st.session_state.get("vfinished") != request_id:
            st.session_state["vfinished"] = request_id
            st.rerun(scope="app")
        render_answer(st, res, principal, v.result, v, ns="ver")
        with st.expander("결과 원본(JSON)"):
            st.code(json.dumps(service.export_request(res, principal, request_id), ensure_ascii=False, indent=1),
                    language="json")

    _poll()


def _compare_tab(st, res, principal) -> None:
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
    st.markdown("**설정 차이**")
    st.json(d["config_changes"] or {"차이": "없음"})
    st.dataframe([{"항목": "근거 토큰", "A": d["evidence_tokens"][0], "B": d["evidence_tokens"][1]},
                  {"항목": "입력 추정 토큰", "A": d["input_tokens"][0], "B": d["input_tokens"][1]},
                  {"항목": "소요(ms)", "A": d["total_ms"][0], "B": d["total_ms"][1]},
                  {"항목": "공통 근거", "A": len(d["evidence_common"]), "B": len(d["evidence_common"])},
                  {"항목": "한쪽에만 있는 근거", "A": len(d["evidence_only_a"]), "B": len(d["evidence_only_b"])}],
                 hide_index=True, width="stretch")
    with st.expander("상위 5개 순위와 근거 차이"):
        st.json({"top5_a": d["top5_a"], "top5_b": d["top5_b"], "only_a": d["evidence_only_a"],
                 "only_b": d["evidence_only_b"]})


def _corrections_tab(st, res, principal) -> None:
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
                    except (service.ServiceError, auth.AuthError, ValueError) as exc:
                        st.error(str(exc))
    rows = service.list_corrections(res, principal)
    if rows:
        st.dataframe([{"시각": r["created_at"][:19], "검토자": r["reviewer"], "사유": r["reason"],
                       "실행": r["run_id"], "요청": r["request_id"], "인용": r["quote"][:60]} for r in rows],
                     hide_index=True, width="stretch")


# ---------------------------------------------------------------- budget administration


def admin_page(st, res, principal) -> None:
    principal = auth.require(principal, "budget_admin")
    st.title("사용량 관리")
    st.caption("소유자 작업 화면입니다. 모든 작업은 입력한 이름·사유와 함께 감사 기록에 남습니다. "
               "'한 시간 지난 보류 일괄 해제' 같은 작업은 제공하지 않습니다.")
    tabs = st.tabs(["미확정 비용", "제공자 대조", "외부 사용 조정", "유료 호출 상태", "감사 기록"])
    with tabs[0]:
        rows = service.unresolved_attempts(res, principal)
        if not rows:
            st.success("미확정 또는 보류 중인 호출이 없습니다.")
        else:
            st.dataframe([{"시도": r["attempt_id"][:8], "상태": r["state"], "단계": r["stage"], "목적": r["purpose"],
                           "구성원": r["member_id"], "예약": usd(r["reserved_micro_usd"]), "발송": r["dispatched_at"],
                           "오류": json.dumps(r["error"], ensure_ascii=False)[:80]} for r in rows],
                         hide_index=True, width="stretch")
            unknown = [r for r in rows if r["state"] in ("unknown", "reconciled")]
            if unknown:
                with st.form("settle"):
                    st.markdown("**제공자 사용량 증빙으로 정산**")
                    a = st.selectbox("시도", unknown, format_func=lambda r: f"{r['attempt_id']} · {r['stage']}")
                    c1, c2, c3 = st.columns(3)
                    prompt = c1.number_input("입력 토큰", 0, step=1)
                    completion = c2.number_input("출력 토큰", 0, step=1)
                    cached = c3.number_input("캐시 입력 토큰", 0, step=1)
                    evidence = st.text_input("증빙(날짜가 있는 제공자 내보내기 위치 등)")
                    reason = st.text_input("사유")
                    if st.form_submit_button("정산"):
                        try:
                            out = service.settle_from_evidence(res, principal, a["attempt_id"], {
                                "prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached},
                                None, evidence, reason)
                            st.success(f"정산 {usd(out['settled_micro_usd'])}")
                        except (service.ServiceError, budget.BudgetError, auth.AuthError) as exc:
                            st.error(str(exc))
    with tabs[1]:
        st.caption("닫힌 구간의 제공자 총액에서 같은 구간의 로컬 정산액을 뺀 차이만 기록합니다. 같은 기록을 다시 넣어도 바뀌지 않습니다.")
        with st.form("reconcile"):
            rec_id = st.text_input("대조 ID")
            c1, c2 = st.columns(2)
            start = c1.text_input("구간 시작(ISO, UTC)", placeholder="2026-10-01T00:00:00+00:00")
            end = c2.text_input("구간 끝(ISO, UTC)", placeholder="2026-10-01T23:59:59+00:00")
            total = st.number_input("제공자 총액(USD)", 0.0, step=0.01, format="%.6f")
            scope = st.text_input("제공자 프로젝트 범위")
            evidence = st.text_input("증빙")
            covered = st.text_area("포함된 미확정 시도 ID(줄마다 하나)")
            if st.form_submit_button("대조 기록"):
                try:
                    service.reconcile(res, principal, {
                        "reconciliation_id": rec_id, "interval_start": start, "interval_end": end,
                        "provider_total_micro_usd": round(total * 1_000_000), "scope": scope, "evidence": evidence,
                        "covered_attempt_ids": [x.strip() for x in covered.splitlines() if x.strip()]})
                    st.success("기록했습니다.")
                except (service.ServiceError, budget.BudgetError, auth.AuthError) as exc:
                    st.error(str(exc))
    with tabs[2]:
        with st.form("adjust"):
            key = st.text_input("조정 키(중복 방지)")
            amount = st.number_input("금액(USD, 음수는 정정)", value=0.0, step=0.01, format="%.6f")
            evidence = st.text_input("증빙")
            reason = st.text_input("사유")
            if st.form_submit_button("조정 기록"):
                try:
                    applied = service.add_external_adjustment(res, principal, key, round(amount * 1_000_000),
                                                              evidence, reason)
                    st.success("기록했습니다." if applied else "이미 기록된 조정입니다.")
                except (service.ServiceError, budget.BudgetError, auth.AuthError) as exc:
                    st.error(str(exc))
    with tabs[3]:
        snap = service.budget_snapshot(res, principal)
        st.markdown(f"유료 호출: **{'켜짐' if snap.paid_enabled else '꺼짐'}**"
                    + (f" · 중지 사유: {plain(snap.frozen_reason)}" if snap.frozen_reason else ""))
        with st.form("paid"):
            enable = st.toggle("유료 호출 허용", value=snap.paid_enabled and not snap.frozen_reason)
            reason = st.text_input("사유")
            if st.form_submit_button("적용"):
                try:
                    service.set_paid_enabled(res, principal, enable, reason)
                    st.success("적용했습니다.")
                except (service.ServiceError, budget.BudgetError, auth.AuthError) as exc:
                    st.error(str(exc))
    with tabs[4]:
        events = service.audit_events(res, principal)
        if not events:
            st.caption("기록된 관리 작업이 없습니다.")
        else:  # static cells: readable by assistive technology and text search, unlike the canvas grid
            st.table([{"시각": e["created_at"][:19], "작업자": e["actor"], "작업": e["action"], "대상": e["target"],
                       "사유": e["reason"]} for e in events])


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
