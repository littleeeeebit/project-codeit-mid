"""Explicit local maintenance commands using the same contracts and gateway.

Run: python -m rfp_assistant.cli <command> ...  Errors exit nonzero with an actionable reason.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import auth, budget, chunking, evaluation, fidelity, gold, ingestion, ops, service, store
from .settings import DEFAULT_RATES, RATE_VERSION, REPO_ROOT, load_settings


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def cmd_init(args, settings) -> int:
    if not args.paid_disabled:
        print("init only initializes in paid-disabled mode; pass --paid-disabled", file=sys.stderr)
        return 2
    version = store.init_schema(settings.db_path)
    budget.ensure_budget_row(settings.db_path)
    snap = budget.snapshot(settings.db_path)
    _print({"schema_version": version, "data_dir": str(settings.data_dir), "paid_enabled": snap.paid_enabled,
            "spent_micro_usd": snap.spent_micro_usd})
    return 0


def cmd_manifest(args, settings) -> int:
    store.init_schema(settings.db_path)
    report = ingestion.import_manifest(settings)
    _print(report)
    return 0


def cmd_ingest(args, settings) -> int:
    results = ingestion.ingest(settings, args.doc_id, force=args.force)
    report = ingestion.write_ingest_report(settings, results)
    _print([{k: v for k, v in r.items() if k != "traceback"} for r in results])
    _print({"parse_status": ingestion.manifest_report(settings)["parse_status"], "report": str(report)})
    return 1 if any(r["status"] == "error" for r in results) else 0


def cmd_import_reviews(args, settings) -> int:
    _print(ingestion.import_reviews(settings, Path(args.file)))
    return 0


def cmd_recover_source(args, settings) -> int:
    result = ingestion.recover_source(settings, args.doc_id, Path(args.converted_file), Path(args.review_file))
    _print(result)
    return 0 if result["status"] == "parsed" else 1


def cmd_identity(args, settings) -> int:
    rows = ingestion.identity_report(settings)
    if not args.all:
        rows = [r for r in rows if r["provenance_conflicts"] or False in r["agreement"].values()]
    _print(rows)
    return 0


def cmd_resolve_metadata(args, settings) -> int:
    value = json.loads(args.value)
    _print({"resolution_id": ingestion.resolve_metadata(settings, args.doc_id, args.field, value, args.rationale,
                                                        args.evidence, args.actor)})
    return 0


def _source_for(settings, doc_id: str) -> str:
    with store.open_db(settings.db_path) as conn:
        row = conn.execute("SELECT active_source_hash FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown doc_id {doc_id}")
    return row[0]


def cmd_review(args, settings) -> int:
    source_hash = _source_for(settings, args.doc_id)
    if args.action == "sheet":
        print(ingestion.review_sheet(settings, source_hash))
        return 0
    if args.action == "print":
        print(fidelity.print_hwp(settings, source_hash))
        return 0
    if args.action == "render":
        print(ingestion.render_pages(settings, source_hash, Path(args.pdf) if args.pdf else None))
        return 0
    locations = json.loads(Path(args.locations).read_text(encoding="utf-8")) if args.locations else []
    findings = json.loads(Path(args.findings).read_text(encoding="utf-8")) if args.findings else {}
    print(ingestion.record_review(settings, source_hash, args.reviewer, args.status, locations, findings))
    return 0


def cmd_fidelity(args, settings) -> int:
    if args.action == "show":
        row = fidelity.latest(settings, _source_for(settings, args.doc_id[0] if args.doc_id else ""))
        _print({**row, "metrics_json": json.loads(row["metrics_json"]),
                "findings_json": json.loads(row["findings_json"])} if row else None)
        return 0
    if args.doc_id:
        results = [fidelity.verify_source(settings, _source_for(settings, d), args.reprint) for d in args.doc_id]
        _print(results)
    else:
        results = fidelity.verify_all(settings, args.reprint)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    _print(counts)
    return 1 if "error" in counts else 0


def cmd_ocr(args, settings) -> int:
    from . import ocr

    hashes = [_source_for(settings, d) for d in args.doc_id] if args.doc_id else None
    _print(ocr.run(settings, hashes, gemini=not args.local_only))
    return 0


def cmd_build_keyword(args, settings) -> int:
    from .retrieval import Analyzer, build_keyword_index

    with store.open_db(settings.db_path) as conn:
        selected = store.get_app_setting(conn, "active_run") is not None
    # Comparison profiles never serve users; after `activate-run` only another activation changes serving.
    activate = args.profile == "structural" and not selected and not args.no_activate
    result = build_keyword_index(settings, Analyzer(), include_unreviewed=args.include_unreviewed,
                                 profile=args.profile, activate=activate)
    if selected and args.profile == "structural":
        result["note"] = "an activate-run selection exists; evaluate this index and activate a run to serve it"
    _print(result)
    return 0


def _paid_resources(settings):
    """Owner maintenance mode: the same gateway, ledger and process-owner lock as the application. A running
    UI that owns the gateway makes this refuse (stop the UI first)."""
    return service.Resources(settings)


def cmd_plan_embeddings(args, settings) -> int:
    from . import dense

    _print(dense.plan_embeddings(settings, args.index))
    return 0


def cmd_build_dense(args, settings) -> int:
    from . import dense

    res = _paid_resources(settings)
    try:
        result = dense.build_dense(settings, res.transport, args.index, args.estimate_id)
    finally:
        res.close()
    _print(result)
    _print(asdict(budget.snapshot(settings.db_path)))
    return 0 if result.get("status") == "ready" else 1


def cmd_evaluate_retrieval(args, settings) -> int:
    res = _paid_resources(settings) if args.allow_paid_queries else None
    try:
        from .retrieval import Analyzer

        summaries = evaluation.evaluate_retrieval(
            settings, res.analyzer if res else Analyzer(), res.transport if res else None, args.dataset,
            [x.strip() for x in args.runs.split(",") if x.strip()], index_version=args.index,
            allow_paid_queries=args.allow_paid_queries, force=args.force)
    finally:
        if res:
            res.close()
    _print(summaries)
    return 0 if all(s.get("status") == "complete" for s in summaries) else 1


def cmd_trial_reranker(args, settings) -> int:
    from .retrieval import Analyzer

    depths = [int(x) for x in args.candidate_counts.split(",") if x.strip()]
    report = evaluation.trial_reranker(settings, Analyzer(), args.dataset, depths, index_version=args.index)
    _print(report)
    return 0


def cmd_activate_run(args, settings) -> int:
    _print(evaluation.activate_run(settings, args.run_id, Path(args.decision_file)))
    return 0


def cmd_compare_runs(args, settings) -> int:
    out = evaluation.compare_runs(settings, args.run_id)
    _print({"markdown": out["markdown"], "recommendation": out["recommendation"]})
    return 0


def cmd_draft_activation(args, settings) -> int:
    draft = evaluation.draft_activation(settings, [r.strip() for r in args.runs.split(",") if r.strip()],
                                        Path(args.out), select=args.select)
    _print(draft)
    return 0


def cmd_report(args, settings) -> int:
    if args.phase == 2:
        print(evaluation.write_phase2_report(settings))
        return 0
    if args.phase == 3:
        print(ops.write_phase3_report(settings))
        return 0
    print("only --phase 2|3 is implemented", file=sys.stderr)
    return 2


PHASE3_MODULES = ("tests.test_service", "tests.test_budget", "tests.test_generation")


def cmd_check(args, settings) -> int:
    if args.phase not in (1, 2, 3) or args.provider != "fake":
        print("only --phase 1|2|3 --provider fake is implemented", file=sys.stderr)
        return 2
    tests_dir = REPO_ROOT / "tests"
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["RFP_DATA_DIR"] = tmp  # isolated state; never production data
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("RFP_ACCOUNTS_FILE", None)
        if args.phase == 3:  # focused service, budget and request-state checks
            suite = unittest.defaultTestLoader.loadTestsFromNames(PHASE3_MODULES)
        else:
            suite = unittest.defaultTestLoader.discover(str(tests_dir), top_level_dir=str(REPO_ROOT))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def cmd_load_check(args, settings) -> int:
    if args.provider != "fake":
        print("load-check runs only against the fake provider; a real load test would spend the team allowance",
              file=sys.stderr)
        return 2
    os.environ.pop("OPENAI_API_KEY", None)
    result = ops.load_check(users=args.users, requests_per_user=args.requests, delay_seconds=args.delay,
                            fail_every=args.fail_every, affordable=args.affordable)
    _print(result)
    if args.save:
        path = ops.phase3_dir(settings) / "load-check.json"
        ops.save_json(path, {**result, "recorded_at": store.utcnow()})
        print(path)
    return 0 if result["passed"] else 1


def cmd_accounts(args, settings) -> int:
    """Owner host only. Tokens are printed once, at provisioning, and never stored in clear text."""
    if args.action == "provision":
        if not args.member_id or not args.capabilities:
            raise auth.AuthError("provision needs --member-id and --capabilities")
        token = auth.provision(settings, args.member_id, [c.strip() for c in args.capabilities.split(",")])
        print(f"member {args.member_id}: token (shown once; deliver privately)\n{token}")
        print(f"accounts file: {settings.accounts_path}", file=sys.stderr)
    elif args.action in ("disable", "enable"):
        auth.set_enabled(settings, args.member_id, args.action == "enable")
        _print({"member_id": args.member_id, "enabled": args.action == "enable"})
    elif args.action == "revoke-sessions":
        _print({"revoked": auth.revoke_sessions(settings, args.member_id, args.reason or "owner revocation")})
    else:
        _print({"mode": auth.mode(settings), "members": auth.list_members(settings)})
    return 0


def cmd_reconcile(args, settings) -> int:
    record = json.loads(Path(args.file).read_text(encoding="utf-8"))
    res = service.Resources(settings)
    try:
        _print(asdict(service.reconcile(res, auth.OWNER_CLI, record)))
    finally:
        res.close()
    return 0


def cmd_unresolved(args, settings) -> int:
    res = service.Resources(settings)
    try:
        _print(service.unresolved_attempts(res, auth.OWNER_CLI))
    finally:
        res.close()
    return 0


def cmd_validate_gold(args, settings) -> int:
    evaluation.assign_families(settings)
    report = evaluation.validate_gold(settings, args.dataset)
    _print(report)
    return 0 if report["ok"] else 1


def cmd_configure_budget(args, settings) -> int:
    if not args.confirm_rates:
        print("recheck current model prices, then pass --confirm-rates", file=sys.stderr)
        return 2
    micro = lambda usd: int((Decimal(usd) * budget.MICRO).to_integral_value(rounding="ROUND_CEILING"))  # noqa: E731
    _print(budget.configure(
        settings.db_path, "owner-cli", project_start=date.fromisoformat(args.start),
        project_end=date.fromisoformat(args.end), prior_use_micro=micro(args.prior_use_usd),
        prior_use_evidence=args.prior_use_evidence, rates=DEFAULT_RATES, rate_version=RATE_VERSION,
        enable_paid=args.enable_paid, allowance_micro=micro(args.allowance_usd) if args.allowance_usd else None,
        cap_micro=micro(args.cap_usd) if args.cap_usd else None))
    _print(asdict(budget.snapshot(settings.db_path)))
    return 0


def cmd_budget_status(args, settings) -> int:
    _print(asdict(budget.snapshot(settings.db_path)))
    return 0


def cmd_gold(args, settings) -> int:
    if args.action == "submit":
        evaluation.assign_families(settings)
        _print(gold.submit(settings, Path(args.file), args.batch, args.dataset, args.drafted_by))
    elif args.action == "infer":
        inference = json.loads(Path(args.file).read_text(encoding="utf-8"))
        _print(gold.infer(settings, args.candidate_id, args.by, inference))
    elif args.action == "check":
        errors = gold.check(settings)
        _print({"ok": not errors, "errors": errors})
        return 1 if errors else 0
    elif args.action == "sync":
        _print({"written": gold.sync(settings)})
    elif args.action == "excerpts":
        if not args.out:
            raise gold.GoldError("excerpts needs --out <absolute new directory>")
        evaluation.assign_families(settings)
        _print(gold.write_excerpts(settings, Path(args.out), per_category=args.per_category))
    elif args.action == "repin":
        _print(gold.repin(settings, args.batch or ""))
    else:
        _print(gold.status(settings))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rfp_assistant.cli")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("init", help="initialize schema/settings idempotently")
    s.add_argument("--paid-disabled", action="store_true")
    sub.add_parser("manifest", help="import all CSV associations and verify originals")
    s = sub.add_parser("ingest", help="parse every unique original once, or only the named documents")
    s.add_argument("--profile", choices=["all"], default="all")
    s.add_argument("--doc-id", action="append")
    s.add_argument("--force", action="store_true", help="reparse even when inputs are unchanged")
    s = sub.add_parser("import-reviews", help="append validated original-fidelity review records")
    s.add_argument("--file", required=True, help="absolute path of a JSON/JSONL review file")
    s = sub.add_parser("recover-source", help="register an approved PDF conversion of a quarantined original")
    s.add_argument("--doc-id", required=True)
    s.add_argument("--converted-file", required=True)
    s.add_argument("--review-file", required=True)
    s = sub.add_parser("identity", help="CSV metadata versus the original's own institution/title cues")
    s.add_argument("--all", action="store_true", help="every association, not only conflicts/disagreements")
    s = sub.add_parser("resolve-metadata", help="owner/reviewer canonical value for a conflicting field")
    s.add_argument("--doc-id", required=True)
    s.add_argument("--field", required=True, choices=ingestion.CONFLICT_FIELDS)
    s.add_argument("--value", required=True, help="JSON value, e.g. '\"기관명\"' or 130000000")
    s.add_argument("--rationale", required=True)
    s.add_argument("--evidence", required=True, help="source/notice location supporting the value")
    s.add_argument("--actor", required=True)
    s = sub.add_parser("review", help="fidelity review: sheet, native print, page PNGs, record")
    s.add_argument("action", choices=["sheet", "print", "render", "record"])
    s.add_argument("--doc-id", required=True)
    s.add_argument("--pdf", help="render: PDF to split instead of the original or the printed HWP")
    s.add_argument("--reviewer", default="")
    s.add_argument("--status", default="sample_checked", choices=ingestion.REVIEW_STATUSES)
    s.add_argument("--locations", help="JSON file: inspected element IDs/locations")
    s.add_argument("--findings", help="JSON file: checks and findings")
    s = sub.add_parser("fidelity", help="automatic HWP check: Hancom print, text-layer comparison, verdict")
    s.add_argument("action", choices=["run", "show"])
    s.add_argument("--doc-id", action="append", help="run: only these documents (default: every parsed HWP)")
    s.add_argument("--reprint", action="store_true", help="run: print again even if a rendering exists")
    s = sub.add_parser("ocr", help="image regions: PaddleOCR-VL, Gemini only where the fallback test fails")
    s.add_argument("--doc-id", action="append", help="only these documents (default: every parsed source)")
    s.add_argument("--local-only", action="store_true", help="no Gemini calls; flagged regions stay unresolved")
    s = sub.add_parser("build-keyword", help="build an immutable Kiwi BM25 index")
    s.add_argument("--profile", choices=sorted(chunking.PROFILES), default="structural")
    s.add_argument("--no-activate", action="store_true", help="build without moving the keyword pointer")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--reviewed-only", action="store_true")
    g.add_argument("--include-unreviewed", action="store_true",
                   help="operating index over every parsed source; unreviewed sources stay labeled")
    s = sub.add_parser("plan-embeddings", help="count unique payload tokens and maximum spend; no provider call")
    s.add_argument("--index", required=True, help="keyword index version")
    s = sub.add_parser("build-dense", help="owner maintenance: metered embedding of cache misses, verified matrix")
    s.add_argument("--index", required=True)
    s.add_argument("--estimate-id", required=True)
    s = sub.add_parser("evaluate-retrieval", help="frozen retrieval-only runs K0,K1,D,H on a reviewed dataset")
    s.add_argument("--dataset", required=True)
    s.add_argument("--runs", default="K0,K1,D,H")
    s.add_argument("--index", help="keyword index version (default: the active one)")
    s.add_argument("--allow-paid-queries", action="store_true",
                   help="embed uncached queries through the gateway (gold_eval envelope)")
    s.add_argument("--force", action="store_true", help="rerun a configuration that already has scores")
    s = sub.add_parser("trial-reranker", help="local reranker on the frozen H candidates; gate report")
    s.add_argument("--dataset", required=True)
    s.add_argument("--candidate-counts", default="10,20")
    s.add_argument("--index")
    s = sub.add_parser("activate-run", help="validate a reviewed selection and switch the serving configuration")
    s.add_argument("--run-id", required=True)
    s.add_argument("--decision-file", required=True)
    s = sub.add_parser("compare-runs", help="comparison table and K1-default recommendation from recorded runs")
    s.add_argument("--run-id", action="append", required=True)
    s = sub.add_parser("draft-activation", help="decision file for the owner to complete; activates nothing")
    s.add_argument("--runs", required=True, help="comma-separated run IDs to compare")
    s.add_argument("--out", required=True, help="absolute path of the draft decision JSON")
    s.add_argument("--select", help="choose this run instead of the recommendation (recorded as an override)")
    s = sub.add_parser("report", help="write the phase handoff report from recorded state")
    s.add_argument("--phase", type=int, required=True)
    s = sub.add_parser("check", help="automated phase gate in temporary state")
    s.add_argument("--phase", type=int, required=True)
    s.add_argument("--provider", required=True)
    s = sub.add_parser("validate-gold")
    s.add_argument("--dataset", required=True)
    s = sub.add_parser("configure-budget", help="owner-only dates, prior use, rates and paid state")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--prior-use-usd", required=True)
    s.add_argument("--prior-use-evidence", required=True)
    s.add_argument("--allowance-usd", help="replace the allowance (set together with --cap-usd)")
    s.add_argument("--cap-usd", help="gateway hard cap; purpose envelopes are re-split from it")
    s.add_argument("--confirm-rates", action="store_true")
    s.add_argument("--enable-paid", action="store_true")
    sub.add_parser("budget-status")
    s = sub.add_parser("load-check", help="concurrent members against a temporary ledger with a fake provider")
    s.add_argument("--users", type=int, default=6)
    s.add_argument("--provider", required=True)
    s.add_argument("--requests", type=int, default=2, help="requests per member")
    s.add_argument("--delay", type=float, default=0.5, help="fake provider delay in seconds")
    s.add_argument("--fail-every", type=int, default=0, help="every Nth call times out after dispatch (unknown)")
    s.add_argument("--affordable", type=int, default=4, help="maximum reservations the temporary cap admits at once")
    s.add_argument("--save", action="store_true", help="record the result for report --phase 3")
    s = sub.add_parser("accounts", help="owner-only member tokens, roles and sessions")
    s.add_argument("action", choices=["list", "provision", "disable", "enable", "revoke-sessions"])
    s.add_argument("--member-id")
    s.add_argument("--capabilities", help="comma-separated: consultant,verifier,budget_admin,sealed_evaluator")
    s.add_argument("--reason")
    s = sub.add_parser("reconcile", help="owner import of a dated provider interval (JSON record)")
    s.add_argument("--file", required=True)
    sub.add_parser("unresolved", help="attempts whose billing is not settled or released")
    s = sub.add_parser("gold", help="dataset candidate queue and rejection wiki")
    s.add_argument("action", choices=["status", "submit", "infer", "check", "sync", "repin", "excerpts"])
    s.add_argument("--out", help="excerpts: absolute new directory for excerpts.jsonl and drafting-context.json "
                                 "(contains source text; keep it in local inputs unless the owner shares it)")
    s.add_argument("--per-category", type=int, default=2, help="excerpts: elements per document and category")
    s.add_argument("--file", help="submit: candidate JSONL; infer: JSON with cause, lesson, drafting_rule")
    s.add_argument("--batch", help="submit: new batch id; repin: batch id for pending rows moved to the active "
                                   "extraction after a parser revision")
    s.add_argument("--dataset", default="dev-pilot", help="submit: target dataset")
    s.add_argument("--drafted-by", default="", help="submit: drafting agent identity")
    s.add_argument("--candidate-id", help="infer: rejected candidate id")
    s.add_argument("--by", default="", help="infer: inferring agent identity")
    return p


COMMANDS = {"init": cmd_init, "manifest": cmd_manifest, "ingest": cmd_ingest, "review": cmd_review,
            "import-reviews": cmd_import_reviews, "recover-source": cmd_recover_source, "identity": cmd_identity,
            "resolve-metadata": cmd_resolve_metadata, "plan-embeddings": cmd_plan_embeddings,
            "build-dense": cmd_build_dense, "evaluate-retrieval": cmd_evaluate_retrieval,
            "trial-reranker": cmd_trial_reranker, "activate-run": cmd_activate_run, "report": cmd_report,
            "compare-runs": cmd_compare_runs, "draft-activation": cmd_draft_activation,
            "fidelity": cmd_fidelity, "ocr": cmd_ocr, "build-keyword": cmd_build_keyword, "check": cmd_check, "validate-gold": cmd_validate_gold,
            "configure-budget": cmd_configure_budget, "budget-status": cmd_budget_status,
            "gold": cmd_gold, "load-check": cmd_load_check, "accounts": cmd_accounts, "reconcile": cmd_reconcile,
            "unresolved": cmd_unresolved}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        if args.command not in ("init", "check", "load-check"):
            store.init_schema(settings.db_path)
        return COMMANDS[args.command](args, settings)
    except (ingestion.IngestionError, budget.BudgetError, service.ServiceError, auth.AuthError, gold.GoldError,
            evaluation.EvaluationError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
