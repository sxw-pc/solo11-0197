"""分卷 map 收件与封面清单准入的回归测试。

样例几何（镜像 MX，dx=2/dy=0，步距 10/20）：

    u=20,v=0  -> 格点(2,0) -> 裸片(0,0)   （B00/site1，ET+WAT）
    u=10,v=0  -> 格点(1,0) -> 裸片(1,0)   （B10/site1，ET+WAT）
    u=20,v=20 -> 格点(2,1) -> 裸片(0,1)   （后续卷，B00/site1）

封面里后续卷还会在 (0,0) 再测一次 ET（同裸片同通道），并把 B00/site1 用到
(0,1)（同落针+站点横跨裸片），因此只有 (1,0) 允许提前交付。
"""

import copy
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from wafer_retest import audit, coordinates, inspection, map_fragments, release
from wafer_retest.cli import main, read_json
from wafer_retest.db import RuleError

from tests.ledger_case import FIXTURES, LOT, LedgerCase


FRAGMENT_FIXTURE = FIXTURES / "map-fragments.json"
SESSION = "FRAG-1"


def sample_entries():
    return json.loads(FRAGMENT_FIXTURE.read_text(encoding="utf-8-sig"))


class MapFragmentCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.entries = sample_entries()

    def entry_args(self, command, part=None):
        for row in self.entries:
            if row["command"] != command:
                continue
            args = row["args"]
            if part is not None and args.get("part") != part:
                continue
            return copy.deepcopy(args)
        raise AssertionError(f"missing fixture entry {command} part={part}")

    def setup_lot(self):
        """载入 fixture 中第一个 map-fragment 之前的全部建账命令。"""
        for row in self.entries:
            if row["command"] == "map-fragment":
                break
            self.action(row["command"], **copy.deepcopy(row["args"]))

    def fragment1(self):
        return self.entry_args("map-fragment", part=1)

    def fragment2(self):
        return self.entry_args("map-fragment", part=2)

    def fragment3(self):
        return self.entry_args("map-fragment", part=3)

    def cover(self):
        return self.entry_args("map-cover")

    def anchors(self):
        return self.entry_args("anchors")

    def reach_early_delivery(self):
        self.setup_lot()
        self.action("map-fragment", **self.fragment1())
        self.action("map-cover", **self.cover())
        self.action("anchors", **self.anchors())

    def measurement_ids(self):
        return {row[0] for row in self.db.execute(
            "SELECT id FROM measurements WHERE session=? ORDER BY id", (SESSION,))}

    def fragment_rows(self):
        return self.db.execute(
            "SELECT part FROM map_fragments WHERE session=? ORDER BY part", (SESSION,)
        ).fetchall()


