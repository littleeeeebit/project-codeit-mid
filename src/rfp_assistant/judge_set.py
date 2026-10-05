"""The judge golden set: known-wrong answers made by mutating answers an independent review approved as correct.

Each source is a positive item of the judge reference (a `supporting` link, a `supported` answer claim or a
`correct` required fact) from its held-out part, so no item the Jev thresholds were fitted on enters the set. A
mutation changes exactly one thing the review approved, so the mutant's label is known by construction and no
person reviews it:

* `amount` - a stated amount of money or a count becomes another number;
* `unit` - the unit after a stated number changes (`개월` -> `년`);
* `qualifier` - VAT or another qualifier flips (`부가세 포함` -> `부가세 별도`, `이상` -> `이하`);
* `date` - a stated date moves by a week, or a deadline or period moves (`15일 이상` -> `22일 이상`, `1년` ->
  `2년`); the reference answers state deadlines and periods rather than calendar dates;
* `negation` - one statement the cited passage makes is negated;
* `dropped_condition` - a required fact's listed condition is removed from the answer;
* `wrong_evidence` - the claim cites another question's passages instead of its own.

Support items (link, answer claim) mutate the claim, so the cited passages no longer support it: `unsupported`.
Required-fact items mutate the answer: a changed value is `wrong_value`, a changed or dropped condition
`incomplete_qualifier`. Every mutation is anchored in what the passages state, and `differs` checks
deterministically that the mutant really differs from its source in the mutated respect; a mutant failing it is
not written. Each source that yields a mutant enters the set unmutated as the matching positive.

The set lives under `judges/judge-set/`, apart from the RAG development set (`datasets/dev.jsonl`), and is counted
apart in the golden-count report. The sealed test set is never read.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from . import evaluation
from .settings import Settings
from .store import utcnow, write_text_atomic

VERSION = "judge-set-1"
SEED = 20261005
TYPES = ("amount", "unit", "qualifier", "date", "negation", "dropped_condition", "wrong_evidence")
POSITIVE = {"link": "supporting", "answer_claim": "supported", "claim": "correct"}
FAIL = {"amount": "wrong_value", "unit": "wrong_value", "date": "wrong_value", "qualifier": "incomplete_qualifier",
        "dropped_condition": "incomplete_qualifier"}
# Units a number states; each maps to the unit a mutation swaps it for.
# Counter words that read alike (회/건, 명/개) are not swapped: the meaning would survive the change.
UNITS = {"개월": "년", "년": "개월", "주": "일", "일": "주", "시간": "분", "분": "시간", "원": "달러", "%": "배"}
COUNTERS = ("명", "개", "회", "건")  # stated counts an amount mutation may change
# Qualifier flips, VAT first; a token is replaced where it stands, longest match first.
FLIPS = (("부가세 포함", "부가세 별도"), ("부가세 별도", "부가세 포함"), ("부가가치세 포함", "부가가치세 별도"),
         ("부가가치세 별도", "부가가치세 포함"), ("VAT 포함", "VAT 별도"), ("VAT 별도", "VAT 포함"),
         ("이상", "이하"), ("이하", "이상"), ("이내", "초과"), ("초과", "이내"), ("미만", "초과"),
         ("필수", "선택"), ("의무", "권장"), ("이전", "이후"), ("이후", "이전"), ("사전", "사후"),
         ("최소", "최대"), ("최대", "최소"), ("포함", "제외"), ("제외", "포함"))
# Sentence-final predicates and their negation, most specific first.
COMPARISONS = ("이상", "이하", "이내", "초과", "미만")
NEGATIONS = (("하여야 합니다", "하지 않아도 됩니다"), ("해야 합니다", "하지 않아도 됩니다"),
             ("해야 하며", "하지 않아도 되며"), ("할 수 있습니다", "할 수 없습니다"), ("필요합니다", "필요하지 않습니다"),
             ("있습니다", "없습니다"), ("됩니다", "되지 않습니다"), ("합니다", "하지 않습니다"))
NEGATION_MARKS = re.compile(r"않|없|아닙|못")
# A period or deadline moves by this much of its own unit; numbers with these units are times, not amounts.
TIME_SHIFT = {"일": 7, "주": 1, "개월": 3, "년": 1, "시간": 12, "분": 30}
ANCHOR = 0.5  # share of a sentence's character bigrams its passages must contain
DONOR_MAX = 0.3  # a wrong-evidence donor shares less than this with the claim


def root(settings: Settings):
    return settings.data_dir / "judges" / "judge-set"


def _squash(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", evaluation.nfc(text or ""))


def overlap(text: str, passages: list[str]) -> float:
    """Share of the text's character bigrams that its passages contain."""
    grams = lambda s: {s[i:i + 2] for i in range(len(s) - 1)}  # noqa: E731
    mine, theirs = grams(_squash(text)), grams(_squash(" ".join(passages)))
    return len(mine & theirs) / len(mine) if mine else 0.0


