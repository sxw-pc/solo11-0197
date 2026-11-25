"""BIN crosswalk intake between the old probe machine and the replacement.

The replacement machine split the same WAT pass bin (source code ``0`` passing
on the closed interval ``[90, 110]``) into two bins (target ``L`` on
``[90, 100]`` and ``H`` on ``[101, 110]``).  This module:

* reads two **sealed dictionaries** (source/old and target/new),
* proves, interval by interval, that every integer value in a range keeps the
  same grade (:data:`wafer_retest.dictionary.GRADES`) **and** the same grading
  basis (``BIN_RULE`` vs ``WAT_LIMIT`` -- the endpoints are closed exactly like
  :func:`wafer_retest.dictionary.classify`),
* decides whether one new code can be burned into a fixed crosswalk table for
  the whole declared range of an old code,
* converts an existing integer-valued ``map`` measurement by measurement while
  preserving the measured value, old code and source dictionary version,
* verifies (but never invents an integer for) slips that only carry a closed
  integer reading interval,
* emits pending-review reasons for unknown old codes and ranges with no
  equivalent new code.

The tool only *prepares* a deliverable map file for later intake; it never
modifies the source map, sealed dictionaries or release records.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:  # normal package import / `python -m wafer_retest.bin_crosswalk`
    from .dictionary import GRADES, classify
except ImportError:  # direct execution as a file path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from wafer_retest.dictionary import GRADES, classify


# Stable review reason tokens.  ``UNKNOWN_BIN`` mirrors dictionary.classify so
# ledger-facing reasoning stays consistent; the ``UNKNOWN_OLD_CODE`` alias
# states which side of the crosswalk is unknown.
UNKNOWN_OLD_CODE = "UNKNOWN_OLD_CODE"
UNKNOWN_BIN = "UNKNOWN_BIN"
NO_EQUIVALENT_NEW_CODE = "NO_EQUIVALENT_NEW_CODE"
NO_EQUIVALENT_BIN = "NO_EQUIVALENT_BIN"
INTERVAL_NOT_UNIFORM = "INTERVAL_NOT_UNIFORMLY_CONVERTIBLE"
RETEST = "RETEST"
INVALID_READING = "INVALID_READING"

CLOSED = (True, True)


# ---------------------------------------------------------------------------
# JSON / dictionary loading
# ---------------------------------------------------------------------------

def read_json(path):
    """Read UTF-8 JSON, tolerating a BOM like :func:`wafer_retest.cli.read_json`."""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _first_key(obj, names, default=None):
    for name in names:
        if isinstance(obj, dict) and name in obj and obj[name] is not None:
            return obj[name]
    return default


def _code_token(value):
    if isinstance(value, bool):
        raise ValueError("bin code must be text/integer, not boolean")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("expected nonempty bin code")


def _norm_pair(channel, code):
    """Best-effort normalization so integer ``0`` and text ``"0"`` are one key."""
    if isinstance(channel, str):
        channel = channel.strip()
    if isinstance(code, bool):
        pass
    elif isinstance(code, int):
        code = str(code)
    elif isinstance(code, str):
        code = code.strip()
    return channel, code


def _channel_token(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("expected nonempty channel")


def _strict_int(value):
    # Mirror wafer_retest.db.integer: bool is not an int here.
    if type(value) is not int:  # noqa: E721 - exact type check is intentional
        raise ValueError("expected a signed integer")
    return value


def normalize_rule(rule):
    if not isinstance(rule, dict):
        raise ValueError("bin rule must be an object")
    channel = _channel_token(_first_key(rule, ("channel", "ch")))
    code = _code_token(_first_key(rule, ("code", "bin", "bin_code", "old_code", "new_code")))
    grade = _first_key(rule, ("grade", "verdict", "tier"))
    if grade not in GRADES:
        raise ValueError(f"invalid bin grade: {grade!r}")
    lo = rule.get("lo", rule.get("low", rule.get("min")))
    hi = rule.get("hi", rule.get("high", rule.get("max")))
    if lo is None and hi is None:
        lo = hi = None
    else:
        if lo is None or hi is None:
            raise ValueError("WAT interval requires both lo and hi")
        lo, hi = _strict_int(lo), _strict_int(hi)
        if lo > hi:
            raise ValueError("invalid WAT interval: lo > hi")
    return {"channel": channel, "code": code, "grade": grade, "lo": lo, "hi": hi}


def coerce_dictionary(source):
    """Accept a dictionary document, a rule list or a JSON path."""
    if isinstance(source, (str, Path)):
        source = read_json(source)
    if isinstance(source, list):
        doc = {"dictionary": None, "state": None, "rules": source}
    elif isinstance(source, dict):
        doc = source
    else:
        raise ValueError("dictionary must be a path, object or rule list")
    raw_rules = _first_key(doc, ("rules", "bin_rules", "bins"), [])
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("dictionary requires bin rules")
    rules = [normalize_rule(row) for row in raw_rules]
    keys = [(r["channel"], r["code"]) for r in rules]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate channel/code rule in dictionary")
    dict_id = _first_key(doc, ("dictionary", "dictionary_id", "id", "name", "version", "dict"))
    state = _first_key(doc, ("state", "status"), "")
    if isinstance(doc.get("sealed"), bool):
        sealed = doc["sealed"]
    else:
        sealed = str(state).upper() == "SEALED"
    normalized = {
        "id": dict_id,
        "dictionary": dict_id,
        "state": str(state).upper() if state else ("SEALED" if sealed else None),
        "sealed": sealed,
        "rules": rules,
        "by_key": {(r["channel"], r["code"]): r for r in rules},
    }
    return normalized


def dictionary_from_db(db, dictionary):
    """Read a sealed dictionary out of the ledger by id."""
    row = db.execute("SELECT state FROM dictionaries WHERE id=?", (dictionary,)).fetchone()
    if row is None:
        raise ValueError(f"unknown dictionary: {dictionary}")
    rules = [dict(r) for r in db.execute(
        "SELECT channel,code,grade,lo,hi FROM bin_rules WHERE dictionary=? ORDER BY channel,code",
        (dictionary,))]
    doc = coerce_dictionary({"dictionary": dictionary, "state": row["state"], "rules": rules})
    return doc


def dict_ref(dictionary):
    return dictionary.get("id") or dictionary.get("dictionary")


# ---------------------------------------------------------------------------
# Closed integer intervals and exact classification equivalence
# ---------------------------------------------------------------------------

def classify_pair(dictionary, channel, code, value):
    """Return ``(grade, basis)`` using the real dictionary classifier."""
    channel, code = _norm_pair(channel, code)
    return classify(dictionary["by_key"], channel, code, value)


def classify_rule_at(rule, value):
    if rule is None:
        return None, "UNKNOWN_BIN"
    if rule["lo"] is not None and not rule["lo"] <= value <= rule["hi"]:
        return "FAIL", "WAT_LIMIT"
    return rule["grade"], "BIN_RULE"


def closed_interval(start, end_exclusive):
    """Half-open integer span ``[start, end)`` -> closed JSON interval."""
    return {"lo": start, "hi": None if end_exclusive is None else end_exclusive - 1,
            "closed": list(CLOSED)}


def interval_key(interval):
    return interval["lo"], interval["hi"]


def rule_edges(rule):
    """Half-open cut points where a rule's classification changes."""
    if rule is None or rule["lo"] is None:
        return set()
    return {rule["lo"], rule["hi"] + 1}


