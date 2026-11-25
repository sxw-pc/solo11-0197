"""Slot custody and per-box environment certificate acceptance tests.

The scenario follows the slot hand-over brief: one preparation chamber has a
LEFT and a RIGHT slot holding two wafer boxes.  At minute 12 the LEFT dew
point is below the stage temperature while the RIGHT dew point is above it;
by minute 15 both sides are dry.
"""
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from wafer_retest import audit, cli, conditioning, environment
from wafer_retest.db import RuleError

from tests.ledger_case import FIXTURES, LedgerCase, read_json


CHAMBER = "PREP-SLOT"
SURFACE = 180  # 18.0 C in tenths of a degree


def reading(identity, minute, dewpoint, slot, surface=SURFACE):
    return {
        "id": identity, "minute": minute, "air_tenths": 200,
        "surface_tenths": surface, "dewpoint_tenths": dewpoint, "slot": slot,
    }


def acceptance_readings():
    return [
        reading("M12-LEFT", 12, 160, "LEFT"),
        reading("M12-RIGHT", 12, 185, "RIGHT"),
        reading("M15-LEFT", 15, 160, "LEFT"),
        reading("M15-RIGHT", 15, 160, "RIGHT"),
    ]


class LegacyComparisonTests(LedgerCase):
    """The requested before/after behaviour comparison."""

    def test_current_format_separate_chambers_split_decisions(self):
        for chamber, dewpoint, expected in (("LEFT", 160, "DRY"), ("RIGHT", 185, "WET_RISK")):
            envelope = {
                "chamber": chamber, "monitor": "M-1",
                "readings": [{
                    "id": f"{chamber}-12", "minute": 12, "air_tenths": 200,
                    "surface_tenths": SURFACE, "dewpoint_tenths": dewpoint,
                }],
            }
            self.action("environment-record", **envelope)
            report = conditioning.trace(self.db, chamber, 12, 16, 4)
            self.assertEqual(report["decision"], expected)

    def test_current_format_shared_chamber_is_unknown(self):
        self.action("environment-record", chamber="BOTH", monitor="M-1", readings=[
            {"id": "L", "minute": 12, "air_tenths": 200,
             "surface_tenths": SURFACE, "dewpoint_tenths": 160},
            {"id": "R", "minute": 12, "air_tenths": 200,
             "surface_tenths": SURFACE, "dewpoint_tenths": 185},
        ])
        report = conditioning.trace(self.db, "BOTH", 12, 16, 4)
        self.assertEqual(report["decision"], "UNKNOWN")

    def test_old_environment_fixture_still_imports_and_traces(self):
        envelope = read_json(FIXTURES / "environment-readings.json")
        self.assertEqual(self.action("environment-record", **envelope)["new_readings"], 3)
        self.assertEqual(conditioning.trace(self.db, "DRY-1", 12, 26, 4)["decision"], "WET_RISK")


class SlotIsolationTests(LedgerCase):
    def record_acceptance(self):
        self.action("environment-record", chamber=CHAMBER, monitor="CM-2",
                    readings=acceptance_readings())

    def test_slot_trace_isolates_the_two_sides(self):
        self.record_acceptance()
        left = conditioning.trace(self.db, CHAMBER, 12, 15, 3, slot="LEFT")
        right = conditioning.trace(self.db, CHAMBER, 12, 15, 3, slot="RIGHT")
        self.assertEqual(left["decision"], "DRY")
        self.assertEqual(right["decision"], "WET_RISK")
        for slot, expected in (("LEFT", "DRY"), ("RIGHT", "DRY")):
            self.assertEqual(
                conditioning.trace(self.db, CHAMBER, 15, 18, 3, slot=slot)["decision"],
                expected)

    def test_envelope_level_slot_is_supported(self):
        self.action("environment-record", chamber=CHAMBER, monitor="ML", slot="LEFT",
                    readings=[{"id": "A", "minute": 12, "air_tenths": 200,
                               "surface_tenths": 180, "dewpoint_tenths": 160}])
        self.assertEqual(
            conditioning.trace(self.db, CHAMBER, 12, 15, 3, slot="LEFT")["decision"], "DRY")

    def test_batch_pending_without_verifiable_custody(self):
        self.record_acceptance()
        batch = conditioning.batch_certificate(self.db, CHAMBER, 12, 18, 3)
        self.assertFalse(batch["certified"])
        self.assertEqual(batch["mode"], "CHAMBER_ONLY")
        self.assertEqual(batch["decision"], "PENDING")
        self.assertIn("WET_RISK", batch["reasons"])
        # The dry post-15 window itself holds no risk history.
        self.assertTrue(conditioning.batch_certificate(self.db, CHAMBER, 15, 18, 3)["certified"])
        # No evidence at all is an environmental gap, hence pending.
        empty = conditioning.batch_certificate(self.db, "NEVER-SEEN", 12, 18, 3)
        self.assertFalse(empty["certified"])
        self.assertIn("ENVIRONMENT_GAP", empty["reasons"])


