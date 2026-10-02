"""Phase 4 operator walkthrough on a temporary fixture corpus, through the real CLI.

    python -B tools/verification/phase4_walkthrough.py <absolute work dir>

Builds the phase-4 fixture corpus (five CSV associations: four PDFs, one unconvertible HWP) under the work
directory, then runs every phase-4 command as a child process with the fake provider: gold submission and
review (dev and sealed test), validation and freezing, retrieval runs, activation, answer-finalist plan and run,
blind review export, release freeze, the sealed run and its refused repeat, a latency sample, backup, staged
restore and the release report. It never reads RFP_DATA_DIR, never needs a key and writes
`<work>/phase4-walkthrough.json`. Everything it produces is synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "src"), str(REPO)]


def main(work: Path) -> int:
    from tests import phase4_fixtures as p4

    root = work / "phase4"
    env_fixture = p4.make_env(root, splits={"기관A": "dev", "기관F": "dev", "기관D": "test", "기관E": "test"},
                              paid=False)
    pending = {"reviewed_by": None, "approved_at": None, "status": "pending", "original_inspected": False}
    dev = [p4.amount_row(env_fixture), p4.deadline_row(env_fixture), p4.warranty_row(env_fixture),
           p4.absent_row(env_fixture)]
    test = [p4.row(env_fixture, "test-seat", "도서관에서 무엇을 만드는 사업인가요?", "기관D", split="test",
                   groups=[p4.group(env_fixture, "g1", "기관D", ("%좌석%", p4.SEATS))],
                   claims=[p4.claim("c1", ["g1"], {"type": "text", "patterns": ["좌석 예약"]})])]
    for name, rows in (("dev", dev), ("test", test)):
        for r in rows:
            r["review"].update(pending)
        (root / f"{name}-batch.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                                 encoding="utf-8")
    (root / "config.json").write_text(json.dumps({"provider": "fake"}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY" and not k.startswith("RFP_")}
    env.update(RFP_SOURCE_DIR=str(env_fixture.settings.source_dir), RFP_DATA_DIR=str(env_fixture.settings.data_dir),
               RFP_CONFIG_FILE=str(root / "config.json"), PYTHONPATH=os.pathsep.join([str(REPO / "src"), str(REPO)]),
               PYTHONUTF8="1")
    steps: list[dict] = []

    def cli(step: str, *args: str, expect: int = 0, contains: str | None = None) -> str:
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, "-B", "-m", "rfp_assistant.cli", *args], cwd=REPO, env=env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        out = proc.stdout + proc.stderr
        ok = proc.returncode == expect and (contains is None or contains in out)
        steps.append({"step": step, "argv": ["cli", *args], "exit": proc.returncode, "expected_exit": expect,
                      "ok": ok, "seconds": round(time.monotonic() - t0, 2),
                      "tail": out.strip().splitlines()[-1][:200] if out.strip() else ""})
        return proc.stdout

    def field(text: str, key: str):
        return json.loads(text)[key] if text.strip().startswith(("{", "[")) else None

    cli("configure budget", "configure-budget", "--start", "2026-09-30", "--end", "2026-10-28", "--prior-use-usd", "0",
        "--prior-use-evidence", "synthetic walkthrough", "--allowance-usd", "5", "--cap-usd", "5", "--confirm-rates",
        "--enable-paid")
    cli("submit dev batch", "gold", "submit", "--file", str(root / "dev-batch.jsonl"), "--batch", "dev-b1",
        "--dataset", "dev", "--drafted-by", "agent-a")
    cli("submit sealed batch", "gold", "submit", "--file", str(root / "test-batch.jsonl"), "--batch", "test-b1",
        "--dataset", "test", "--drafted-by", "agent-a")
    cli("approval needs the original", "gold", "decide", "--candidate-id", "dev-amount-r1", "--decision", "approve",
        "--reviewer", "person-b", expect=1, contains="원문을 직접 확인")
    for cid in ("dev-amount-r1", "dev-deadline-r1", "dev-warranty-r1", "dev-absent-r1", "test-seat-r1"):
        cli(f"approve {cid}", "gold", "decide", "--candidate-id", cid, "--decision", "approve", "--reviewer",
            "person-b", "--original-inspected")
    cli("validate dev", "validate-gold", "--dataset", "dev", contains='"label": "pilot"')
    cli("validate sealed test", "validate-gold", "--dataset", "test")
    cli("freeze dev", "freeze-dataset", "--dataset", "dev", "--actor", "owner", "--reason", "walkthrough")
    cli("freeze test", "freeze-dataset", "--dataset", "test", "--actor", "owner", "--reason", "walkthrough")
    runs = json.loads(cli("retrieval K0,K1 on dev", "evaluate-retrieval", "--dataset", "dev", "--runs", "K0,K1") or "[]")
    cli("sealed split refused to evaluate-retrieval", "evaluate-retrieval", "--dataset", "test", "--runs", "K1",
        expect=1, contains="sealed")
    k0, k1 = (next(r["run_id"] for r in runs if r["label"] == x) for x in ("K0", "K1"))
    decision = root / "decision.json"
    cli("draft activation", "draft-activation", "--runs", f"{k0},{k1}", "--out", str(decision))
    draft = json.loads(decision.read_text(encoding="utf-8"))
    draft.update(decided_by="owner", rationale="walkthrough: keep the K1 baseline")
    decision.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    cli("activate K1", "activate-run", "--run-id", k1, "--decision-file", str(decision))
    est = field(cli("plan answer finalists", "plan-run", "--action", "answer-finalists", "--dataset", "dev",
                    "--runs", f"{k1},{k0}"), "estimate_id")
    answer = cli("run answer finalists", "run-answers", "--estimate-id", est, "--actor", "owner")
    answer_run = json.loads(answer.split("\n}\n")[0] + "\n}")["run_id"] if answer else None
    est = field(cli("replan after completion", "plan-run", "--action", "answer-finalists", "--dataset", "dev",
                    "--runs", f"{k1},{k0}", contains='"rows_remaining": 0'), "estimate_id")
    cli("export blind review", "export-review", "--run-id", answer_run)
    freeze = field(cli("freeze release", "freeze-release", "--run-id", k1, "--answer-run", answer_run,
                       "--decided-by", "owner", "--rationale", "walkthrough"), "freeze_id")
    est = field(cli("plan sealed run", "plan-run", "--action", "sealed", "--freeze-id", freeze), "estimate_id")
    cli("run sealed set once", "run-answers", "--estimate-id", est, "--actor", "owner")
    est = field(cli("plan a second sealed run", "plan-run", "--action", "sealed", "--freeze-id", freeze),
                "estimate_id")
    cli("second sealed run refused", "run-answers", "--estimate-id", est, "--actor", "owner", expect=1,
        contains="post-test regression")
    est = field(cli("plan latency sample", "plan-run", "--action", "latency", "--waves", "2", "--users", "6"),
                "estimate_id")
    cli("latency sample (fake provider)", "latency-run", "--estimate-id", est, "--actor", "owner")
    backup = root / "backup-1"
    cli("backup", "backup", "--destination", str(backup), "--actor", "owner")
    cli("restore check", "restore-check", "--backup", str(backup / "manifest.json"), "--staging",
        str(root / "staging"), contains='"passed": true')
    report_path = cli("release report", "release-report", "--latest").strip()
    manifest = json.loads((Path(report_path).parent / "manifest.json").read_text(encoding="utf-8")) \
        if report_path else {}
    result = {"kind": "phase4_cli_walkthrough", "synthetic": True, "provider": "fake", "corpus": "phase-4 fixture",
              "steps": steps, "passed": all(s["ok"] for s in steps),
              "release_status": manifest.get("status"), "release_reasons": manifest.get("reasons"),
              "release_report": report_path}
    (work / "phase4-walkthrough.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "steps": len(steps),
                      "failed": [s["step"] for s in steps if not s["ok"]]}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_absolute():
        print(__doc__)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
