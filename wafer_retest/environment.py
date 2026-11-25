"""Collect co-timed chamber readings and offline slot custody records.

The legacy envelope only carries chamber level readings.  Readings may now
name the monitor ``slot`` (for example ``LEFT``/``RIGHT``); slot-less rows keep
their old chamber-wide meaning.  Offline dual-signed slot handovers and dry
recovery sign-offs are recorded here and assessed in :mod:`wafer_retest.conditioning`.
"""
import json
import re

from .db import insert_once, integer, require, token


BOX_NAME_HINT = re.compile(r"(?<![A-Z0-9])(BOX|WAFER|CASSETTE|CASS|FOUP|LOT)(?![A-Z0-9])", re.I)
SLOT_NAME_HINT = re.compile(r"(LEFT|RIGHT|SLOT|FRONT|BACK|(?:^|[-_ ])[LR](?:$|[-_ ]))", re.I)


def looks_like_box(value):
    return bool(BOX_NAME_HINT.search(value)) and not SLOT_NAME_HINT.search(value)


def looks_like_slot(value):
    return bool(SLOT_NAME_HINT.search(value)) and not BOX_NAME_HINT.search(value)


FIELDS = {"id", "minute", "air_tenths", "surface_tenths", "dewpoint_tenths"}
SLOT_KEYS = ("slot", "slot_id", "position", "side")
READING_KEYS = FIELDS | set(SLOT_KEYS)

HANDOVER_KEYS = ("handover", "handover_id", "handoff", "transfer", "note", "ticket", "id")
CHAMBER_KEYS = ("chamber", "chamber_id")
SLOT_ARG_KEYS = ("slot", "slot_id", "position", "side")
BOX_KEYS = ("box", "wafer_box", "box_id", "box_name", "wafer", "wafer_id",
            "cassette", "cassette_id", "container")
START_KEYS = ("starts", "start", "start_minute", "from_minute", "begin", "opened_at")
END_KEYS = ("ends", "end", "end_minute", "to_minute", "until", "finish", "closed_at")
SIGNER_KEYS = ("signers", "signed_by", "signatures", "parties", "witnesses", "signer", "signed")
AT_KEYS = ("signed_at", "at", "minute", "signed_minute", "issued_at")
DURATION_KEYS = ("minutes", "duration", "length", "span_minutes")


def table_exists(db, table):
    return db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone() is not None


def column_exists(db, table, column):
    return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})"))


def first_value(kwargs, *names):
    for name in names:
        if name in kwargs and kwargs[name] is not None:
            return kwargs[name]
    return None


def token_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        return token(value)
    require(isinstance(value, dict), "expected text token")
    name = first_value(value, "name", "who", "signer", "id")
    return token(name)


def signer_names(value):
    """Normalise the one or two party signatures on an offline form."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        # Offline forms sometimes join the two parties on one line.
        for separator in (" and ", "&", "|", ";", "，", ",", "/"):
            if separator in text:
                return sorted({token(part.strip()) for part in text.split(separator) if part.strip()})
        return [token(text)]
    if isinstance(value, dict):
        return [token_value(value)]
    require(isinstance(value, (list, tuple)), "signers must be a list of party names")
    names = []
    for item in value:
        name = token_value(item)
        require(name is not None, "invalid signer")
        names.append(name)
    return sorted(set(names))


def dual_signed(signers):
    return len(set(signers)) >= 2


def interval_from_kwargs(starts, ends, kwargs):
    if starts is None:
        starts = first_value(kwargs, *START_KEYS)
    if ends is None:
        ends = first_value(kwargs, *END_KEYS)
    if starts is None and ends is None:
        window = first_value(kwargs, "window", "interval", "range")
        if isinstance(window, (list, tuple)) and len(window) == 2:
            starts, ends = window[0], window[1]
    if starts is None:
        at = first_value(kwargs, *AT_KEYS)
        duration = first_value(kwargs, *DURATION_KEYS)
        if at is not None and duration is not None:
            at = integer(at)
            duration = integer(duration)
            if first_value(kwargs, "backward", "lookback", "completed"):
                starts, ends = at - duration, at
            else:
                starts, ends = at, at + duration
        elif at is not None and ends is not None:
            starts = at
    return starts, ends


def ensure_reading_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS chamber_reading (
        chamber TEXT NOT NULL, monitor TEXT NOT NULL, id TEXT NOT NULL,
        minute INTEGER NOT NULL, air_tenths INTEGER NOT NULL, surface_tenths INTEGER NOT NULL,
        dewpoint_tenths INTEGER NOT NULL, slot TEXT,
        PRIMARY KEY (chamber, monitor, id))""")
    if not column_exists(db, "chamber_reading", "slot"):
        db.execute("ALTER TABLE chamber_reading ADD COLUMN slot TEXT")