def _date_spans(text: str) -> list[tuple[int, int, str]]:
    out = []
    for m in evaluation._DATE_RE.finditer(evaluation.nfc(text)):
        days = sorted(d for d in evaluation.extract_dates(m.group(0)) if "T" not in d)
        if days:
            out.append((m.start(), m.end(), days[0]))
    return out


def _numbers(text: str) -> list[tuple[Decimal, int, int, str]]:
    """(value, start, end, unit) of every integral number outside a date, with the unit written right after it."""
    t = evaluation.nfc(text)
    dates = _date_spans(t)
    out = []
    for value, start, end in evaluation.number_spans(t):
        if value != value.to_integral_value() or any(s < end and start < e for s, e, _ in dates):
            continue
        after = t[end:end + 3].lstrip()
        unit = next((u for u in sorted((*UNITS, *COUNTERS), key=len, reverse=True) if after.startswith(u)), "")
        if unit == "년" and 1900 <= value <= 2100:
            continue  # a year, not a duration
        out.append((evaluation._plain(value), start, end, unit))
    return out


def _render(value: Decimal, written: str) -> str:
    return f"{int(value):,}" if "," in written or re.search(r"[조억만천]", written) else str(int(value))


def _changed(value: Decimal, taken: set) -> Decimal | None:
    """Another integer that none of `taken` states: the leading digit up by one, else doubled, else plus one."""
    v = int(value)
    step = 10 ** (len(str(v)) - 1) if v >= 10 else 1
    for new in (v + step, v * 2, v + 1, v + 2 * step):
        if new != v and Decimal(new) not in taken:
            return Decimal(new)
    return None


def _replace_spans(text: str, spans: list[tuple[int, int, str]]) -> str:
    for start, end, new in sorted(spans, reverse=True):
        text = text[:start] + new + text[end:]
    return text


# ---------------------------------------------------------------- mutations of one text


def mutate_amount(text: str, anchors: set, taken: set, rng: random.Random) -> tuple[str, dict] | None:
    t = evaluation.nfc(text)
    found = [(v, s, e, u) for v, s, e, u in _numbers(t) if v in anchors and u not in TIME_SHIFT
             and (u or re.search(r"[조억만천]", t[s:e]))]
    if not found:
        return None
    value = rng.choice(sorted({f[0] for f in found}))
    new = _changed(value, taken | {f[0] for f in found})
    if new is None:
        return None
    out = _replace_spans(t, [(s, e, _render(new, t[s:e])) for v, s, e, _ in found if v == value])
    return out, {"from": str(value), "to": str(new)}


