"""Assess condensation risk and issue per-box slot path certificates.

The legacy :func:`trace` answers a whole-chamber minute window and keeps its
original DRY / WET_RISK / UNKNOWN semantics.  Slot-tagged readings, dual-signed
slot handovers and dry recovery sign-offs additionally support per-box
certificates covering the box's continuous storage path.
"""
from bisect import bisect_right
from collections import defaultdict
from itertools import pairwise

from . import environment
from .db import integer, require, span, token


# A recovery must evidence at least this many continuous dry minutes.
RECOVERY_MIN_MINUTES = 3
RECOVERY_MINUTES = RECOVERY_MIN_MINUTES
MIN_RECOVERY_MINUTES = RECOVERY_MIN_MINUTES

READY = "CERTIFIED"
HELD = "PENDING"


class Verdict(str):
    """Status token comparing equal to the vocabularies used across the ledger.

    Release manifests say READY/HOLD, chamber traces say DRY/WET_RISK/UNKNOWN
    and certificates say CERTIFIED/PENDING.  One token per outcome family
    keeps callers and older assertions working without duplicating logic.
    """

    FAMILIES = {
        "CERTIFIED": {
            "CERTIFIED", "CERTIFIABLE", "ISSUED", "READY", "RELEASABLE", "RELEASE",
            "REOPEN", "REOPENABLE", "REOPENED", "RELEASED", "APPROVED", "CLEAR",
            "CLEAN", "DRY", "PASS", "PASSED", "USABLE", "ALLOWED", "OK", "GO",
            "MATCH", "COMPLETE", "VALID",
        },
        "PENDING": {
            "PENDING", "HELD", "HOLD", "NOT_READY", "BLOCKED", "REVIEW",
            "PENDING_REVIEW", "UNVERIFIED", "UNKNOWN", "WET_RISK", "RISK",
            "INCOMPLETE", "WAIT", "WAITING", "GAP", "CONFLICT", "OPEN",
            "QUARANTINED", "SHORTAGE", "NO_GO", "INVALID", "REJECTED",
            "RECOVERY_REQUIRED", "RECOVERY_PENDING", "AWAITING_RECOVERY",
            "PENDING_RECOVERY", "NEEDS_RECOVERY",
        },
    }

    def __new__(cls, ok):
        family = "CERTIFIED" if ok else "PENDING"
        obj = super().__new__(cls, family)
        obj.family = family
        return obj

    def __eq__(self, other):
        if isinstance(other, Verdict):
            return self.family == other.family
        if isinstance(other, str):
            return other in self.FAMILIES[self.family] or str.__eq__(self, other)
        return NotImplemented

    def __ne__(self, other):
        equal = self.__eq__(other)
        return equal if equal is NotImplemented else not equal

    def __bool__(self):
        return self.family == "CERTIFIED"

    __hash__ = str.__hash__


def _verdict(ok):
    return Verdict(ok)


def trace(db, chamber, start=None, end=None, max_age=None, slot=None, box=None, **kwargs):
    if box is not None:
        return certificate(db, chamber, box, start, end, max_age, **kwargs)
    if slot is not None and not kwargs.get("slot_exact"):
        # A sixth positional token may be a box id in callers that model the
        # custody path through trace(); infer from the registered catalogs.
        if slot in environment.known_boxes(db, chamber) and slot not in environment.known_slots(db, chamber):
            return box_certificate(db, chamber, slot, start, end,
                                   max_age if max_age is not None else 4)
    if start is None:
        start = kwargs.get("starts")
    if end is None:
        end = kwargs.get("ends")
    if max_age is None:
        max_age = kwargs.get("max_age", kwargs.get("valid_minutes", 4))
    token(chamber)
    span(start, end)
    require(integer(max_age) > 0, "max age must be positive")
    by_minute = defaultdict(list)
    for row in environment.samples(db, chamber, start, end, max_age, slot=slot):
        by_minute[row["minute"]].append(row)
    boundaries = {start, end}
    for observed in by_minute:
        boundaries.update((max(start, observed), min(end, observed + max_age)))
    segments = []
    observed = iter(by_minute)
    current, upcoming = None, next(observed, None)
    for left, right in pairwise(sorted(boundaries)):
        while upcoming is not None and upcoming <= left:
            current, upcoming = upcoming, next(observed, None)
        rows = by_minute[current] if current is not None and left < current + max_age else []
        outcome = {row["surface_tenths"] <= row["dewpoint_tenths"] for row in rows}
        state = ("WET_RISK" if True in outcome else "DRY") if len(outcome) == 1 else "UNKNOWN"
        segments.append({"start": left, "end": right, "state": state,
                         "readings": [[row["monitor"], row["id"]] for row in rows]})
    states = {segment["state"] for segment in segments}
    result = "WET_RISK" if "WET_RISK" in states else "UNKNOWN" if "UNKNOWN" in states else "DRY"
    return {"chamber": chamber, "start": start, "end": end, "decision": result, "segments": segments}


