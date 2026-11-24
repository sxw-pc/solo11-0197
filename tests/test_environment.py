from wafer_retest import audit, conditioning
from wafer_retest.db import RuleError

from tests.ledger_case import FIXTURES, LedgerCase, read_json


class ChamberTests(LedgerCase):
    def test_fresh_intervals_and_expiry(self):
        envelope = read_json(FIXTURES / "environment-readings.json")
        self.assertEqual(self.action("environment-record", **envelope)["new_readings"], 3)
        self.assertEqual(self.action("environment-record", **envelope)["new_readings"], 0)
        report = conditioning.trace(self.db, "DRY-1", 12, 26, 4)
        self.assertEqual(report["decision"], "WET_RISK")
        self.assertEqual([(x["start"], x["end"], x["state"]) for x in report["segments"]],
                         [(12, 16, "DRY"), (16, 17, "UNKNOWN"), (17, 21, "WET_RISK"),
                          (21, 25, "DRY"), (25, 26, "UNKNOWN")])
        self.assertEqual(audit.audit(self.db)["decision"], "MATCH")

    def test_bad_reading_rolls_back_envelope(self):
        envelope = read_json(FIXTURES / "environment-readings.json")
        envelope["readings"][1]["dewpoint_tenths"] = 500
        with self.assertRaises(RuleError):
            self.action("environment-record", **envelope)
        self.assertEqual(conditioning.trace(self.db, "DRY-1", 12, 18, 4)["decision"], "UNKNOWN")