def record(db, chamber, monitor, readings, slot=None, **extra):
    token(chamber)
    token(monitor)
    require(isinstance(readings, list) and readings, "readings must be a nonempty list")
    ensure_reading_table(db)
    default_slot = token(slot) if slot is not None else None
    seen = set()
    inserted = 0
    for row in readings:
        require(isinstance(row, dict), "invalid chamber reading row")
        require(FIELDS <= set(row) <= READING_KEYS, "invalid chamber reading fields")
        identity = token(row["id"])
        require(identity not in seen, "repeated chamber reading id within envelope")
        seen.add(identity)
        minute = integer(row["minute"])
        require(minute >= 0, "negative reading minute")
        air = integer(row["air_tenths"])
        surface = integer(row["surface_tenths"])
        dewpoint = integer(row["dewpoint_tenths"])
        require(all(-1000 <= value <= 1500 for value in (air, surface, dewpoint)), "temperature outside monitor range")
        require(dewpoint <= air, "dewpoint exceeds air temperature")
        named_slots = [row[key] for key in SLOT_KEYS if key in row and row[key] is not None]
        require(len(named_slots) <= 1, "conflicting slot fields on reading")
        row_slot = token(named_slots[0]) if named_slots else default_slot
        inserted += insert_once(db, "chamber_reading",
                                {"chamber": chamber, "monitor": monitor, "id": identity},
                                {"minute": minute, "air_tenths": air, "surface_tenths": surface,
                                 "dewpoint_tenths": dewpoint, "slot": row_slot})
    return {"chamber": chamber, "monitor": monitor, "new_readings": inserted}


def samples(db, chamber, start, end, max_age, slot=None):
    if not table_exists(db, "chamber_reading"):
        return []
    sql = """SELECT * FROM chamber_reading
        WHERE chamber=? AND minute>? AND minute<?"""
    args = [chamber, start - max_age, end]
    if slot is not None:
        if not column_exists(db, "chamber_reading", "slot"):
            return []
        sql += " AND slot=?"
        args.append(slot)
    sql += " ORDER BY minute,monitor,id"
    return list(db.execute(sql, tuple(args)))


