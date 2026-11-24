import json
from contextlib import closing
from pathlib import Path
import unittest

from wafer_retest import audit, focus_review, workflow
from wafer_retest.db import connect, initialize, transaction


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class FocusPlaneTests(unittest.TestCase):
    def test_focus_uses_exact_plane_and_changes_when_references_conflict(self):
        with closing(connect(":memory:")) as db:
            initialize(db)
            operation = json.loads((FIXTURES / "focus-plane.json").read_text())[0]
            checks = json.loads((FIXTURES / "focus-checks.json").read_text())
            with transaction(db):
                workflow.apply(db, operation["command"], operation["args"])
            result = focus_review.review(db, "STAGE-Z1", checks)
            self.assertEqual([r["status"] for r in result["checks"]], ["FOCUSED", "OUT_OF_FOCUS"])
            with transaction(db):
                workflow.apply(db, "focus-capture", {"scan": "STAGE-Z1", "readings": [
                    {"id": "R3", "x": 0, "y": 0, "height": 1}]})
            result = focus_review.review(db, "STAGE-Z1", checks)
            self.assertEqual(result["reference_state"], "REFERENCE_CONFLICT")
            self.assertEqual([r["status"] for r in result["checks"]], ["PENDING", "PENDING"])
            self.assertEqual(audit.audit(db)["decision"], "MATCH")