def mutate_unit(text: str, anchors: set[tuple[Decimal, str]], rng: random.Random) -> tuple[str, dict] | None:
    t = evaluation.nfc(text)
    found = [(v, s, e, u) for v, s, e, u in _numbers(t) if u in UNITS and (v, u) in anchors]
    if not found:
        return None
    value, unit = rng.choice(sorted({(v, u) for v, _, _, u in found}))
    swaps = []
    for v, s, e, u in found:
        if (v, u) == (value, unit):
            at = t.index(u, e)
            swaps.append((at, at + len(u), UNITS[u]))
    return _replace_spans(t, swaps), {"from": f"{value}{unit}", "to": f"{value}{UNITS[unit]}"}


def mutate_date(text: str, anchors: set[str], taken: set[str], periods: set[tuple[Decimal, str]],
                rng: random.Random) -> tuple[str, dict] | None:
    """A calendar date its anchor states moves by a week; without one, a stated period or deadline moves by
    TIME_SHIFT of its unit."""
    t = evaluation.nfc(text)
    found = [(s, e, d) for s, e, d in _date_spans(t) if d in anchors]
    if found:
        day = found[0][2]
        new = date.fromisoformat(day) + timedelta(days=7)
        if new.isoformat() in taken:
            new += timedelta(days=7)
        swaps = []
        for s, e, d in found:
            if d == day:
                times = sorted(x for x in evaluation.extract_dates(t[s:e]) if "T" in x)
                when = f" {times[0][11:]}" if times else ""
                swaps.append((s, e, f"{new.year}년 {new.month}월 {new.day}일{when}"))
        return _replace_spans(t, swaps), {"from": day, "to": new.isoformat()}
    spans = [(v, s, e, u) for v, s, e, u in _numbers(t) if u in TIME_SHIFT and (v, u) in periods]
    if not spans:
        return None
    value, unit = rng.choice(sorted({(v, u) for v, _, _, u in spans}))
    new = value + TIME_SHIFT[unit]
    while (new, unit) in periods:
        new += TIME_SHIFT[unit]
    out = _replace_spans(t, [(s, e, _render(new, t[s:e])) for v, s, e, u in spans if (v, u) == (value, unit)])
    return out, {"from": f"{value}{unit}", "to": f"{new}{unit}"}


def _flip_pattern(token: str) -> re.Pattern:
    """A comparison word only right after a number or its unit (`5년 이상`), never inside a noun (`이상 징후`)."""
    if token in COMPARISONS:
        return re.compile(r"(?<=[0-9%일주월년간분명개회건원])(\s*)" + token)
    return re.compile(r"()" + re.escape(token))


def mutate_qualifier(text: str, anchor_text: str) -> tuple[str, dict] | None:
    """The first flip whose token both the text and the anchor state, replaced everywhere it stands in the text;
    a longer flip containing a token (부가세 포함 before 포함) is tried first."""
    t, anchor = evaluation.nfc(text), evaluation.nfc(anchor_text)
    for old, new in FLIPS:
        pattern = _flip_pattern(old)
        if pattern.search(t) and pattern.search(anchor):
            return pattern.sub(lambda m: m.group(1) + new, t), {"from": old, "to": new}
    return None


def _sentences(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(r"[^.!?\n]+[.!?]?", text) if m.group(0).strip()]


def mutate_negation(text: str, passages: list[str]) -> tuple[str, dict] | None:
    """The negation of the sentence its passages state most (at least ANCHOR of its bigrams)."""
    t = evaluation.nfc(text)
    ranked = sorted(((overlap(t[s:e], passages), s, e) for s, e in _sentences(t)), reverse=True)
    for score, s, e in ranked:
        if score < ANCHOR:
            break
        sentence = t[s:e]
        for old, new in NEGATIONS:
            at = sentence.rfind(old)
            if at >= 0:
                changed = sentence[:at] + new + sentence[at + len(old):]
                return t[:s] + changed + t[e:], {"from": sentence.strip(), "to": changed.strip()}
    return None


