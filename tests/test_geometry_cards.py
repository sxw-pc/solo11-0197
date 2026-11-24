from wafer_retest import cards, coordinates, release
from wafer_retest.cli import read_json
from wafer_retest.db import RuleError

from tests.ledger_case import FIXTURES, LOT, PLAN, LedgerCase


class CoordinatesTests(LedgerCase):
    def test_mirror_with_unequal_pitch_and_priority(self):
        self.baseline()
        self.action("anchors", session="BASE-1", anchors=[
            dict(id="mistag", u=20, v=0, x=1, y=0, source="OPERATOR"),
        ])
        projection = coordinates.project(self.db, "BASE-1")
        self.assertEqual(projection["frame"], {"orientation": "MX", "dx": 2, "dy": 0})
        self.assertEqual([(hit["xy"], hit["location"]) for hit in projection["hits"][:2]], [((0, 0), "LOCATED")] * 2)
        self.assertEqual(self.action("recompute", lot=LOT)["manifest"]["counts"]["PASS"], 4)

    def test_collinear_and_off_lattice_anchors(self):
        self.baseline()
        self.action("map", session="COL", lot=LOT, wafer="W01", card="PC-1", measured_at=8,
                    pitch_u=10, pitch_v=20, measurements=[])
        points = [dict(id=f"A{x}", u=x * 10, v=0, x=x, y=0, source="STAGE") for x in range(3)]
        self.action("anchors", session="COL", anchors=points)
        self.assertEqual(coordinates.project(self.db, "COL")["state"], "ANCHORS_COLLINEAR")
        with self.assertRaisesRegex(RuleError, "off probe lattice"):
            self.action("anchors", session="COL", anchors=[dict(id="D", u=0, v=19, x=0, y=1, source="STAGE")])
        self.action("anchors", session="COL", anchors=[dict(id="D", u=0, v=20, x=0, y=1, source="STAGE")])
        self.assertEqual(coordinates.project(self.db, "COL")["frame"]["orientation"], "I")

    def test_geometry_masks_notch_off_lattice_and_collisions(self):
        self.baseline()
        source = read_json(FIXTURES / "baseline.json")[-2]["args"]
        template = source["measurements"][0]
        hits = [dict(template, id="dupe"), dict(template, id="notch", u=0, v=20),
                dict(template, id="off", u=19), dict(template, id="outside", u=-10)]
        self.action("map", **dict(source, measurements=hits))
        projected = {hit["id"]: hit["location"] for hit in coordinates.project(self.db, "BASE-1")["hits"]}
        self.assertEqual({name: projected[name] for name in ("dupe", "00-ET", "notch", "off", "outside")},
                         {"dupe": "DUPLICATE_CHANNEL_LOCATION", "00-ET": "DUPLICATE_CHANNEL_LOCATION",
                          "notch": "OUTSIDE_MASK", "off": "OFF_LATTICE", "outside": "OUTSIDE_MASK"})
        self.assertEqual(self.project_die(self.action("recompute", lot=LOT), 0, 0)["grade"], "HOLD")

    def test_stage_origin_and_site_landing_alias(self):
        self.baseline()
        source = read_json(FIXTURES / "baseline.json")[-2]["args"]
        hits = [dict(hit, u=hit["u"] - 3, v=hit["v"] + 7, touchdown="SAME", site=1)
                for hit in source["measurements"] if hit["id"][:2] in ("00", "10")]
        self.action("map", **dict(source, session="ALIAS", origin_u=-3, origin_v=7, measurements=hits))
        anchors = read_json(FIXTURES / "baseline.json")[-1]["args"]["anchors"]
        self.action("anchors", session="ALIAS", anchors=[dict(row, u=row["u"] - 3, v=row["v"] + 7) for row in anchors])
        projection = coordinates.project(self.db, "ALIAS")
        self.assertEqual(projection["frame"], {"orientation": "MX", "dx": 2, "dy": 0})
        self.assertEqual({hit["xy"] for hit in projection["hits"]}, {(0, 0), (1, 0)})
        self.assertEqual({hit["location"] for hit in projection["hits"]}, {"SITE_LANDING_ALIAS"})

    def test_equal_rank_anchor_conflict_withdraws_release(self):
        self.ready()
        self.action("publish", lot=LOT)
        self.action("anchors", session="RETEST-1", anchors=[
            dict(id="F-conflict", u=0, v=40, x=1, y=0, source="FIDUCIAL"),
        ])
        self.assertEqual(coordinates.project(self.db, "RETEST-1")["state"], "ANCHOR_CONFLICT")
        with self.assertRaisesRegex(RuleError, "stale"):
            release.export(self.db, LOT, 1)
        held = self.action("recompute", lot=LOT)
        self.assertEqual(held["manifest"]["counts"]["HOLD"], 2)
        self.assertEqual(release.status(self.db, LOT)["releases"][0]["state"], "REVOKED")


