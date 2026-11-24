from pathlib import Path
from unittest import TestCase

from wafer_retest import release, workflow
from wafer_retest.cli import load, read_json
from wafer_retest.db import connect, initialize, transaction


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
LOT = "LOT-P7"
PLAN = "PLAN-P7"


class LedgerCase(TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)
        initialize(self.db)

    def action(self, command, **args):
        with transaction(self.db):
            return workflow.apply(self.db, command, args)[0]

    def fixture(self, name):
        with transaction(self.db):
            return load(self.db, FIXTURES / f"{name}.json")

    def baseline(self):
        self.fixture("baseline")

    def ready(self):
        self.baseline()
        self.fixture("retest")
        return self.action("recompute", lot=LOT)

    def epoch(self):
        return release.status(self.db, LOT)["epoch"]

    def map_from(self, seed, session, measured_at=None, measurements=None, **changes):
        map_fields = dict(next(row["args"] for row in read_json(FIXTURES / f"{seed}.json") if row["command"] == "map"))
        anchor_fields = next(row["args"] for row in read_json(FIXTURES / f"{seed}.json") if row["command"] == "anchors")
        map_fields.update(changes, session=session)
        if measured_at is not None:
            map_fields["measured_at"] = measured_at
        if measurements is not None:
            map_fields["measurements"] = measurements
        self.action("map", **map_fields)
        self.action("anchors", session=session, anchors=anchor_fields["anchors"])

    def project_die(self, evaluation, x, y, wafer="W01"):
        return next(die for die in evaluation["manifest"]["dies"] if (die["wafer"], die["x"], die["y"]) == (wafer, x, y))

    def two_wafer_baseline(self):
        inputs = read_json(FIXTURES / "baseline.json")
        inputs[0]["args"]["wafers"].append("W02")
        for row in inputs:
            self.action(row["command"], **row["args"])
        self.map_from("baseline", "BASE-W02", wafer="W02")