def drop_condition(texts: list[str], conditions: list[str]) -> tuple[list[str], dict] | None:
    for condition in conditions:
        spellings = sorted({c.strip() for c in condition.split(" / ") if c.strip()}, key=len, reverse=True)
        if not any(sp in t for t in texts for sp in spellings):
            continue
        out = []
        for t in texts:
            for sp in spellings:
                t = t.replace(sp, "")
            out.append(re.sub(r"[ \t]{2,}", " ", re.sub(r"\(\s*\)", "", t)).strip())
        return out, {"from": condition, "to": ""}
    return None


# ---------------------------------------------------------------- the deterministic check


def _answer(item: dict) -> str:
    return "\n".join([item.get("answer_summary") or "", *(item.get("answer_claims") or [])])


def target_text(item: dict) -> str:
    return _answer(item) if item["kind"] == "claim" else item.get("claim") or ""


def differs(source: dict, mutant: dict) -> str | None:
    """None when the mutant differs from its source in the mutated respect, else why not. Pure code, no judge."""
    kind, m = mutant["mutation"]["type"], mutant["mutation"]
    before, after = target_text(source), target_text(mutant)
    passages = mutant.get("passages") or []
    if kind != "wrong_evidence" and before == after:
        return "text unchanged"
    if kind == "amount":
        old, new = Decimal(m["from"]), Decimal(m["to"])
        nums = evaluation.extract_numbers(after)
        if old in nums or new not in nums:
            return "the amount was not replaced everywhere"
        if source["kind"] != "claim" and new in evaluation.extract_numbers(" ".join(passages)):
            return "the passages also state the new amount"
    elif kind == "unit":
        value, old_unit = m["from"], next(u for u in sorted(UNITS, key=len, reverse=True) if m["from"].endswith(u))
        v, new_unit = Decimal(value[:-len(old_unit)]), UNITS[old_unit]
        if v in evaluation.stated_numbers(after, old_unit) or v not in evaluation.stated_numbers(after, new_unit):
            return "the unit was not replaced everywhere"
        if source["kind"] != "claim" and v in evaluation.stated_numbers(" ".join(passages), new_unit):
            return "the passages also state the new unit"
    elif kind == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", m["from"]):
        days = evaluation.extract_dates(after)
        if m["from"] in days or m["to"] not in days:
            return "the date was not replaced everywhere"
        if source["kind"] != "claim" and m["to"] in evaluation.extract_dates(" ".join(passages)):
            return "the passages also state the new date"
    elif kind == "date":
        unit = next(u for u in sorted(TIME_SHIFT, key=len, reverse=True) if m["from"].endswith(u))
        old, new = Decimal(m["from"][:-len(unit)]), Decimal(m["to"][:-len(unit)])
        if old in evaluation.stated_numbers(after, unit) or new not in evaluation.stated_numbers(after, unit):
            return "the period was not replaced everywhere"
        if source["kind"] != "claim" and new in evaluation.stated_numbers(" ".join(passages), unit):
            return "the passages also state the new period"
    elif kind == "qualifier":
        if after.count(m["to"]) <= before.count(m["to"]) or after.count(m["from"]) >= before.count(m["from"]):
            return "the qualifier did not flip"
        at = after.find(m["to"])
        window = _squash(after[max(0, at - 8):at + len(m["to"])])
        if source["kind"] != "claim" and window in _squash(" ".join(passages)):
            return "the passages state the flipped wording too"
    elif kind == "negation":
        if len(NEGATION_MARKS.findall(after)) <= len(NEGATION_MARKS.findall(before)):
            return "no negation was added"
    elif kind == "dropped_condition":
        spellings = [c.strip() for c in m["from"].split(" / ") if c.strip()]
        if any(sp in after for sp in spellings) or not any(sp in before for sp in spellings):
            return "the condition is still stated"
    elif kind == "wrong_evidence":
        if set(passages) & set(source.get("passages") or []) or not passages:
            return "the passages were not replaced"
        if overlap(after, passages) >= DONOR_MAX:
            return "the new passages state the claim"
    else:
        return f"unknown mutation {kind}"
    return None