def representative(start, end_exclusive):
    """An integer guaranteed to lie in the span (spans are never empty)."""
    if start is not None:
        return start
    if end_exclusive is not None:
        return end_exclusive - 1
    return 0


def atomic_spans(source_rule, target_rules, lo=None, hi=None):
    """Partition the closed domain ``[lo, hi]`` (``None`` = unbounded) into
    maximal half-open spans on which every involved classifier is constant.
    """
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("invalid proof interval: lo > hi")
    domain_s = lo
    domain_e = None if hi is None else hi + 1
    edges = set()
    for rule in [source_rule, *target_rules]:
        for edge in rule_edges(rule):
            if (domain_s is None or domain_s < edge) and (domain_e is None or edge < domain_e):
                edges.add(edge)
    points = sorted(set(
        ([domain_s] if domain_s is not None else [])
        + list(edges)
        + ([domain_e] if domain_e is not None else [])
    ))
    spans = []
    if domain_s is None:
        if not points:
            return [(None, None)]
        spans.append((None, points[0]))
    for left, right in zip(points, points[1:]):
        if left < right:
            spans.append((left, right))
    if domain_e is None:
        if points:
            spans.append((points[-1], None))
        elif domain_s is None:
            spans.append((None, None))
    return spans


def candidate_codes(target, channel):
    return sorted(r["code"] for r in target["rules"] if r["channel"] == channel)


def nearest_distance(rule, value):
    if rule["lo"] is None:
        return 0
    if rule["lo"] <= value <= rule["hi"]:
        return 0
    if value < rule["lo"]:
        return rule["lo"] - value
    return value - rule["hi"]


def choose_for_channel(codes, target, channel, value):
    by_key = target["by_key"]
    return sorted(codes, key=lambda code: (nearest_distance(by_key[(channel, code)], value), code))[0]


def equivalent_codes_at(source, target, channel, old_code, value):
    grade, basis = classify_pair(source, channel, old_code, value)
    if grade is None:
        return [], (grade, basis)
    matches = []
    for code in candidate_codes(target, channel):
        if classify_pair(target, channel, code, value) == (grade, basis):
            matches.append(code)
    return matches, (grade, basis)


