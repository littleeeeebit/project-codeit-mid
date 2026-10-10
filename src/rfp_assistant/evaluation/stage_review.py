"""Stage review: a baseline git ref and candidate refs run one stage on identical frozen inputs.

Each ref is checked out in its own git worktree (`.` is the working tree: HEAD plus its uncommitted and untracked
files) and runs `stage_review_worker.py` in a fresh process. One scorer here grades every candidate's output and
writes one run folder, `<data_dir>/review/runs/<run-id>/`, that the review app (`review/`) shows:

    run.json              schema, stage, refs with full commits, input hashes, host and packages per candidate
    inputs/               the frozen inputs every candidate read (copied with the folder when it is imported)
    candidates/<id>.json  raw worker output: rankings or chunks
    view.json             what the app shows first; chunking adds docs/<n>.json, one per document

Every candidate runs with the frozen serving activation (retriever, generation) or the serving chunk profile
(chunking). A candidate changes code, and may override configuration with a committed `review-variant.json`:
{"retriever": {"mode", "embedding", "reranker", "limits", "dense_version"}, "chunking": {"profile"},
"generation": {"reasoning_effort", "max_output_tokens"}}.
Retriever and chunking never activate, build or pay: their worker sessions are read-only, no API embedding is sent.
Generation and OCR are paid (`stage_review_paid`): a run stops at a priced estimate, and only `resume` of an
approved estimate gives the workers a key. A candidate that needs a local GPU model on a host without CUDA is
refused with its reason. A failed candidate stays in the run with its reason.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..settings import REPO_ROOT, Settings
from ..storage.store import write_text_atomic
from . import evaluation as ev
from . import stage_review_paid as paid
from . import stage_review_worker

SCHEMA = "review-run-1"
STAGES = ("retriever", "chunking", "generation", "ocr")
PAID_STAGES = ("generation", "ocr")
WORKING_TREE = "."
VARIANT_FILE = "review-variant.json"
TOP_K = 10  # passages per side kept for the view; nDCG@5 and needle top-5 use the evaluation's own cut-offs
WORKER_TIMEOUT_S = 3600
VARIANT_TYPES = {"chunking": {"profile": (str,)},
                 "retriever": {"mode": (str,), "embedding": (str,), "reranker": (str, dict), "limits": (dict,),
                               "dense_version": (str,)},
                 "generation": {"reasoning_effort": (str,), "max_output_tokens": (int,)}, "ocr": {}}
TITLE_LINES, TITLE_CHARS = 2, 60  # the title rule the chunking view checks, fixed here so no candidate can move it
_SECRETS = ("OPENAI_", "GEMINI_", "GOOGLE_API_", "LANGFUSE_", "TYPESAFE_", "HF_TOKEN")


class ReviewError(RuntimeError):
    pass


def review_dir(settings: Settings) -> Path:
    return settings.data_dir / "review"


def run_folder(settings: Settings, run_id: str) -> Path:
    folder = review_dir(settings) / "runs" / run_id
    if not re.fullmatch(r"[a-z]+-\d{8}T\d{6}Z-[0-9a-f]{6}", run_id) or not (folder / "run.json").exists():
        raise ReviewError(f"no review run {run_id}")
    return folder


def _git(*args: str, cwd: Path = REPO_ROOT) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        raise ReviewError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve(ref: str) -> dict:
    if ref == WORKING_TREE:
        return {"ref": ref, "label": "working tree", "commit": _git("rev-parse", "HEAD").strip(), "working_tree": True}
    return {"ref": ref, "label": ref, "commit": _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip(),
            "working_tree": False}


def checkout(cand: dict, path: Path) -> None:
    """A detached worktree at the candidate's commit; the working tree adds its changed and untracked files."""
    _git("worktree", "add", "--detach", str(path), cand["commit"])
    if not cand["working_tree"]:
        return
    names = [n for n in (_git("diff", "--name-only", "-z", "HEAD") + _git("ls-files", "--others", "--exclude-standard",
                                                                            "-z")).split("\0") if n]
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        src, dst = REPO_ROOT / name, path / name
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            digest.update(name.encode() + b"\0" + _sha(src.read_bytes()).encode())
        else:
            dst.unlink(missing_ok=True)
            digest.update(name.encode() + b"\0deleted")
    cand.update(changed_files=sorted(set(names)), changed_sha256=digest.hexdigest())


