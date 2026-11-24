import json
from contextlib import closing

from . import release, workflow
from .db import connect, dump, initialize, require, transaction


def tables(db):
    return [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]


def export_journal(db):
    return {"schema": 1, "events": [
        {"seq": row["seq"], "command": row["command"], "args": json.loads(row["arguments"]),
         "receipt": json.loads(row["receipt"])}
        for row in db.execute("SELECT * FROM journal ORDER BY seq")
    ]}


def sql_table(table):
    return '"' + table.replace('"', '""') + '"'


def compare_rows(db, table):
    return sorted(dump(dict(row)) for row in db.execute(f"SELECT * FROM {sql_table(table)}"))


def summaries(db):
    lots = []
    for row in db.execute("SELECT id FROM lots ORDER BY id"):
        state = release.status(db, row["id"])
        last = state["releases"][-1] if state["releases"] else None
        lots.append({"lot": row["id"], "epoch": state["epoch"], "exportable": state["exportable"],
                     "latest_revision": last["revision"] if last else None,
                     "latest_digest": last["digest"] if last else None})
    return lots


def replay(db, archive):
    require(all(db.execute(f"SELECT 1 FROM {sql_table(table)} LIMIT 1").fetchone() is None for table in tables(db)),
            "replay requires an empty ledger")
    require(isinstance(archive, dict) and set(archive) == {"schema", "events"}
            and type(archive["schema"]) is int and archive["schema"] == 1
            and isinstance(archive["events"], list), "invalid journal envelope")
    for seq, event in enumerate(archive["events"], 1):
        require(isinstance(event, dict) and set(event) == {"seq", "command", "args", "receipt"}
                and type(event["seq"]) is int and event["seq"] == seq, f"journal sequence error at event {seq}")
        _, receipt = workflow.apply(db, event["command"], event["args"])
        require(dump(receipt) == dump(event["receipt"]), f"journal receipt mismatch at event {seq}")
    return {"decision": "REPLAYED", "events": len(archive["events"]), "lots": summaries(db)}


def audit(db):
    archive = export_journal(db)
    with closing(connect(":memory:")) as rebuilt:
        initialize(rebuilt)
        with transaction(rebuilt):
            result = replay(rebuilt, archive)
        original_tables, rebuilt_tables = set(tables(db)), set(tables(rebuilt))
        differences = sorted(original_tables ^ rebuilt_tables)
        differences.extend(sorted(table for table in original_tables & rebuilt_tables
                                  if compare_rows(db, table) != compare_rows(rebuilt, table)))
    result["decision"] = "MISMATCH" if differences else "MATCH"
    result["different_tables"] = differences
    return result