def proof_rows(source, target, channel, old_code, lo=None, hi=None):
    """Interval-by-interval proof of grade+basis equivalence.

    Returns maximal adjacent spans sharing the same chosen code and the same
    source/target grade and basis.  A row with ``equivalent=False`` is a raw
    interval for which no target code exists.
    """
    channel, old_code = _norm_pair(channel, old_code)
    source_rule = source["by_key"].get((channel, old_code))
    target_rules = [r for r in target["rules"] if r["channel"] == channel]
    rows = []
    for start, end in atomic_spans(source_rule, target_rules, lo, hi):
        value = representative(start, end)
        s_grade, s_basis = classify_rule_at(source_rule, value)
        candidates = []
        t_grade = t_basis = None
        for rule in target_rules:
            grade, basis = classify_rule_at(rule, value)
            if (grade, basis) == (s_grade, s_basis):
                candidates.append(rule["code"])
        candidates = sorted(candidates)
        chosen = choose_for_channel(candidates, target, channel, value) if candidates else None
        if chosen is not None:
            t_grade, t_basis = classify_pair(target, channel, chosen, value)
        interval = closed_interval(start, end)
        row = {
            "channel": channel,
            "old_code": old_code,
            **interval,
            "start_exclusive": start,
            "end_exclusive": end,
            "representative": value,
            "uniform": True,
            "source_grade": s_grade,
            "source_basis": s_basis,
            "grade": s_grade,
            "basis": s_basis,
            "candidate_new_codes": candidates,
            "new_code": chosen,
            "target_grade": t_grade,
            "target_basis": t_basis,
            "equivalent": chosen is not None,
            "closed_endpoints": CLOSED,
        }
        rows.append(row)
    return _merge_proof(rows)


def _merge_proof(rows):
    merged = []
    for row in rows:
        key = (row["source_grade"], row["source_basis"], row["new_code"],
               row["target_grade"], row["target_basis"])
        if merged:
            last = merged[-1]
            last_key = (last["source_grade"], last["source_basis"], last["new_code"],
                        last["target_grade"], last["target_basis"])
            adjacent = last["hi"] is None or row["lo"] is None or row["lo"] == last["hi"] + 1
            if adjacent and key == last_key:
                last["hi"] = row["hi"]
                last["end_exclusive"] = row["end_exclusive"]
                last["representative"] = row["representative"]
                continue
        merged.append(dict(row))
    return merged


def code_holds_over_interval(source, target, channel, old_code, new_code, lo, hi):
    """True iff one candidate matches source for *every* integer in [lo, hi]."""
    channel, old_code = _norm_pair(channel, old_code)
    _, new_code = _norm_pair(channel, new_code)
    if lo is None or hi is None:
        return code_holds_everywhere(source, target, channel, old_code, new_code)
    source_rule = source["by_key"].get((channel, old_code))
    candidate = target["by_key"].get((channel, new_code))
    if source_rule is None or candidate is None:
        return False
    for start, end in atomic_spans(source_rule, [candidate], lo, hi):
        value = representative(start, end)
        if classify_rule_at(candidate, value) != classify_rule_at(source_rule, value):
            return False
    return True


def code_holds_everywhere(source, target, channel, old_code, new_code):
    channel, old_code = _norm_pair(channel, old_code)
    _, new_code = _norm_pair(channel, new_code)
    source_rule = source["by_key"].get((channel, old_code))
    candidate = target["by_key"].get((channel, new_code))
    if source_rule is None or candidate is None:
        return False
    return _cell_signature(source_rule) == _cell_signature(candidate)


def _cell_signature(rule):
    return tuple((cell["lo"], cell["hi"], cell["grade"], cell["basis"])
                 for cell in rule_cells(rule))


def rule_cells(rule):
    """Maximal constant classification cells over the integer line."""
    if rule is None:
        return [{"lo": None, "hi": None, "grade": None, "basis": "UNKNOWN_BIN", "closed": list(CLOSED)}]
    if rule["lo"] is None:
        return [{"lo": None, "hi": None, "grade": rule["grade"], "basis": "BIN_RULE", "closed": list(CLOSED)}]
    lo, hi = rule["lo"], rule["hi"]
    return [
        {"lo": None, "hi": lo - 1, "grade": "FAIL", "basis": "WAT_LIMIT", "closed": list(CLOSED)},
        {"lo": lo, "hi": hi, "grade": rule["grade"], "basis": "BIN_RULE", "closed": list(CLOSED)},
        {"lo": hi + 1, "hi": None, "grade": "FAIL", "basis": "WAT_LIMIT", "closed": list(CLOSED)},
    ]