def remove_worktree(path: Path) -> None:
    subprocess.run(["git", "-C", str(REPO_ROOT), "worktree", "remove", "--force", str(path)], capture_output=True)
    shutil.rmtree(path, ignore_errors=True)
    subprocess.run(["git", "-C", str(REPO_ROOT), "worktree", "prune"], capture_output=True)


def runner_host() -> dict:
    return {**stage_review_worker.host(), "cpu_count": os.cpu_count()}


# ---------------------------------------------------------------- frozen inputs


def _documents(settings: Settings, index) -> dict[str, dict]:
    """extraction -> the first document (CSV order) that serves it, with its title."""
    from ..storage.store import open_db

    by_source = {h: x for h, x in index.source_extraction.items()}
    out: dict[str, dict] = {}
    with open_db(settings.db_path) as conn:
        for r in conn.execute("SELECT doc_id, active_source_hash, normalized_metadata_json FROM documents "
                              "ORDER BY csv_row_id"):
            x = by_source.get(r["active_source_hash"])
            if x and x not in out:
                out[x] = {"doc_id": r["doc_id"], "title": json.loads(r["normalized_metadata_json"]).get("title")}
    return out


def freeze(settings: Settings, stage: str, inputs: Path) -> None:
    from ..retrieval.retrieval import KeywordIndex, corpus_scope
    from ..service.service import active_serving

    inputs.mkdir(parents=True)
    if stage == "ocr":
        return paid.freeze(settings, stage, inputs)
    activation = active_serving(settings)
    if not activation.get("index_version"):
        raise ReviewError("no active keyword index to freeze")
    write_text_atomic(inputs / "activation.json", json.dumps(activation, ensure_ascii=False, indent=1))
    if stage == "generation":
        return paid.freeze(settings, stage, inputs)
    index = KeywordIndex.load(settings, activation["index_version"])
    docs = _documents(settings, index)
    if stage == "chunking":
        from ..corpus.ingestion import load_elements

        documents = [{"extraction_id": x, **docs.get(x, {"doc_id": x, "title": None}),
                      "elements": load_elements(settings, x)} for x in index.rows_by_extraction]
        with gzip.open(inputs / "documents.json.gz", "wt", encoding="utf-8") as f:
            json.dump({"profile": index.profile, "documents": documents}, f, ensure_ascii=False)
        return
    corpus = corpus_scope(settings, index)
    questions, rows, populations = [], {}, {}
    for population, dataset, unscoped in (("dev", "dev", False), ("needle", "corpus", True)):
        scored, skipped, sha = ev.load_eval_rows(settings, dataset, extractions=index.source_extraction)
        passage = [r for r in scored if ev.is_passage_row(r)]
        populations[population] = {"dataset": dataset, "dataset_sha256": sha, "rows": len(passage),
                                   "skipped": skipped, "not_ranked": len(scored) - len(passage),
                                   "population_sha256": ev.population_identity(scored, skipped)}
        for row in passage:
            scope = ev.row_scope(row)
            key = f"{population}:{ev.row_id(row)}"
            per_doc = not unscoped and len(scope) > 1  # one scoped retrieval per selected document, as served
            questions.append({"key": key, "id": ev.row_id(row), "population": population,
                              "question": row["question"], "type": ev.row_type(row),
                              "sides": ["corpus"] if unscoped else
                              [[[r.doc_id, r.source_hash, x]] for r, x in scope] if per_doc else
                              [[[r.doc_id, r.source_hash, x] for r, x in scope]],
                              "side_docs": [r.doc_id for r, _ in scope] if per_doc else [None]})
            rows[key] = row
    write_text_atomic(inputs / "questions.json", json.dumps(
        {"populations": populations, "corpus_scope": [[r.doc_id, r.source_hash, x] for r, x in corpus],
         "questions": questions}, ensure_ascii=False))
    write_text_atomic(inputs / "rows.json", json.dumps(rows, ensure_ascii=False))
    write_text_atomic(inputs / "documents.json", json.dumps(docs, ensure_ascii=False))