class MirrorSampleTests(MapFragmentCase):
    def test_cover_releases_only_die_one_zero(self):
        self.setup_lot()
        first = self.action("map-fragment", **self.fragment1())
        self.assertEqual(first["state"], "AWAITING_COVER")
        self.assertEqual(self.measurement_ids(), set())

        # 封面先到、锚点未对齐：只能等待，不提前交付。
        waiting = self.action("map-cover", **self.cover())
        self.assertEqual(waiting["alignment"], "ANCHORS_INSUFFICIENT")
        self.assertEqual(waiting["delivered"], [])
        self.assertEqual(self.measurement_ids(), set())

        # 镜像锚点对齐后自动重判：仅 (1,0) 的 ET/WAT 入库。
        self.action("anchors", **self.anchors())
        status = map_fragments.status(self.db, SESSION)
        self.assertEqual(status["frame"], {"orientation": "MX", "dx": 2, "dy": 0})
        self.assertEqual(status["delivered_ids"], ["10-ET", "10-WAT"])
        self.assertEqual(self.measurement_ids(), {"10-ET", "10-WAT"})

        by_xy = {tuple(g["xy"]): g for g in status["groups"] if g["state"] == "HELD"}
        self.assertEqual(set(by_xy), {(0, 0)})
        zero_zero = by_xy[(0, 0)]
        self.assertTrue(zero_zero["complete"])
        self.assertEqual(zero_zero["channels"], ["ET", "WAT"])
        self.assertIn("DUPLICATE_CHANNEL_LOCATION", zero_zero["reasons"])
        self.assertIn("SITE_LANDING_ALIAS", zero_zero["reasons"])

        released = [g for g in status["groups"] if g["state"] == "RELEASED"]
        self.assertEqual([(tuple(g["xy"]), g["measurement_ids"]) for g in released],
                         [((1, 0), ["10-ET", "10-WAT"])])

        # 既有 project 只看得到已交付的两读数，且都正常 LOCATED。
        projection = coordinates.project(self.db, SESSION)
        self.assertEqual({h["id"]: h["location"] for h in projection["hits"]},
                         {"10-ET": "LOCATED", "10-WAT": "LOCATED"})

    def test_without_cover_both_dies_wait(self):
        self.setup_lot()
        self.action("map-fragment", **self.fragment1())
        self.action("anchors", **self.anchors())
        status = map_fragments.status(self.db, SESSION)
        self.assertEqual(status["state"], "AWAITING_COVER")
        held = {tuple(g["xy"]): g for g in status["groups"]}
        self.assertEqual(set(held), {(0, 0), (1, 0)})
        for group in held.values():
            self.assertEqual(group["state"], "HELD")
            self.assertIn("COVER_REQUIRED", group["reasons"])
        self.assertEqual(self.measurement_ids(), set())
        self.assertEqual(coordinates.project(self.db, SESSION)["hits"], [])

    def test_clean_cover_can_release_both_complete_groups(self):
        self.setup_lot()
        one = self.fragment1()
        self.action("map-fragment", **one)
        signatures = [{k: hit[k] for k in ("id", "u", "v", "site", "touchdown", "channel")}
                      for hit in one["measurements"]]
        clean_cover = {
            "session": SESSION, "inspector": "QC-CLEAN",
            "frame": {"orientation": "MX", "dx": 2, "dy": 0},
            "parts": [
                {"part": 1, "measurements": signatures},
                # 后续卷落在 (1,1)，落针/站点独立，不构成任何碰撞。
                {"part": 2, "measurements": [
                    {"id": "11-ET", "u": 10, "v": 20, "site": 2,
                     "touchdown": "B11", "channel": "ET"}]},
            ],
        }
        self.action("map-cover", **clean_cover)
        self.action("anchors", **self.anchors())
        self.assertEqual(self.measurement_ids(), {"00-ET", "00-WAT", "10-ET", "10-WAT"})

    def test_early_reading_feeds_project_and_batch_collection(self):
        self.reach_early_delivery()

        attempts, trace, dictionary = inspection.collect(self.db, LOT)
        self.assertEqual(dictionary, "BIN-1")
        self.assertIn(("W01", 1, 0), attempts)
        attempt = attempts["W01", 1, 0][0]
        self.assertEqual(attempt["session"], SESSION)
        self.assertEqual(sorted(attempt["channels"]), ["ET", "WAT"])

        result = self.action("recompute", lot=LOT)
        die = self.project_die(result, 1, 0)
        self.assertEqual(die["grade"], "PASS")
        self.assertEqual(die["state"], "BASE")
        self.assertEqual(die["reading"]["session"], SESSION)
        self.assertEqual(result["manifest"]["counts"]["PASS"], 1)

        # 提前交付的读数同样能作为采样计划的基线快照。
        plan = self.action("create-plan", plan="PLAN-P7", lot=LOT, opens=20, closes=28, capacity=2)
        snapshot_die = next(d for d in plan["snapshot"]["dies"] if (d["x"], d["y"]) == (1, 0))
        self.assertEqual((snapshot_die["risk"], snapshot_die["baseline"]), ("PASS", SESSION))
        self.action("transition-plan", plan="PLAN-P7", state="SEALED")
        chosen = self.action("recompute", lot=LOT)
        self.assertEqual(self.project_die(chosen, 1, 0)["reading"]["session"], SESSION)

    def test_early_delivery_flows_through_publish_and_export(self):
        self.setup_lot()
        baseline_map = next(row["args"] for row in read_json(FIXTURES / "baseline.json")
                            if row["command"] == "map")
        hits = baseline_map["measurements"]
        session = "FRAG-PUB"
        self.action("map-fragment", session=session, lot=LOT, wafer="W01", card="PC-1",
                    measured_at=10, pitch_u=10, pitch_v=20, plan=None,
                    part=1, expected_parts=2, measurements=hits)
        signatures = [{k: hit[k] for k in ("id", "u", "v", "site", "touchdown", "channel")}
                      for hit in hits]
        # 未到的第 2 卷只用独立落针/站点测一个非必需通道 VT，不威胁任何 ET/WAT 组。
        cover = {
            "session": session, "inspector": "QC-PUB",
            "frame": {"orientation": "MX", "dx": 2, "dy": 0},
            "parts": [
                {"part": 1, "measurements": signatures},
                {"part": 2, "measurements": [
                    {"id": "future-vt", "u": 0, "v": 0, "site": 2,
                     "touchdown": "FUT", "channel": "VT"}]},
            ],
        }
        self.action("map-cover", **cover)
        baseline_anchors = next(row["args"] for row in read_json(FIXTURES / "baseline.json")
                                if row["command"] == "anchors")["anchors"]
        self.action("anchors", session=session, anchors=baseline_anchors)

        self.assertEqual(
            {row[0] for row in self.db.execute(
                "SELECT id FROM measurements WHERE session=?", (session,))},
            {hit["id"] for hit in hits},
        )
        ready = self.action("recompute", lot=LOT)
        self.assertEqual(ready["manifest"]["decision"], "READY")
        published = self.action("publish", lot=LOT)
        exported = release.export(self.db, LOT, published["revision"])
        self.assertEqual(len(exported["manifest"]["dies"]), 5)