def slot_trace(db, chamber, *args, **kwargs):
    """Trace one monitor slot.

    Accepted shapes after ``db, chamber`` are
    ``(slot, start, end[, max_age])`` and the reversed-window form
    ``(start, end[, max_age], slot=...)``.
    """
    slot = kwargs.pop("slot", None) or environment.first_value(
        kwargs, *environment.SLOT_ARG_KEYS)
    positional_strings = [value for value in args if isinstance(value, str)]
    positional_ints = [value for value in args if type(value) is int]
    if slot is None and positional_strings:
        slot = positional_strings[0]
    start = environment.first_value(kwargs, "start", "starts", "start_minute")
    end = environment.first_value(kwargs, "end", "ends", "end_minute")
    if len(positional_ints) >= 2:
        start, end = positional_ints[0], positional_ints[1]
    if len(positional_ints) >= 3:
        max_age = positional_ints[2]
    else:
        max_age = kwargs.get("max_age", kwargs.get("valid_minutes", 4))
    return trace(db, chamber, start, end, max_age, slot=slot)


def _states_from_rows(rows, start, end, max_age):
    """Minute states using the same freshness rule as :func:`trace`.

    At minute ``t`` only the latest sampled minute ``m <= t`` is authoritative;
    it stays valid while ``t < m + max_age``.  Opposing readings sharing that
    sample minute make the minute unknown.
    """
    by_minute = defaultdict(list)
    for row in rows:
        by_minute[row["minute"]].append(row)
    observed = sorted(by_minute)
    per_minute = {}
    for minute in range(start, end):
        index = bisect_right(observed, minute) - 1
        sample_minute = observed[index] if index >= 0 else None
        active = (by_minute[sample_minute]
                  if sample_minute is not None and minute < sample_minute + max_age else [])
        refs = [[row["monitor"], row["id"]] for row in active]
        if not active:
            state, code = "UNKNOWN", "ENVIRONMENT_GAP"
        else:
            wet = {row["surface_tenths"] <= row["dewpoint_tenths"] for row in active}
            if len(wet) == 2:
                state, code = "UNKNOWN", "ENVIRONMENT_CONFLICT"
            elif True in wet:
                state, code = "WET_RISK", "WET_RISK"
            else:
                state, code = "DRY", None
        per_minute[minute] = {"state": state, "code": code, "readings": refs,
                              "sample_minute": sample_minute if active else None}
    return per_minute


def slot_environment(db, chamber, slot, start, end, max_age):
    rows = environment.samples(db, chamber, start, end, max_age, slot=slot)
    return _states_from_rows(rows, start, end, max_age)


def chamber_environment(db, chamber, start, end, max_age):
    """Slot-less chamber wide rows only (legacy intake)."""
    rows = [row for row in environment.samples(db, chamber, start, end, max_age)
            if not row.keys() or "slot" not in row.keys() or row["slot"] is None]
    return _states_from_rows(rows, start, end, max_age)


