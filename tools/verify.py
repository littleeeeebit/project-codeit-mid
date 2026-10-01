"""Managed local verification: runs the commands of `verification.json` that the owner approved and writes a
receipt bound to the exact commit. Manual (browser) steps are recorded separately and never inferred.

    python tools/verify.py approve-template            # starting point for verification.local.json
    python tools/verify.py list
    python tools/verify.py run [--flow F1-access ...] [--command phase3-gate ...]
    python tools/verify.py record-manual <receipt dir> <manual results json>
    python tools/verify.py prepare-ui --corpus fixture|real-corpus

Every command uses the fake provider. OPENAI_API_KEY and RFP_* variables are removed from child environments,
temporary files stay inside the receipt directory, and nothing runs before the owner approves this contract's
SHA-256 and the command IDs in `verification.local.json` (git-ignored, not secret).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "verification.json"
MANUAL_RESULTS = ("pass", "fail", "blocked", "not_applicable")


class VerifyError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def contract_sha256(path: Path = CONTRACT) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_contract(path: Path = CONTRACT) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_local(contract: dict, repo: Path = REPO) -> dict:
    path = repo / contract["local_settings_file"]
    if not path.exists():
        raise VerifyError(f"{path.name} is missing: run `python tools/verify.py approve-template`, inspect the "
                          "commands, and save the completed approval")
    return json.loads(path.read_text(encoding="utf-8"))


def check_approval(local: dict, sha: str, wanted: list[str]) -> None:
    approval = local.get("approval") or {}
    if not (approval.get("approved_by") or "").strip():
        raise VerifyError("approval.approved_by is empty")
    if approval.get("contract_sha256") != sha:
        raise VerifyError("verification.json changed since it was approved: inspect it again and update "
                          "approval.contract_sha256")
    missing = [c for c in wanted if c not in (approval.get("commands") or [])]
    if missing:
        raise VerifyError(f"commands not approved: {missing}")


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=60)
    return out.stdout.strip() if out.returncode == 0 else ""


def argv_for(command: dict, python: str, run_dir: Path) -> list[str]:
    if "tests" in command:
        return [python, "-B", "-m", "unittest", "-v", *command["tests"]]
    return [python if a == "{python}" else str(run_dir) if a == "{run_dir}" else a for a in command["argv"]]


def child_env(local: dict, run_dir: Path, repo: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY" and not k.startswith("RFP_")}
    tmp = run_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env.update(PYTHONPATH=os.pathsep.join([str(repo / "src"), str(repo)]), PYTHONDONTWRITEBYTECODE="1",
               PYTHONUTF8="1", TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp))
    if local.get("tiktoken_cache_dir"):
        env["TIKTOKEN_CACHE_DIR"] = local["tiktoken_cache_dir"]
    return env


def real_corpus(local: dict, repo: Path) -> dict | None:
    """The owner-declared isolated copy, refused when it is (or is inside) the authoritative runtime."""
    rc = local.get("real_corpus")
    if not rc:
        return None
    for key in ("source_dir", "data_dir", "hwp_doc_id", "pdf_doc_id", "question"):
        if not rc.get(key):
            raise VerifyError(f"real_corpus.{key} is required")
    if rc.get("isolated_copy") is not True:
        raise VerifyError("real_corpus.isolated_copy must be true: real-corpus checks write verifier runs and fake "
                          "ledger rows into the declared copy")
    data = Path(rc["data_dir"])
    if not data.is_absolute() or data.resolve() == (repo / ".runtime").resolve():
        raise VerifyError("real_corpus.data_dir must be an absolute path to a copy, not the authoritative .runtime")
    return rc


def selected_commands(contract: dict, flows: list[str], commands: list[str]) -> list[str]:
    known_flows = {f["id"]: f for f in contract["flows"]}
    known = {c["id"] for c in contract["commands"]}
    unknown = [f for f in flows if f not in known_flows] + [c for c in commands if c not in known]
    if unknown:
        raise VerifyError(f"unknown flow or command IDs: {unknown}")
    if not flows and not commands:
        flows = list(known_flows)
    ordered = [c for f in flows for c in known_flows[f]["commands"]] + commands
    return list(dict.fromkeys(ordered))


def run(contract: dict, local: dict, *, flows: list[str], commands: list[str], repo: Path = REPO,
        contract_path: Path = CONTRACT) -> Path:
    sha = contract_sha256(contract_path)
    wanted = selected_commands(contract, flows, commands)
    check_approval(local, sha, wanted)
    rc = real_corpus(local, repo)
    head = git(repo, "rev-parse", "HEAD")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = repo / contract["receipts_dir"] / f"{stamp}-{head[:7] or 'nogit'}"
    run_dir.mkdir(parents=True)
    python = local.get("python") or sys.executable
    env = child_env(local, run_dir, repo)
    (run_dir / "inputs.json").write_text(json.dumps({"real_corpus": rc}, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    by_id = {c["id"]: c for c in contract["commands"]}
    receipt = {
        "schema": "project-codeit-mid/verification-receipt", "schema_version": 1,
        "contract_sha256": sha, "head": head, "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "tracked_changes": bool(git(repo, "status", "--porcelain", "--untracked-files=no")),
        "platform": platform.platform(), "python": python, "python_version": platform.python_version(),
        "approved_by": local["approval"]["approved_by"], "browser": local.get("browser"),
        "started_at": now(), "commands": [], "flows": [], "provider": "fake", "paid_calls": 0,
    }
    for cid in wanted:
        command = by_id[cid]
        entry = {"id": cid, "purpose": command["purpose"]}
        if "real_corpus" in command.get("requires", []) and rc is None:
            entry.update(status="skipped", reason="real_corpus is not configured in the local settings")
            receipt["commands"].append(entry)
            continue
        argv = argv_for(command, python, run_dir)
        log = run_dir / f"{cid}.log"
        started = time.monotonic()
        try:
            with log.open("w", encoding="utf-8") as out:
                proc = subprocess.run(argv, cwd=repo, env=env, stdout=out, stderr=subprocess.STDOUT,
                                      timeout=command["timeout_s"])
            entry.update(status="pass" if proc.returncode == 0 else "fail", exit_code=proc.returncode)
        except subprocess.TimeoutExpired:
            entry.update(status="fail", exit_code=None, reason=f"timeout after {command['timeout_s']} s")
        entry.update(argv=[a.replace(str(run_dir), "{run_dir}") for a in argv], log=log.name,
                     duration_s=round(time.monotonic() - started, 1))
        receipt["commands"].append(entry)
        print(f"{entry['status']:>7}  {cid}  ({entry['duration_s']} s)", flush=True)
    results = {c["id"]: c["status"] for c in receipt["commands"]}
    for flow in contract["flows"]:
        if flows and flow["id"] not in flows:
            continue
        states = [results.get(c, "not_run") for c in flow["commands"]]
        automated = ("none" if not states else "fail" if "fail" in states
                     else "pass" if all(s == "pass" for s in states) else "incomplete")
        receipt["flows"].append({"id": flow["id"], "title": flow["title"], "automated": automated,
                                 "commands": dict(zip(flow["commands"], states)),
                                 "manual": [{"id": m["id"], "scope": m["scope"], "result": "pending"}
                                            for m in flow["manual"]]})
    receipt["finished_at"] = now()
    receipt["summary"] = summarize(receipt)
    write_json(run_dir / "receipt.json", receipt)
    write_json(run_dir / "manual-template.json", manual_template(contract, receipt))
    print(json.dumps(receipt["summary"], ensure_ascii=False), flush=True)
    print(f"receipt: {run_dir / 'receipt.json'}", flush=True)
    return run_dir


def summarize(receipt: dict) -> dict:
    statuses = [c["status"] for c in receipt["commands"]]
    manual = [m["result"] for f in receipt["flows"] for m in f["manual"]]
    return {"automated": "fail" if "fail" in statuses else "pass" if all(s == "pass" for s in statuses)
            else "incomplete",
            "commands": {s: statuses.count(s) for s in sorted(set(statuses))},
            "manual": {s: manual.count(s) for s in sorted(set(manual))},
            "complete": bool(statuses) and all(s == "pass" for s in statuses)
            and all(m in ("pass", "not_applicable") for m in manual)}


def manual_template(contract: dict, receipt: dict) -> dict:
    steps = {m["id"]: m for f in contract["flows"] for m in f["manual"]}
    wanted = [m["id"] for f in receipt["flows"] for m in f["manual"]]
    return {"head": receipt["head"], "observer": "", "browser": receipt.get("browser"), "steps": {
        sid: {"scope": steps[sid]["scope"], "do": steps[sid]["do"], "expect": steps[sid]["expect"],
              "result": None, "observed": "", "evidence": [], "at": None} for sid in wanted}}


def record_manual(run_dir: Path, results_path: Path) -> dict:
    """Merges observed browser results; refuses another commit, an empty observer or an unknown result."""
    receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    results = json.loads(results_path.read_text(encoding="utf-8"))
    if results.get("head") != receipt["head"]:
        raise VerifyError("manual results were recorded for another commit")
    if not (results.get("observer") or "").strip():
        raise VerifyError("observer is empty")
    steps = results.get("steps") or {}
    for f in receipt["flows"]:
        for m in f["manual"]:
            got = steps.get(m["id"])
            if not got or got.get("result") is None:
                continue
            if got["result"] not in MANUAL_RESULTS:
                raise VerifyError(f"{m['id']}: result must be one of {MANUAL_RESULTS}")
            if not (got.get("observed") or "").strip():
                raise VerifyError(f"{m['id']}: describe what was observed")
            m.update(result=got["result"], observed=got["observed"], evidence=got.get("evidence") or [],
                     at=got.get("at"), observer=results["observer"])
    receipt["manual_recorded_at"] = now()
    receipt["summary"] = summarize(receipt)
    write_json(run_dir / "receipt.json", receipt)
    return receipt["summary"]


def approve_template(contract: dict, sha: str) -> dict:
    return {"approval": {"approved_by": "", "contract_sha256": sha,
                         "commands": [c["id"] for c in contract["commands"]]},
            "python": sys.executable, "tiktoken_cache_dir": "",
            "real_corpus": None, "browser": {"name": "", "version": "", "viewport": ""},
            "_commands_to_inspect": {c["id"]: c.get("tests") or c["argv"] for c in contract["commands"]}}


def prepare_ui(contract: dict, local: dict, corpus: str, repo: Path = REPO) -> dict:
    """An isolated fake-provider environment for the manual steps, plus how to launch it."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = repo / contract["receipts_dir"] / f"ui-{corpus}-{stamp}"
    root.mkdir(parents=True)
    config = root / "config.json"
    config.write_text(json.dumps({"provider": "fake", "fake_delay_seconds": 4.0}), encoding="utf-8")
    if corpus == "fixture":
        sys.path[:0] = [str(repo / "src"), str(repo)]
        from tests import fixtures

        env = fixtures.make_env(root)
        source, data = env.settings.source_dir, env.settings.data_dir
    else:
        rc = real_corpus(local, repo)
        if rc is None:
            raise VerifyError("real_corpus is not configured in the local settings")
        source, data = Path(rc["source_dir"]), Path(rc["data_dir"])
    launch = {"RFP_SOURCE_DIR": str(source), "RFP_DATA_DIR": str(data), "RFP_CONFIG_FILE": str(config)}
    info = {"environment": launch, "powershell": [f"$env:{k} = '{v}'" for k, v in launch.items()]
            + ["Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue",
               "streamlit run app.py --server.address=127.0.0.1"],
            "posix": ["env -u OPENAI_API_KEY " + " ".join(f"{k}='{v}'" for k, v in launch.items())
                      + " streamlit run app.py --server.address=127.0.0.1"]}
    write_json(root / "launch.json", info)
    return info


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("approve-template")
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("--flow", action="append", default=[])
    r.add_argument("--command", action="append", default=[])
    m = sub.add_parser("record-manual")
    m.add_argument("receipt_dir", type=Path)
    m.add_argument("results", type=Path)
    u = sub.add_parser("prepare-ui")
    u.add_argument("--corpus", choices=("fixture", "real-corpus"), default="fixture")
    args = p.parse_args(argv)
    contract = load_contract()
    try:
        if args.cmd == "approve-template":
            print(json.dumps(approve_template(contract, contract_sha256()), ensure_ascii=False, indent=1))
        elif args.cmd == "list":
            for f in contract["flows"]:
                print(f"{f['id']}: {f['title']}\n  commands: {', '.join(f['commands']) or '-'}\n"
                      f"  manual: {', '.join(m['id'] for m in f['manual']) or '-'}")
        elif args.cmd == "run":
            receipt = json.loads((run(contract, load_local(contract), flows=args.flow, commands=args.command)
                                  / "receipt.json").read_text(encoding="utf-8"))
            return 0 if receipt["summary"]["automated"] == "pass" else 1
        elif args.cmd == "record-manual":
            print(json.dumps(record_manual(args.receipt_dir, args.results), ensure_ascii=False))
        elif args.cmd == "prepare-ui":
            local = load_local(contract) if args.corpus == "real-corpus" else {}
            print(json.dumps(prepare_ui(contract, local, args.corpus), ensure_ascii=False, indent=1))
    except VerifyError as exc:
        print(f"verify: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
