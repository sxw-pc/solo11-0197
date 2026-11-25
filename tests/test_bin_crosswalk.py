"""Regression for the ``bin-crosswalk`` old->new probe dictionary intake.

Acceptance specimen (synthetic):

* source WAT code ``0`` PASS on the closed interval ``[90, 110]``;
* target code ``L`` PASS on ``[90, 100]`` and ``H`` PASS on ``[101, 110]``.

The tests pin closed-endpoint handling, grade+basis equivalence, the fixed
table refusal to promise code ``0`` over its whole declared range, per-value
map conversion with provenance, interval-only slips (``[100, 101]`` must be
pending retest), unknown/no-equivalent review reasons, consumption of the
deliverable by the original ``map``/``load`` intake commands, and the release
FAIL-total difference between a naive "everything to L" map and the correct
per-measurement crosswalk on the existing ``fixtures/baseline.json`` map.
"""

import copy
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase

from wafer_retest import bin_crosswalk as xw
from wafer_retest.cli import load, main, read_json
from wafer_retest.db import connect, initialize, transaction
from wafer_retest import workflow

from tests.ledger_case import FIXTURES, LOT, LedgerCase


CROSSWALK_FIXTURE = FIXTURES / "bin-crosswalk.json"
BASELINE_FIXTURE = FIXTURES / "baseline.json"
REQUEST = read_json(CROSSWALK_FIXTURE)
SOURCE = xw.coerce_dictionary(REQUEST["source_dictionary"])
TARGET = xw.coerce_dictionary(REQUEST["target_dictionary"])

BASELINE_OPS = read_json(BASELINE_FIXTURE)
SOURCE_BIN1_RULES = next(op["args"]["rules"] for op in BASELINE_OPS if op["command"] == "dictionary")
BASE_MAP_ARGS = copy.deepcopy(next(op["args"] for op in BASELINE_OPS if op["command"] == "map"))
BASE_ANCHORS = copy.deepcopy(next(op["args"]["anchors"] for op in BASELINE_OPS if op["command"] == "anchors"))

# Target dictionary for the LOT-P7 release comparison: same ET bins, split WAT.
RELEASE_TARGET_RULES = [
    {"channel": "ET", "code": "0", "grade": "PASS"},
    {"channel": "ET", "code": "1", "grade": "MARGINAL"},
    {"channel": "ET", "code": "9", "grade": "FAIL"},
    {"channel": "WAT", "code": "L", "grade": "PASS", "lo": 90, "hi": 100},
    {"channel": "WAT", "code": "H", "grade": "PASS", "lo": 101, "hi": 110},
]
RELEASE_SOURCE = xw.coerce_dictionary(
    {"dictionary": "BIN-1", "state": "SEALED", "rules": SOURCE_BIN1_RULES})
RELEASE_TARGET = xw.coerce_dictionary(
    {"dictionary": "BIN-NEW", "state": "SEALED", "rules": RELEASE_TARGET_RULES})


def wat_decision(value, old_code="0"):
    return xw.convert_point(SOURCE, TARGET, "WAT", old_code, value)