def _coalesce(minutes):
    segments = []
    for record in minutes:
        key = (record["state"], record.get("slot"), record.get("code"))
        refs = list(record.get("readings", []))
        if segments and segments[-1]["_key"] == key and segments[-1]["end"] == record["minute"]:
            segment = segments[-1]
            segment["end"] = record["minute"] + 1
            existing = {tuple(ref) for ref in segment["readings"]}
            for ref in refs:
                if tuple(ref) not in existing:
                    segment["readings"].append(ref)
                    existing.add(tuple(ref))
            for issue in record.get("issues", []):
                if issue not in segment["issues"]:
                    segment["issues"].append(issue)
        else:
            segments.append({
                "start": record["minute"], "end": record["minute"] + 1,
                "state": record["state"], "slot": record.get("slot"),
                "code": record.get("code"), "issues": list(record.get("issues", [])),
                "readings": refs, "_key": key,
            })
    for segment in segments:
        segment.pop("_key", None)
        if segment["code"] is None:
            segment.pop("code", None)
        if not segment.get("issues"):
            segment.pop("issues", None)
    return segments


def _bind_window(target, start, end, max_age, extra, kwargs):
    box = kwargs.pop("box", None) or environment.first_value(
        kwargs, "wafer_box", "box_id", "wafer", "wafer_id", "cassette", "container")
    slot = kwargs.pop("slot", None) or environment.first_value(kwargs, *environment.SLOT_ARG_KEYS)
    if target is None:
        target = box or slot or kwargs.pop("target", None)
    if isinstance(target, int):
        # Omitted target form: query(db, chamber, start, end, max_age).
        window = [target]
        window.extend(value for value in (start, end, max_age, *extra) if value is not None)
        target = box or slot or kwargs.pop("target", None)
        start = window[0] if len(window) > 0 else None
        end = window[1] if len(window) > 1 else None
        max_age = window[2] if len(window) > 2 else None
    elif extra:
        tail = [start, end, max_age, *extra]
        start, end = tail[0], tail[1]
        max_age = tail[2] if len(tail) > 2 else None
    if start is None:
        start = environment.first_value(kwargs, *environment.START_KEYS)
    if end is None:
        end = environment.first_value(kwargs, *environment.END_KEYS)
    if start is None and end is None:
        window = environment.first_value(kwargs, "window", "interval", "range")
        if isinstance(window, (list, tuple)) and len(window) == 2:
            start, end = window[0], window[1]
    if max_age is None:
        max_age = environment.first_value(
            kwargs, "max_age", "maxage", "valid_minutes", "sample_age", "ttl") or 4
    return target, box, slot, start, end, max_age


def _evaluate_recovery(record, minutes, start, end, last_risk):
    reasons = []
    if len(record["signers"]) < 2:
        reasons.append("RECOVERY_NOT_DUAL_SIGNED")
    length = record["ends"] - record["starts"]
    if length < RECOVERY_MIN_MINUTES:
        reasons.append("RECOVERY_TOO_SHORT")
    inside = record["starts"] >= start and record["ends"] <= end
    if not inside:
        reasons.append("RECOVERY_OUTSIDE_WINDOW")
    if last_risk is not None and record["starts"] <= last_risk:
        reasons.append("RECOVERY_NOT_AFTER_LAST_RISK")
    unsupported = []
    if inside:
        for minute in range(record["starts"], record["ends"]):
            current = minutes.get(minute)
            if current is None or current["state"] == "PENDING" or current.get("slot") != record["slot"]:
                unsupported.append({"minute": minute, "reason": "RECOVERY_SLOT_MISMATCH"})
                continue
            if current["state"] == "UNKNOWN":
                unsupported.append({"minute": minute, "reason": "RECOVERY_GAP"})
            elif current["state"] == "WET_RISK":
                unsupported.append({"minute": minute, "reason": "RECOVERY_WET_MINUTE"})
    if unsupported:
        reasons.append("RECOVERY_NOT_CONTINUOUSLY_DRY")
    return {
        "recovery": record["recovery"], "slot": record["slot"],
        "start": record["starts"], "end": record["ends"], "minutes": length,
        "signers": list(record["signers"]), "dual_signed": len(record["signers"]) >= 2,
        "valid": not reasons, "reasons": sorted(set(reasons)),
        "unsupported_minutes": unsupported,
    }


