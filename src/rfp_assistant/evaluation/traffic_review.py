"""Traffic review: real consultant answers from the serving database, frozen read-only into one review run.

    python -m rfp_assistant.cli stage-review run traffic --since 2026-10-01 --sample 30 --source-dsn-env CODEIT_DATABASE_DSN

Reads, in one read-only transaction, every 질문하기 request (idempotency key = generation id = a UUID, no verifier
run: never an evaluation, stage-review or sealed-set request) created since the date that finished with a stored
outcome (status completed, or failed with its validation failure), with its request_json, result_json and
trace_json. The whole window is frozen into `<data>/review/runs/traffic-<stamp>-<hex>/`, member IDs replaced by a
stable pseudonym (an HMAC under a key kept in `<data>/review/traffic/`, outside every run folder), and the review app
shows it like a generation run with one candidate, `traffic`:

    run.json                 window, population identity, the sampling rule and seed; never the DSN
    inputs/requests.jsonl.gz every request of the window: request, result and trace, pseudonymised
    view.json                every answer (annotatable), the groups and the drawn sample with each pick's reason

The sample is drawn by rule-based groups (mode, outcome, validation failure, document): one representative per
group, failures first, then extra picks for large groups in proportion to their size. It is a guess and covers
nothing beyond what it picks; the app shows every request of the window and adds any of them by hand. Nothing here
writes to the source database or calls a model.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..settings import Settings
from .stage_review import SCHEMA, ReviewError, review_dir

STATUSES = ("completed", "failed")
ASK_KEY = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CANDIDATE = "traffic"
CORPUS = "전체 문서"
NONE = "none"
PROCESSING = "processing_failure"  # a failure without a validator verdict, e.g. a provider error


# ---------------------------------------------------------------- the read


def read_window(dsn: str, since: str) -> tuple[list[dict], list[str], dict[str, str]]:
    """The window's 질문하기 requests, every member ID the database knows, and document titles. One read-only
    transaction: the session refuses any write, whatever this code does."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, connect_timeout=10, row_factory=dict_row,
                         options="-c default_transaction_read_only=on") as conn:
        conn.read_only = True
        with conn.transaction():
            rows = conn.execute(
                "SELECT request_id, member_id, idempotency_key, generation_id, status, mode, scope_json, "
                "request_json, result_json, trace_json, created_at FROM requests WHERE request_json IS NOT NULL "
                "AND result_json IS NOT NULL AND status = ANY(%s) AND created_at >= %s "
                "ORDER BY created_at, request_id", (list(STATUSES), since)).fetchall()
            members = [r["member_id"] for r in conn.execute("SELECT DISTINCT member_id FROM requests")]
            titles = {r["doc_id"]: (json.loads(r["normalized_metadata_json"] or "{}").get("title") or r["doc_id"])
                      for r in conn.execute("SELECT doc_id, normalized_metadata_json FROM documents")}
    asked = []
    for r in rows:
        snap = json.loads(r["request_json"])
        if ASK_KEY.match(r["idempotency_key"]) and r["idempotency_key"] == r["generation_id"] \
                and not snap.get("verifier_run_id"):
            asked.append(r)
    return asked, members, titles


# ---------------------------------------------------------------- pseudonyms and the exit gate


def pseudonym_key(settings: Settings) -> bytes:
    """This data folder's pseudonym key: created once, kept outside every run folder, so the same member gets the
    same pseudonym in every traffic run here and a run folder alone cannot be reversed."""
    path = review_dir(settings) / "traffic" / "pseudonym.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
    return path.read_bytes()


def pseudonym(key: bytes, member_id: str) -> str:
    return "member-" + hmac.new(key, member_id.encode(), hashlib.sha256).hexdigest()[:8]