# ---------------------------------------------------------------- the set


def _mutant(source: dict, kind: str, label: str, mutation: dict, **fields) -> dict:
    item = {**source, **fields, "blind_id": f"{source['blind_id']}-{kind}", "source_id": source["blind_id"],
            "reference": label, "mutation": {"type": kind, **mutation}}
    if source["kind"] == "claim":
        item["deterministic"] = None  # the sheet's pre-check described the unmutated answer
    return item


def mutants(source: dict, donors: list[dict]) -> list[dict]:
    """Every applicable mutation of one approved positive, at most one per type."""
    rng = random.Random(f"{SEED}:{source['blind_id']}")
    passages = source.get("passages") or []
    joined = " ".join(passages)
    out = []
    if source["kind"] == "claim":
        texts = [source.get("answer_summary") or "", *(source.get("answer_claims") or [])]
        required = source.get("required") or ""
        anchors = evaluation.extract_numbers(required)
        unit_anchors = {(v, u) for v, _, _, u in _numbers(required) if u}
        anchor_text = " ".join([required, *(source.get("conditions") or [])])

        def each(fn):
            changed, info = [], None
            for t in texts:
                got = fn(t)
                changed.append(got[0] if got else t)
                info = info or (got[1] if got else None)
            return (changed, info) if info else None

        taken = evaluation.extract_numbers(_answer(source))
        tries = {
            "amount": each(lambda t: mutate_amount(t, anchors, taken, random.Random(f"{SEED}:{source['blind_id']}"))),
            "unit": each(lambda t: mutate_unit(t, unit_anchors, random.Random(f"{SEED}:{source['blind_id']}"))),
            "date": each(lambda t: mutate_date(t, evaluation.extract_dates(required),
                                               evaluation.extract_dates(_answer(source)), unit_anchors,
                                               random.Random(f"{SEED}:{source['blind_id']}"))),
            "qualifier": each(lambda t: mutate_qualifier(t, anchor_text)),
            "dropped_condition": drop_condition(texts, source.get("conditions") or []),
        }
        for kind, got in tries.items():
            if got:
                summary, *claims = got[0]
                out.append(_mutant(source, kind, FAIL[kind], got[1], answer_summary=summary, answer_claims=claims))
    else:
        claim = source.get("claim") or ""
        unsupported = "unsupported"
        tries = {
            "amount": mutate_amount(claim, evaluation.extract_numbers(joined), evaluation.extract_numbers(joined), rng),
            "unit": mutate_unit(claim, {(v, u) for v, _, _, u in _numbers(joined) if u}, rng),
            "date": mutate_date(claim, {d for d in evaluation.extract_dates(joined) if "T" not in d},
                                evaluation.extract_dates(joined), {(v, u) for v, _, _, u in _numbers(joined) if u},
                                rng),
            "qualifier": mutate_qualifier(claim, joined),
            "negation": mutate_negation(claim, passages),
        }
        for kind, got in tries.items():
            if got:
                out.append(_mutant(source, kind, unsupported, got[1], claim=got[0]))
        others = [d for d in donors if d["question_id"] != source["question_id"] and d["kind"] != "claim"
                  and d.get("passages")]
        scored = sorted(((overlap(claim, d["passages"]), d["blind_id"], d) for d in others), key=lambda x: x[:2],
                        reverse=True)
        donor = next((d for score, _, d in scored if score < DONOR_MAX), None)
        if donor is not None:
            out.append(_mutant(source, "wrong_evidence", unsupported, {"from": source["blind_id"],
                                                                       "to": donor["blind_id"]},
                               passages=list(donor["passages"])))
    return out