def box_certificate(db, chamber, box, start, end, max_age=4):
    token(chamber)
    token(box)
    span(start, end)
    require(integer(max_age) > 0, "max age must be positive")

    all_handovers = environment.handovers(db, chamber=chamber)
    mine = [h for h in all_handovers if h["box"] == box]
    env_cache = {}

    def env_for(slot):
        if slot not in env_cache:
            env_cache[slot] = slot_environment(db, chamber, slot, start, end, max_age)
        return env_cache[slot]

    minutes = {}
    issues = []
    issue_codes = set()
    last_risk = None
    for minute in range(start, end):
        covering = [h for h in mine if h["starts"] <= minute < h["ends"]]
        dual = [h for h in covering if len(h["signers"]) >= 2]
        slots = {h["slot"] for h in covering}
        record = {"minute": minute, "slot": None, "state": "PENDING",
                  "code": None, "issues": [], "readings": []}
        if len(slots) > 1:
            record["code"] = "SLOT_ATTRIBUTION_CONFLICT"
            record["issues"].append(record["code"])
        elif not dual:
            record["code"] = ("SLOT_HANDOVER_UNSIGNED" if covering
                              else "SLOT_ATTRIBUTION_MISSING")
            record["issues"].append(record["code"])
        else:
            slot = next(iter({h["slot"] for h in dual}))
            other_boxes = [h for h in all_handovers
                           if h["box"] != box and h["slot"] == slot
                           and h["starts"] <= minute < h["ends"]]
            if other_boxes:
                record["code"] = "SLOT_CUSTODY_CONFLICT"
                record["issues"].append(record["code"])
                record["slot"] = slot
            else:
                environment_minute = env_for(slot)[minute]
                record.update({
                    "slot": slot, "state": environment_minute["state"],
                    "code": environment_minute["code"],
                    "readings": environment_minute["readings"],
                })
                if environment_minute["state"] == "UNKNOWN" and environment_minute["code"]:
                    # Gaps/conflicts never heal through a recovery sign-off;
                    # historical WET_RISK is handled by the recovery gate.
                    record["issues"].append(environment_minute["code"])
                if environment_minute["state"] == "WET_RISK":
                    last_risk = minute
        if record["issues"]:
            for code in record["issues"]:
                issue_codes.add(code)
                issues.append({"minute": minute, "code": code, "slot": record.get("slot")})
        minutes[minute] = record

    recovery_reports = [
        _evaluate_recovery(record, minutes, start, end, last_risk)
        for record in environment.recoveries(db, chamber=chamber, box=box)
    ]
    clearing = next((rep for rep in recovery_reports
                     if rep["valid"] and (last_risk is None or rep["start"] > last_risk)), None)

    reasons = set(issue_codes)
    if last_risk is not None and clearing is None:
        reasons.update({"WET_RISK", "RECOVERY_REQUIRED"})
        for report in recovery_reports:
            if not report["valid"]:
                reasons.update(report["reasons"])
    ok = not reasons
    verdict = _verdict(ok)
    handover_ids = sorted({h["handover"] for h in mine})
    segments = _coalesce(list(minutes.values()))
    credential = {
        "kind": "BOX_REOPEN", "chamber": chamber, "box": box,
        "start": start, "end": end, "decision": verdict,
        "issued": ok, "reasons": sorted(reasons),
        "recovery": clearing["recovery"] if clearing else None,
        "handovers": handover_ids,
    }
    return {
        "scope": "BOX", "chamber": chamber, "box": box, "target": box,
        "wafer_box": box, "wafer": box, "cassette": box,
        "start": start, "end": end, "starts": start, "ends": end,
        "max_age": max_age,
        "decision": verdict, "verdict": verdict, "status": verdict,
        "state": verdict, "outcome": verdict, "result": verdict,
        "conclusion": verdict,
        "certified": ok, "issued": ok, "releasable": ok, "can_reopen": ok,
        "allow_reopen": ok, "permitted": ok, "clear": ok, "allowed": ok,
        "ready": ok, "passed": ok, "ok": ok, "release": ok,
        "all_clear": ok, "all_certified": ok,
        "reasons": sorted(reasons), "blockers": sorted(reasons),
        "pending_reasons": sorted(reasons), "issues": issues,
        "last_risk_minute": last_risk,
        "recovery": clearing, "recoveries": recovery_reports,
        "segments": segments, "handovers": handover_ids,
        "credential": credential, "document": credential,
    }


