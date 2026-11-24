import json
from collections import defaultdict

from .coordinates import lot_mask
from .db import dump, get, integer, lot_row, require, span, token, touch
from .inspection import collect, consensus


RISK = {"NO_COMPLETE_ATTEMPT": 0, "ATTEMPT_DISAGREEMENT": 1, "FAIL": 2, "MARGINAL": 3, "PASS": 4}


def active_plan(db, lot):
    return db.execute("SELECT * FROM sampling_plans WHERE lot=? AND state IN ('OPEN','SEALED')", (lot,)).fetchone()


def targets_for(db, plan):
    return {(row["wafer"], row["x"], row["y"]): dict(row) for row in db.execute(
        "SELECT wafer,x,y,sector,risk,priority FROM sampling_targets WHERE plan=? ORDER BY priority", (plan,))}


def create(db, plan, lot, opens, closes, capacity):
    token(plan)
    span(opens, closes)
    current = lot_row(db, lot)
    existing = db.execute("SELECT * FROM sampling_plans WHERE id=?", (plan,)).fetchone()
    if existing:
        require((lot, opens, closes, capacity) == tuple(existing[key] for key in ("lot", "opens", "closes", "capacity"))
                and type(capacity) is int, "conflicting sampling plan redelivery")
        return inspect(db, plan)
    require(active_plan(db, lot) is None, "lot already has a live sampling plan")
    mask = lot_mask(db, lot)
    require(1 <= integer(capacity) <= len(mask), "sampling capacity outside die count")
    attempts, _, dictionary = collect(db, lot)
    require(dictionary is not None, "sampling requires a bound dictionary")
    strata = defaultdict(list)
    snapshot = []
    for (wafer, x, y), sector in mask.items():
        baseline, reason = consensus([row for row in attempts[wafer, x, y] if row["plan"] is None and row["when"] < opens])
        risk = baseline["grade"] if baseline else reason
        strata[wafer, sector].append((RISK[risk], wafer, x, y, sector, risk))
        snapshot.append({"wafer": wafer, "x": x, "y": y, "sector": sector, "risk": risk,
                         "baseline": baseline["session"] if baseline else None})
    require(len(strata) <= capacity, "sampling capacity cannot cover every wafer-sector")
    floors = {min(members) for members in strata.values()}
    rest = sorted(set().union(*strata.values()) - floors)
    selected = sorted(floors) + rest[:capacity - len(floors)]
    db.execute("INSERT INTO sampling_plans VALUES(?,?,?,?,?,?,'OPEN',?)",
               (plan, lot, current["epoch"], opens, closes, capacity, dump({"dictionary": dictionary, "dies": snapshot})))
    db.executemany("INSERT INTO sampling_targets VALUES(?,?,?,?,?,?,?)", (
        (plan, wafer, x, y, sector, risk, i) for i, (_, wafer, x, y, sector, risk) in enumerate(selected, 1)
    ))
    touch(db, lot)
    return inspect(db, plan)


def inspect(db, plan):
    row = dict(get(db, "SELECT * FROM sampling_plans WHERE id=?", (plan,), "sampling plan"))
    row["snapshot"] = json.loads(row["snapshot"])
    row["targets"] = list(targets_for(db, plan).values())
    return row


def transition(db, plan, state):
    require(state in ("SEALED", "VOID"), "expected SEALED or VOID")
    row = get(db, "SELECT * FROM sampling_plans WHERE id=?", (plan,), "sampling plan")
    if row["state"] == state:
        return inspect(db, plan)
    require(row["state"] == "OPEN", "only an open sampling plan can change state")
    if state == "VOID":
        used = db.execute("SELECT 1 FROM sessions WHERE plan=? LIMIT 1", (plan,)).fetchone()
        require(used is None, "cannot void a sampling plan with acquired maps")
    db.execute("UPDATE sampling_plans SET state=? WHERE id=?", (state, plan))
    touch(db, row["lot"])
    return inspect(db, plan)


def choose(die, attempts, plan, targets):
    baseline, retests, exclusions = [], [], []
    for row in attempts:
        if row["plan"] is None:
            if plan is None or row["when"] < plan["opens"]:
                baseline.append(row)
                continue
            reason = "BASE_AT_OR_AFTER_PLAN_START"
        elif plan is None or row["plan"] != plan["id"]:
            reason = "INACTIVE_PLAN"
        elif die not in targets:
            reason = "NOT_A_TARGET"
        elif not plan["opens"] <= row["when"] < plan["closes"]:
            reason = "OUTSIDE_PLAN_WINDOW"
        else:
            retests.append(row)
            continue
        exclusions.append({"wafer": die[0], "xy": die[1:], "session": row["session"], "state": reason})
    if die in targets:
        chosen, state = consensus(retests)
        return chosen, "RETEST" if chosen else "RETEST_" + state, exclusions
    chosen, state = consensus(baseline)
    return chosen, "BASE" if chosen else state, exclusions