class CoverSafetyTests(MapFragmentCase):
    def test_frame_mismatch_waits_for_reverification(self):
        self.setup_lot()
        self.action("map-fragment", **self.fragment1())
        # 认可封面声明的是 MX 镜像框架。
        self.action("map-cover", **self.cover())

        # STAGE 锚点拟合出 I 框架，与封面 MX 不一致 -> 只等待，不交付。
        stage_anchors = [
            {"id": "A0", "u": 0, "v": 0, "x": 0, "y": 0, "source": "STAGE"},
            {"id": "A1", "u": 10, "v": 0, "x": 1, "y": 0, "source": "STAGE"},
            {"id": "A2", "u": 10, "v": 20, "x": 1, "y": 1, "source": "STAGE"},
        ]
        self.action("anchors", session=SESSION, anchors=stage_anchors)
        status = map_fragments.status(self.db, SESSION)
        self.assertEqual(status["frame"], {"orientation": "I", "dx": 0, "dy": 0})
        self.assertEqual(status["state"], "FRAME_MISMATCH")
        self.assertEqual(status["delivered_ids"], [])
        self.assertTrue(all("FRAME_MISMATCH" in g["reasons"] for g in status["groups"]))

        # 更高级 FIDUCIAL 锚点重新核验为 MX（与封面一致）后自动恢复提前交付。
        self.action("anchors", session=SESSION, anchors=[
            {"id": "F-OVERRIDE", "u": 0, "v": 0, "x": 2, "y": 0, "source": "FIDUCIAL"},
        ])
        status = map_fragments.status(self.db, SESSION)
        self.assertEqual(status["alignment"], "ALIGNED")
        self.assertEqual(status["frame"], {"orientation": "MX", "dx": 2, "dy": 0})
        self.assertEqual(status["delivered_ids"], ["10-ET", "10-WAT"])