def fixed_table(source, target):
    """One burn-table row per source channel/code.

    ``fixed=True`` only when a single target code is equivalent over the whole
    declared range of the old code (its ``[lo, hi]`` WAT interval, or the
    entire integer line for code-only bins).
    """
    table = []
    for rule in sorted(source["rules"], key=lambda r: (r["channel"], r["code"])):
        channel, old_code = rule["channel"], rule["code"]
        declared_lo, declared_hi = rule["lo"], rule["hi"]
        whole = []
        for new_code in candidate_codes(target, channel):
            if code_holds_over_interval(source, target, channel, old_code, new_code,
                                        declared_lo, declared_hi):
                whole.append(new_code)
        chosen = choose_for_channel(whole, target, channel,
                                    declared_lo if declared_lo is not None else 0) if whole else None
        range_proof = proof_rows(source, target, channel, old_code, declared_lo, declared_hi)
        full_proof = proof_rows(source, target, channel, old_code)
        if chosen is not None:
            reason = "FIXED"
        elif any(row["equivalent"] for row in range_proof):
            reason = "SPLIT_REQUIRED"
        else:
            reason = NO_EQUIVALENT_NEW_CODE
        table.append({
            "channel": channel,
            "old_code": old_code,
            "source_dictionary": dict_ref(source),
            "new_code": chosen,
            "fixed": chosen is not None,
            "reason": reason,
            "declared_range": closed_interval(declared_lo,
                                             None if declared_hi is None else declared_hi + 1)
            if declared_lo is not None else {"lo": None, "hi": None, "closed": list(CLOSED)},
            "candidate_new_codes": sorted(whole),
            "proof": range_proof,
            "range_proof": range_proof,
            "full_range_proof": full_proof,
        })
    return table


def burn_table(source, target):
    """Only the rows that can actually be burned into one fixed code table."""
    return [{"channel": row["channel"], "old_code": row["old_code"], "new_code": row["new_code"]}
            for row in fixed_table(source, target) if row["fixed"]]


def unmatched_intervals(source, target):
    """Raw source intervals (whole integer line) with no equivalent new code."""
    gaps = []
    for rule in sorted(source["rules"], key=lambda r: (r["channel"], r["code"])):
        for row in proof_rows(source, target, rule["channel"], rule["code"]):
            if not row["equivalent"]:
                gaps.append({
                    "channel": row["channel"],
                    "old_code": row["old_code"],
                    "lo": row["lo"],
                    "hi": row["hi"],
                    "closed": list(CLOSED),
                    "grade": row["source_grade"],
                    "basis": row["source_basis"],
                    "source_grade": row["source_grade"],
                    "source_basis": row["source_basis"],
                    "reason": NO_EQUIVALENT_NEW_CODE,
                    "reason_tokens": [NO_EQUIVALENT_NEW_CODE, NO_EQUIVALENT_BIN],
                })
    return gaps


# ---------------------------------------------------------------------------
# Reading extraction (exact integer vs closed integer interval)
# ---------------------------------------------------------------------------

_RANGE_KEYS = ("value_range", "reading_range", "integer_range", "range", "interval",
               "bounds", "span", "values", "reading", "closed_interval")
_LOW_KEYS = ("lo", "low", "min", "minimum", "from_value", "start", "lower")
_HIGH_KEYS = ("hi", "high", "max", "maximum", "to_value", "end", "upper")