class CardsTests(LedgerCase):
    def test_half_open_qualification_and_overlap(self):
        self.baseline()
        self.assertEqual(cards.inspect(self.db, "PC-1", 19, 1)["state"], "USABLE")
        self.assertEqual(cards.inspect(self.db, "PC-1", 20, 1)["state"], "UNQUALIFIED")
        with self.assertRaisesRegex(RuleError, "overlap"):
            self.action("qualify", qualification="OVER", card="PC-1", starts=19, ends=30, verdict="FAIL")
        self.action("qualify", qualification="NEXT", card="PC-1", starts=20, ends=30, verdict="FAIL")
        self.action("evidence", evidence="clear", card="PC-1", starts=20, ends=30,
                    scope="CARD", source="SIGNED", verdict="CLEAR")
        self.assertEqual(cards.inspect(self.db, "PC-1", 20, 1)["state"], "QUALIFICATION_FAILED")

    def test_evidence_trust_scope_and_equally_ranked_conflict(self):
        self.baseline()
        common = dict(card="PC-1", starts=9, ends=11)
        self.action("evidence", evidence="operator", scope="SITE", source="OPERATOR", verdict="CLEAR", site=1, **common)
        self.action("evidence", evidence="signed", scope="CARD", source="SIGNED", verdict="BLOCK", **common)
        self.assertEqual(cards.inspect(self.db, "PC-1", 10, 1)["evidence"], ["signed"])
        self.assertEqual(cards.inspect(self.db, "PC-1", 10, 1)["state"], "CONTACT_BLOCKED")
        self.action("evidence", evidence="exception", scope="SITE", source="SIGNED", verdict="CLEAR", site=1, **common)
        self.assertEqual(cards.inspect(self.db, "PC-1", 10, 1)["state"], "USABLE")
        self.assertEqual(cards.inspect(self.db, "PC-1", 10, 2)["state"], "CONTACT_BLOCKED")
        self.action("evidence", evidence="tie", scope="SITE", source="SIGNED", verdict="BLOCK", site=1, **common)
        self.assertEqual(cards.inspect(self.db, "PC-1", 10, 1),
                         {"state": "EVIDENCE_CONFLICT", "qualification": "Q1", "evidence": ["exception", "tie"]})
        self.assertEqual(cards.inspect(self.db, "PC-1", 11, 1)["state"], "USABLE")

    def test_touchdown_block_fans_out_replacement_recovers(self):
        self.assertEqual(self.ready()["manifest"]["decision"], "READY")
        first = self.action("publish", lot=LOT)
        self.fixture("late-evidence")
        for site in (1, 2):
            self.assertEqual(cards.inspect(self.db, "PC-2", 22, site, "RETEST-1", "TD1")["state"], "CONTACT_BLOCKED")
            self.assertEqual(cards.inspect(self.db, "PC-2", 22, site, "RETEST-1", "TD2")["state"], "USABLE")
        self.assertFalse(release.status(self.db, LOT)["exportable"])
        withheld = self.action("recompute", lot=LOT)
        self.assertEqual(withheld["manifest"]["counts"], dict(PASS=3, MARGINAL=0, FAIL=0, HOLD=2))
        self.assertEqual(self.project_die(withheld, 1, 0)["state"], "BASE")
        with self.assertRaisesRegex(RuleError, "revoked"):
            release.export(self.db, LOT, first["revision"])
        self.fixture("replacement")
        self.assertEqual(self.action("recompute", lot=LOT)["manifest"]["counts"]["PASS"], 5)
        self.assertEqual(self.action("publish", lot=LOT)["revision"], 2)

    def test_late_qualification_reopens_unqualified_targets(self):
        self.baseline()
        self.action("card", card="NEW", sites=2)
        self.action("create-plan", plan=PLAN, lot=LOT, opens=20, closes=28, capacity=2)
        self.map_from("retest", "NO-QUAL", card="NEW")
        self.action("transition-plan", plan=PLAN, state="SEALED")
        self.assertEqual(self.action("recompute", lot=LOT)["manifest"]["counts"]["HOLD"], 2)
        prior = self.epoch()
        self.action("qualify", qualification="Q-NEW", card="NEW", starts=22, ends=23, verdict="PASS")
        self.assertEqual(self.epoch(), prior + 1)
        self.assertEqual(self.action("recompute", lot=LOT)["manifest"]["decision"], "READY")