class RejectionAndIdempotencyTests(MapFragmentCase):
    def test_cover_mismatch_rejects_whole_volume_without_overwrite(self):
        self.reach_early_delivery()
        before = self.measurement_ids()
        early_value = self.db.execute(
            "SELECT value FROM measurements WHERE session=? AND id='10-ET'", (SESSION,)
        ).fetchone()[0]

        bad = self.fragment2()
        bad["measurements"][0]["u"] = 21  # 实物格点与封面 (20,0) 不符
        with self.assertRaisesRegex(RuleError, "cover mismatch"):
            self.action("map-fragment", **bad)

        # 整卷退回：暂存里没有第 2 卷，已提前交付的测量原样保留。
        self.assertEqual([r[0] for r in self.fragment_rows()], ["1"])
        self.assertEqual(self.measurement_ids(), before)
        value = self.db.execute(
            "SELECT value FROM measurements WHERE session=? AND id='10-ET'", (SESSION,)
        ).fetchone()[0]
        self.assertEqual(value, early_value)
        locations = {h["id"]: h["location"] for h in coordinates.project(self.db, SESSION)["hits"]}
        self.assertEqual(locations, {"10-ET": "LOCATED", "10-WAT": "LOCATED"})

    def test_volume_reusing_delivered_id_is_rejected(self):
        self.reach_early_delivery()
        attack = self.fragment2()
        attack["measurements"] = [{
            "id": "10-ET", "u": 10, "v": 0, "site": 1, "touchdown": "B10",
            "channel": "ET", "code": "9", "value": 999,
        }]
        with self.assertRaises(RuleError):
            self.action("map-fragment", **attack)
        row = self.db.execute(
            "SELECT code,value FROM measurements WHERE session=? AND id='10-ET'", (SESSION,)
        ).fetchone()
        self.assertEqual((row["code"], row["value"]), ("0", 1))

    def test_conflicting_same_part_redelivery_rejected(self):
        self.reach_early_delivery()
        changed = self.fragment1()
        changed["measurements"][2]["value"] = 999  # 10-ET 的值被改动
        with self.assertRaisesRegex(RuleError, "conflicting redelivery"):
            self.action("map-fragment", **changed)
        row = self.db.execute(
            "SELECT value FROM measurements WHERE session=? AND id='10-ET'", (SESSION,)
        ).fetchone()
        self.assertEqual(row["value"], 1)

    def test_identical_redelivery_is_idempotent(self):
        self.reach_early_delivery()
        epoch_before = self.epoch()
        result = self.action("map-fragment", **self.fragment1())
        self.assertTrue(result["duplicate"])
        self.assertFalse(result["changed"])
        self.assertEqual(len(self.fragment_rows()), 1)
        self.assertEqual(len(self.measurement_ids()), 2)
        self.assertEqual(self.epoch(), epoch_before)