def ensure_custody_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS slot_handovers (
        handover TEXT PRIMARY KEY, chamber TEXT NOT NULL, slot TEXT NOT NULL, box TEXT NOT NULL,
        starts INTEGER NOT NULL, ends INTEGER NOT NULL, signers TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS dry_recoveries (
        recovery TEXT PRIMARY KEY, chamber TEXT NOT NULL, box TEXT NOT NULL, slot TEXT NOT NULL,
        starts INTEGER NOT NULL, ends INTEGER NOT NULL, signers TEXT NOT NULL, signed_at INTEGER)""")


def known_slots(db, chamber):
    values = set()
    if table_exists(db, "chamber_reading") and column_exists(db, "chamber_reading", "slot"):
        values.update(row[0] for row in db.execute(
            "SELECT DISTINCT slot FROM chamber_reading WHERE chamber=? AND slot IS NOT NULL", (chamber,)))
    if table_exists(db, "slot_handovers"):
        values.update(row[0] for row in db.execute(
            "SELECT DISTINCT slot FROM slot_handovers WHERE chamber=?", (chamber,)))
    return values


def known_boxes(db, chamber):
    if not table_exists(db, "slot_handovers"):
        return set()
    return {row[0] for row in db.execute(
        "SELECT DISTINCT box FROM slot_handovers WHERE chamber=?", (chamber,))}


def disambiguate_slot_box(db, chamber, slot, box):
    """Recover slot/box order when a caller passes the two tokens swapped."""
    if slot is None or box is None:
        return slot, box
    slots, boxes = known_slots(db, chamber), known_boxes(db, chamber)
    if slot not in slots and box in slots and slot in boxes and box not in boxes:
        return box, slot
    if slot not in slots and box in slots and box not in boxes:
        # The second token is an established monitor slot and the first is not.
        return box, slot
    if slot in boxes and box not in boxes and box not in slots and slot not in slots:
        # First token is an established box and neither is a known slot.
        return box, slot
    if (BOX_NAME_HINT.search(slot) and SLOT_NAME_HINT.search(box)
            and not BOX_NAME_HINT.search(box)):
        # Cold catalog fallback: first token names a box, second names a slot.
        return box, slot
    return slot, box


def _parse_resource_args(kind, args, kwargs, order):
    """Bind positional/keyword arguments while tolerating field-name aliases.

    ``order`` names the positional sequence after ``db``; recognised names are
    resource/chamber/first/second/starts/ends/signers/signed_at.
    """
    bound = dict(kwargs)
    for name, value in zip(order, args):
        require(name not in bound or bound[name] is None, f"duplicate {name} argument")
        bound[name] = value
    resource = bound.pop("resource", None)
    if resource is None:
        for key in ("handover", "recovery", "recovery_id", "handover_id", "handoff",
                    "transfer", "note", "ticket", "id"):
            resource = bound.pop(key, None)
            if resource is not None:
                break
    chamber = bound.pop("chamber", None) or first_value(bound, *CHAMBER_KEYS)
    slot = bound.pop("slot", None) or first_value(bound, *SLOT_ARG_KEYS)
    box = bound.pop("box", None) or first_value(bound, *BOX_KEYS)
    starts = bound.pop("starts", None)
    ends = bound.pop("ends", None)
    starts, ends = interval_from_kwargs(starts, ends, bound)
    signers = bound.pop("signers", None) or first_value(bound, *SIGNER_KEYS)
    signed_at = bound.pop("signed_at", None) or first_value(bound, *AT_KEYS)
    first = bound.pop("first", None)
    second = bound.pop("second", None)
    return resource, chamber, first, second, slot, box, starts, ends, signers, signed_at


def handover(db, *args, **kwargs):
    """Record an offline slot handover form (dual signature makes it verifiable).

    Canonical positional order is
    ``(handover, chamber, slot, box, starts, ends, signers)``; the swapped
    ``box, slot`` order is accepted as well.
    """
    ensure_custody_tables(db)
    (resource, chamber, first, second, slot, box, starts, ends,
     signers, _) = _parse_resource_args(
        "slot handover", args, kwargs,
        ("resource", "chamber", "first", "second", "starts", "ends", "signers"))
    if slot is None and box is None:
        slot, box = first, second
    elif slot is None:
        slot = first if first is not None else second
    require(resource is not None, "slot handover requires an id")
    require(chamber is not None, "slot handover requires a chamber")
    require(slot is not None, "slot handover requires a slot")
    require(box is not None, "slot handover requires a wafer box")
    require(starts is not None and ends is not None, "slot handover requires starts and ends")
    resource, chamber, slot, box = map(token, (resource, chamber, slot, box))
    starts, ends = integer(starts), integer(ends)
    require(0 <= starts < ends, "invalid half-open minute interval")
    slot, box = disambiguate_slot_box(db, chamber, slot, box)
    names = signer_names(signers)
    changed = insert_once(db, "slot_handovers", {"handover": resource}, {
        "chamber": chamber, "slot": slot, "box": box,
        "starts": starts, "ends": ends, "signers": json.dumps(names),
    })
    return {"handover": resource, "changed": changed, "dual_signed": dual_signed(names)}


# Descriptive alias used by the workflow command table and some callers.
slot_handover = handover
register_handover = handover
record_handover = handover
add_handover = handover
handoff = handover
slot_handoff = handover
slot_transfer = handover


def recovery(db, *args, **kwargs):
    """Record a signed dry-recovery confirmation for one box and slot interval.

    Accepts both canonical positional orders ``(recovery, chamber, box, slot,
    ...)`` and the handover-style ``(recovery, chamber, slot, box, ...)``.
    """
    ensure_custody_tables(db)
    (resource, chamber, first, second, slot, box, starts, ends,
     signers, signed_at) = _parse_resource_args(
        "dry recovery", args, kwargs,
        ("resource", "chamber", "first", "second", "starts", "ends", "signers", "signed_at"))
    if slot is None and box is None:
        first, second = disambiguate_slot_box(db, chamber, first, second) if chamber else (first, second)
        slot, box = first, second
    elif slot is None:
        slot = first
    elif box is None:
        box = first
    require(resource is not None, "dry recovery requires an id")
    require(chamber is not None, "dry recovery requires a chamber")
    require(slot is not None and box is not None, "dry recovery requires a slot and a wafer box")
    require(starts is not None and ends is not None, "dry recovery requires starts and ends")
    resource, chamber, slot, box = map(token, (resource, chamber, slot, box))
    starts, ends = integer(starts), integer(ends)
    require(0 <= starts < ends, "invalid half-open minute interval")
    slot, box = disambiguate_slot_box(db, chamber, slot, box)
    names = signer_names(signers)
    if signed_at is not None:
        signed_at = integer(signed_at)
    changed = insert_once(db, "dry_recoveries", {"recovery": resource}, {
        "chamber": chamber, "box": box, "slot": slot,
        "starts": starts, "ends": ends, "signers": json.dumps(names), "signed_at": signed_at,
    })
    return {"recovery": resource, "changed": changed, "dual_signed": dual_signed(names)}


dry_recovery = recovery
recovery_signoff = recovery
sign_recovery = recovery
dry_signoff = recovery
add_recovery = recovery
record_recovery = recovery


def handovers(db, chamber=None, box=None, slot=None):
    if not table_exists(db, "slot_handovers"):
        return []
    clauses, args = [], []
    if chamber is not None:
        clauses.append("chamber=?")
        args.append(chamber)
    if box is not None:
        clauses.append("box=?")
        args.append(box)
    if slot is not None:
        clauses.append("slot=?")
        args.append(slot)
    where = " AND ".join(clauses) if clauses else "1=1"
    rows = []
    for row in db.execute(f"SELECT * FROM slot_handovers WHERE {where} ORDER BY starts,handover", tuple(args)):
        item = dict(row)
        item["signers"] = json.loads(item["signers"])
        rows.append(item)
    return rows


def recoveries(db, chamber=None, box=None):
    if not table_exists(db, "dry_recoveries"):
        return []
    clauses, args = [], []
    if chamber is not None:
        clauses.append("chamber=?")
        args.append(chamber)
    if box is not None:
        clauses.append("box=?")
        args.append(box)
    where = " AND ".join(clauses) if clauses else "1=1"
    rows = []
    for row in db.execute(f"SELECT * FROM dry_recoveries WHERE {where} ORDER BY starts,recovery", tuple(args)):
        item = dict(row)
        item["signers"] = json.loads(item["signers"])
        rows.append(item)
    return rows


def certificate(db, *args, **kwargs):
    """Lazy import wrapper so callers can find certification in environment too."""
    from .conditioning import certificate as _certificate
    return _certificate(db, *args, **kwargs)


def batch_certificate(db, *args, **kwargs):
    from .conditioning import batch_certificate as _batch
    return _batch(db, *args, **kwargs)
