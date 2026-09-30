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

from . import auth, budget, evaluation, fidelity, gold, ingestion, service, store
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
    results = ingestion.ingest(settings, args.doc_id)
    _print(results)
    _print(ingestion.manifest_report(settings)["parse_status"])
    return 1 if any(r["status"] == "error" for r in results) else 0


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

    _print(build_keyword_index(settings, Analyzer(), include_unreviewed=args.include_unreviewed))
    return 0


def cmd_check(args, settings) -> int:
    if args.phase != 1 or args.provider != "fake":
        print("only --phase 1 --provider fake is implemented", file=sys.stderr)
        return 2
    tests_dir = REPO_ROOT / "tests"
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["RFP_DATA_DIR"] = tmp  # isolated state; never production data
        os.environ.pop("OPENAI_API_KEY", None)
        suite = unittest.defaultTestLoader.discover(str(tests_dir), top_level_dir=str(REPO_ROOT))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


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
    s = sub.add_parser("ingest", help="parse every manifest original, or only the named documents")
    s.add_argument("--doc-id", action="append")
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
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--reviewed-only", action="store_true")
    g.add_argument("--include-unreviewed", action="store_true",
                   help="operating index over every parsed source; unreviewed sources stay labeled")
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
    s = sub.add_parser("gold", help="dataset candidate queue and rejection wiki")
    s.add_argument("action", choices=["status", "submit", "infer", "check", "sync", "repin"])
    s.add_argument("--file", help="submit: candidate JSONL; infer: JSON with cause, lesson, drafting_rule")
    s.add_argument("--batch", help="submit: new batch id; repin: batch id for pending rows moved to the active "
                                   "extraction after a parser revision")
    s.add_argument("--dataset", default="dev-pilot", help="submit: target dataset")
    s.add_argument("--drafted-by", default="", help="submit: drafting agent identity")
    s.add_argument("--candidate-id", help="infer: rejected candidate id")
    s.add_argument("--by", default="", help="infer: inferring agent identity")
    return p


COMMANDS = {"init": cmd_init, "manifest": cmd_manifest, "ingest": cmd_ingest, "review": cmd_review,
            "fidelity": cmd_fidelity, "ocr": cmd_ocr, "build-keyword": cmd_build_keyword, "check": cmd_check, "validate-gold": cmd_validate_gold,
            "configure-budget": cmd_configure_budget, "budget-status": cmd_budget_status,
            "gold": cmd_gold}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        if args.command not in ("init", "check"):
            store.init_schema(settings.db_path)
        return COMMANDS[args.command](args, settings)
    except (ingestion.IngestionError, budget.BudgetError, service.ServiceError, auth.AuthError, gold.GoldError,
            ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