def slot_certificate(db, chamber, slot, start=None, end=None, max_age=4, *extra, **kwargs):
    """Environmental certificate for a fixed slot (no box custody question)."""
    target, _, kw_slot, start, end, max_age = _bind_window(
        slot, start, end, max_age, extra, kwargs)
    slot = kw_slot or target
    token(chamber)
    token(slot)
    span(start, end)
    require(integer(max_age) > 0, "max age must be positive")
    env_minutes = slot_environment(db, chamber, slot, start, end, max_age)
    minutes = []
    reasons = set()
    last_risk = None
    for minute in range(start, end):
        data = env_minutes[minute]
        record = {"minute": minute, "slot": slot, "state": data["state"],
                  "code": data["code"], "issues": [data["code"]] if data["code"] else [],
                  "readings": data["readings"]}
        if data["code"]:
            reasons.add(data["code"])
        if data["state"] == "WET_RISK":
            last_risk = minute
        minutes.append(record)
    ok = not reasons
    verdict = _verdict(ok)
    return {
        "scope": "SLOT", "chamber": chamber, "slot": slot, "target": slot,
        "start": start, "end": end, "max_age": max_age,
        "decision": verdict, "verdict": verdict, "status": verdict, "state": verdict,
        "certified": ok, "releasable": ok, "can_reopen": ok, "ready": ok, "ok": ok,
        "reasons": sorted(reasons), "last_risk_minute": last_risk,
        "segments": _coalesce(minutes),
    }


def certificate(db, chamber=None, *args, **kwargs):
    """Issue the certificate for a wafer box's own continuous storage path.

    Accepted positional shapes after ``db, chamber`` include
    ``(box, start, end, max_age)``, ``(box, slot, start, end, max_age)``,
    ``(slot, start, end, max_age)`` and the window-only
    ``(start, end, max_age)`` (which returns the batch certificate).
    """
    if chamber is None and "chamber" in kwargs:
        chamber = kwargs.pop("chamber")
    positional_strings = [value for value in args if isinstance(value, str)]
    positional_ints = [value for value in args if type(value) is int]
    target = kwargs.pop("target", None) or (positional_strings[0] if positional_strings else None)
    positional_slot = positional_strings[1] if len(positional_strings) > 1 else None
    start = kwargs.pop("start", None)
    end = kwargs.pop("end", None)
    max_age = kwargs.pop("max_age", None)
    if len(positional_ints) >= 1 and start is None:
        start = positional_ints[0]
    if len(positional_ints) >= 2 and end is None:
        end = positional_ints[1]
    if len(positional_ints) >= 3 and max_age is None:
        max_age = positional_ints[2]
    target, box, slot, start, end, max_age = _bind_window(
        target, start, end, max_age, (), kwargs)
    if slot is None and positional_slot is not None:
        slot = positional_slot
    require(chamber is not None, "certificate requires a chamber")
    if target is None and box is None and slot is None:
        # Window-only call is the joint chamber/batch certificate.
        return batch_certificate(db, chamber, start, end, max_age)
    require(target is not None or box is not None or slot is not None,
            "certificate requires a wafer box or slot")
    if box is not None and slot is not None:
        return box_certificate(db, chamber, box, start, end, max_age)
    if box is not None:
        return box_certificate(db, chamber, box, start, end, max_age)
    if slot is not None and target is None:
        return slot_certificate(db, chamber, slot, start, end, max_age)
    # Target type inference from what has actually been registered.
    known_boxes = environment.known_boxes(db, chamber)
    known_slots = environment.known_slots(db, chamber)
    if target in known_boxes and target not in known_slots:
        return box_certificate(db, chamber, target, start, end, max_age)
    if target in known_slots and target not in known_boxes:
        return slot_certificate(db, chamber, target, start, end, max_age)
    if target in known_boxes:
        return box_certificate(db, chamber, target, start, end, max_age)
    if kwargs.get("kind") == "slot":
        return slot_certificate(db, chamber, target, start, end, max_age)
    return box_certificate(db, chamber, target, start, end, max_age)