def input_hashes(inputs: Path) -> dict[str, str]:
    return {p.relative_to(inputs).as_posix(): _sha(p.read_bytes()) for p in sorted(inputs.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------- candidate configuration


def candidate_config(settings: Settings, stage: str, inputs: Path, variant: dict) -> dict:
    """The frozen serving configuration with the candidate's `review-variant.json` section applied."""
    from ..retrieval.models import EMBEDDINGS, RERANKERS
    from ..retrieval.retrieval import DENSE_MODES, RUN_MODES

    own = variant.get(stage) or {}
    if stage in PAID_STAGES:
        return paid.candidate_config(settings, stage, inputs, own)
    activation = json.loads((inputs / "activation.json").read_text(encoding="utf-8"))
    if stage == "chunking":
        with gzip.open(inputs / "documents.json.gz", "rt", encoding="utf-8") as f:
            profile = json.load(f)["profile"]
        unknown = set(own) - {"profile"}
        if unknown:
            raise ReviewError(f"{VARIANT_FILE}: unknown chunking keys {sorted(unknown)}")
        return {"profile": own.get("profile", profile), "gpu": False}
    unknown = set(own) - {"mode", "embedding", "reranker", "limits", "dense_version"}
    if unknown:
        raise ReviewError(f"{VARIANT_FILE}: unknown retriever keys {sorted(unknown)}")
    cfg = {"mode": own.get("mode", activation["mode"]), "index_version": activation["index_version"],
           "dense_version": activation.get("dense_version"), "embedding": activation.get("embedding"),
           "reranker": activation.get("reranker"), "limits": {**(activation.get("limits") or {}),
                                                              **(own.get("limits") or {})},
           "rank_depth": ev.RANK_DEPTH}
    if cfg["mode"] not in RUN_MODES.values():
        raise ReviewError(f"{VARIANT_FILE}: mode {cfg['mode']!r} is not one of {sorted(RUN_MODES.values())}")
    if "embedding" in own:
        spec = EMBEDDINGS.get(own["embedding"])
        if spec is None:
            raise ReviewError(f"{VARIANT_FILE}: embedding {own['embedding']!r} is not in the runner's registry")
        cfg["embedding"] = {"model": spec.key, "dims": spec.dims, "backend": spec.backend}
        cfg["dense_version"] = own.get("dense_version") or ev._ready_dense_for(
            settings.with_(embedding_model=spec.key, embedding_dimensions=spec.dims), cfg["index_version"])
        if cfg["mode"] in DENSE_MODES and not cfg["dense_version"]:
            raise ReviewError(f"no ready vector set of {spec.key} over index {cfg['index_version']}; build it with "
                              "`compare --matrix embedding` first (a stage review never builds or pays)")
    elif own.get("dense_version"):
        cfg["dense_version"] = own["dense_version"]
    if "reranker" in own:
        rr = own["reranker"] if isinstance(own["reranker"], dict) else {"model": own["reranker"]}
        spec = RERANKERS.get(rr.get("model"))
        if spec is None:
            raise ReviewError(f"{VARIANT_FILE}: reranker {rr.get('model')!r} is not in the runner's registry")
        cfg["reranker"] = {"model": spec.key, "revision": rr.get("revision") or spec.revision,
                           "max_length": rr.get("max_length") or spec.max_length,
                           "precision": rr.get("precision") or spec.precision,
                           "depth": rr.get("depth") or cfg["limits"].get("fused_top_k", settings.fused_top_k),
                           "protect": rr.get("protect", 0)}
    if cfg["mode"] == "hybrid_rerank" and not cfg["reranker"]:
        raise ReviewError("mode hybrid_rerank needs a reranker in the variant")
    if cfg["mode"] in DENSE_MODES and not cfg["embedding"]:
        raise ReviewError(f"mode {cfg['mode']} needs an embedding model")
    local_embedding = cfg["mode"] in DENSE_MODES and (cfg["embedding"] or {}).get("backend") == "local"
    cfg["gpu"] = [why for why, needed in (
        (f"local embedding model {(cfg['embedding'] or {}).get('model')}", local_embedding),
        (f"local reranker {(cfg['reranker'] or {}).get('model')}", cfg["mode"] == "hybrid_rerank")) if needed]
    return cfg


def read_variant(worktree: Path) -> dict:
    path = worktree / VARIANT_FILE
    if not path.exists():
        return {}
    try:
        variant = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ReviewError(f"{VARIANT_FILE} is not valid JSON: {exc}") from None
    if not isinstance(variant, dict) or set(variant) - set(STAGES) or not all(
            isinstance(v, dict) for v in variant.values()):
        raise ReviewError(f"{VARIANT_FILE} holds one object per stage among {STAGES}")
    for stage, section in variant.items():
        for key, value in section.items():
            if key in VARIANT_TYPES[stage] and not isinstance(value, VARIANT_TYPES[stage][key]):
                raise ReviewError(f"{VARIANT_FILE}: {stage}.{key} must be "
                                  f"{' or '.join(t.__name__ for t in VARIANT_TYPES[stage][key])}")
    return variant


def execute(settings: Settings, worker: Path, worktree: Path, stage: str, inputs: Path, config: Path,
            output: Path, mode: str = "run", approval: dict | None = None) -> dict:
    """One worker process. `mode` is "run" (retriever, chunking), "estimate" (a paid stage's free pricing pass) or
    "paid", which gets the API key only from `paid.approved_env`, i.e. from an approved estimate of this run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SECRETS)}
    if mode == "paid":
        env.update(paid.approved_env(worker.parent, approval or {}))
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               RFP_DATA_DIR=str(settings.data_dir), RFP_SOURCE_DIR=str(settings.source_dir))
    if stage in ("retriever", "chunking") or stage == "ocr" and mode == "estimate":
        env["PGOPTIONS"] = "-c default_transaction_read_only=on"  # generation records requests; paid reads settle
    try:
        done = subprocess.run([sys.executable, "-I", "-B", str(worker), str(worktree), stage, str(inputs), str(config),
                               str(output), mode], cwd=worktree, env=env, capture_output=True, timeout=WORKER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": f"the worker exceeded {WORKER_TIMEOUT_S} s"}
    if not output.exists():
        return {"status": "failed", "reason": "the worker exited without output: "
                + done.stderr.decode("utf-8", errors="replace")[-2000:]}
    return json.loads(output.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- the run


def run(settings: Settings, stage: str, base: str, cands: list[str], inputs_from: str | None = None,
        ledger_env: str | None = None) -> dict:
    """Freezes the inputs and runs every candidate. A paid stage stops after pricing: run.json says needs_approval
    and estimate.json holds the price; `paid.approve` and `resume` continue it."""
    if stage not in STAGES:
        raise ReviewError(f"stage must be one of {STAGES}")
    if not cands:
        raise ReviewError("name at least one candidate (--cand <ref>, or --cand . for the working tree)")
    refs = [resolve(r) for r in [base, *cands]]
    for i, ref in enumerate(refs):
        ref.update(id="base" if i == 0 else f"c{i}", role="baseline" if i == 0 else "candidate")
    run_id = f"{stage}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    root = review_dir(settings) / "runs"
    folder = root / run_id
    inputs = folder / "inputs"
    folder.mkdir(parents=True)
    if inputs_from:
        source = json.loads((root / inputs_from / "run.json").read_text(encoding="utf-8"))
        if source.get("stage") != stage:
            raise ReviewError(f"run {inputs_from} is a {source.get('stage')} run, not {stage}")
        shutil.copytree(root / inputs_from / "inputs", inputs)
    else:
        freeze(settings, stage, inputs)
    worker = folder / "worker.py"
    shutil.copy2(Path(__file__).with_name("stage_review_worker.py"), worker)
    host = runner_host()
    (folder / "candidates").mkdir()
    for cand in refs:
        worktree = review_dir(settings) / "worktrees" / f"{run_id}-{cand['id']}"
        try:
            checkout(cand, worktree)
            variant = read_variant(worktree)
            cand["variant"] = variant.get(stage)
            cand["config"] = config = candidate_config(settings, stage, inputs, variant)
            if stage == "ocr":  # the remote read is priced on the shared ledger's rate card, as it will settle there
                config["ledger_env"] = ledger_env
            if config["gpu"] and not host["cuda"]:
                cand.update(status="refused", host=host, reason=(
                    f"needs a CUDA GPU for the {' and the '.join(config['gpu'])}; this host has none. Run this "
                    "comparison on a GPU host and import its run folder"))
                continue
            config_path = folder / "candidates" / f"{cand['id']}.config.json"
            write_text_atomic(config_path, json.dumps(config, ensure_ascii=False, indent=1))
            priced = stage in PAID_STAGES
            out = execute(settings, worker, worktree, stage, inputs, config_path, folder / "candidates" / (
                f"{cand['id']}.estimate.json" if priced else f"{cand['id']}.json"), "estimate" if priced else "run")
            cand.update({k: out[k] for k in ("status", "reason", "host", "packages", "elapsed_s") if k in out})
        except ReviewError as exc:
            cand.update(status="failed", reason=str(exc), host=host)
        except Exception as exc:  # noqa: BLE001 - one broken candidate must not cost the others' results
            cand.update(status="failed", reason=f"{type(exc).__name__}: {exc}", host=host)
        finally:
            remove_worktree(worktree)
    record = {"schema": SCHEMA, "run_id": run_id, "stage": stage, "created_at": datetime.now(timezone.utc).isoformat(),
              "inputs_from": inputs_from, "input_sha256": input_hashes(inputs),
              "worker_sha256": _sha(worker.read_bytes()), "scorer": {**ev.code_fingerprint(),
                                                                      "metric_code_sha256": ev.metric_code_sha256()},
              "runner_host": host, "candidates": refs}
    if stage in PAID_STAGES:
        record.update(state="needs_approval", ledger_env=ledger_env)
        if stage == "ocr":
            paid.render_images(folder)
        # Unpriced on purpose: `reprice` prices it on a ledger connection opened after an hour of local reads, not
        # one held idle through them; a failed pricing step then costs a re-price, not the reads.
        write_text_atomic(folder / "run.json", json.dumps(record, ensure_ascii=False, indent=1))
        return record
    view = score_retriever(settings, folder, record) if stage == "retriever" else score_chunking(folder, record)
    write_text_atomic(folder / "view.json", json.dumps(view, ensure_ascii=False))
    write_text_atomic(folder / "run.json", json.dumps(record, ensure_ascii=False, indent=1))
    return record


def reprice(settings: Settings, run_id: str) -> dict:
    """Prices an unpaid run again from what its candidates already priced (no worker runs): after a pricing step that
    failed, or an estimate that expired. Any earlier approval belongs to the replaced estimate and does not carry."""
    folder = run_folder(settings, run_id)
    record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    if record["stage"] not in PAID_STAGES or record.get("state") != "needs_approval":
        raise ReviewError(f"{run_id} is not waiting for an estimate")
    record["estimate"] = paid.estimate(settings, folder, record)["estimate_id"]
    write_text_atomic(folder / "run.json", json.dumps(record, ensure_ascii=False, indent=1))
    return record


def resume(settings: Settings, run_id: str) -> dict:
    """Pays an approved estimate: each candidate that priced and has not finished answers or reads with the key, at
    the commit it was priced at. A candidate stopped by the budget, unknown billing or its approved maximum keeps
    what it finished; resuming again pays only for what is left. Writes view.json after every candidate ran."""
    folder = run_folder(settings, run_id)
    record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    if record["stage"] not in PAID_STAGES or record.get("state") not in ("needs_approval", "stopped"):
        raise ReviewError(f"{run_id} has nothing left to pay for")
    estimate = paid.load_estimate(folder)
    paid.require_approved(estimate)  # before any worktree: an unapproved estimate makes nothing
    paid.recheck(settings, folder, record, estimate)
    inputs, worker = folder / "inputs", folder / "worker.py"
    for cand in record["candidates"]:
        if cand["id"] not in estimate["candidates"] or paid._finished(folder, cand["id"]):
            continue
        worktree = review_dir(settings) / "worktrees" / f"{run_id}-{cand['id']}"
        try:
            now = resolve(cand["ref"])
            if now["commit"] != cand["commit"]:
                raise ReviewError(f"{cand['ref']} moved to {now['commit'][:12]} since it was priced")
            checkout(now, worktree)
            if now.get("changed_sha256") != cand.get("changed_sha256"):
                raise ReviewError("the working tree changed since it was priced")
            config_path = folder / "candidates" / f"{cand['id']}.paid.json"
            write_text_atomic(config_path, json.dumps(paid.paid_config(folder, record, estimate, cand),
                                                      ensure_ascii=False, indent=1))
            out = execute(settings, worker, worktree, record["stage"], inputs, config_path,
                          folder / "candidates" / f"{cand['id']}.json", "paid", estimate)
            cand.update({"reason": None, **{k: out[k] for k in ("status", "reason", "host", "packages", "elapsed_s")
                                            if k in out}})
        except ReviewError as exc:
            cand.update(status="failed", reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - one broken candidate must not cost the others' results
            cand.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        finally:
            remove_worktree(worktree)
    # A candidate stopped or failed midway (a full disk, a lost connection) keeps the run resumable.
    done = all(c.get("status") == "complete" for c in record["candidates"] if c["id"] in estimate["candidates"])
    record.update(state="complete" if done else "stopped", paid_at=datetime.now(timezone.utc).isoformat())
    return _score_paid(settings, folder, record)


def rescore(settings: Settings, run_id: str) -> dict:
    """Re-reads the ledger and re-scores a paid run from its stored outputs: no worker, no call, no payment."""
    folder = run_folder(settings, run_id)
    record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    if record["stage"] not in PAID_STAGES or record.get("state") not in ("complete", "stopped"):
        raise ReviewError(f"{run_id} has no paid results to score")
    return _score_paid(settings, folder, record)


def on_ledger(settings: Settings, action: str, run_id: str, ledger_env: str | None = None) -> dict:
    """Runs reprice, resume or rescore inside the ledger's lifecycle, opened only now and never held idle through a
    worker's reads. The shared ledger may live in another database (codeit's, through a tunnel): no schema there."""
    from ..storage import store
    from ..storage.postgres import require_imported_database

    record = json.loads((run_folder(settings, run_id) / "run.json").read_text(encoding="utf-8"))
    step = {"reprice": reprice, "resume": resume, "rescore": rescore}[action]
    ledger_env = ledger_env or record.get("ledger_env")
    if not ledger_env:
        return step(settings, run_id)
    ledger = settings.with_(database_dsn_env=ledger_env)
    with store.database_lifecycle(ledger.db_path):
        require_imported_database(ledger.db_path)
        return step(settings, run_id)


def _score_paid(settings: Settings, folder: Path, record: dict) -> dict:
    paid.ledger_spend(settings, folder, record)
    view = paid.score_generation(settings, folder, record) if record["stage"] == "generation" else \
        paid.score_ocr(folder, record)
    write_text_atomic(folder / "view.json", json.dumps(view, ensure_ascii=False))
    write_text_atomic(folder / "run.json", json.dumps(record, ensure_ascii=False, indent=1))
    return record


def _output(folder: Path, cand: dict) -> dict | None:
    path = folder / "candidates" / f"{cand['id']}.json"
    if cand.get("status") != "complete" or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _summary(cand: dict) -> dict:
    return {k: cand.get(k) for k in ("id", "label", "ref", "role", "commit", "working_tree", "changed_files",
                                     "status", "reason", "host", "elapsed_s", "config", "variant", "ledger")}


# ---------------------------------------------------------------- retriever scoring


def score_retriever(settings: Settings, folder: Path, record: dict) -> dict:
    from ..retrieval.retrieval import KeywordIndex

    inputs = folder / "inputs"
    frozen = json.loads((inputs / "questions.json").read_text(encoding="utf-8"))
    rows = json.loads((inputs / "rows.json").read_text(encoding="utf-8"))
    docs = json.loads((inputs / "documents.json").read_text(encoding="utf-8"))
    activation = json.loads((inputs / "activation.json").read_text(encoding="utf-8"))
    index = KeywordIndex.load(settings, activation["index_version"])
    outputs = {c["id"]: _output(folder, c) for c in record["candidates"]}
    answers = {cid: {q["key"]: q for q in out["questions"]} for cid, out in outputs.items() if out}
    questions = []
    for q in frozen["questions"]:
        row = rows[q["key"]]
        groups = ev.row_groups(row)
        per = {}
        for cid, by_key in answers.items():
            per[cid] = _score_question(index, docs, row, groups, q, by_key.get(q["key"]))
        complete = [p for p in per.values() if "error" not in p]
        tops = {json.dumps([[t["chunk_id"] for t in s[:5]] for s in p["sides"]]) for p in complete}
        outcomes = {json.dumps([p["gold_rank"], p["ndcg"], p["hit5"], p["complete20"]]) for p in complete}
        questions.append({"key": q["key"], "id": q["id"], "population": q["population"], "question": q["question"],
                          "type": q["type"], "gold": [{"group_id": g["group_id"], "doc_id": g.get("doc_id"),
                                                       "quotes": [a.get("quote") for a in g["alternatives"]]}
                                                      for g in groups],
                          "candidates": per, "differs": len(tops) > 1 or len(outcomes) > 1,
                          "outcome_differs": len(outcomes) > 1 or len(complete) < len(per)})
    summary = {}
    for cid in answers:
        dev = [q["candidates"][cid] for q in questions if q["population"] == "dev"]
        needle = [q["candidates"][cid] for q in questions if q["population"] == "needle"]
        ndcg = [p["ndcg"] for p in dev if p.get("ndcg") is not None]
        summary[cid] = {"ndcg5": round(sum(ndcg) / len(ndcg), 4) if ndcg else None, "ndcg5_n": len(ndcg),
                        "dev_rows": len(dev), "needle_hits": sum(bool(p.get("hit5")) for p in needle),
                        "needle_rows": len(needle), "errors": sum("error" in p for p in dev + needle)}
    return {"schema": SCHEMA, "stage": "retriever", "run_id": record["run_id"],
            "candidates": [_summary(c) for c in record["candidates"]], "summary": summary,
            "populations": {k: {kk: v[kk] for kk in ("dataset", "rows", "not_ranked")} | {"skipped": len(v["skipped"])}
                            for k, v in frozen["populations"].items()},
            "activation": {k: activation.get(k) for k in ("run_id", "mode", "index_version", "dense_version")},
            "questions": questions}


def _score_question(index, docs: dict, row: dict, groups: list[dict], q: dict, answer: dict | None) -> dict:
    if answer is None:
        return {"error": "no output for this question"}
    sides, parts, gold_rank = [], [], None
    try:
        for side, doc in zip(answer["sides"], q["side_docs"]):
            ranking = [index.chunks[index.row_of[c]] for c in side["ranking"]]
            packed = [index.chunks[index.row_of[c]] for c in side["packed"]]
            mine = groups if doc is None else [g for g in groups if g.get("doc_id") == doc]
            parts.append(ev.score_row(row, ranking, packed, index.elements, mine))
            grades = [max((ev.group_grade(chunk, g, index.elements) for g in mine), default=0) for chunk in ranking]
            if len(answer["sides"]) == 1:  # over the whole scored ranking, not only the passages shown
                gold_rank = next((rank for rank, grade in enumerate(grades, start=1) if grade == 2), None)
            top = []
            for rank, (chunk, grade) in enumerate(zip(ranking[:TOP_K], grades), start=1):
                top.append({"rank": rank, "chunk_id": chunk["chunk_id"], "grade": grade,
                            "doc": (docs.get(chunk["extraction_id"]) or {}).get("title"),
                            "section": " > ".join(chunk.get("section_path") or []),
                            "text": chunk.get("body") or chunk["payload"],
                            "packed": chunk["chunk_id"] in side["packed"]})
            sides.append(top)
    except KeyError as exc:
        return {"error": f"ranked chunk {exc} is not in the frozen index"}
    metrics = parts[0] if len(parts) == 1 else ev.combine_sides(parts)
    return {"ndcg": metrics.get("ndcg@5"), "hit5": metrics.get("hit@5"), "gold_rank": gold_rank,
            "complete20": metrics.get("complete@20"), "sides": sides, "side_docs": q["side_docs"],
            "fallback": next((s["fallback"] for s in answer["sides"] if s.get("fallback")), None),
            "limitations": sorted({x for s in answer["sides"] for x in s.get("limitations") or []})}


# ---------------------------------------------------------------- chunking scoring


def _is_title(el: dict) -> bool:
    text = el["raw_text"].strip()
    return 0 < len(text) <= TITLE_CHARS and not re.search(r"(다|음|함|임)\s*[.。]?$", text)


def table_titles(elements: list[dict]) -> dict[str, list[str]]:
    """Table element -> its title: up to two short title lines right before it in the same section, and its
    caption. A table without either is not counted."""
    out, before = {}, []
    for el in elements:
        if el["kind"] == "toc" or not el["raw_text"].strip():
            continue
        if el["kind"] == "table" and el["table"]["rows"] * el["table"]["cols"] > 1:
            titles = [e["raw_text"].strip() for e in before[-TITLE_LINES:]]
            titles += [el["table"]["caption"].strip()] if (el["table"].get("caption") or "").strip() else []
            if titles:
                out[el["element_id"]] = titles
            before = []
        elif el["kind"] in ("paragraph", "heading") and _is_title(el) and (
                not before or before[-1]["location"].get("section_path") == el["location"].get("section_path")):
            before.append(el)
        else:
            before = []
    return out


def _squash(text: str) -> str:
    return "".join(text.split())


def _end_key(order: dict, chunk: dict) -> list:
    """Where a chunk ends in document order: its last span's element, row and character."""
    span = chunk["spans"][-1]
    i = order.get(span["element_id"], -1)
    if "rows" in span:
        frag = span.get("fragment")
        return [i, max(span["rows"]), frag["end"] if frag else 10 ** 9]
    return [i, 0, span.get("end", 0)]


def _sizes(tokens: list[int]) -> dict:
    if not tokens:
        return {"count": 0}
    s = sorted(tokens)
    pick = lambda q: s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]  # noqa: E731
    edges = [100, 200, 300, 400, 500, 600, 700, 800]
    hist = [sum(1 for t in s if lo < t <= hi) for lo, hi in zip([0] + edges, edges)] + [sum(t > 800 for t in s)]
    return {"count": len(s), "min": s[0], "p50": pick(0.5), "p90": pick(0.9), "max": s[-1],
            "mean": round(sum(s) / len(s), 1), "histogram": hist, "edges": edges}


