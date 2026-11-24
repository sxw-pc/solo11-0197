import json

from .db import get, insert_once, integer, lot_row, require, token, touch


GRADES = {"PASS": 0, "MARGINAL": 1, "FAIL": 2}


def worst(grades):
    return max(grades, key=GRADES.__getitem__)


def read_rules(db, dictionary):
    return {(row["channel"], row["code"]): dict(row) for row in db.execute(
        "SELECT channel,code,grade,lo,hi FROM bin_rules WHERE dictionary=? ORDER BY channel,code", (dictionary,))}


def define(db, dictionary, rules):
    require(isinstance(rules, list) and rules, "dictionary requires bin rules")
    normalized = {}
    for row in rules:
        key = token(row["channel"]), token(row["code"])
        grade = row["grade"]
        lo, hi = row.get("lo"), row.get("hi")
        require(key not in normalized and grade in GRADES, "duplicate or invalid bin rule")
        if lo is not None or hi is not None:
            require(lo is not None and hi is not None and integer(lo) <= integer(hi), "invalid WAT interval")
        normalized[key] = dict(channel=key[0], code=key[1], grade=grade, lo=lo, hi=hi)
    existing = db.execute("SELECT state FROM dictionaries WHERE id=?", (token(dictionary),)).fetchone()
    if existing:
        require(normalized == read_rules(db, dictionary), "conflicting dictionary version redelivery")
    else:
        db.execute("INSERT INTO dictionaries VALUES(?,'DRAFT')", (dictionary,))
        db.executemany("INSERT INTO bin_rules VALUES(?,?,?,?,?,?)", (
            (dictionary, channel, code, row["grade"], row["lo"], row["hi"])
            for (channel, code), row in normalized.items()
        ))
    return {"dictionary": dictionary, "rules": len(normalized), "state": existing[0] if existing else "DRAFT"}


def seal(db, dictionary):
    get(db, "SELECT * FROM dictionaries WHERE id=?", (dictionary,), "dictionary")
    db.execute("UPDATE dictionaries SET state='SEALED' WHERE id=?", (dictionary,))
    return {"dictionary": dictionary, "state": "SEALED"}


def bind(db, lot, dictionary):
    channels = set(json.loads(lot_row(db, lot)["channels"]))
    row = get(db, "SELECT * FROM dictionaries WHERE id=?", (dictionary,), "dictionary")
    require(row["state"] == "SEALED", "dictionary is not sealed")
    require(channels <= {channel for channel, _ in read_rules(db, dictionary)}, "dictionary lacks required channels")
    current = bound_version(db, lot)
    if current != dictionary:
        db.execute("INSERT INTO lot_dictionaries VALUES(?,?) ON CONFLICT(lot) DO UPDATE SET dictionary=excluded.dictionary",
                   (lot, dictionary))
        touch(db, lot)
    return {"lot": lot, "dictionary": dictionary}


def bound_version(db, lot):
    result = db.execute("SELECT dictionary FROM lot_dictionaries WHERE lot=?", (lot,)).fetchone()
    return result[0] if result else None


def classify(rules, channel, code, value):
    rule = rules.get((channel, code))
    if rule is None:
        return None, "UNKNOWN_BIN"
    if rule["lo"] is not None and not rule["lo"] <= value <= rule["hi"]:
        return "FAIL", "WAT_LIMIT"
    return rule["grade"], "BIN_RULE"
