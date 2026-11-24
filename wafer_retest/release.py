import hashlib
import json

from . import coordinates, inspection, sampling
from .db import dump, get, lot_row, require


def digest_of(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def calculate(db, lot):
    current = lot_row(db, lot)
    attempts, trace, dictionary = inspection.collect(db, lot)
    plan = sampling.active_plan(db, lot)
    targets = sampling.targets_for(db, plan["id"]) if plan else {}
    atlas = []
    counts = dict(PASS=0, MARGINAL=0, FAIL=0, HOLD=0)
    for die, sector in coordinates.lot_mask(db, lot).items():
        chosen, state, exclusions = sampling.choose(die, attempts[die], plan, targets)
        grade = chosen["grade"] if chosen else "HOLD"
        counts[grade] += 1
        wafer, x, y = die
        atlas.append({"wafer": wafer, "x": x, "y": y, "sector": sector, "grade": grade,
                      "state": state, "target": die in targets, "reading": chosen})
        trace.extend(exclusions)
    blockers = []
    if dictionary is None:
        blockers.append("NO_DICTIONARY")
    if plan and plan["state"] != "SEALED":
        blockers.append("PLAN_OPEN")
    if counts["HOLD"]:
        blockers.append("INCOMPLETE_ATLAS")
    if counts["FAIL"] > current["fail_limit"]:
        blockers.append("FAIL_LIMIT")
    manifest = {"lot": lot, "dictionary": dictionary, "plan": plan["id"] if plan else None,
                "plan_state": plan["state"] if plan else None, "channels": json.loads(current["channels"]),
                "fail_limit": current["fail_limit"], "counts": counts, "blockers": blockers,
                "decision": "HOLD" if blockers else "READY", "dies": atlas}
    return current["epoch"], manifest, trace


def recompute(db, lot):
    epoch, manifest, trace = calculate(db, lot)
    encoded = dump(manifest)
    digest = digest_of(encoded)
    db.execute("""INSERT INTO assessments VALUES(?,?,?,?,?) ON CONFLICT(lot,epoch)
                  DO UPDATE SET digest=excluded.digest,manifest=excluded.manifest,trace=excluded.trace""",
               (lot, epoch, digest, encoded, dump(trace)))
    db.execute("""UPDATE releases SET state='REVOKED', revoked_at_epoch=?
                  WHERE lot=? AND state='ACTIVE' AND (digest<>? OR manifest<>?)""", (epoch, lot, digest, encoded))
    return {"epoch": epoch, "digest": digest, "manifest": manifest, "trace": trace}


def active_release(db, lot):
    return db.execute("SELECT * FROM releases WHERE lot=? AND state='ACTIVE'", (lot,)).fetchone()


def current_assessment(db, lot):
    current = lot_row(db, lot)
    assessment = db.execute("SELECT * FROM assessments WHERE lot=? AND epoch=?", (lot, current["epoch"])).fetchone()
    require(assessment is not None, "lot is stale or has no assessment, recompute first")
    require(digest_of(assessment["manifest"]) == assessment["digest"], "assessment integrity failed, recompute first")
    return assessment


def publish(db, lot):
    assessment = current_assessment(db, lot)
    manifest = json.loads(assessment["manifest"])
    require(manifest["decision"] == "READY", f"atlas held: {','.join(manifest['blockers'])}")
    active = active_release(db, lot)
    if active is not None:
        require(active["digest"] == assessment["digest"] and active["manifest"] == assessment["manifest"],
                "active release differs, recompute first")
        return export(db, lot, active["revision"])
    revision = db.execute("SELECT coalesce(max(revision),0)+1 FROM releases WHERE lot=?", (lot,)).fetchone()[0]
    db.execute("INSERT INTO releases VALUES(?,?,?,?,?,'ACTIVE',NULL)",
               (lot, revision, assessment["epoch"], assessment["digest"], assessment["manifest"]))
    return export(db, lot, revision)


def export(db, lot, revision):
    assessment = current_assessment(db, lot)
    row = get(db, "SELECT * FROM releases WHERE lot=? AND revision=?", (lot, revision), "release revision")
    require(row["state"] == "ACTIVE", "release is revoked")
    require(row["digest"] == assessment["digest"] and row["manifest"] == assessment["manifest"],
            "release differs from the current atlas")
    return {"revision": revision, "digest": row["digest"], "manifest": json.loads(row["manifest"])}


def status(db, lot):
    current = lot_row(db, lot)
    assessment = db.execute("SELECT digest,manifest FROM assessments WHERE lot=? AND epoch=?",
                            (lot, current["epoch"])).fetchone()
    active = active_release(db, lot)
    history = [dict(row) for row in db.execute("""
        SELECT revision,epoch,digest,state,revoked_at_epoch FROM releases WHERE lot=? ORDER BY revision""", (lot,))]
    fresh = assessment is not None and digest_of(assessment["manifest"]) == assessment["digest"]
    exportable = bool(fresh and active and (assessment["digest"], assessment["manifest"]) == (active["digest"], active["manifest"]))
    return {"lot": lot, "epoch": current["epoch"], "fresh": fresh, "exportable": exportable, "releases": history}
