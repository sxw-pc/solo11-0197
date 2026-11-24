from wafer_retest import audit, coordinates, release, sampling, workflow
from wafer_retest.cli import demo, read_json
from wafer_retest.db import RuleError, connect, initialize, transaction

from tests.ledger_case import FIXTURES, LOT, PLAN, LedgerCase


class SamplingTests(LedgerCase):
    def test_capacity_has_floors_and_snapshot_does_not_rerank(self):
        self.baseline()
        with self.assertRaisesRegex(RuleError, "every wafer-sector"):
            self.action("create-plan", plan=PLAN, lot=LOT, opens=20, closes=28, capacity=1)
        frozen = self.action("create-plan", plan=PLAN, lot=LOT, opens=20, closes=28, capacity=2)
        self.assertEqual([(row["x"], row["y"], row["risk"]) for row in frozen["targets"]], [(0, 0, "FAIL"), (2, 0, "PASS")])
        self.action("evidence", evidence="left", card="PC-1", starts=10, ends=11,
                    scope="SITE", source="SIGNED", verdict="BLOCK", site=2)
        self.assertEqual(sampling.inspect(self.db, PLAN), frozen)
        evaluated = self.action("recompute", lot=LOT)
        self.assertEqual(self.project_die(evaluated, 1, 1)["state"], "NO_COMPLETE_ATTEMPT")
        self.assertIn("PLAN_OPEN", evaluated["manifest"]["blockers"])

    def test_retest_scope_window_and_late_arrival(self):
        self.baseline()
        self.action("create-plan", plan=PLAN, lot=LOT, opens=20, closes=28, capacity=2)
        self.action("transition-plan", plan=PLAN, state="SEALED")
        self.map_from("retest", "END-EXCLUDED", measured_at=28)
        self.map_from("retest", "UNPLANNED", measured_at=21, plan=None)
        withheld = self.action("recompute", lot=LOT)
        self.assertEqual(self.project_die(withheld, 0, 0)["state"], "RETEST_NO_COMPLETE_ATTEMPT")
        self.assertTrue({"OUTSIDE_PLAN_WINDOW", "BASE_AT_OR_AFTER_PLAN_START"} <= {row["state"] for row in withheld["trace"]})
        self.map_from("retest", "LATE-DELIVERY", measured_at=20)
        current = self.action("recompute", lot=LOT)
        self.assertEqual(self.project_die(current, 0, 0)["reading"]["session"], "LATE-DELIVERY")
        self.assertEqual(self.project_die(current, 1, 0)["grade"], "PASS")
        self.assertIn("NOT_A_TARGET", [row["state"] for row in current["trace"]])

    def test_partial_channels_never_splice_and_disagreement_holds(self):
        self.baseline()
        self.action("create-plan", plan=PLAN, lot=LOT, opens=20, closes=28, capacity=2)
        self.action("transition-plan", plan=PLAN, state="SEALED")
        hits = read_json(FIXTURES / "retest.json")[1]["args"]["measurements"]
        self.map_from("retest", "ET-ONLY", measurements=[row for row in hits if row["channel"] == "ET"])
        self.map_from("retest", "WAT-ONLY", measurements=[row for row in hits if row["channel"] == "WAT"])
        partial = self.action("recompute", lot=LOT)
        self.assertEqual(partial["manifest"]["counts"]["HOLD"], 2)
        self.assertIn("PARTIAL_CHANNELS", [row["state"] for row in partial["trace"]])
        self.map_from("retest", "FULL")
        self.assertEqual(self.action("recompute", lot=LOT)["manifest"]["decision"], "READY")
        changed = [dict(row, code="1") if row["id"] == "00-ET" else row for row in hits]
        self.map_from("retest", "DIFFERENT", measured_at=23, measurements=changed)
        result = self.action("recompute", lot=LOT)
        self.assertEqual(self.project_die(result, 0, 0)["state"], "RETEST_ATTEMPT_DISAGREEMENT")
        self.assertEqual(self.project_die(result, 2, 0)["reading"]["session"], "DIFFERENT")

    def test_void_requires_no_maps_and_does_not_reopen_sealed_plan(self):
        self.baseline()
        self.action("create-plan", plan="UNUSED", lot=LOT, opens=20, closes=28, capacity=2)
        self.action("transition-plan", plan="UNUSED", state="VOID")
        self.action("create-plan", plan=PLAN, lot=LOT, opens=20, closes=28, capacity=2)
        self.map_from("retest", "TEST-PLAN")
        with self.assertRaisesRegex(RuleError, "acquired maps"):
            self.action("transition-plan", plan=PLAN, state="VOID")
        self.action("transition-plan", plan=PLAN, state="SEALED")
        with self.assertRaisesRegex(RuleError, "open sampling plan"):
            self.action("transition-plan", plan=PLAN, state="VOID")

    def test_identical_xy_on_second_wafer_does_not_share_coverage_or_contact(self):
        self.two_wafer_baseline()
        with self.assertRaisesRegex(RuleError, "every wafer-sector"):
            self.action("create-plan", plan=PLAN, lot=LOT, opens=20, closes=28, capacity=3)
        target = self.action("create-plan", plan=PLAN, lot=LOT, opens=20, closes=28, capacity=4)
        self.assertEqual({(row["wafer"], row["sector"]) for row in target["targets"]},
                         {(wafer, sector) for wafer in ("W01", "W02") for sector in ("LEFT", "RIGHT")})
        self.map_from("retest", "RETEST-1")
        self.action("transition-plan", plan=PLAN, state="SEALED")
        self.assertEqual(self.action("recompute", lot=LOT)["manifest"]["counts"]["HOLD"], 2)
        self.map_from("retest", "RETEST-W02", wafer="W02")
        self.assertEqual(self.action("recompute", lot=LOT)["manifest"]["counts"]["PASS"], 10)
        self.fixture("late-evidence")
        result = self.action("recompute", lot=LOT)
        self.assertEqual(self.project_die(result, 0, 0, "W01")["grade"], "HOLD")
        self.assertEqual(self.project_die(result, 0, 0, "W02")["reading"]["session"], "RETEST-W02")


