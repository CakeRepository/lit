import unittest

from concurrent_merge.promotion import Bridges, Stage


def bridges():
    b = Bridges([
        Stage("lab", checks=("smoke", "soak")),
        Stage("early", checks=("canary",)),
        Stage("production", checks=("canary",), approval=True),
    ])
    for snapshot in ("m1", "m2", "m3"):
        b.integrate(snapshot)
    return b


def passed(b, stage, snapshot):
    for check in b.by_name[stage].checks:
        b.record(stage, snapshot, check, True)


class PromotionTests(unittest.TestCase):
    def test_one_snapshot_goes_through_every_stage_unchanged_with_provenance(self):
        b = bridges()
        passed(b, "lab", "m2")
        self.assertIsNone(b.promote("lab", "m2", None))
        passed(b, "early", "m2")
        self.assertIsNone(b.promote("early", "m2", None))
        passed(b, "production", "m2")
        b.approve("production", "m2", "release manager")
        self.assertIsNone(b.promote("production", "m2", None))
        self.assertEqual(b.at, {"lab": "m2", "early": "m2", "production": "m2"})
        self.assertEqual([(p["stage"], p["snapshot"], p["source"]) for p in b.provenance],
                         [("lab", "m2", "main"), ("early", "m2", "lab"), ("production", "m2", "early")])
        self.assertEqual(b.provenance[-1]["approved_by"], "release manager")
        self.assertEqual(b.provenance[0]["checks"], {"smoke": True, "soak": True})

    def test_a_stage_takes_only_what_reached_the_stage_before_it(self):
        b = bridges()
        passed(b, "early", "m3")
        self.assertEqual(b.promote("early", "m3", None), ("not at source", "lab"))
        passed(b, "lab", "m9")
        self.assertEqual(b.promote("lab", "m9", None), ("not at source", "main"))
        self.assertEqual(b.at, {})

    def test_evidence_counts_only_for_its_own_stage_and_snapshot(self):
        b = bridges()
        passed(b, "lab", "m1")
        self.assertIsNone(b.promote("lab", "m1", None))
        passed(b, "lab", "m2")
        self.assertIsNone(b.promote("lab", "m2", "m1"))
        b.record("early", "m1", "canary", True)  # a pass on another snapshot
        self.assertEqual(b.promote("early", "m2", None), ("missing", {"unrun": ["canary"]}))
        b.record("lab", "m3", "smoke", True)
        self.assertEqual(b.promote("lab", "m3", "m2"), ("missing", {"unrun": ["soak"]}))

    def test_production_waits_for_a_person_on_that_snapshot(self):
        b = bridges()
        for stage in ("lab", "early"):
            passed(b, stage, "m1")
            b.promote(stage, "m1", None)
        passed(b, "production", "m1")
        b.approve("production", "m0", "someone")  # approval of another snapshot
        self.assertEqual(b.promote("production", "m1", None), ("missing", {"approval": True}))
        with self.assertRaises(ValueError):
            b.approve("lab", "m1", "someone")  # lab takes no approval
        b.approve("production", "m1", "release manager")
        self.assertIsNone(b.promote("production", "m1", None))

    def test_a_failure_downstream_holds_only_that_bridge(self):
        b = bridges()
        passed(b, "lab", "m1")
        b.promote("lab", "m1", None)
        b.record("early", "m1", "canary", False)
        self.assertEqual(b.promote("early", "m1", None), ("missing", {"failed": ["canary"]}))
        # Main keeps integrating and lab keeps promoting newer snapshots.
        b.integrate("m4")
        passed(b, "lab", "m4")
        self.assertIsNone(b.promote("lab", "m4", "m1"))
        passed(b, "early", "m4")
        self.assertIsNone(b.promote("early", "m4", None))
        self.assertEqual(b.at, {"lab": "m4", "early": "m4"})
        self.assertEqual(b.holds("main"), "m4")

    def test_stages_move_by_compare_and_swap(self):
        b = bridges()
        passed(b, "lab", "m1")
        passed(b, "lab", "m2")
        self.assertIsNone(b.promote("lab", "m1", None))
        # A second promoter that saw the empty stage loses the race and changes nothing.
        self.assertEqual(b.promote("lab", "m2", None), ("moved", "m1"))
        self.assertEqual(b.at["lab"], "m1")
        self.assertEqual(b.rollback("lab", None), ("moved", "m1"))

    def test_rollback_returns_to_the_last_good_snapshot_and_bars_the_bad_one(self):
        b = bridges()
        for snapshot in ("m1", "m2"):
            passed(b, "lab", snapshot)
            b.promote("lab", snapshot, b.at.get("lab"))
        self.assertIsNone(b.rollback("lab", "m2"))
        self.assertEqual(b.at["lab"], "m1")
        self.assertEqual(b.provenance[-1], dict(stage="lab", snapshot="m1", source="lab", rollback_from="m2"))
        passed(b, "early", "m2")
        self.assertEqual(b.promote("early", "m2", None), ("not at source", "lab"))
        self.assertEqual(b.rollback("lab", "m1"), ("nothing to roll back to",))

    def test_bad_configurations_are_refused(self):
        with self.assertRaises(ValueError):
            Bridges([])
        with self.assertRaises(ValueError):
            Bridges([Stage("lab"), Stage("lab")])
        with self.assertRaises(ValueError):
            Bridges([Stage("main")])
        b = bridges()
        with self.assertRaises(ValueError):
            b.record("lab", "m1", "canary", True)  # lab does not use canary
        with self.assertRaises(ValueError):
            b.integrate("m1")


if __name__ == "__main__":
    unittest.main()
