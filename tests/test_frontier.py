import unittest

from concurrent_merge.frontier import Frontier
from concurrent_merge.frontier_bench import bench
from concurrent_merge.frontier_sim import makespan, program, simulate


def mapped():
    """main: a.ts#f, a.ts#g, b.ts#h. Unit t1 reads f, t2 reads g, t3 reads f and h."""
    f = Frontier({"a.ts#f": "f0", "a.ts#g": "g0", "b.ts#h": "h0"})
    f.record("t1", ["a.ts#f"])
    f.record("t2", ["a.ts#g"])
    f.record("t3", ["a.ts#f", "b.ts#h"])
    return f


class FrontierTests(unittest.TestCase):
    def test_a_change_lands_on_its_own_passes_whatever_else_is_in_flight(self):
        f = mapped()
        self.assertIsNone(f.admit("A", {"a.ts#g": "g1"}))
        self.assertIsNone(f.admit("B", {"b.ts#h": "h1"}))
        self.assertEqual(f.needs(["A"]), ["t2"])
        f.record("t2", members={"A"})  # A's own PR run
        self.assertEqual(f.needs(["A"]), [])
        f.record("t3", members={"B"})
        # Each run saw only its own change, yet together they prove main plus both.
        self.assertEqual(f.needs(["A", "B"]), [])
        self.assertEqual(f.overlaps(), [])
        self.assertEqual(sorted(map(sorted, f.landable())), [["A"], ["B"]])

    def test_only_a_unit_reading_two_changes_runs_again(self):
        f = mapped()
        f.admit("A", {"a.ts#f": "f1"})
        f.admit("B", {"b.ts#h": "h1"})
        f.record("t1", members={"A"})
        f.record("t3", members={"A"})
        f.record("t3", members={"B"})
        self.assertEqual(f.overlaps(), ["t3"])
        self.assertEqual(f.components(), [["A", "B"]])
        self.assertEqual(f.needs(["A"]), [])  # A alone is still proven
        self.assertEqual(f.needs(["A", "B"]), ["t3"])
        f.record("t3", members={"A", "B"})
        self.assertEqual(f.needs(["A", "B"]), [])

    def test_landing_keeps_certificates_because_they_are_keyed_by_content(self):
        f = mapped()
        f.admit("A", {"a.ts#f": "f1"})
        f.admit("B", {"a.ts#g": "g1"})
        f.record("t1", members={"A"})
        f.record("t3", members={"A"})
        f.record("t2", members={"B"})
        f.land(["A"])
        self.assertEqual(f.base["a.ts#f"], "f1")
        self.assertEqual(f.needs(["B"]), [])  # B's proof did not read A's function
        with self.assertRaises(ValueError):
            f.admit("A", {"x": "1"})

    def test_a_proof_on_an_older_main_is_stale_only_where_it_read_what_landed(self):
        f = mapped()
        f.admit("A", {"a.ts#f": "f1"})
        f.admit("B", {"b.ts#h": "h1"})
        f.record("t3", members={"B"})  # B's run saw f0
        f.record("t1", members={"A"})
        f.record("t3", members={"A"})
        f.land(["A"])
        self.assertEqual(f.needs(["B"]), ["t3"])

    def test_the_same_function_twice_is_refused_with_a_packet_an_agent_can_act_on(self):
        f = mapped()
        f.admit("A", {"a.ts#f": "f1"}, dict(title="Speed up f", intent="cache f",
                                             diffs={"a.ts#f": "-old\n+cached"}))
        packet = f.admit("B", {"a.ts#f": "f2", "a.ts#g": "g1"},
                         dict(title="Fix f", intent="null check", diffs={"a.ts#f": "+if (!x) return"}))
        self.assertEqual(packet["kind"], "lit-conflict")
        self.assertEqual(packet["inputs"], ["a.ts#f"])
        self.assertEqual(packet["landed_first"][0]["change"], "A")
        self.assertEqual(packet["landed_first"][0]["diffs"], {"a.ts#f": "-old\n+cached"})
        self.assertEqual(packet["diffs"], {"a.ts#f": "+if (!x) return"})
        self.assertEqual(packet["tests"], ["t1", "t3"])
        self.assertIn("Rewrite B on top of it", packet["task"])
        self.assertNotIn("B", f.changes)
        self.assertNotIn("a.ts#g", f.writer)  # a refused change claims nothing

    def test_eject_frees_its_functions(self):
        f = mapped()
        f.admit("A", {"a.ts#f": "f1"})
        f.eject("A")
        self.assertIsNone(f.admit("B", {"a.ts#f": "f2"}))
        self.assertEqual(f.content("a.ts#f"), "f2")

    def test_a_deleted_function_leaves_main(self):
        f = mapped()
        f.admit("A", {"b.ts#h": None})
        f.land(["A"])
        self.assertNotIn("b.ts#h", f.base)


class ScaleTests(unittest.TestCase):
    def test_cost_per_change_does_not_grow_with_changes_in_flight(self):
        rows = list(bench(changes=20_000, functions=200_000, units=5_000, reads=20,
                          report=(2_000, 20_000)))
        self.assertGreater(rows[1]["in_flight"], 5 * rows[0]["in_flight"])
        self.assertLess(rows[1]["us_per_change"], 3 * rows[0]["us_per_change"])


class SimTests(unittest.TestCase):
    def test_makespan_packs_longest_first(self):
        self.assertEqual(makespan([4, 3, 3, 2], 2), 6)
        self.assertEqual(makespan([], 4), 0)

    def test_a_light_stream_lands_every_change_quickly(self):
        prog = program(app_keys=600, lib_keys=400, web=100, db=40, journeys=8, journey_share=0.02)
        r = simulate(prog, per_hour=20, hours=1, cores=8)
        self.assertEqual(r["landed"] + r["sent_back"], r["arrived"])
        self.assertLess(r["median"], 120)


if __name__ == "__main__":
    unittest.main()


class StoreTests(unittest.TestCase):
    def test_the_map_comes_from_lit_unit_store_with_functions_where_recorded(self):
        import json
        import tempfile
        from pathlib import Path
        from concurrent_merge.frontier import from_store
        with tempfile.TemporaryDirectory() as store:
            job = Path(store) / "portal"
            (job / "units").mkdir(parents=True)
            (job / "symbols").mkdir()
            (job / "symbols" / "oid-db.json").write_text(json.dumps({"": "top", "query": "q1", "close": "c1"}))
            bundle = dict(kind="lit-unit-footprints", commit="abc",
                          tracked={"src/db.ts": ["100644", "oid-db"], "src/a.test.ts": ["100644", "oid-a"]},
                          units={"src/a.test.ts": dict(tracked=["src/db.ts", "src/a.test.ts"],
                                                       symbols={"src/db.ts": ["query"]}, seconds=1.5)})
            (job / "units" / "x.json").write_text(json.dumps(bundle))
            f = from_store(store, "portal")
        self.assertEqual(f.reads["src/a.test.ts"], {"src/db.ts#", "src/db.ts#query", "src/a.test.ts"})
        self.assertEqual(f.base["src/db.ts#query"], "q1")
        f.admit("A", {"src/db.ts#close": "c2"})  # a function the test never ran
        self.assertEqual(f.needs(["A"]), [])
        f.admit("B", {"src/db.ts#query": "q2"})
        self.assertEqual(f.needs(["B"]), ["src/a.test.ts"])