class FixtureAcceptanceTests(LedgerCase):
    def import_slot_fixture(self):
        return self.fixture("environment-slots")

    def test_fixture_imports_repeatably_and_audits(self):
        results = self.import_slot_fixture()
        self.assertEqual(results[0]["new_readings"], 4)
        self.assertEqual(audit.audit(self.db)["decision"], "MATCH")
        again = self.import_slot_fixture()
        self.assertEqual(again[0]["new_readings"], 0)
        self.assertEqual(audit.audit(self.db)["decision"], "MATCH")

    def test_batch_pending_left_issued_right_waits(self):
        self.import_slot_fixture()
        batch = conditioning.batch_certificate(self.db, CHAMBER, 12, 18, 3)
        self.assertFalse(batch["certified"])
        self.assertIn("RECOVERY_REQUIRED", batch["reasons"])

        left = conditioning.certificate(self.db, CHAMBER, "BOX-LEFT", 12, 18, 3)
        self.assertTrue(left["certified"], left["reasons"])
        self.assertEqual(left["decision"], "CERTIFIED")
        # Compatibility with the release ledger vocabulary.
        self.assertEqual(left["decision"], "READY")
        self.assertTrue(left["can_reopen"])
        self.assertEqual(
            [(segment["start"], segment["end"], segment["state"], segment["slot"])
             for segment in left["segments"]],
            [(12, 18, "DRY", "LEFT")])

        right = conditioning.certificate(self.db, CHAMBER, "BOX-RIGHT", 12, 18, 3)
        self.assertFalse(right["certified"])
        self.assertEqual(right["decision"], "PENDING")
        self.assertEqual(right["decision"], "HOLD")
        self.assertEqual(right["last_risk_minute"], 14)
        self.assertIn("RECOVERY_REQUIRED", right["reasons"])
        # Batch stays pending while the right box waits.
        self.assertFalse(conditioning.batch_certificate(self.db, CHAMBER, 12, 18, 3)["certified"])

    def test_max_age_four_tells_the_same_story(self):
        self.import_slot_fixture()
        self.assertTrue(conditioning.certificate(self.db, CHAMBER, "BOX-LEFT", 12, 18, 4)["certified"])
        right = conditioning.certificate(self.db, CHAMBER, "BOX-RIGHT", 12, 18, 4)
        self.assertFalse(right["certified"])
        # A newer dry sample supersedes the minute-12 wet sample even while the
        # old sample would still be inside its nominal max-age window.
        self.action("dry-recovery", recovery="RC4", chamber=CHAMBER, slot="RIGHT",
                    box="BOX-RIGHT", starts=15, ends=18, signers=["CHEN", "PARK"])
        self.assertTrue(
            conditioning.certificate(self.db, CHAMBER, "BOX-RIGHT", 12, 18, 4)["certified"])


class RecoverySignoffTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.fixture("environment-slots")

    def sign(self, recovery="RC-DUAL", **changes):
        args = {
            "recovery": recovery, "chamber": CHAMBER, "slot": "RIGHT", "box": "BOX-RIGHT",
            "starts": 15, "ends": 18, "signers": ["CHEN", "PARK"],
        }
        args.update(changes)
        return self.action("dry-recovery", **args)

    def test_single_signature_does_not_clear(self):
        self.sign(recovery="RC-ONE", signers=["CHEN"])
        right = conditioning.certificate(self.db, CHAMBER, "BOX-RIGHT", 12, 18, 3)
        self.assertFalse(right["certified"])
        report = right["recoveries"][0]
        self.assertFalse(report["valid"])
        self.assertIn("RECOVERY_NOT_DUAL_SIGNED", report["reasons"])

    def test_two_minute_recovery_is_too_short(self):
        self.sign(recovery="RC-SHORT", starts=15, ends=17)
        right = conditioning.certificate(self.db, CHAMBER, "BOX-RIGHT", 12, 18, 3)
        self.assertFalse(right["certified"])
        self.assertIn("RECOVERY_TOO_SHORT", right["reasons"])

    def test_recovery_overlapping_last_risk_is_rejected(self):
        # [14, 17) starts on the last effective wet minute and includes it.
        self.sign(recovery="RC-EARLY", starts=14, ends=17)
        right = conditioning.certificate(self.db, CHAMBER, "BOX-RIGHT", 12, 18, 3)
        self.assertFalse(right["certified"])
        report = right["recoveries"][0]
        self.assertIn("RECOVERY_NOT_AFTER_LAST_RISK", report["reasons"])
        self.assertIn("RECOVERY_NOT_CONTINUOUSLY_DRY", report["reasons"])

    def test_dual_signed_three_minute_dry_recovery_issues_certificate(self):
        self.sign()
        right = conditioning.certificate(self.db, CHAMBER, "BOX-RIGHT", 12, 18, 3)
        self.assertTrue(right["certified"], right["reasons"])
        self.assertEqual(right["recovery"]["recovery"], "RC-DUAL")
        self.assertEqual(right["recovery"]["minutes"], 3)
        batch = conditioning.batch_certificate(self.db, CHAMBER, 12, 18, 3)
        self.assertTrue(batch["certified"], batch["reasons"])
        self.assertEqual(audit.audit(self.db)["decision"], "MATCH")

    def test_redelivery_does_not_lengthen_recovery(self):
        first = self.sign()
        self.assertTrue(first["changed"])
        second = self.sign()
        self.assertFalse(second["changed"])
        rows = environment.recoveries(self.db, chamber=CHAMBER, box="BOX-RIGHT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ends"] - rows[0]["starts"], 3)
        with self.assertRaises(RuleError):
            self.sign(ends=19)
        right = conditioning.certificate(self.db, CHAMBER, "BOX-RIGHT", 12, 18, 3)
        self.assertTrue(right["certified"])