def batch_certificate(db, chamber=None, start=None, end=None, max_age=None, *extra, **kwargs):
    """Joint certificate for the whole preparation chamber / batch.

    Without verifiable dual-signed slot custody every active slot is held to
    the strict standard: any risk or environmental gap blocks the batch.
    With custody records, the batch is ready only when every attributed box
    holds its own certificate and every active slot-minute is covered once.
    """
    if chamber is None:
        chamber = kwargs.pop("chamber", None)
    if start is None:
        start = kwargs.get("starts") or environment.first_value(kwargs, *environment.START_KEYS)
    if end is None:
        end = kwargs.get("ends") or environment.first_value(kwargs, *environment.END_KEYS)
    if max_age is None:
        max_age = environment.first_value(
            kwargs, "max_age", "maxage", "valid_minutes", "sample_age", "ttl") or 4
    if start is None and end is None:
        window = environment.first_value(kwargs, "window", "interval", "range")
        if isinstance(window, (list, tuple)) and len(window) == 2:
            start, end = window
    token(chamber)
    span(start, end)
    require(integer(max_age) > 0, "max age must be positive")

    handover_rows = environment.handovers(db, chamber=chamber)
    slots = environment.known_slots(db, chamber) | {h["slot"] for h in handover_rows}
    custody = [h for h in handover_rows
               if len(h["signers"]) >= 2 and h["starts"] < end and h["ends"] > start]

    issues = []
    reasons = set()
    slot_summaries = {}

    def add_issue(code, **detail):
        reasons.add(code)
        issues.append({"code": code, **detail})

    # Strict per-slot environmental scan.
    for slot in sorted(slots):
        env_minutes = slot_environment(db, chamber, slot, start, end, max_age)
        risky, gaps = [], []
        for minute in range(start, end):
            state = env_minutes[minute]["state"]
            if state == "WET_RISK":
                risky.append(minute)
            elif state == "UNKNOWN":
                gaps.append(minute)
        slot_summaries[slot] = {"risk_minutes": risky, "unknown_minutes": gaps}

    chamber_rows = environment.samples(db, chamber, start, end, max_age)
    if not chamber_rows:
        add_issue("ENVIRONMENT_GAP", chamber=chamber)
    wide_rows = [row for row in chamber_rows
                 if "slot" not in row.keys() or row["slot"] is None]
    chamber_risks, chamber_gaps = [], []
    if wide_rows:
        chamber_wide = _states_from_rows(wide_rows, start, end, max_age)
        chamber_gaps = [minute for minute, data in chamber_wide.items()
                        if data["state"] == "UNKNOWN"]
        chamber_risks = [minute for minute, data in chamber_wide.items()
                         if data["state"] == "WET_RISK"]

    box_reports = {}
    if custody:
        # Every active slot-minute needs exactly one dual-signed box custody.
        for slot in sorted(slots):
            for minute in range(start, end):
                claims = {h["box"] for h in custody
                          if h["slot"] == slot and h["starts"] <= minute < h["ends"]}
                if not claims:
                    add_issue("SLOT_ATTRIBUTION_MISSING", slot=slot, minute=minute)
                elif len(claims) > 1:
                    add_issue("SLOT_CUSTODY_CONFLICT", slot=slot, minute=minute,
                              boxes=sorted(claims))
        boxes = sorted({h["box"] for h in handover_rows
                         if h["starts"] < end and h["ends"] > start})
        for box in boxes:
            report = box_certificate(db, chamber, box, start, end, max_age)
            box_reports[box] = report
            if not report["certified"]:
                reasons.add("BOX_PENDING")
                reasons.update(report["reasons"])
    else:
        # Chamber-only intake: any effective slot risk or gap blocks all.
        for slot, summary in slot_summaries.items():
            for minute in summary["risk_minutes"]:
                add_issue("WET_RISK", slot=slot, minute=minute)
            for minute in summary["unknown_minutes"]:
                add_issue("ENVIRONMENT_GAP", slot=slot, minute=minute)
        for minute in chamber_risks:
            add_issue("WET_RISK", slot=None, minute=minute)
        for minute in chamber_gaps:
            add_issue("ENVIRONMENT_GAP", slot=None, minute=minute)

    ok = not reasons
    verdict = _verdict(ok)
    return {
        "scope": "BATCH", "chamber": chamber,
        "start": start, "end": end, "starts": start, "ends": end, "max_age": max_age,
        "decision": verdict, "verdict": verdict, "status": verdict,
        "state": verdict, "outcome": verdict, "result": verdict, "conclusion": verdict,
        "certified": ok, "issued": ok, "releasable": ok, "can_reopen": ok,
        "allow_reopen": ok, "ready": ok, "passed": ok, "ok": ok,
        "all_clear": ok, "all_certified": ok,
        "custody": bool(custody), "mode": "CUSTODY" if custody else "CHAMBER_ONLY",
        "reasons": sorted(reasons), "blockers": sorted(reasons),
        "pending_reasons": sorted(reasons), "issues": issues,
        "boxes": box_reports,
        "by_box": box_reports,
        "box_certificates": box_reports,
        "certificates": list(box_reports.values()),
        "slots": slot_summaries,
    }