class ReleaseTests(LedgerCase):
    def test_dictionary_seal_coverage_version_and_wat_limit(self):
        self.baseline()
        self.action("dictionary", dictionary="GAPS", rules=[dict(channel="ET", code="0", grade="PASS")])
        with self.assertRaisesRegex(RuleError, "not sealed"):
            self.action("bind-dictionary", lot=LOT, dictionary="GAPS")
        self.action("seal-dictionary", dictionary="GAPS")
        with self.assertRaisesRegex(RuleError, "required channels"):
            self.action("bind-dictionary", lot=LOT, dictionary="GAPS")
        before = self.action("recompute", lot=LOT)
        self.assertEqual(before["manifest"]["decision"], "READY")
        self.action("publish", lot=LOT)
        rules = [dict(channel="ET", code="0", grade="PASS"), dict(channel="ET", code="9", grade="FAIL"),
                 dict(channel="WAT", code="0", grade="PASS", lo=100, hi=110)]
        self.action("dictionary", dictionary="BIN-2", rules=rules)
        self.action("seal-dictionary", dictionary="BIN-2")
        self.action("bind-dictionary", lot=LOT, dictionary="BIN-2")
        self.assertFalse(release.status(self.db, LOT)["exportable"])
        revised = self.action("recompute", lot=LOT)
        self.assertEqual(self.project_die(revised, 1, 0)["reading"]["channels"]["WAT"]["rule"], "WAT_LIMIT")
        self.assertEqual(self.project_die(revised, 0, 1)["grade"], "PASS")
        self.assertEqual(revised["manifest"]["blockers"], ["FAIL_LIMIT"])
        self.assertEqual(release.status(self.db, LOT)["releases"][0]["state"], "REVOKED")
        with self.assertRaisesRegex(RuleError, "conflicting dictionary"):
            self.action("dictionary", dictionary="BIN-2", rules=rules[:-1])

    def test_dictionary_rebind_changes_disposition_without_reselecting_targets(self):
        self.ready()
        prior = sampling.inspect(self.db, PLAN)
        self.action("publish", lot=LOT)
        rules = [dict(channel="ET", code="0", grade="PASS"), dict(channel="ET", code="9", grade="FAIL"),
                 dict(channel="WAT", code="0", grade="PASS", lo=101, hi=110)]
        self.action("dictionary", dictionary="BIN-TIGHT", rules=rules)
        self.action("seal-dictionary", dictionary="BIN-TIGHT")
        self.action("bind-dictionary", lot=LOT, dictionary="BIN-TIGHT")
        current = self.action("recompute", lot=LOT)
        self.assertEqual(sampling.inspect(self.db, PLAN), prior)
        self.assertEqual(self.project_die(current, 2, 0)["state"], "RETEST")
        self.assertEqual(self.project_die(current, 2, 0)["grade"], "FAIL")
        self.assertEqual(current["manifest"]["blockers"], ["FAIL_LIMIT"])
        self.assertEqual(release.status(self.db, LOT)["releases"][0]["state"], "REVOKED")

    def test_unknown_bin_holds_instead_of_default_classification(self):
        self.baseline()
        rules = [dict(channel="ET", code="0", grade="PASS"), dict(channel="WAT", code="0", grade="PASS")]
        self.action("dictionary", dictionary="NO-NINE", rules=rules)
        self.action("seal-dictionary", dictionary="NO-NINE")
        self.action("bind-dictionary", lot=LOT, dictionary="NO-NINE")
        result = self.action("recompute", lot=LOT)
        self.assertIn("UNKNOWN_BIN", [row["state"] for row in result["trace"]])
        self.assertEqual(self.project_die(result, 0, 0)["grade"], "HOLD")
        with self.assertRaisesRegex(RuleError, "INCOMPLETE_ATLAS"):
            self.action("publish", lot=LOT)

    def test_ineffective_late_evidence_preserves_release_after_recompute(self):
        self.baseline()
        self.action("evidence", evidence="signed", card="PC-1", starts=10, ends=11,
                    scope="CARD", source="SIGNED", verdict="CLEAR")
        self.action("recompute", lot=LOT)
        first = self.action("publish", lot=LOT)
        self.action("evidence", evidence="weaker", card="PC-1", starts=10, ends=11,
                    scope="SITE", site=1, source="OPERATOR", verdict="BLOCK")
        with self.assertRaisesRegex(RuleError, "stale"):
            release.export(self.db, LOT, 1)
        same = self.action("recompute", lot=LOT)
        self.assertEqual(first["digest"], same["digest"])
        self.assertEqual(self.action("publish", lot=LOT), first)
        self.assertTrue(release.status(self.db, LOT)["exportable"])

    def test_redelivery_and_interrupted_recompute_recover_from_raw(self):
        self.ready()
        first = self.action("publish", lot=LOT)
        old_epoch = self.epoch()
        self.fixture("retest")
        self.assertEqual(self.epoch(), old_epoch)
        with self.assertRaisesRegex(RuntimeError, "interrupt"):
            with transaction(self.db):
                self.db.execute("DELETE FROM assessments")
                workflow.apply(self.db, "recompute", {"lot": LOT})
                raise RuntimeError("interrupt")
        self.assertEqual(release.export(self.db, LOT, 1), first)
        self.db.execute("DELETE FROM assessments WHERE lot=? AND epoch=?", (LOT, old_epoch))
        with self.assertRaisesRegex(RuleError, "recompute"):
            release.export(self.db, LOT, 1)
        self.assertEqual(self.action("recompute", lot=LOT)["digest"], first["digest"])
        self.assertEqual(release.export(self.db, LOT, 1), first)

    def test_conflicting_input_rolls_back_rows_and_journal(self):
        self.baseline()
        source = read_json(FIXTURES / "baseline.json")[-2]["args"]
        first = source["measurements"][0]
        before = audit.export_journal(self.db)
        epoch = self.epoch()
        with self.assertRaisesRegex(RuleError, "conflicting redelivery"):
            self.action("map", **dict(source, measurements=[dict(first, id="new"), dict(first, value=999)]))
        self.assertEqual(self.epoch(), epoch)
        self.assertEqual(audit.export_journal(self.db), before)
        self.assertNotIn("new", [hit["id"] for hit in coordinates.project(self.db, "BASE-1")["hits"]])

    def test_independent_audit_checks_sequence_receipts_and_state(self):
        with transaction(self.db):
            summary = demo(self.db)
        archived = audit.export_journal(self.db)
        result = audit.audit(self.db)
        self.assertEqual((result["decision"], result["different_tables"]), ("MATCH", []))
        rebuilt = connect(":memory:")
        self.addCleanup(rebuilt.close)
        initialize(rebuilt)
        last = archived["events"][-1]
        with self.assertRaisesRegex(RuleError, "sequence error"):
            with transaction(rebuilt):
                audit.replay(rebuilt, dict(archived, events=archived["events"][1:]))
        last["receipt"]["revision"] = 99
        with self.assertRaisesRegex(RuleError, "receipt mismatch"):
            with transaction(rebuilt):
                audit.replay(rebuilt, archived)
        self.assertEqual(rebuilt.execute("SELECT count(*) FROM lots").fetchone()[0], 0)
        last["receipt"]["revision"] = 2
        with transaction(rebuilt):
            restored = audit.replay(rebuilt, archived)
        self.assertEqual(restored["decision"], "REPLAYED")
        self.assertEqual(release.export(rebuilt, LOT, 2)["digest"], summary["replacement_digest"])
        self.db.execute("UPDATE assessments SET digest='bad' WHERE lot=? AND epoch=?", (LOT, self.epoch()))
        self.assertEqual(audit.audit(self.db)["different_tables"], ["assessments"])
        self.action("recompute", lot=LOT)
        self.assertEqual(audit.audit(self.db)["decision"], "MATCH")
        self.db.execute("DELETE FROM measurements WHERE session='RETEST-2' AND id='00-ET'")
        self.assertEqual(audit.audit(self.db)["different_tables"], ["measurements"])
