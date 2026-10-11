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
from .error_analysis import write_lock
from .stage_review import SCHEMA, ReviewError, review_dir

STATUSES = ("completed", "failed")
ASK_KEY = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CANDIDATE = "traffic"
CORPUS = "전체 문서"
PASSED = "passed"  # an answer call was sent and the validator accepted its output
UNCHECKED = "not_validated"  # no answer call to validate: a deterministic metadata or inventory answer
PROCESSING = "processing_failure"  # a failure without a validator verdict, e.g. a provider error
CLEAN = (PASSED, UNCHECKED)
RAN = ("dispatching", "settled", "unknown", "reconciled")  # ledger states of an answer call that was sent
LISTING_DATES = ("published_at", "bid_start", "bid_close")  # a listing's date columns (answer-parts.tsx LISTING_DATES)
ESCAPE = re.compile(r'\\u([0-9a-fA-F]{4})|\\(["\\/bfnrt])')  # one JSON escape
SHORT = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
LONE = re.compile("[\ud800-\udfff]")  # a surrogate left unpaired: no UTF-8 file can hold it
WITHHELD = "[구성원 ID가 인코딩된 채 들어 있어 가렸습니다]"


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
            # A corpus metadata answer keeps its listing outside result_json (service._envelope); a database no
            # version with listings has served has no such table.
            listings = conn.execute("SELECT to_regclass('request_listings') IS NOT NULL AS present").fetchone()
            listing = ("(SELECT l.listing_json FROM request_listings l WHERE l.request_id = requests.request_id)"
                       if listings["present"] else "NULL")
            rows = conn.execute(
                "SELECT request_id, member_id, idempotency_key, generation_id, status, mode, scope_json, "
                f"request_json, result_json, trace_json, created_at, {listing} AS listing_json FROM requests "
                "WHERE request_json IS NOT NULL AND result_json IS NOT NULL AND status = ANY(%s) AND created_at >= %s "
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
    with write_lock(settings.data_dir):  # two first runs at once must not each make a key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(f"{path.name}.{uuid.uuid4().hex}.part")
            part.write_bytes(secrets.token_bytes(32))
            os.replace(part, path)  # a reader never sees a half-written key
        key = path.read_bytes()
    if len(key) != 32:
        raise ReviewError(f"{path} is not a 32-byte pseudonym key; nothing was written")
    return key


def pseudonym(key: bytes, member_id: str) -> str:
    return "member-" + hmac.new(key, member_id.encode(), hashlib.sha256).hexdigest()[:8]


def decodings(text: str) -> list[str]:
    """The text as written and after each further round of JSON escape decoding until none is left: everything a
    reader can see however many times they decode, whatever spells the escape itself (`\\u005c` is a backslash
    too). Every level counts, not only the last: an ID holding a backslash and a letter (`team\\n`) reads as itself
    one level down and as a newline at the bottom. Surrogate pairs join into their character. For finding member
    IDs only, never for writing."""
    joined = lambda t: t.encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass")  # noqa: E731
    levels = [text]
    while "\\" in text:
        out = ESCAPE.sub(lambda m: chr(int(m[1], 16)) if m[1] else SHORT[m[2]], text)
        if out == text:
            break
        text = out
        levels.append(joined(text))
    return levels


def unescaped(text: str) -> str:
    return decodings(text)[-1]


def _names(text: str, members) -> bool:
    """A member ID is in the text as written or at any level decoding it recovers."""
    return any(m in level for level in decodings(text) for m in members)


def scrub(value, table: dict[str, str]):
    """Every string and key with no member ID left in it, however escaped. A string that names none stays as it was.
    One that does: a JSON text is cleaned value by value and serialised again (valid JSON, every other value the
    same); other text has the ID replaced where it is spelled plainly or JSON-escaped; if an ID still hides behind
    an escape, the whole string is withheld, since what cannot be told apart is not written as text. A surrogate
    left unpaired, which no UTF-8 file holds, becomes U+FFFD.
    ponytail: substring match, so a member ID that is also a common word would be replaced inside text too;
    a token-aware match if member IDs ever get that short."""
    order = sorted((m for m in table if m), key=len, reverse=True)
    spelled = [(json.dumps(m, ensure_ascii=False)[1:-1], table[m]) for m in order]

    def text(s: str) -> str:
        s = LONE.sub("�", s)
        if not _names(s, order):
            return s
        try:
            parsed = json.loads(s)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list, str)):
            try:
                return json.dumps(walk(parsed), ensure_ascii=False)
            except RecursionError:
                pass  # nested too deep to walk or serialise: clean it as text below
        for escaped, alias in spelled:
            s = s.replace(escaped, alias)
        for m in order:
            s = s.replace(m, table[m])
        return WITHHELD if _names(s, order) else s

    def walk(v):
        if isinstance(v, str):
            return text(v)
        if isinstance(v, dict):
            return {walk(k): walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(value)


def write_out(path: Path, value, table: dict[str, str]):
    """The only way anything enters a traffic run folder: scrubbed, serialised, refused if any member ID can still
    be recovered from the file however often it is decoded, then written. Returns what was written."""
    value = scrub(value, table)
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if _names(data.decode("utf-8"), [m for m in table if m]):
        raise ReviewError(f"{path.name}: a member ID survived pseudonymisation; nothing was written")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".gz":
        lines = b"".join(json.dumps(v, ensure_ascii=False).encode("utf-8") + b"\n" for v in value)
        with gzip.open(path, "wb") as f:
            f.write(lines)
    else:
        path.write_bytes(data)
    return value


# ---------------------------------------------------------------- one answer as the review app shows it


def _rejected(trace: dict) -> tuple[dict | None, bool]:
    """The answer the validator refused, from the trace's raw output (capped at 4000 characters, so a long one is
    read as far as it goes): (answer, cut short)."""
    raw = trace.get("raw_output")
    if not isinstance(raw, str) or not raw.strip():
        return None, False
    try:
        return json.loads(raw), False
    except (ValueError, RecursionError):  # the pure-Python decoder recurses per nesting level
        pass
    try:
        from pydantic_core import from_json

        parsed = from_json(raw, allow_partial="trailing-strings")
    except (ValueError, RecursionError):
        return None, False  # unreadable, not known to be cut: `answer` shows the raw text as it came
    return (parsed if isinstance(parsed, dict) else None), True


def _text(v) -> str | None:
    return v if isinstance(v, str) else None


def _dicts(v) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _ids(v) -> list[str]:
    return [i for i in v if isinstance(i, str)] if isinstance(v, list) else []


def _listing(v) -> dict | None:
    """A corpus metadata answer's list as the consultant saw it: every matching project with its cited description,
    and 확인 필요 with its reasons. Read as untrusted like the rest of the stored answer."""
    if not isinstance(v, dict):
        return None

    def when(d) -> dict | None:  # a stored date as the consultant's table read it: {value, precision}
        ok = isinstance(d, dict) and isinstance(d.get("value"), str) and d.get("precision") in ("date", "timestamp")
        return {"value": d["value"], "precision": d["precision"]} if ok else None

    def row(r: dict) -> dict:
        amount = r.get("amount_krw")
        return {"title": _text(r.get("title")) or "", "institution": _text(r.get("institution")),
                "institution_type": _text(r.get("institution_type")),
                "type_source": _text(r.get("type_source")),
                "amount_krw": amount if isinstance(amount, int) and not isinstance(amount, bool) else None,
                **{f: when(r.get(f)) for f in LISTING_DATES},
                "description": _text(r.get("description")), "evidence_ids": _ids(r.get("evidence_ids")),
                "note": _text(r.get("note"))}
    return {"conditions": _text(v.get("conditions")) or "", "count_only": v.get("count_only") is True,
            # the date columns the consultant's table showed: the dates the query filtered or sorted on
            "fields": [f for f in _ids(v.get("fields")) if f in LISTING_DATES],
            "rows": [row(r) for r in _dicts(v.get("rows"))],
            "review": [row(r) | {"reasons": [_text(x.get("label")) or "" for x in _dicts(r.get("reasons"))]}
                       for r in _dicts(v.get("review"))]}


def answer(result: dict, trace: dict, titles: dict[str, str]) -> dict:
    """The stored outcome in the generation view's answer shape. A validation failure shows the refused answer, as
    the consultant never saw it, so its spans can be noted. The refused answer failed its schema, so every field is
    read as untrusted: a field of the wrong type is dropped, and output with nothing readable is shown as it came.
    `checked`: an answer call was sent (any ledger state but reserved or released), popped into the group by `run`."""
    title = lambda d: titles.get(d, d) if isinstance(d, str) else None  # noqa: E731
    status = _text(result.get("status")) or ""
    error = _text(result.get("error")) or ""
    code = error.split(":", 1)[0]
    validation = code if error and status == "technical_error" and re.fullmatch(r"[a-z_]+", code) else None
    body, cut = (_rejected(trace) if validation else (None, False))
    shown = body if isinstance(body, dict) else result
    claims = [{"text": _text(c.get("text")) or "", "kind": _text(c.get("kind")), "doc": title(c.get("doc_id")),
               "evidence_ids": _ids(c.get("evidence_ids")), "supported": None, "links": []}
              for c in _dicts(shown.get("claims"))]
    summary = _text(shown.get("summary")) or ""
    raw = _text(trace.get("raw_output")) if validation else None
    if raw and not summary and not any(c["text"] for c in claims):
        summary = raw  # nothing readable parsed out of the refused output
    evidence = {}
    for eid, e in (result.get("evidence") if isinstance(result.get("evidence"), dict) else {}).items():
        e = e if isinstance(e, dict) else {}
        where = (e.get("location") if isinstance(e.get("location"), dict) else {}).get("section_path")
        evidence[eid] = {"doc": title(e.get("doc_id")), "quote": _text(e.get("quote")),
                         "section": " > ".join(_ids(where)), "text": None}
    billing = _dicts(trace.get("billing"))
    return {
        "outcome": status, "passed": None, "status_ok": status != "technical_error", "metadata": False,
        "summary": summary, "claims": claims, "evidence": evidence,
        "conflicts": [{"field": _text(c.get("field")), "alternatives": [
            {"doc": title(a.get("doc_id")), "value": _text(a.get("value")) or "",
             "evidence_ids": _ids(a.get("evidence_ids"))} for a in _dicts(c.get("alternatives"))]}
            for c in _dicts(shown.get("conflicts"))],
        "next_action": _text(shown.get("next_action")),
        "missing": [{"doc_id": _text(m.get("doc_id")), "field": _text(m.get("field")) or "",
                     "reason": _text(m.get("reason"))} for m in _dicts(shown.get("missing_fields"))],
        "facts": _dicts(result.get("facts")), "listing": _listing(result.get("listing")),
        "validation": validation, "validation_detail": error if validation else None,
        "failure": error if error and not validation else None,
        "rejected_shown": isinstance(body, dict) or bool(raw) and summary == raw, "rejected_cut": cut,
        "checked": any(a.get("stage") == "generation" and a.get("state") in RAN for a in billing),
        "required": [], "latency_ms": None, "replayed": False, "trace_url": None,
        "cost_micro_usd": sum(a["settled_micro_usd"] for a in billing if isinstance(a.get("settled_micro_usd"), int))}


# ---------------------------------------------------------------- groups and the sample


def group_of(mode: str, outcome: str, validation: str | None, failure: str | None, docs: list[str],
             checked: bool) -> dict:
    """`checked`: an answer call was sent. The service validates every response it gets back and stores an error for
    every call that returned none (service.py `_call_and_settle`, `_paid_answer`), so with no failure recorded the
    validator passed its output, whether or not the call's usage settled. Without a call nothing was validated (a
    deterministic answer), and the group says so rather than claim a pass."""
    doc = CORPUS if mode == "corpus" or not docs else " · ".join(sorted(docs))
    v = validation or (PROCESSING if failure else PASSED if checked else UNCHECKED)
    return {"key": json.dumps([mode, outcome, v, doc], ensure_ascii=False), "mode": mode, "outcome": outcome,
            "validation": v, "doc": doc}


def _priority(g: dict) -> int:
    """Failures first, then answers that were not plain answers, then the rest."""
    return 0 if g["validation"] not in CLEAN else 1 if g["outcome"] != "answered" else 2


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
        if r.get("listing_json"):  # the answer as the consultant saw it, before it is scrubbed and frozen
            result["listing"] = json.loads(r["listing_json"])
        snap.pop("idempotency_key", None)  # equal to the generation id, kept once
        member = table.get(r["member_id"]) or pseudonym(key, r["member_id"])
        record = scrub({"request_id": r["request_id"], "member": member, "status": r["status"],
                        "mode": r["mode"] or snap.get("mode"), "created_at": r["created_at"],
                        "scope": json.loads(r["scope_json"] or "[]"), "request": snap, "result": result,
                        "trace": trace}, table)
        frozen.append(record)
        snap, result = record["request"], record["result"]
        a = answer(result, record["trace"], titles)  # from the frozen record, so the view never disagrees with it
        docs = [titles.get(s.get("doc_id"), s.get("doc_id")) for s in record["scope"]]
        g = group_of(record["mode"], a["outcome"], a["validation"], a["failure"], docs, a.pop("checked"))
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
        write_out(inputs, frozen, table)
        record["input_sha256"] = {inputs.name: hashlib.sha256(inputs.read_bytes()).hexdigest()}
        write_out(folder / "view.json", view, table)
        write_out(folder / "run.json", record, table)  # last: the app lists a run once it is there
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {"run_id": run_id, "folder": str(folder), "requests": len(rows), "groups": len(groups),
            "sampled": len(picks)}
