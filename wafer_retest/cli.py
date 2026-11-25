import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from . import audit, bin_crosswalk, cards, coordinates, release, sampling, workflow
from .db import RuleError, connect, initialize, require, transaction


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _configure_crosswalk(operation):
    bin_crosswalk.configure_arguments(operation)


def load(db, path):
    entries = read_json(path)
    require(isinstance(entries, list), "input batch must be a JSON list")
    result = []
    for row in entries:
        command, fields = row["command"], row["args"]
        require(command in workflow.INPUT, "unsupported batch operation")
        result.append(workflow.apply(db, command, fields)[0])
    return result


def demo(db):
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    lot = "LOT-P7"
    load(db, fixtures / "baseline.json")
    baseline = workflow.apply(db, "recompute", {"lot": lot})[0]
    load(db, fixtures / "retest.json")
    ready = workflow.apply(db, "recompute", {"lot": lot})[0]
    first = workflow.apply(db, "publish", {"lot": lot})[0]
    load(db, fixtures / "late-evidence.json")
    stale = release.status(db, lot)
    held = workflow.apply(db, "recompute", {"lot": lot})[0]
    retired = release.status(db, lot)
    load(db, fixtures / "replacement.json")
    recovered = workflow.apply(db, "recompute", {"lot": lot})[0]
    second = workflow.apply(db, "publish", {"lot": lot})[0]
    return {
        "baseline": baseline["manifest"]["counts"],
        "targets": [{"wafer": die["wafer"], "xy": [die["x"], die["y"]], "risk": die["risk"]}
                    for die in sampling.inspect(db, "PLAN-P7")["targets"]],
        "orientations": [row["frame"]["orientation"] for row in ready["trace"]
                         if row.get("frame")],
        "first_release": first["revision"], "first_digest": first["digest"],
        "late_exportable": stale["exportable"],
        "late_counts": held["manifest"]["counts"],
        "late_decision": held["manifest"]["decision"],
        "first_release_after_recompute": retired["releases"][0]["state"],
        "recovered_counts": recovered["manifest"]["counts"],
        "replacement_release": second["revision"], "replacement_digest": second["digest"],
    }


def parser():
    root = argparse.ArgumentParser(description="Offline wafer probe retest ledger")
    root.add_argument("--db", required=True, help="SQLite database path, or :memory:")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("demo", help="run the bundled scenario on an empty database")
    commands.add_parser("load", help="apply a JSON list of input commands atomically").add_argument("file")
    for name in workflow.INPUT:
        commands.add_parser(name).add_argument("file", help="JSON object with command inputs")
    commands.add_parser("journal-export")
    commands.add_parser("replay").add_argument("file")
    commands.add_parser("audit")
    commands.add_parser("project").add_argument("session")
    crosswalk = commands.add_parser(
        "bin-crosswalk", help="prove BIN equivalence and convert an old machine map")
    _configure_crosswalk(crosswalk)
    for alias in ("crosswalk", "bin_crosswalk", "bin-crosswalk-convert"):
        _configure_crosswalk(commands.add_parser(alias, help=argparse.SUPPRESS))
    commands.add_parser("plan").add_argument("plan")
    for name in ("recompute", "status", "publish", "export"):
        operation = commands.add_parser(name)
        operation.add_argument("lot")
        if name == "export":
            operation.add_argument("revision", type=int)
    contact = commands.add_parser("contact")
    contact.add_argument("card")
    contact.add_argument("minute", type=int)
    contact.add_argument("site", type=int)
    contact.add_argument("--session")
    contact.add_argument("--touchdown")
    return root


def dispatch(db, args):
    command = args.command
    if command in workflow.INPUT:
        return workflow.apply(db, command, read_json(args.file))[0]
    if command in workflow.DECISIONS:
        return workflow.apply(db, command, {"lot": args.lot})[0]
    if command == "journal-export":
        return audit.export_journal(db)
    if command == "replay":
        return audit.replay(db, read_json(args.file))
    if command == "audit":
        return audit.audit(db)
    if command == "load":
        return load(db, args.file)
    if command == "demo":
        require(db.execute("SELECT 1 FROM lots LIMIT 1").fetchone() is None, "demo requires an empty ledger")
        return demo(db)
    if command == "project":
        return coordinates.project(db, args.session)
    if command in ("bin-crosswalk", "crosswalk", "bin_crosswalk", "bin-crosswalk-convert"):
        return bin_crosswalk.dispatch_namespace(args, db)
    if command == "contact":
        return cards.inspect(db, args.card, args.minute, args.site, args.session, args.touchdown)
    if command == "plan":
        return sampling.inspect(db, args.plan)
    if command == "export":
        return release.export(db, args.lot, args.revision)
    if command == "status":
        return release.status(db, args.lot)
    raise RuleError(f"unsupported operation: {command}")


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    try:
        with closing(connect(args.db)) as db:
            if args.command == "init":
                initialize(db)
                result = {"schema": db.execute("PRAGMA user_version").fetchone()[0]}
            else:
                if args.command in ("demo", "replay", "bin-crosswalk", "crosswalk",
                                     "bin_crosswalk", "bin-crosswalk-convert"):
                    initialize(db)
                require(db.execute("PRAGMA user_version").fetchone()[0] == 1, "initialize a v1 ledger first")
                with transaction(db):
                    result = dispatch(db, args)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
        if args.command == "audit" and result["decision"] == "MISMATCH":
            return 2
        if getattr(args, "strict", False) and isinstance(result, dict) and result.get("pending"):
            return 2
        return 0
    except (ValueError, OSError, sqlite3.Error, TypeError, KeyError) as error:
        command.exit(2, f"wafer-retest: {error}\n")