class CompletionTests(MapFragmentCase):
    def test_without_cover_full_count_delivers_all(self):
        self.setup_lot()
        self.action("map-fragment", **self.fragment1())
        self.action("anchors", **self.anchors())
        self.assertEqual(self.measurement_ids(), set())

        self.action("map-fragment", **self.fragment2())
        # 3 卷只到了 2 卷，仍然不提前交付。
        self.assertEqual(self.measurement_ids(), set())
        self.action("map-fragment", **self.fragment3())

        self.assertEqual(len(self.measurement_ids()), 6)
        locations = {h["id"]: h["location"] for h in coordinates.project(self.db, SESSION)["hits"]}
        self.assertEqual(locations, {
            "00-ET": "DUPLICATE_CHANNEL_LOCATION",
            "00-ET-2": "DUPLICATE_CHANNEL_LOCATION",
            "00-WAT": "SITE_LANDING_ALIAS",
            "01-ET": "SITE_LANDING_ALIAS",
            "10-ET": "LOCATED",
            "10-WAT": "LOCATED",
        })
        result = self.action("recompute", lot=LOT)
        self.assertEqual(result["manifest"]["counts"],
                         {"PASS": 1, "MARGINAL": 0, "FAIL": 0, "HOLD": 4})

    def test_explicit_complete_delivers_staged_groups(self):
        self.setup_lot()
        one = self.fragment1()
        one.pop("expected_parts", None)
        self.action("map-fragment", **one)
        self.assertEqual(self.measurement_ids(), set())
        result = self.action("map-fragment-complete", session=SESSION)
        self.assertTrue(result["all_parts_received"])
        self.assertEqual(len(self.measurement_ids()), 4)

    def test_full_delivery_matches_whole_map_projection(self):
        self.setup_lot()
        self.action("map-fragment", **self.fragment1())
        self.action("map-fragment", **self.fragment2())
        self.action("map-fragment", **self.fragment3())
        self.action("anchors", **self.anchors())
        fragment_locations = {
            h["id"]: h["location"] for h in coordinates.project(self.db, SESSION)["hits"]
        }

        all_hits = self.fragment1()["measurements"] + self.fragment2()["measurements"] \
            + self.fragment3()["measurements"]
        whole = self.fragment1()
        whole.pop("part", None)
        whole.pop("expected_parts", None)
        whole.update(session="WHOLE", measurements=all_hits)
        self.action("map", **whole)
        self.action("anchors", session="WHOLE", anchors=self.anchors()["anchors"])
        whole_locations = {
            h["id"]: h["location"] for h in coordinates.project(self.db, "WHOLE")["hits"]
        }
        self.assertEqual(fragment_locations, whole_locations)


class FixtureAndCliTests(MapFragmentCase):
    def test_fixture_loads_and_audit_matches(self):
        results = self.fixture("map-fragments")
        self.assertTrue(results)
        self.assertEqual(len(self.measurement_ids()), 6)
        report = audit.audit(self.db)
        self.assertEqual((report["decision"], report["different_tables"]), ("MATCH", []))

    def test_map_command_routes_fragment_envelope(self):
        self.setup_lot()
        routed = self.action("map", **self.fragment1())
        self.assertIn("duplicate", routed)
        self.assertEqual([r[0] for r in self.fragment_rows()], ["1"])
        # 不带分卷标记的 map 仍走整份 map 录入。
        whole = {
            "session": "WHOLE-MAP", "lot": LOT, "wafer": "W01", "card": "PC-1",
            "measured_at": 10, "pitch_u": 10, "pitch_v": 20, "plan": None,
            "measurements": self.fragment2()["measurements"],
        }
        self.action("map", **whole)
        self.assertIn("00-ET-2", {r[0] for r in self.db.execute(
            "SELECT id FROM measurements WHERE session='WHOLE-MAP'")})

    def test_cli_early_delivery_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / "cli.db")
            early_doc = self.entries[:9]  # 建账 + 卷1 + 封面 + 锚点
            early_file = Path(tmp) / "early.json"
            early_file.write_text(json.dumps(early_doc), encoding="utf-8")

            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["--db", database, "init"]), 0)
                self.assertEqual(main(["--db", database, "load", str(early_file)]), 0)

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--db", database, "fragments", SESSION])
            self.assertEqual(code, 0)
            status = json.loads(out.getvalue())
            self.assertEqual(status["delivered_ids"], ["10-ET", "10-WAT"])
            self.assertFalse(status["all_parts_received"])

            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["--db", database, "fragment-release", SESSION]), 0)
            release_report = json.loads(out.getvalue())
            self.assertEqual(release_report["delivered"], [])

            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["--db", database, "project", SESSION]), 0)
            projection = json.loads(out.getvalue())
            self.assertEqual({h["id"]: h["location"] for h in projection["hits"]},
                             {"10-ET": "LOCATED", "10-WAT": "LOCATED"})