def score_chunking(folder: Path, record: dict) -> dict:
    with gzip.open(folder / "inputs" / "documents.json.gz", "rt", encoding="utf-8") as f:
        documents = json.load(f)["documents"]
    outputs = {c["id"]: _output(folder, c) for c in record["candidates"]}
    chunks = {cid: {d["extraction_id"]: d["chunks"] for d in out["documents"]} for cid, out in outputs.items() if out}
    (folder / "docs").mkdir(exist_ok=True)
    listing, totals = [], {cid: {"tokens": [], "titled": 0, "kept": 0} for cid in chunks}
    for n, doc in enumerate(documents):
        x = doc["extraction_id"]
        order = {el["element_id"]: i for i, el in enumerate(doc["elements"])}
        titles = table_titles(doc["elements"])
        per, ends = {}, {}
        for cid, by_doc in chunks.items():
            mine = by_doc.get(x) or []
            lost, titled = [], len(titles)  # titled tables come from the frozen source; a dropped table is lost
            for table, lines in titles.items():
                carrying = [c for c in mine if any(s["element_id"] == table for s in c["spans"])]
                if not carrying or not all(_squash(t) in _squash(c["payload"]) for c in carrying for t in lines):
                    lost.append(table)
            totals[cid]["titled"] += titled
            totals[cid]["kept"] += titled - len(lost)
            totals[cid]["tokens"] += [c["token_count"] for c in mine]
            per[cid] = {"sizes": _sizes([c["token_count"] for c in mine]), "titles_lost": lost, "titled": titled}
            for c in mine:
                ends.setdefault(tuple(_end_key(order, c)), {})[cid] = {
                    "chunk_id": c["chunk_id"], "tokens": c["token_count"], "type": c["chunk_type"],
                    "text": c["payload"], "title_lost": any(s["element_id"] in lost for s in c["spans"])}
        aligned = []
        for key in sorted(ends):
            cells = ends[key]
            el = doc["elements"][key[0]] if 0 <= key[0] < len(doc["elements"]) else None
            aligned.append({"at": {"element": key[0], "row": key[1], "kind": el["kind"] if el else None},
                            "cells": cells, "differs": len(cells) != len(chunks)
                            or len({c["text"] for c in cells.values()}) > 1})
        differing = sum(r["differs"] for r in aligned)
        write_text_atomic(folder / "docs" / f"{n}.json", json.dumps(
            {"n": n, "extraction_id": x, "doc_id": doc["doc_id"], "title": doc["title"], "candidates": per,
             "rows": aligned}, ensure_ascii=False))
        listing.append({"n": n, "doc_id": doc["doc_id"], "title": doc["title"], "differing": differing,
                        "boundaries": len(aligned), "tables_titled": len(titles),
                        "counts": {cid: p["sizes"]["count"] for cid, p in per.items()}})
    summary = {cid: {"sizes": _sizes(t["tokens"]), "tables_kept": t["kept"], "tables_titled": t["titled"],
                     "documents": len(documents)} for cid, t in totals.items()}
    return {"schema": SCHEMA, "stage": "chunking", "run_id": record["run_id"],
            "candidates": [_summary(c) for c in record["candidates"]], "summary": summary,
            "documents": sorted(listing, key=lambda d: (-d["differing"], d["n"]))}