class RecoveryContinuityTests(LedgerCase):
    def test_unknown_minutes_cannot_be_stitched_into_recovery(self):
        self.action("environment-record", chamber="DRY-GAP", monitor="M", readings=[
            reading("D15", 15, 160, "S"), reading("D17", 17, 160, "S")])
        self.action("slot-handover", handover="H", chamber="DRY-GAP", slot="S",
                    box="B", starts=15, ends=18, signers=["A", "B"])
        self.action("dry-recovery", recovery="RC", chamber="DRY-GAP", slot="S",
                    box="B", starts=15, ends=18, signers=["A", "B"])
        report = conditioning.certificate(self.db, "DRY-GAP", "B", 15, 18, 1)
        self.assertFalse(report["certified"])
        self.assertIn("ENVIRONMENT_GAP", report["reasons"])
        recovery = report["recoveries"][0]
        self.assertIn("RECOVERY_NOT_CONTINUOUSLY_DRY", recovery["reasons"])
        self.assertEqual([item["minute"] for item in recovery["unsupported_minutes"]], [16])
        # A real dry sample for the missing minute closes the gap.
        self.action("environment-record", chamber="DRY-GAP", monitor="M",
                    readings=[reading("D16", 16, 160, "S")])
        self.assertTrue(
            conditioning.certificate(self.db, "DRY-GAP", "B", 15, 18, 1)["certified"])

    def test_moving_to_a_dry_position_does_not_auto_clear_risk(self):
        self.action("environment-record", chamber="MOVE", monitor="M", readings=[
            reading("R12", 12, 185, "RIGHT"),
            reading("L15", 15, 160, "LEFT"),
            reading("R15", 15, 160, "RIGHT"),
        ])
        self.action("slot-handover", handover="HR", chamber="MOVE", slot="RIGHT",
                    box="BX", starts=12, ends=15, signers=["A", "B"])
        self.action("slot-handover", handover="HL", chamber="MOVE", slot="LEFT",
                    box="BX", starts=15, ends=18, signers=["A", "B"])
        report = conditioning.certificate(self.db, "MOVE", "BX", 12, 18, 3)
        self.assertFalse(report["certified"])
        self.assertIn("RECOVERY_REQUIRED", report["reasons"])
        # Recovery must be supported at the slot the box actually occupies.
        self.action("dry-recovery", recovery="RC", chamber="MOVE", slot="LEFT",
                    box="BX", starts=15, ends=18, signers=["A", "B"])
        self.assertTrue(conditioning.certificate(self.db, "MOVE", "BX", 12, 18, 3)["certified"])

    def test_recovery_in_wrong_slot_is_not_supported(self):
        self.action("environment-record", chamber="WRONG", monitor="M", readings=[
            reading("R12", 12, 185, "RIGHT"), reading("R15", 15, 160, "RIGHT")])
        self.action("slot-handover", handover="HR", chamber="WRONG", slot="RIGHT",
                    box="B", starts=12, ends=18, signers=["A", "B"])
        # Paper recovery names LEFT while custody keeps the box on RIGHT.
        self.action("dry-recovery", recovery="RC", chamber="WRONG", slot="LEFT",
                    box="B", starts=15, ends=18, signers=["A", "B"])
        report = conditioning.certificate(self.db, "WRONG", "B", 12, 18, 3)
        self.assertFalse(report["certified"])
        recovery = report["recoveries"][0]
        self.assertIn("RECOVERY_NOT_CONTINUOUSLY_DRY", recovery["reasons"])


class AttributionConflictTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.action("environment-record", chamber="C", monitor="M",
                    readings=acceptance_readings())

    def test_unattributed_minutes_stay_pending(self):
        self.action("slot-handover", handover="H1", chamber="C", slot="LEFT",
                    box="B", starts=12, ends=16, signers=["A", "B"])
        report = conditioning.certificate(self.db, "C", "B", 12, 18, 3)
        self.assertFalse(report["certified"])
        self.assertIn("SLOT_ATTRIBUTION_MISSING", report["reasons"])

    def test_overlapping_slots_for_one_box_conflict(self):
        self.action("slot-handover", handover="H1", chamber="C", slot="LEFT",
                    box="B", starts=12, ends=16, signers=["A", "B"])
        self.action("slot-handover", handover="H2", chamber="C", slot="RIGHT",
                    box="B", starts=13, ends=15, signers=["A", "B"])
        report = conditioning.certificate(self.db, "C", "B", 12, 18, 3)
        self.assertIn("SLOT_ATTRIBUTION_CONFLICT", report["reasons"])
        self.assertFalse(report["certified"])

    def test_two_boxes_in_one_slot_is_custody_conflict(self):
        for handover, box in (("H1", "B1"), ("H2", "B2")):
            self.action("slot-handover", handover=handover, chamber="C", slot="LEFT",
                        box=box, starts=12, ends=18, signers=["A", "B"])
        for box in ("B1", "B2"):
            report = conditioning.certificate(self.db, "C", box, 12, 18, 3)
            self.assertIn("SLOT_CUSTODY_CONFLICT", report["reasons"])
            self.assertFalse(report["certified"])
        batch = conditioning.batch_certificate(self.db, "C", 12, 18, 3)
        self.assertFalse(batch["certified"])

    def test_batch_requires_coverage_of_effective_slots(self):
        # Only the LEFT box is handed over; RIGHT monitor evidence stays
        # unattributed, so the joint certificate must remain pending.
        self.action("slot-handover", handover="HL", chamber="C", slot="LEFT",
                    box="BL", starts=12, ends=18, signers=["A", "B"])
        batch = conditioning.batch_certificate(self.db, "C", 12, 18, 3)
        self.assertFalse(batch["certified"])
        self.assertIn("SLOT_ATTRIBUTION_MISSING", batch["reasons"])
        self.assertTrue(conditioning.certificate(self.db, "C", "BL", 12, 18, 3)["certified"])