class EquivalenceProofTests(TestCase):
    def test_endpoints_are_closed_and_split_at_100_101(self):
        # Both endpoints follow dictionary.classify's closed interpretation.
        self.assertEqual(wat_decision(90)["new_code"], "L")
        self.assertEqual(wat_decision(100)["new_code"], "L")
        self.assertEqual(wat_decision(101)["new_code"], "H")
        self.assertEqual(wat_decision(110)["new_code"], "H")
        for value, code in ((90, "L"), (100, "L"), (101, "H"), (110, "H")):
            decision = wat_decision(value)
            self.assertTrue(decision["equivalent"])
            self.assertEqual((decision["source_grade"], decision["source_basis"]),
                             ("PASS", "BIN_RULE"))
            self.assertEqual((decision["target_grade"], decision["target_basis"]),
                             ("PASS", "BIN_RULE"))
            self.assertEqual(decision["new_code"], code)

    def test_out_of_range_values_keep_wat_limit_evidence(self):
        low = wat_decision(89)
        high = wat_decision(111)
        self.assertEqual(low["new_code"], "L")
        self.assertEqual(high["new_code"], "H")
        for decision in (low, high):
            self.assertEqual((decision["source_grade"], decision["source_basis"]),
                             ("FAIL", "WAT_LIMIT"))
            self.assertEqual((decision["target_grade"], decision["target_basis"]),
                             ("FAIL", "WAT_LIMIT"))
            self.assertTrue(decision["evidence_preserved"])

    def test_native_fail_bin_cannot_smuggle_wat_limit_failure(self):
        # Target F is a native FAIL (BIN_RULE).  Grade matches but basis does
        # not, so it is not an equivalent code for an out-of-bounds reading.
        self.assertEqual(xw.classify_pair(TARGET, "WAT", "F", 89), ("FAIL", "BIN_RULE"))
        decision = wat_decision(89)
        self.assertNotIn("F", decision["candidate_new_codes"])
        self.assertIn("L", decision["candidate_new_codes"])
        self.assertIn("H", decision["candidate_new_codes"])

    def test_fixed_table_refuses_single_code_for_code_zero(self):
        row = next(r for r in xw.fixed_table(SOURCE, TARGET)
                   if r["channel"] == "WAT" and r["old_code"] == "0")
        self.assertFalse(row["fixed"])
        self.assertIsNone(row["new_code"])
        self.assertEqual(row["reason"], "SPLIT_REQUIRED")
        proof = row["proof"]
        self.assertEqual([(p["lo"], p["hi"], p["new_code"]) for p in proof],
                         [(90, 100, "L"), (101, 110, "H")])
        for part in proof:
            self.assertTrue(part["uniform"])
            self.assertEqual(part["source_grade"], "PASS")
            self.assertEqual(part["source_basis"], "BIN_RULE")
            self.assertEqual(part["target_grade"], "PASS")
            self.assertEqual(part["target_basis"], "BIN_RULE")
            self.assertTrue(part["equivalent"])
            self.assertEqual(part["closed_endpoints"], (True, True))

    def test_code_only_bins_are_fixed_identity(self):
        burn = {(r["channel"], r["old_code"]): r["new_code"] for r in xw.burn_table(SOURCE, TARGET)}
        self.assertEqual(burn[("ET", "0")], "0")
        self.assertEqual(burn[("ET", "1")], "1")
        self.assertEqual(burn[("ET", "9")], "9")
        row = next(r for r in xw.fixed_table(SOURCE, TARGET)
                   if r["channel"] == "ET" and r["old_code"] == "0")
        self.assertTrue(row["fixed"])

    def test_unmatched_raw_interval_is_reported(self):
        gaps = xw.unmatched_intervals(SOURCE, TARGET)
        wat_gaps = [g for g in gaps if g["channel"] == "WAT" and g["old_code"] == "7"]
        self.assertEqual(len(wat_gaps), 1)
        gap = wat_gaps[0]
        self.assertEqual((gap["lo"], gap["hi"]), (40, 60))
        self.assertEqual((gap["grade"], gap["basis"]), ("MARGINAL", "BIN_RULE"))
        self.assertEqual(gap["reason"], "NO_EQUIVALENT_NEW_CODE")

    def test_unknown_old_code_is_distinct_pending_reason(self):
        decision = wat_decision(95, old_code="404")
        self.assertEqual(decision["status"], "review")
        self.assertTrue(decision["needs_review"])
        self.assertEqual(decision["reason"], "UNKNOWN_OLD_CODE")
        self.assertIn("UNKNOWN_BIN", decision["reason_tokens"])

    def test_no_equivalent_new_code_pending_reason(self):
        decision = wat_decision(50, old_code="7")
        self.assertEqual(decision["status"], "review")
        self.assertEqual(decision["reason"], "NO_EQUIVALENT_NEW_CODE")
        self.assertIn("NO_EQUIVALENT_BIN", decision["reason_tokens"])


