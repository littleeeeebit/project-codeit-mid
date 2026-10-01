"""Frozen verifier runs on an isolated corpus copy (prepared by tools/verify.py) generate from exactly their evidence.

For the configured HWP and PDF: a single-document run, a two-document run and a two-document `whitespace_bm25`
run limited to 2 evidence units. Each is frozen with `verifier_trace`, then generated through the verifier page's
consent checkbox and button (Streamlit AppTest) with the fake provider. Expected for each: same retrieval mode,
same `(doc_id, extraction_id, element_ids)` and quotes, same input-token estimate, a single generation attempt
whose reservation equals the displayed maximum, and no query embedding. Writes verifier runs and fake ledger rows
into the declared copy only. Writes `<run_dir>/real-corpus-frozen.json`; exit 0 only when every case passes.

    python tools/verification/real_corpus_frozen.py <run_dir>     # inputs.json written by the verifier-runs flow
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from rfp_assistant import auth, service, store
from rfp_assistant.contracts import DocRef
from rfp_assistant.settings import load_settings

APP = ("import streamlit as st\nfrom rfp_assistant import ui\n"
       "ui._render_run(st, st.session_state.res, st.session_state.principal, st.session_state.run)\n")


def evidence(run: dict) -> list:
    return [(e["doc_id"], e["extraction_id"], e["element_ids"], e["quote"]) for e in run["retrieval"]["evidence"]]


def case(res, principal, name: str, refs: list[DocRef], question: str, **options) -> dict:
    frozen = service.verifier_run(res, principal, service.verifier_trace(
        res, principal, question, refs, date.today().isoformat(), **options)["run_id"])
    app = AppTest.from_string(APP)
    app.session_state["res"], app.session_state["principal"], app.session_state["run"] = res, principal, frozen
    app.run(timeout=60)
    app.checkbox[0].check().run(timeout=60)
    next(b for b in app.button if b.key == f"vbtn-{frozen['run_id']}").click().run(timeout=60)
    errors = [e.message for e in app.exception]
    rid = app.session_state[f"vgen-{frozen['run_id']}"] if not errors else None
    out = {"case": name, "run_id": frozen["run_id"], "request_id": rid, "ui_errors": errors,
           "frozen_mode": frozen["retrieval"]["mode"], "coverage": frozen.get("coverage")}
    if rid is None:
        return {**out, "passed": False}
    deadline = time.monotonic() + 60
    while (view := service.request_status(res, principal, rid)).status in ("queued", "running"):
        if time.monotonic() > deadline:
            return {**out, "passed": False, "error": "request did not finish"}
        time.sleep(0.05)
    trace = json.loads(service.get_request(res, principal, rid)["trace_json"])
    with store.open_db(res.settings.db_path) as conn:
        attempts = [dict(r) for r in conn.execute(
            "SELECT stage, state, reserved_micro_usd FROM attempts WHERE request_id = ?", (rid,))]
    checks = {
        "answered": (view.status, view.result.status) == ("completed", "answered"),
        "same_mode": trace["retrieval"]["mode"] == frozen["retrieval"]["mode"],
        "same_evidence": evidence(trace) == evidence(frozen),
        "same_input_tokens": trace["input_tokens_estimate"] == frozen["input_tokens"],
        "one_generation_attempt": [a["stage"] for a in attempts] == ["generation"],
        "reserved_equals_displayed_maximum": sum(a["reserved_micro_usd"] for a in attempts)
        == frozen["estimate_micro_usd"],
    }
    return {**out, "paid_mode": trace["retrieval"]["mode"], "evidence_units": len(evidence(frozen)),
            "input_tokens": frozen["input_tokens"], "displayed_maximum_micro_usd": frozen["estimate_micro_usd"],
            "attempts": attempts, "checks": checks, "passed": all(checks.values())}


def main(run_dir: Path) -> int:
    rc = json.loads((run_dir / "inputs.json").read_text(encoding="utf-8"))["real_corpus"]
    if not rc or rc.get("isolated_copy") is not True:
        print("real_corpus is not configured as an isolated copy", file=sys.stderr)
        return 2
    settings = load_settings(source_dir=Path(rc["source_dir"]), data_dir=Path(rc["data_dir"]), provider="fake")
    res = service.Resources(settings, recover=True)
    principal = auth.visitor("verification-runner")
    try:
        serving = {k: v for k, v in res.serving().items() if k in ("run_id", "mode", "index_version", "dense_version")}
        with store.open_db(settings.db_path) as conn:
            refs = {}
            for key in ("hwp_doc_id", "pdf_doc_id"):
                row = conn.execute("SELECT active_source_hash FROM documents WHERE doc_id = ?",
                                   (rc[key],)).fetchone()
                if row is None:
                    raise SystemExit(f"{key} {rc[key]} is not in the copy's database")
                refs[key] = DocRef(rc[key], row[0])
        q, hwp, pdf = rc["question"], refs["hwp_doc_id"], refs["pdf_doc_id"]
        results = [case(res, principal, "hwp_single", [hwp], q),
                   case(res, principal, "pair", [hwp, pdf], q),
                   case(res, principal, "pair_whitespace_units_2", [hwp, pdf], q, mode="whitespace_bm25",
                        limits={"evidence_max_units": 2})]
    finally:
        res.close()
    report = {"serving": serving, "cases": results, "passed": all(r["passed"] for r in results)}
    (run_dir / "real-corpus-frozen.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")
    print(json.dumps({c["case"]: c["passed"] for c in results}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
