import random
import unittest

from concurrent_merge.frontier import Certificate, Frontier
from concurrent_merge.frontier_bench import bench, grouping
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

    def test_grouping_reports_one_component_swallowing_what_is_in_flight(self):
        rows = list(grouping(changes=20_000, functions=200_000, units=5_000, reads=20,
                             report=(2_000, 20_000)))
        self.assertEqual([r["seen"] for r in rows], [2_000, 20_000])
        self.assertGreater(rows[1]["largest"], 20 * rows[0]["largest"])
        self.assertLess(rows[1]["components"], rows[1]["in_flight"])

    def test_components_keep_admission_order_inside_a_large_group(self):
        f = Frontier({})
        f.record("shared", [f"k{i}" for i in range(500)])
        ids = [f"c{i:03d}" for i in range(500)]
        order = list(reversed(ids))  # admitted out of name order
        for cid in order:
            self.assertIsNone(f.admit(cid, {f"k{ids.index(cid)}": cid}))
        self.assertEqual(f.components(), [order])
        self.assertEqual(f.components(ids[:3]), [[ids[2], ids[1], ids[0]]])


class SafetyTests(unittest.TestCase):
    """Stale evidence and a lost race on main never land anything unproven."""

    def test_a_decision_taken_before_main_moved_lands_nothing(self):
        f = mapped()
        f.admit("A", {"a.ts#f": "f1"})
        f.admit("B", {"b.ts#h": "h1"})
        f.record("t3", members={"B"})  # B's run saw f0
        seen = f.version
        self.assertEqual(f.needs(["B"]), [])  # B looks ready at `seen`
        f.record("t1", members={"A"})
        f.record("t3", members={"A"})
        self.assertIsNone(f.land_if(["A"], seen))  # A wins the race; t3 read f, which A wrote
        self.assertEqual(f.land_if(["B"], seen), ("moved", f.version))
        self.assertIn("B", f.changes)
        self.assertNotEqual(f.base["b.ts#h"], "h1")
        # Rechecked at the new version, the stale proof shows up as work.
        self.assertEqual(f.land_if(["B"], f.version), ("unproven", ["t3"]))
        f.record("t3", members={"B"})
        self.assertIsNone(f.land_if(["B"], f.version))
        self.assertEqual(f.landed, ["A", "B"])

    def test_an_unrelated_landing_costs_a_recheck_not_a_rerun(self):
        f = mapped()
        f.admit("A", {"a.ts#g": "g1"})
        f.admit("B", {"b.ts#h": "h1"})
        f.record("t2", members={"A"})
        f.record("t3", members={"B"})
        seen = f.version
        self.assertIsNone(f.land_if(["A"], seen))
        self.assertEqual(f.land_if(["B"], seen), ("moved", 1))
        self.assertIsNone(f.land_if(["B"], f.version))  # B's own passes still hold

    def test_a_blocked_change_does_not_hold_up_an_independent_one(self):
        f = mapped()
        f.admit("A", {"a.ts#f": "f1"})  # its tests never pass
        f.admit("B", {"a.ts#g": "g1"})
        f.record("t2", members={"B"})
        self.assertEqual(f.land_if(["A"], f.version), ("unproven", ["t1", "t3"]))
        self.assertEqual(f.land_if(["A", "B"], f.version), ("unproven", ["t1", "t3"]))
        self.assertIsNone(f.land_if(["B"], f.version))
        self.assertEqual(f.base["a.ts#g"], "g1")
        self.assertIn("A", f.changes)

    def test_absent_members_and_unknown_units_refuse(self):
        f = mapped()
        f.admit("A", {"a.ts#f": "f1"})
        self.assertEqual(f.land_if(["A", "Z"], f.version), ("absent", ["Z"]))
        self.assertEqual(f.land_if([], f.version), ("absent", []))
        f.eject("A")
        self.assertEqual(f.land_if(["A"], f.version), ("absent", ["A"]))
        # A unit that has never passed proves nothing, even for a change it barely touches.
        f.admit("C", {"new.ts#n": "n1"})
        f.readers.setdefault("new.ts#n", set()).add("t9")
        f.reads["t9"] = {"new.ts#n"}
        self.assertEqual(f.land_if(["C"], f.version), ("unproven", ["t9"]))

    def test_a_certificate_for_another_tree_does_not_prove_this_one(self):
        f = mapped()
        f.admit("A", {"a.ts#f": "f1"})
        f.record("t1", cert=Certificate("t1", {"a.ts#f": "f2"}))  # passed on some other f
        f.record("t3", members={"A"})
        self.assertEqual(f.land_if(["A"], f.version), ("unproven", ["t1"]))

    def test_random_races_never_land_an_unproven_group(self):
        for seed in range(40):
            rng = random.Random(seed)
            keys = [f"k{i}" for i in range(30)]
            f = Frontier({k: "v0" for k in keys})
            units = {f"u{i}": rng.sample(keys, 4) for i in range(25)}
            for u, ks in units.items():
                f.record(u, ks)
            pending, cid = [], 0
            for _ in range(120):
                roll = rng.random()
                if roll < 0.3:
                    if f.admit(cid, {k: f"c{cid}" for k in rng.sample(keys, 2)}) is None:
                        pending.append(cid)
                    cid += 1
                elif roll < 0.55 and f.changes:
                    # A run of a few units on main plus some in-flight changes, maybe an old main.
                    members = set(rng.sample(list(f.changes), min(len(f.changes), rng.randint(1, 3))))
                    for u in rng.sample(list(units), 3):
                        f.record(u, members=members)
                elif roll < 0.75 and f.changes:
                    # A scheduler decides on a group: usually it proves it first, sometimes on a
                    # main it read a moment ago.
                    group = rng.sample(list(f.changes), min(len(f.changes), rng.randint(1, 3)))
                    if rng.random() < 0.7:
                        for u in f.needs(group):
                            f.record(u, members=set(group))
                    pending.append((tuple(group), f.version - (rng.random() < 0.2)))
                elif pending:
                    item = pending.pop(rng.randrange(len(pending)))
                    if not isinstance(item, tuple):
                        continue
                    group, version = item
                    tree = dict(f.base)
                    for c in group:
                        tree.update(f.changes.get(c, {}))
                    before = (dict(f.base), f.version)
                    reason = f.land_if(list(group), version)
                    if reason is None:
                        # Independent of the indexes: every unit reading a written key held a
                        # certificate matching the whole new main on everything it read.
                        self.assertEqual(version, before[1])
                        written = {k for c in group for k in tree if tree[k] != before[0].get(k)}
                        for u, ks in units.items():
                            if set(f.reads[u]) & written:
                                self.assertTrue(any(all(tree.get(k) == d for k, d in cert.inputs.items())
                                                    for cert in f.certs[u]), (seed, u, group))
                    else:
                        self.assertEqual((dict(f.base), f.version), before)


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