class IntervalSlipTests(TestCase):
    def test_100_101_uncertainty_span_needs_retest(self):
        verdict = xw.verify_interval(SOURCE, TARGET, "WAT", "0", 100, 101)
        self.assertEqual(verdict["status"], "retest")
        self.assertTrue(verdict["needs_retest"])
        self.assertFalse(verdict["map_written"])
        self.assertEqual(verdict["common_new_codes"], [])

    def test_uniform_span_is_verified_but_never_written(self):
        verdict = xw.verify_interval(SOURCE, TARGET, "WAT", "0", 95, 97)
        self.assertEqual(verdict["status"], "verified")
        self.assertEqual(verdict["new_code"], "L")
        self.assertFalse(verdict["map_written"])

    def test_interval_spanning_split_endpoint_only_fails(self):
        # Closed endpoints: [100,101] straddles the L/H split; singleton
        # [100,100] and [101,101] each have exactly one valid code.
        self.assertEqual(xw.verify_interval(SOURCE, TARGET, "WAT", "0", 100, 100)["new_code"], "L")
        self.assertEqual(xw.verify_interval(SOURCE, TARGET, "WAT", "0", 101, 101)["new_code"], "H")
        self.assertEqual(xw.verify_interval(SOURCE, TARGET, "WAT", "0", 100, 101)["status"], "retest")

    def test_interval_without_equivalent_code_is_review(self):
        verdict = xw.verify_interval(SOURCE, TARGET, "WAT", "7", 45, 55)
        self.assertEqual(verdict["status"], "review")
        self.assertEqual(verdict["reason"], "NO_EQUIVALENT_NEW_CODE")

    def test_value_less_measurement_never_fabricates_integer(self):
        rows = [
            {"id": "A", "channel": "WAT", "code": "0", "value_range": [100, 101]},
            {"id": "B", "channel": "WAT", "code": "0", "range": [95, 97]},
        ]
        converted, decisions = xw.convert_measurements(SOURCE, TARGET, rows)
        self.assertEqual(converted, [])  # no invented value, no map row
        self.assertEqual({d["id"]: d["status"] for d in decisions}, {"A": "retest", "B": "verified"})
        self.assertTrue(all(d["map_written"] is False for d in decisions))

    def test_illegal_interval_is_invalid_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            xw.extract_reading({"range": [101, 100]})
        kind, lo, hi = xw.extract_reading({"lo": 90, "hi": 110})
        self.assertEqual((kind, lo, hi), ("interval", 90, 110))


class FixtureReportTests(TestCase):
    def test_fixture_report_summary_and_provenance(self):
        report = xw.crosswalk(
            REQUEST["source_dictionary"], REQUEST["target_dictionary"],
            old_map=REQUEST["old_map"], interval_readings=REQUEST["interval_readings"])
        self.assertEqual(report["summary"]["converted"], 8)
        self.assertEqual(report["summary"]["pending_review"], 4)
        self.assertEqual(report["summary"]["retest"], 1)
        self.assertEqual(report["summary"]["unmatched_intervals"], 1)
        self.assertTrue(report["dictionaries_sealed"])
        # The [100,101] span is 待复测 and also sits in the broad 待核 bucket.
        split = next(d for d in report["decisions"] if d.get("id") == "RNG-100-101")
        self.assertEqual(split["status"], "retest")
        self.assertTrue(split["pending_verification"] and split["needs_review"])
        self.assertIn("RNG-100-101", [d["id"] for d in report["pending_verification"]])
        # JSON round trip keeps the whole deliverable serializable.
        text = json.dumps(report, ensure_ascii=False)
        self.assertIn("BIN-OLD-XWALK", text)

    def test_converted_rows_preserve_value_old_code_and_source_version(self):
        args, decisions = xw.convert_map_args(SOURCE, TARGET, REQUEST["old_map"])
        by_id = {row["id"]: row for row in args["measurements"]}
        self.assertEqual(len(args["measurements"]), 8)
        cases = {"W90": "L", "W100": "L", "W101": "H", "W110": "H", "W89": "L", "W111": "H"}
        for measurement_id, new_code in cases.items():
            row = by_id[measurement_id]
            original = next(m for m in REQUEST["old_map"]["measurements"] if m["id"] == measurement_id)
            self.assertEqual(row["code"], new_code)
            self.assertEqual(row["value"], original["value"])  # measured integer preserved
            self.assertEqual(row["old_code"], "0")
            self.assertEqual(row["source_code"], "0")
            self.assertEqual(row["source_dictionary"], "BIN-OLD-XWALK")
            self.assertEqual(row["source_dictionary_version"], "BIN-OLD-XWALK")
            self.assertEqual(row["crosswalk"]["source_value"], original["value"])
        # Pending readings are not delivered in the clean map by default.
        self.assertNotIn("WGAP", by_id)
        self.assertNotIn("WUNK", by_id)

    def test_map_args_top_level_only_use_ingest_keywords(self):
        args, _ = xw.convert_map_args(SOURCE, TARGET, REQUEST["old_map"])
        self.assertEqual(
            set(args),
            {"session", "lot", "card", "wafer", "measured_at", "pitch_u", "pitch_v",
             "origin_u", "origin_v", "plan", "measurements"})

    def test_committed_deliverable_matches_tool_output(self):
        committed = read_json(FIXTURES / "bin-crosswalk-map.json")
        args, _ = xw.convert_map_args(SOURCE, TARGET, REQUEST["old_map"])
        self.assertEqual(committed, args)