class CallShapeRegressionTests(LedgerCase):
    def test_slot_trace_accepts_window_before_slot_keyword(self):
        self.action("environment-record", chamber="C", monitor="M", readings=[
            reading("a", 12, 160, "LEFT")])
        self.assertEqual(
            conditioning.slot_trace(self.db, "C", 12, 15, 3, slot="LEFT")["decision"], "DRY")
        self.assertEqual(
            conditioning.slot_trace(self.db, "C", "LEFT", 12, 15, 3)["decision"], "DRY")

    def test_cold_catalog_positional_slot_box_orders(self):
        # No readings registered yet: naming hints still resolve the order.
        environment.handover(self.db, "H1", "C", "BOX-X", "LEFT", 12, 18, ["A", "B"])
        handover = environment.handovers(self.db)[0]
        self.assertEqual((handover["slot"], handover["box"]), ("LEFT", "BOX-X"))
        environment.recovery(self.db, "R1", "C", "BOX-Y", "RIGHT", 15, 18, ["A", "B"])
        recovery = environment.recoveries(self.db, box="BOX-Y")[0]
        self.assertEqual((recovery["slot"], recovery["box"]), ("RIGHT", "BOX-Y"))

    def test_fully_dry_legacy_chamber_certifies_batch(self):
        self.action("environment-record", chamber="DRY", monitor="M", readings=[
            {"id": "a", "minute": 12, "air_tenths": 200,
             "surface_tenths": 180, "dewpoint_tenths": 160}])
        self.assertEqual(conditioning.trace(self.db, "DRY", 12, 15, 3)["decision"], "DRY")
        self.assertTrue(
            conditioning.batch_certificate(self.db, "DRY", 12, 15, 3)["certified"])
        # A chamber with no evidence at all still blocks.
        self.assertFalse(
            conditioning.batch_certificate(self.db, "NEVER", 12, 15, 3)["certified"])


class CliFlowTests(LedgerCase):
    def test_cli_load_and_certificate_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "slot.db")

            def run_cli(*argv):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = cli.main(["--db", db_path, *argv])
                self.assertEqual(code, 0)
                return json.loads(buffer.getvalue())

            self.assertEqual(run_cli("init")["schema"], 1)
            loaded = run_cli("load", str(FIXTURES / "environment-slots.json"))
            self.assertEqual(loaded[0]["new_readings"], 4)
            batch = run_cli("batch-certificate", CHAMBER, "12", "18", "--max-age", "3")
            self.assertEqual(batch["decision"], "PENDING")
            left = run_cli("box-certificate", CHAMBER, "BOX-LEFT", "12", "18", "--max-age", "3")
            self.assertEqual(left["decision"], "CERTIFIED")
            # Legacy single-envelope intake and chamber query still work.
            record = run_cli("environment-record", str(FIXTURES / "environment-readings.json"))
            self.assertEqual(record["new_readings"], 3)
            trace = run_cli("environment-trace", "DRY-1", "12", "26", "--max-age", "4")
            self.assertEqual(trace["decision"], "WET_RISK")