def scrub(value, table: dict[str, str]):
    """Every string and key with each known member ID replaced by its pseudonym, longest first.
    ponytail: substring replacement, so a member ID that is also a common word would be replaced inside text too;
    a token-aware match if member IDs ever get that short."""
    order = sorted(table, key=len, reverse=True)

    def walk(v):
        if isinstance(v, str):
            for m in order:
                if m in v:
                    v = v.replace(m, table[m])
            return v
        if isinstance(v, dict):
            return {walk(k): walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(value)


def write_out(path: Path, value, members: list[str]) -> bytes:
    """The only way anything enters a traffic run folder: serialised, refused if any member ID survived, written."""
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    leaked = [m for m in members if m and m.encode("utf-8") in data]
    if leaked:
        raise ReviewError(f"{path.name}: a member ID survived pseudonymisation; nothing was written")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".gz":
        lines = b"".join(json.dumps(v, ensure_ascii=False).encode("utf-8") + b"\n" for v in value)
        with gzip.open(path, "wb") as f:
            f.write(lines)
    else:
        path.write_bytes(data)
    return data


# ---------------------------------------------------------------- one answer as the review app shows it


def _rejected(trace: dict) -> tuple[dict | None, bool]:
    """The answer the validator refused, from the trace's raw output (capped at 4000 characters, so a long one is
    read as far as it goes): (answer, cut short)."""
    raw = trace.get("raw_output")
    if not isinstance(raw, str) or not raw.strip():
        return None, False
    try:
        return json.loads(raw), False
    except ValueError:
        pass
    try:
        from pydantic_core import from_json

        parsed = from_json(raw, allow_partial="trailing-strings")
    except ValueError:
        return None, True
    return (parsed if isinstance(parsed, dict) else None), True


def answer(result: dict, trace: dict, titles: dict[str, str]) -> dict:
    """The stored outcome in the generation view's answer shape. A validation failure shows the refused answer, as
    the consultant never saw it, so its spans can be noted."""
    title = lambda d: titles.get(d, d)  # noqa: E731
    status = result.get("status") or ""
    error = result.get("error") or ""
    code = error.split(":", 1)[0]
    validation = code if error and status == "technical_error" and re.fullmatch(r"[a-z_]+", code) else None
    body, cut = (_rejected(trace) if validation else (None, False))
    shown = body if isinstance(body, dict) else result

    def listed(key: str) -> list:
        v = shown.get(key)
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    evidence = {}
    for eid, e in (result.get("evidence") or {}).items():
        where = (e.get("location") or {}).get("section_path") or []
        evidence[eid] = {"doc": title(e.get("doc_id")), "quote": e.get("quote"),
                         "section": " > ".join(where) if isinstance(where, list) else "", "text": None}
    billing = trace.get("billing") or []
    return {
        "outcome": status, "passed": None, "status_ok": status != "technical_error", "metadata": False,
        "summary": shown.get("summary") if isinstance(shown.get("summary"), str) else "",
        "claims": [{"text": c.get("text") or "", "kind": c.get("kind"), "doc": title(c.get("doc_id")),
                    "evidence_ids": [i for i in c.get("evidence_ids") or [] if isinstance(i, str)],
                    "supported": None, "links": []} for c in listed("claims")],
        "evidence": evidence,
        "conflicts": [{"field": c.get("field"), "alternatives": [
            {"doc": title(a.get("doc_id")), "value": a.get("value") or "",
             "evidence_ids": [i for i in a.get("evidence_ids") or [] if isinstance(i, str)]}
            for a in c.get("alternatives") or [] if isinstance(a, dict)]} for c in listed("conflicts")],
        "next_action": shown.get("next_action") if isinstance(shown.get("next_action"), str) else None,
        "missing": [{"doc_id": m.get("doc_id"), "field": m.get("field") or "", "reason": m.get("reason")}
                    for m in listed("missing_fields")],
        "facts": result.get("facts") or [],
        "validation": validation, "validation_detail": error if validation else None,
        "failure": error if error and not validation else None,
        "rejected_shown": isinstance(body, dict), "rejected_cut": cut,
        "required": [], "cost_micro_usd": sum(a.get("settled_micro_usd") or 0 for a in billing if isinstance(a, dict)),
        "latency_ms": None, "replayed": False, "trace_url": None}


# ---------------------------------------------------------------- groups and the sample


def group_of(mode: str, outcome: str, validation: str | None, failure: str | None, docs: list[str]) -> dict:
    doc = CORPUS if mode == "corpus" or not docs else " · ".join(sorted(docs))
    v = validation or (PROCESSING if failure else NONE)
    return {"key": json.dumps([mode, outcome, v, doc], ensure_ascii=False), "mode": mode, "outcome": outcome,
            "validation": v, "doc": doc}


def _priority(g: dict) -> int:
    """Failures first, then answers that were not plain answers, then the rest."""
    return 0 if g["validation"] != NONE else 1 if g["outcome"] != "answered" else 2


def draw(items: list[dict], n: int, seed: int) -> tuple[list[dict], list[dict]]:
    """(groups in sampling order, picks). `items` are {key, group}; each pick names its group and why it was taken.
    One representative per group in order (failures, then non-answers, then larger groups) while picks remain; the
    rest go to the groups with the most unpicked requests per pick (D'Hondt), so large groups get more."""
    groups: dict[str, dict] = {}
    for it in items:
        g = groups.setdefault(it["group"]["key"], {**it["group"], "requests": []})
        g["requests"].append(it["key"])
    ordered = sorted(groups.values(), key=lambda g: (_priority(g), -len(g["requests"]), g["key"]))
    rng = random.Random(seed)
    left = {g["key"]: list(g["requests"]) for g in ordered}
    picks, taken = [], {g["key"]: 0 for g in ordered}

    def take(g: dict, kind: str) -> None:
        key = left[g["key"]].pop(rng.randrange(len(left[g["key"]])))
        taken[g["key"]] += 1
        picks.append({"key": key, "group": g["key"], "kind": kind, "group_size": len(g["requests"]),
                      "nth": taken[g["key"]]})
    for g in ordered[:max(0, n)]:
        take(g, "representative")
    while len(picks) < n:
        open_ = [g for g in ordered if left[g["key"]]]
        if not open_:
            break
        take(max(open_, key=lambda g: (len(g["requests"]) / (taken[g["key"]] + 1), -ordered.index(g))), "extra")
    return ordered, picks


def reason(pick: dict, seed: int) -> str:
    """Why a request is in the sample, in words a person can check against the map; its group is recorded beside."""
    if pick["kind"] == "representative":
        return f"묶음 대표: 이 묶음 {pick['group_size']}건 중 하나를 시드 {seed}로 무작위로 골랐습니다."
    return (f"큰 묶음 추가 표본: 이 묶음 {pick['group_size']}건에서 {pick['nth']}번째로 뽑았습니다 "
            f"(남은 표본을 묶음 크기에 비례해 나눔).")


RULE = ("요청을 모드 · 결과 · 검증 실패 · 문서가 모두 같은 것끼리 묶었습니다. 검증 실패 묶음, 답변이 아닌 결과 묶음, "
        "나머지 순으로, 같은 순위에서는 큰 묶음부터 대표를 하나씩 뽑고, 표본이 남으면 큰 묶음에 크기 비례로 더 뽑았습니다.")


# ---------------------------------------------------------------- the run


def run(settings: Settings, since: str | None, sample: int | None, source_dsn_env: str, dsn: str | None = None) -> dict:
    """Freeze one window and its sample; returns what the CLI prints. The DSN comes from source_dsn_env unless given."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since or ""):
        raise ReviewError("stage-review run traffic needs --since <YYYY-MM-DD>")
    if not sample or sample < 1:
        raise ReviewError("stage-review run traffic needs --sample <n>, a positive number of requests")
    dsn = dsn or os.environ.get(source_dsn_env)
    if not dsn:
        raise ReviewError(f"{source_dsn_env} must hold the DSN of the serving database to read")
    rows, members, titles = read_window(dsn, since)
    if not rows:
        raise ReviewError(f"no finished 질문하기 request since {since} in the database of {source_dsn_env}")
    key = pseudonym_key(settings)
    table = {m: pseudonym(key, m) for m in members if m}
    run_id = f"traffic-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    folder = review_dir(settings) / "runs" / run_id
    frozen, questions, items = [], [], []
    for r in rows:
        snap, result, trace = (json.loads(r[k] or "{}") for k in ("request_json", "result_json", "trace_json"))
        snap.pop("idempotency_key", None)  # equal to the generation id, kept once
        record = {"request_id": r["request_id"], "member": table.get(r["member_id"], pseudonym(key, r["member_id"])),
                  "status": r["status"], "mode": r["mode"] or snap.get("mode"), "created_at": r["created_at"],
                  "scope": json.loads(r["scope_json"] or "[]"), "request": snap, "result": result, "trace": trace}
        frozen.append(scrub(record, table))
        a = answer(result, trace, titles)
        docs = [titles.get(s.get("doc_id"), s.get("doc_id")) for s in record["scope"]]
        g = group_of(record["mode"], a["outcome"], a["validation"], a["failure"], docs)
        items.append({"key": r["request_id"], "group": g})
        questions.append({"key": r["request_id"], "id": r["request_id"], "question": snap.get("question") or "",
                          "standalone_question": result.get("standalone_question") or "",
                          "follow_up": bool(snap.get("previous_request_id")), "type": "traffic",
                          "mode": record["mode"], "expected_status": "", "answerability": "", "docs": docs,
                          "created_at": r["created_at"], "member": record["member"], "group": g["key"],
                          "required": [], "candidates": {CANDIDATE: a}, "differs": False})
    population_sha = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    seed = int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16)
    groups, picks = draw(items, sample, seed)
    candidate = {"id": CANDIDATE, "label": "실제 요청", "ref": source_dsn_env, "role": "candidate",
                 "commit": population_sha, "working_tree": False, "status": "complete", "reason": None}
    window = {"since": since, "until": datetime.now(timezone.utc).isoformat(), "source_dsn_env": source_dsn_env,
              "statuses": list(STATUSES), "requests": len(rows),
              "filter": "질문하기 requests only: idempotency key = generation id (a UUID), no verifier run"}
    view = {"schema": SCHEMA, "stage": "traffic", "run_id": run_id, "candidates": [candidate], "window": window,
            "rule": RULE, "seed": seed,
            "groups": [{k: g[k] for k in ("key", "mode", "outcome", "validation", "doc", "requests")}
                       | {"priority": _priority(g)} for g in groups],
            "sample": [{"key": p["key"], "group": p["group"], "kind": p["kind"],
                        "reason": reason(p, seed)} for p in picks],
            "questions": questions}
    record = {"schema": SCHEMA, "run_id": run_id, "stage": "traffic", "created_at": datetime.now(timezone.utc).isoformat(),
              "window": window, "population_sha256": population_sha, "sample": {"n": sample, "seed": seed,
                                                                                "drawn": len(picks)},
              "groups": len(groups), "candidates": [candidate]}
    try:
        inputs = folder / "inputs" / "requests.jsonl.gz"
        write_out(inputs, frozen, members)
        record["input_sha256"] = {inputs.name: hashlib.sha256(inputs.read_bytes()).hexdigest()}
        write_out(folder / "view.json", scrub(view, table), members)
        write_out(folder / "run.json", scrub(record, table), members)  # last: the app lists a run once it is there
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {"run_id": run_id, "folder": str(folder), "requests": len(rows), "groups": len(groups),
            "sampled": len(picks)}