class IntakeConsumptionTests(LedgerCase):
    def test_converted_map_is_consumed_by_original_map_command(self):
        self.baseline()  # LOT-P7, W01, PC-1 and qualifications already registered
        args, _ = xw.convert_map_args(SOURCE, TARGET, REQUEST["old_map"])
        result = self.action("map", **args)
        self.assertTrue(result["changed"])
        count = self.db.execute(
            "SELECT count(*) FROM measurements WHERE session=?", ("XWALK-SAMPLE",)).fetchone()[0]
        self.assertEqual(count, 8)

    def test_converted_batch_is_consumed_by_load(self):
        self.baseline()
        report = xw.crosswalk(
            REQUEST["source_dictionary"], REQUEST["target_dictionary"], old_map=REQUEST["old_map"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "converted.load.json"
            xw.write_json(path, report["converted_batch"])
            with transaction(self.db):
                results = load(self.db, path)
        self.assertEqual(results[0]["session"], "XWALK-SAMPLE")
        self.assertTrue(results[0]["changed"])
        count = self.db.execute(
            "SELECT count(*) FROM measurements WHERE session=?", ("XWALK-SAMPLE",)).fetchone()[0]
        self.assertEqual(count, 8)

    def test_cli_bin_crosswalk_outputs_files_consumable_by_map(self):
        def invoke(argv):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                rc = main(argv)
            return rc, buffer.getvalue()

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            database = directory / "ops.db"
            out_map = directory / "new-machine-map.json"
            batch = directory / "new-machine-map.load.json"
            report_file = directory / "report.json"
            self.assertEqual(invoke(["--db", str(database), "init"])[0], 0)
            self.assertEqual(invoke(["--db", str(database), "load", str(BASELINE_FIXTURE)])[0], 0)
            rc, _ = invoke(["--db", str(database), "bin-crosswalk", str(CROSSWALK_FIXTURE),
                            "-o", str(out_map), "--batch-output", str(batch),
                            "--report", str(report_file)])
            self.assertEqual(rc, 0)
            rc, output = invoke(["--db", str(database), "map", str(out_map)])
            self.assertEqual(rc, 0)
            consumed = json.loads(output)
            self.assertTrue(consumed["changed"])
            self.assertEqual(consumed["session"], "XWALK-SAMPLE")
            self.assertTrue(out_map.exists() and batch.exists() and report_file.exists())
            self.assertEqual(json.loads(out_map.read_text(encoding="utf-8"))["session"], "XWALK-SAMPLE")

    def test_standalone_module_main_writes_output_and_strict_flags_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out.json"
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                rc_ok = xw.main([str(CROSSWALK_FIXTURE), "-o", str(out)])
                self.assertEqual(rc_ok, 0)
                self.assertTrue(out.exists())
                rc_strict = xw.main([str(CROSSWALK_FIXTURE), "--strict"])
            self.assertEqual(rc_strict, 2)

    def test_positional_dictionary_map_output_form(self):
        request = read_json(CROSSWALK_FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            src = directory / "source.json"
            tgt = directory / "target.json"
            old_map = directory / "old-map.json"
            out = directory / "converted.json"
            xw.write_json(src, request["source_dictionary"])
            xw.write_json(tgt, request["target_dictionary"])
            xw.write_json(old_map, request["old_map"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                rc = xw.main([str(src), str(tgt), str(old_map), str(out)])
            self.assertEqual(rc, 0)
            converted = read_json(out)
            self.assertEqual(len(converted["measurements"]), 8)
            self.assertEqual(
                [m["code"] for m in converted["measurements"] if m["id"] in ("W90", "W101")],
                ["L", "H"])


def seed_target_lot(db):
    for op in BASELINE_OPS:
        if op["command"] in ("lot", "card", "qualify"):
            workflow.apply(db, op["command"], copy.deepcopy(op["args"]))
    workflow.apply(db, "dictionary", {"dictionary": "BIN-NEW", "rules": RELEASE_TARGET_RULES})
    workflow.apply(db, "seal-dictionary", {"dictionary": "BIN-NEW"})
    workflow.apply(db, "bind-dictionary", {"lot": LOT, "dictionary": "BIN-NEW"})


def atlas_for(map_args):
    """Ingest one converted map into a fresh target-dictionary ledger."""
    db = connect(":memory:")
    try:
        initialize(db)
        with transaction(db):
            seed_target_lot(db)
            workflow.apply(db, "map", copy.deepcopy(map_args))
            workflow.apply(db, "anchors", {
                "session": map_args["session"], "anchors": copy.deepcopy(BASE_ANCHORS)})
            result = workflow.apply(db, "recompute", {"lot": LOT})[0]
        return result["manifest"], result["digest"]
    finally:
        db.close()


def all_to_l_args(value_overrides=None):
    args = copy.deepcopy(BASE_MAP_ARGS)
    for measurement in args["measurements"]:
        if measurement["channel"] == "WAT":
            if value_overrides and measurement["id"] in value_overrides:
                measurement["value"] = value_overrides[measurement["id"]]
            measurement["code"] = "L"  # naive fixed table: code 0 -> L everywhere
    return args


def correct_args(value_overrides=None):
    args = copy.deepcopy(BASE_MAP_ARGS)
    if value_overrides:
        for measurement in args["measurements"]:
            if measurement["id"] in value_overrides:
                measurement["value"] = value_overrides[measurement["id"]]
    converted, _ = xw.convert_map_args(RELEASE_SOURCE, RELEASE_TARGET, args)
    return converted


def wat_view(manifest, x, y):
    die = next(d for d in manifest["dies"] if (d["wafer"], d["x"], d["y"]) == ("W01", x, y))
    return die, die["reading"]["channels"]["WAT"]


class ReleasePublishTests(LedgerCase):
    def test_correct_conversion_still_publishes_under_fail_limit_one(self):
        for op in BASELINE_OPS:
            if op["command"] in ("lot", "card", "qualify"):
                self.action(op["command"], **copy.deepcopy(op["args"]))
        self.action("dictionary", dictionary="BIN-NEW", rules=RELEASE_TARGET_RULES)
        self.action("seal-dictionary", dictionary="BIN-NEW")
        self.action("bind-dictionary", lot=LOT, dictionary="BIN-NEW")
        self.action("map", **correct_args())
        self.action("anchors", session="BASE-1", anchors=copy.deepcopy(BASE_ANCHORS))
        ready = self.action("recompute", lot=LOT)["manifest"]
        self.assertEqual(ready["counts"]["FAIL"], 1)
        self.assertEqual(ready["decision"], "READY")
        published = self.action("publish", lot=LOT)
        self.assertEqual(published["revision"], 1)


class ReleaseComparisonTests(TestCase):
    def test_existing_map_all_L_vs_correct_crosswalk_atlas_delta(self):
        naive_manifest, naive_digest = atlas_for(all_to_l_args())
        correct_manifest, correct_digest = atlas_for(correct_args())

        # Existing LOT-P7 fail limit is 1; the only ET FAIL is die (0,0).
        self.assertEqual(naive_manifest["fail_limit"], 1)
        for manifest in (naive_manifest, correct_manifest):
            self.assertEqual(manifest["counts"]["FAIL"], 1)
            self.assertEqual(manifest["counts"]["PASS"], 4)
            self.assertEqual(manifest["blockers"], [])
            self.assertEqual(manifest["decision"], "READY")

        # The atlases are not the same: the 101 shot at die (0,0) flips basis.
        self.assertNotEqual(naive_digest, correct_digest)
        naive_die, naive_wat = wat_view(naive_manifest, 0, 0)
        correct_die, correct_wat = wat_view(correct_manifest, 0, 0)
        self.assertEqual((naive_wat["code"], naive_wat["value"]), ("L", 101))
        self.assertEqual((naive_wat["grade"], naive_wat["rule"]), ("FAIL", "WAT_LIMIT"))
        self.assertEqual((correct_wat["code"], correct_wat["value"]), ("H", 101))
        self.assertEqual((correct_wat["grade"], correct_wat["rule"]), ("PASS", "BIN_RULE"))
        # Die stays FAIL in both because ET code 9 already fails die (0,0).
        self.assertEqual(naive_die["grade"], "FAIL")
        self.assertEqual(correct_die["grade"], "FAIL")

        # Every other WAT shot is equivalent between naive L and correct.
        for x, y in ((0, 1), (1, 0), (1, 1), (2, 0)):
            _, nwat = wat_view(naive_manifest, x, y)
            _, cwat = wat_view(correct_manifest, x, y)
            self.assertEqual((nwat["grade"], nwat["rule"]), ("PASS", "BIN_RULE"))
            self.assertEqual((cwat["grade"], cwat["rule"]), ("PASS", "BIN_RULE"))
            self.assertEqual(cwat["code"], "L")

    def test_naive_all_L_can_raise_fail_total_and_block_release(self):
        # Move a 101 onto die (1,0), whose ET shot passes, so the naive L
        # mapping manufactures a second FAIL die above the limit of 1.
        overrides = {"10-WAT": 101}
        naive_manifest, _ = atlas_for(all_to_l_args(overrides))
        correct_manifest, _ = atlas_for(correct_args(overrides))

        self.assertEqual(naive_manifest["counts"]["FAIL"], 2)
        self.assertIn("FAIL_LIMIT", naive_manifest["blockers"])
        self.assertEqual(naive_manifest["decision"], "HOLD")
        _, bad_wat = wat_view(naive_manifest, 1, 0)
        self.assertEqual((bad_wat["grade"], bad_wat["rule"]), ("FAIL", "WAT_LIMIT"))

        self.assertEqual(correct_manifest["counts"]["FAIL"], 1)
        self.assertEqual(correct_manifest["blockers"], [])
        self.assertEqual(correct_manifest["decision"], "READY")
        _, good_wat = wat_view(correct_manifest, 1, 0)
        self.assertEqual((good_wat["code"], good_wat["grade"]), ("H", "PASS"))

    def test_correct_conversion_reproduces_source_grades_on_existing_map(self):
        # End-to-end through the tool against the real sealed BIN-1 rules: the
        # converted map must preserve every source grade/basis after rebinding.
        source_manifest, _ = self._source_bin1_atlas()
        converted_manifest, _ = atlas_for(correct_args())
        source_grades = sorted(
            (d["x"], d["y"], d["grade"],
             d["reading"]["channels"]["WAT"]["grade"], d["reading"]["channels"]["WAT"]["rule"])
            for d in source_manifest["dies"])
        target_grades = sorted(
            (d["x"], d["y"], d["grade"],
             d["reading"]["channels"]["WAT"]["grade"], d["reading"]["channels"]["WAT"]["rule"])
            for d in converted_manifest["dies"])
        self.assertEqual(source_grades, target_grades)

    def _source_bin1_atlas(self):
        db = connect(":memory:")
        try:
            initialize(db)
            with transaction(db):
                self._seed_full_baseline(db)
                result = workflow.apply(db, "recompute", {"lot": LOT})[0]
            return result["manifest"], result["digest"]
        finally:
            db.close()

    @staticmethod
    def _seed_full_baseline(db):
        for op in BASELINE_OPS:
            workflow.apply(db, op["command"], copy.deepcopy(op["args"]))


if __name__ == "__main__":
    import unittest
    unittest.main()