def build(items: list[dict], held_out: list[str]) -> tuple[list[dict], dict]:
    """The set from the reference items' held-out positives, with every mutant passing `differs`."""
    by_id = {i["blind_id"]: i for i in items}
    sources = [by_id[b] for b in sorted(held_out) if by_id[b]["reference"] == POSITIVE[by_id[b]["kind"]]]
    rows, rejected = [], Counter()
    for source in sources:
        kept = []
        for m in mutants(source, sources):
            why = differs(source, m)
            if why is None:
                kept.append(m)
            else:
                rejected[f"{m['mutation']['type']}: {why}"] += 1
        if kept:
            rows.append({**source, "source_id": source["blind_id"], "mutation": None})
            rows += kept
    stats = {"positives": sum(r["mutation"] is None for r in rows), "negatives": sum(r["mutation"] is not None
                                                                                     for r in rows),
             "by_type": dict(sorted(Counter(r["mutation"]["type"] for r in rows if r["mutation"]).items())),
             "by_kind": dict(sorted(Counter(f"{r['kind']}:{r['reference']}" for r in rows).items())),
             "sources": len(sources), "rejected": dict(sorted(rejected.items()))}
    return rows, stats


def generate(settings: Settings) -> dict:
    """Writes the set from the installed reference and split. Deterministic: the same reference, split and
    generator write byte-identical files, and a set already written is kept as it is."""
    from . import judges

    items, manifest = judges.load_reference(settings)
    split = judges.load_split(settings)
    rows, stats = build(items, split["held_out"])
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    out = {"version": VERSION, "seed": SEED, "reference_sha256": manifest["reference_sha256"],
           "split_sha256": split["split_sha256"], "set_sha256": hashlib.sha256(body.encode()).hexdigest(),
           "items": len(rows), **stats,
           "note": "Mutated from the reference's held-out positives; labels known by construction. Separate from "
                   "the RAG development set (datasets/dev.jsonl)."}
    d = root(settings)
    path = d / "manifest.json"
    if path.exists():
        prior = json.loads(path.read_text(encoding="utf-8"))
        if prior["set_sha256"] == out["set_sha256"]:
            return {**prior, "reused": True}
    write_text_atomic(d / "items.jsonl", body)
    write_text_atomic(path, json.dumps({**out, "generated_at": utcnow()}, ensure_ascii=False, indent=1))
    return out


def load(settings: Settings) -> tuple[list[dict], dict]:
    d = root(settings)
    if not (d / "manifest.json").exists():
        raise ValueError("no judge set: run `judge-set` first")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    raw = (d / "items.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["set_sha256"]:
        raise ValueError("the judge set changed after it was generated; run `judge-set` again")
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()], manifest


def counts(settings: Settings) -> dict | None:
    """The judge set's counts for the golden-count report, or None before it is generated."""
    path = root(settings) / "manifest.json"
    if not path.exists():
        return None
    m = json.loads(path.read_text(encoding="utf-8"))
    return {k: m.get(k) for k in ("version", "items", "positives", "negatives", "by_type", "by_kind", "set_sha256")}


def mutation_rates(rows: list[dict]) -> dict:
    """False-accept rate per mutation type for one arm: rows {type, judged (the judge's own label, None =
    abstained), code (a deterministic value check would settle it)}; positives (`type` None) report how many the
    arm passed."""
    out: dict[str, dict] = {}
    for r in rows:
        cell = out.setdefault(r["type"] or "unmutated", {"items": 0, "judged": 0, "passed": 0, "code_settled": 0})
        cell["items"] += 1
        cell["code_settled"] += bool(r.get("code"))
        if r["judged"] is not None:
            cell["judged"] += 1
            cell["passed"] += r["judged"] in POSITIVE.values()
    for name, cell in out.items():
        rate = cell["passed"] / cell["judged"] if cell["judged"] else None
        cell["rate"] = None if rate is None else round(rate, 4)
        cell["wilson95"] = evaluation.wilson(cell["passed"], cell["judged"])
        cell["measure"] = "pass rate" if name == "unmutated" else "false-accept rate"
    return dict(sorted(out.items(), key=lambda kv: (kv[0] == "unmutated", kv[0])))