def extract_reading(raw):
    """Return ``("point", value)``, ``("interval", lo, hi)`` or raise ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("measurement must be an object")
    if "value" in raw and raw["value"] is not None:
        return "point", _strict_int(raw["value"])
    for key in _RANGE_KEYS:
        candidate = raw.get(key)
        if isinstance(candidate, (list, tuple)) and len(candidate) == 2:
            lo, hi = _strict_int(candidate[0]), _strict_int(candidate[1])
            if lo <= hi:
                return "interval", lo, hi
            raise ValueError("invalid closed integer interval: lo > hi")
    low = _first_key(raw, _LOW_KEYS)
    high = _first_key(raw, _HIGH_KEYS)
    if low is not None and high is not None:
        lo, hi = _strict_int(low), _strict_int(high)
        if lo <= hi:
            return "interval", lo, hi
        raise ValueError("invalid closed integer interval: lo > hi")
    raise ValueError("measurement needs an integer value or closed integer interval")


# ---------------------------------------------------------------------------
# Map conversion
# ---------------------------------------------------------------------------

def _provenance(source, target, channel, old_code, value, decision):
    return {
        "tool": "bin-crosswalk",
        "status": decision["status"],
        "source_dictionary": dict_ref(source),
        "source_dictionary_version": dict_ref(source),
        "target_dictionary": dict_ref(target),
        "channel": channel,
        "source_channel": channel,
        "old_code": old_code,
        "source_code": old_code,
        "source_value": value,
        "original_value": value,
        "measured_value": value,
        "value_preserved": True,
        "new_code": decision.get("new_code"),
        "source_grade": decision.get("source_grade"),
        "source_basis": decision.get("source_basis"),
        "target_grade": decision.get("target_grade"),
        "target_basis": decision.get("target_basis"),
        "equivalent": decision.get("equivalent", False),
        "reason": decision.get("reason"),
    }


def convert_point(source, target, channel, old_code, value):
    """Decide one exact integer reading."""
    channel, old_code = _norm_pair(channel, old_code)
    if (channel, old_code) not in source["by_key"]:
        return {
            "kind": "point", "status": "review", "pending": True, "needs_review": True,
            "channel": channel, "old_code": old_code, "value": value,
            "equivalent": False, "new_code": None,
            "reason": UNKNOWN_OLD_CODE,
            "reason_tokens": [UNKNOWN_OLD_CODE, UNKNOWN_BIN],
            "source_grade": None, "source_basis": UNKNOWN_BIN,
        }
    candidates, (grade, basis) = equivalent_codes_at(source, target, channel, old_code, value)
    if not candidates:
        return {
            "kind": "point", "status": "review", "pending": True, "needs_review": True,
            "channel": channel, "old_code": old_code, "value": value,
            "equivalent": False, "new_code": None, "candidate_new_codes": [],
            "reason": NO_EQUIVALENT_NEW_CODE,
            "reason_tokens": [NO_EQUIVALENT_NEW_CODE, NO_EQUIVALENT_BIN],
            "source_grade": grade, "source_basis": basis,
        }
    chosen = choose_for_channel(candidates, target, channel, value)
    t_grade, t_basis = classify_pair(target, channel, chosen, value)
    return {
        "kind": "point", "status": "converted", "pending": False, "converted": True,
        "channel": channel, "old_code": old_code, "value": value,
        "equivalent": True, "new_code": chosen, "candidate_new_codes": candidates,
        "source_grade": grade, "source_basis": basis,
        "target_grade": t_grade, "target_basis": t_basis,
        "evidence_preserved": basis == t_basis and grade == t_grade,
        "reason": "EQUIVALENT",
    }


def verify_interval(source, target, channel, old_code, lo, hi):
    """Verification conclusion for an interval-only slip.  Never invents a value."""
    interval = closed_interval(lo, hi)
    channel, old_code = _norm_pair(channel, old_code)
    base = {
        "kind": "interval", "channel": channel, "old_code": old_code,
        "interval": interval, "lo": lo, "hi": hi, "map_written": False,
    }
    if (channel, old_code) not in source["by_key"]:
        return {**base, "status": "review", "pending": True, "needs_review": True,
                "verified": False, "new_code": None,
                "reason": UNKNOWN_OLD_CODE,
                "reason_tokens": [UNKNOWN_OLD_CODE, UNKNOWN_BIN]}
    holding = [code for code in candidate_codes(target, channel)
               if code_holds_over_interval(source, target, channel, old_code, code, lo, hi)]
    proof = proof_rows(source, target, channel, old_code, lo, hi)
    has_equivalent_point = any(row["equivalent"] for row in proof)
    if any(not row["equivalent"] for row in proof) or not has_equivalent_point:
        # Part (or all) of the uncertainty interval has no equivalent new code
        # at all: this is a pending-review gap, not merely a split decision.
        return {**base, "status": "review", "pending": True, "needs_review": True,
                "verified": False, "new_code": None, "candidate_new_codes": [],
                "reason": NO_EQUIVALENT_NEW_CODE,
                "reason_tokens": [NO_EQUIVALENT_NEW_CODE, NO_EQUIVALENT_BIN], "proof": proof}
    if not holding:
        # Equivalent codes exist point by point, but no ONE code covers the
        # whole closed uncertainty interval -> the die must be retested.
        point_candidates = sorted({code for row in proof for code in row["candidate_new_codes"]})
        return {**base, "status": "retest", "pending": True, "needs_retest": True,
                "verified": False, "new_code": None,
                "candidate_new_codes": point_candidates,
                "common_new_codes": [],
                "reason": INTERVAL_NOT_UNIFORM,
                "reason_tokens": [INTERVAL_NOT_UNIFORM, RETEST], "proof": proof}
    chosen = choose_for_channel(holding, target, channel, lo)
    return {**base, "status": "verified", "pending": False, "verified": True,
            "new_code": chosen, "candidate_new_codes": sorted(holding),
            "common_new_codes": sorted(holding),
            "reason": "UNIFORMLY_EQUIVALENT", "proof": proof}


def _reading_identity(raw, index):
    return raw.get("id") or raw.get("measurement") or raw.get("measurement_id") or f"reading-{index}"


def convert_measurements(source, target, measurements, keep_pending=False):
    converted_rows, decisions = [], []
    for index, raw in enumerate(measurements):
        if not isinstance(raw, dict):
            decisions.append({"kind": "invalid", "status": "invalid", "pending": True,
                              "reason": INVALID_READING, "index": index})
            continue
        identity = _reading_identity(raw, index)
        channel = raw.get("channel")
        old_code = raw.get("code", raw.get("old_code", raw.get("source_code")))
        try:
            channel = _channel_token(channel)
            old_code = _code_token(old_code)
            kind, *bounds = extract_reading(raw)
        except ValueError as error:
            decisions.append({"id": identity, "status": "invalid", "pending": True,
                              "needs_review": True, "reason": INVALID_READING,
                              "message": str(error), "raw": raw})
            continue
        if kind == "interval":
            lo, hi = bounds
            decision = verify_interval(source, target, channel, old_code, lo, hi)
            decision["id"] = identity
            decisions.append(decision)
            continue
        value = bounds[0]
        decision = convert_point(source, target, channel, old_code, value)
        decision["id"] = identity
        decisions.append(decision)
        if decision["status"] == "converted":
            new_row = dict(raw)
            new_row["code"] = decision["new_code"]
            prov = _provenance(source, target, channel, old_code, value, decision)
            new_row["old_code"] = old_code
            new_row["source_code"] = old_code
            new_row["source_dictionary"] = dict_ref(source)
            new_row["source_dictionary_version"] = dict_ref(source)
            new_row["crosswalk"] = prov
            converted_rows.append(new_row)
        elif keep_pending:
            # Explicit opt-in: retain the raw row untouched and flagged.  The
            # target dictionary still classifies it (often UNKNOWN_BIN/HOLD);
            # default delivery instead omits it so it can never default-pass.
            flagged = dict(raw)
            flagged["crosswalk"] = _provenance(source, target, channel, old_code, value, decision)
            flagged.setdefault("crosswalk_status", decision["status"])
            converted_rows.append(flagged)
    return converted_rows, decisions


def convert_map_args(source, target, args, keep_pending=False):
    if not isinstance(args, dict) or not isinstance(args.get("measurements"), list):
        raise ValueError("map args require a measurements list")
    converted, decisions = convert_measurements(source, target, args["measurements"], keep_pending)
    new_args = dict(args)
    new_args["measurements"] = converted
    return new_args, decisions


def convert_document(source, target, document, keep_pending=False):
    """Convert a map-args object, a single op envelope or a ``load`` batch."""
    all_decisions, map_args_list = [], []

    def handle_args(args):
        new_args, decisions = convert_map_args(source, target, args, keep_pending)
        all_decisions.extend(decisions)
        map_args_list.append(new_args)
        return new_args

    if isinstance(document, dict) and isinstance(document.get("measurements"), list):
        return handle_args(document), all_decisions, map_args_list
    if isinstance(document, dict) and document.get("command") == "map" and isinstance(document.get("args"), dict):
        out = dict(document)
        out["args"] = handle_args(document["args"])
        return out, all_decisions, map_args_list
    if isinstance(document, list):
        out = []
        for op in document:
            if isinstance(op, dict) and op.get("command") == "map" and isinstance(op.get("args"), dict):
                new_args = handle_args(op["args"])
                new_op = dict(op)
                new_op["args"] = new_args
                out.append(new_op)
            else:
                out.append(op)
        return out, all_decisions, map_args_list
    raise ValueError("old map must be map args, a map op, or a load batch list")


def load_batch(map_args_list):
    return [{"command": "map", "args": args} for args in map_args_list]


# ---------------------------------------------------------------------------
# Top level crosswalk report
# ---------------------------------------------------------------------------

def crosswalk(source_dictionary, target_dictionary, old_map=None, interval_readings=None,
              keep_pending=False, request=None):
    source = coerce_dictionary(source_dictionary)
    target = coerce_dictionary(target_dictionary)
    table = fixed_table(source, target)
    gaps = unmatched_intervals(source, target)
    report = {
        "tool": "bin-crosswalk",
        "schema": "bin-crosswalk-report/v1",
        "source_dictionary": {"id": dict_ref(source), "state": source["state"],
                               "sealed": source["sealed"], "rules": len(source["rules"])},
        "target_dictionary": {"id": dict_ref(target), "state": target["state"],
                              "sealed": target["sealed"], "rules": len(target["rules"])},
        "dictionaries_sealed": bool(source["sealed"] and target["sealed"]),
        "fixed_table": table,
        "fixed": table,
        "burn_table": burn_table(source, target),
        "crosswalk_table": table,
        "unmatched_intervals": gaps,
        "gaps": gaps,
        "no_equivalent_intervals": gaps,
    }
    decisions = []
    map_args_list = []
    output_document = None
    if old_map is not None:
        output_document, decisions, map_args_list = convert_document(
            source, target, old_map, keep_pending=keep_pending)
        report["output_document"] = output_document
        report["converted_map"] = map_args_list[0] if len(map_args_list) == 1 else None
        report["converted_maps"] = map_args_list
        report["converted_batch"] = load_batch(map_args_list)
    extra_intervals = interval_readings
    if extra_intervals is None and request and isinstance(request, dict):
        extra_intervals = request.get("interval_readings")
    for raw in extra_intervals or []:
        channel = _channel_token(raw.get("channel"))
        old_code = _code_token(raw.get("old_code", raw.get("code", raw.get("source_code"))))
        kind, *bounds = extract_reading(raw)
        if kind != "interval":
            raise ValueError("interval_readings entries must carry a closed integer interval")
        decision = verify_interval(source, target, channel, old_code, bounds[0], bounds[1])
        decision["id"] = _reading_identity(raw, len(decisions))
        decisions.append(decision)
    labels = {"converted": "已按实测值换码", "verified": "区间核验通过（不写入整数）",
              "retest": "待复测", "review": "待核", "invalid": "待核（读数不合法）"}
    for decision in decisions:
        decision.setdefault("verdict_zh", labels.get(decision["status"], "待核"))
        if decision.get("pending"):
            # 待复测 is one form of 待核: it must never be auto-released.
            decision["pending_verification"] = True
            decision["needs_review"] = True
    converted = [d for d in decisions if d["status"] == "converted"]
    verified = [d for d in decisions if d["status"] == "verified"]
    review = [d for d in decisions if d["status"] in ("review", "invalid")]
    retest = [d for d in decisions if d["status"] == "retest"]
    report.update({
        "decisions": decisions,
        "converted_count": len(converted),
        "pending_review": review,
        "retest": retest,
        "interval_verifications": [d for d in decisions if d.get("kind") == "interval"],
        "pending": review + retest,
        # Broad 待核 bucket: unknown/no-equivalent review plus 待复测 spans.
        "pending_verification": review + retest,
        "held": review + retest,
        "summary": {
            "decisions": len(decisions),
            "converted": len(converted),
            "interval_verified": len(verified),
            "pending_review": len(review),
            "retest": len(retest),
            "pending_total": len(review) + len(retest),
            "unmatched_intervals": len(gaps),
            "fixed_bins": sum(1 for row in table if row["fixed"]),
            "unfixed_bins": sum(1 for row in table if not row["fixed"]),
        },
    })
    return report


# Concise public aliases.
build_crosswalk = crosswalk
run_crosswalk = crosswalk
bin_crosswalk = crosswalk
analyze = crosswalk
run = crosswalk
build_fixed_table = fixed_table


def convert_map(source, target, old_map, **options):
    """Convert a map document and return the full crosswalk report."""
    return crosswalk(source, target, old_map=old_map, **options)


# ---------------------------------------------------------------------------
# Request files and output writers / CLIs
# ---------------------------------------------------------------------------

_REQUEST_SOURCE_KEYS = ("source_dictionary", "source", "old_dictionary", "from_dictionary", "src")
_REQUEST_TARGET_KEYS = ("target_dictionary", "target", "new_dictionary", "to_dictionary", "dst")
_REQUEST_MAP_KEYS = ("old_map", "source_map", "input_map", "map")


def _resolve_dictionary(value, request_path):
    if isinstance(value, str):
        path = Path(value)
        if not path.is_absolute() and request_path is not None:
            path = request_path.parent / path
        return read_json(path)
    return value


def report_from_request(request_path, *, source_path=None, target_path=None, map_path=None,
                         source_id=None, target_id=None, db=None, keep_pending=False):
    request_path = Path(request_path) if request_path else None
    request = read_json(request_path) if request_path else {}
    if not isinstance(request, dict):
        raise ValueError("crosswalk request must be a JSON object")
    source_doc = None
    if source_id is not None and db is not None:
        source_doc = dictionary_from_db(db, source_id)
    elif source_path:
        source_doc = read_json(source_path)
    else:
        source_doc = _first_key(request, _REQUEST_SOURCE_KEYS)
        source_doc = _resolve_dictionary(source_doc, request_path)
    target_doc = None
    if target_id is not None and db is not None:
        target_doc = dictionary_from_db(db, target_id)
    elif target_path:
        target_doc = read_json(target_path)
    else:
        target_doc = _first_key(request, _REQUEST_TARGET_KEYS)
        target_doc = _resolve_dictionary(target_doc, request_path)
    if source_doc is None or target_doc is None:
        raise ValueError("crosswalk needs source and target dictionaries")
    old_map = None
    if map_path:
        old_map = read_json(map_path)
    else:
        old_map = _first_key(request, _REQUEST_MAP_KEYS)
        if isinstance(old_map, str):
            path = Path(old_map)
            if not path.is_absolute() and request_path is not None:
                path = request_path.parent / path
            old_map = read_json(path)
        elif old_map is None and request_path is not None:
            map_file = request.get("old_map_file") or request.get("map_file")
            if map_file:
                path = Path(map_file)
                if not path.is_absolute():
                    path = request_path.parent / path
                old_map = read_json(path)
    interval_readings = request.get("interval_readings")
    return crosswalk(source_doc, target_doc, old_map=old_map,
                     interval_readings=interval_readings, keep_pending=keep_pending,
                     request=request)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                          encoding="utf-8")


def write_outputs(report, output=None, report_path=None, batch_output=None, map_output=None,
                  fixed_output=None, gaps_output=None):
    written = {}
    if output:
        write_json(output, report["output_document"])
        written["output"] = str(output)
    if map_output:
        maps = report.get("converted_maps") or []
        payload = maps[0] if len(maps) == 1 else maps
        write_json(map_output, payload)
        written["map_output"] = str(map_output)
    if batch_output:
        write_json(batch_output, report["converted_batch"])
        written["batch_output"] = str(batch_output)
    if fixed_output:
        write_json(fixed_output, {
            "source_dictionary": report["source_dictionary"],
            "target_dictionary": report["target_dictionary"],
            "burn_table": report["burn_table"],
            "fixed_table": report["fixed_table"],
        })
        written["fixed_output"] = str(fixed_output)
    if gaps_output:
        write_json(gaps_output, report["unmatched_intervals"])
        written["gaps_output"] = str(gaps_output)
    if report_path:
        write_json(report_path, report)
        written["report"] = str(report_path)
    return written


def configure_arguments(operation):
    operation.add_argument(
        "paths", nargs="*",
        help="request.json [output.json]  |  source.json target.json [old-map.json [output.json]]")
    operation.add_argument("-s", "--source-dictionary", "--source", "--source-dict",
                           dest="source_path")
    operation.add_argument("-t", "--target-dictionary", "--target", "--target-dict",
                           dest="target_path")
    operation.add_argument("-m", "--old-map", "--map", "--map-file", dest="map_path")
    operation.add_argument("-o", "--output", "--out", "--output-file", dest="output")
    operation.add_argument("-r", "--report", "--report-output", "--report-file", dest="report_path")
    operation.add_argument("--batch-output", "--load-output", dest="batch_output")
    operation.add_argument("--map-output", dest="map_output")
    operation.add_argument("--fixed-table-output", "--fixed-output", dest="fixed_output")
    operation.add_argument("--gaps-output", "--unmatched-output", dest="gaps_output")
    operation.add_argument("--source-id", dest="source_id")
    operation.add_argument("--target-id", dest="target_id")
    operation.add_argument("--keep-pending", action="store_true")
    operation.add_argument("--strict", action="store_true",
                           help="exit 2 when any reading is pending review/retest")


def make_parser():
    import argparse
    parser = argparse.ArgumentParser(description="Convert old probe maps to the new BIN dictionary")
    configure_arguments(parser)
    return parser


def _is_request_document(path):
    try:
        doc = read_json(path)
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and any(k in doc for k in _REQUEST_SOURCE_KEYS) \
        and any(k in doc for k in _REQUEST_TARGET_KEYS)


def _is_map_document(path):
    try:
        doc = read_json(path)
    except (OSError, ValueError):
        return False
    if isinstance(doc, dict) and isinstance(doc.get("measurements"), list):
        return True
    return isinstance(doc, list) and any(
        isinstance(op, dict) and op.get("command") == "map" for op in doc)


def infer_call(args):
    """Resolve the flexible positional/flag invocation to concrete paths."""
    paths = list(getattr(args, "paths", []) or [])
    if not paths and getattr(args, "request", None):
        paths = [args.request]
        if getattr(args, "output_positional", None):
            paths.append(args.output_positional)
    source_path = getattr(args, "source_path", None)
    target_path = getattr(args, "target_path", None)
    map_path = getattr(args, "map_path", None)
    output = getattr(args, "output", None)
    request = None

    if source_path and target_path:
        if len(paths) >= 2 and not map_path:
            map_path, output = paths[0], output or paths[1]
        elif len(paths) == 1:
            if map_path or not _is_map_document(paths[0]):
                output = output or paths[0]
            else:
                map_path = paths[0]
        return request, source_path, target_path, map_path, output

    if len(paths) >= 4:
        source_path, target_path = paths[0], paths[1]
        map_path = map_path or paths[2]
        output = output or paths[3]
    elif len(paths) == 3:
        source_path, target_path = paths[0], paths[1]
        map_path = map_path or paths[2]
    elif len(paths) == 2:
        if _is_request_document(paths[0]):
            request, output = paths[0], output or paths[1]
        else:
            source_path, target_path = paths[0], paths[1]
    elif len(paths) == 1:
        only = paths[0]
        if _is_request_document(only) or not (source_path and target_path):
            request = only
        else:
            output = output or only
    return request, source_path, target_path, map_path, output


def dispatch_namespace(args, db=None):
    request, source_path, target_path, map_path, output = infer_call(args)
    report = report_from_request(
        request,
        source_path=source_path,
        target_path=target_path,
        map_path=map_path,
        source_id=getattr(args, "source_id", None),
        target_id=getattr(args, "target_id", None),
        db=db,
        keep_pending=getattr(args, "keep_pending", False),
    )
    written = write_outputs(
        report, output=output, report_path=getattr(args, "report_path", None),
        batch_output=getattr(args, "batch_output", None),
        map_output=getattr(args, "map_output", None),
        fixed_output=getattr(args, "fixed_output", None),
        gaps_output=getattr(args, "gaps_output", None))
    report["written"] = written
    return report


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        report = dispatch_namespace(args)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        if args.strict and report["pending"]:
            return 2
        return 0
    except (ValueError, OSError, TypeError, KeyError) as error:
        print(f"bin-crosswalk: {error}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