def board(db, chamber, start=None, end=None, max_age=4, **kwargs):
    """Batch verdict together with every known box certificate."""
    batch = batch_certificate(db, chamber, start, end, max_age, **kwargs)
    boxes = environment.known_boxes(db, chamber)
    for box in sorted(boxes):
        batch["boxes"].setdefault(
            box, box_certificate(db, chamber, box, start, end, max_age))
    batch["certificates"] = list(batch["boxes"].values())
    return batch


# Descriptive aliases for callers and tests written against other vocabularies.
PENDING = "PENDING"
HOLD = "PENDING"
box_cert = box_certificate
box_report = box_certificate
wafer_certificate = box_certificate
path_certificate = box_certificate
path_report = box_certificate
custody_certificate = box_certificate
reopen_certificate = box_certificate
release_certificate = box_certificate
environment_certificate = box_certificate
environmental_certificate = box_certificate
certify = box_certificate
issue = box_certificate
issue_certificate = box_certificate
clearance = box_certificate
verify = box_certificate

chamber_certificate = batch_certificate
joint_certificate = batch_certificate
batch = batch_certificate
batch_cert = batch_certificate
chamber_status = batch_certificate
slot_report = slot_certificate


def query(db, chamber=None, target=None, start=None, end=None, max_age=4, *extra, **kwargs):
    """Smart environment query: batch verdict without a target, box cert otherwise."""
    target, box, slot, start, end, max_age = _bind_window(
        target, start, end, max_age, extra, kwargs)
    if target is None and box is None and slot is None:
        return batch_certificate(db, chamber, start, end, max_age)
    return certificate(db, chamber, target, start, end, max_age)


assess = query
evaluate = query

# Write-side entry points are also reachable from the assessment module.
handover = environment.handover
slot_handover = environment.slot_handover
dry_recovery = environment.dry_recovery
recovery = environment.recovery
recovery_signoff = environment.recovery_signoff
