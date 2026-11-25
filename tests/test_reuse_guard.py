"""lit must not lose every record to one run-specific variable, and must flag a run that reuses nothing.

A FlowRMM run started ignoring a new variable that held an absolute path. Records written before then still
held it, and only the record's own ignore list was applied, so nearly every file reran on every run until
someone noticed by hand.
"""
import unittest

from concurrent_merge.footprint import environment, environment_changes
from concurrent_merge.unit_store import starved


class EnvironmentIgnoredByThisRun(unittest.TestCase):
    def test_a_variable_this_run_ignores_does_not_force_a_rerun_of_an_older_record(self):
        old = {"PATH": "/usr/bin", "PARSER_PATHS": "/runner-a/web"}
        manifest = dict(environment=environment(old, ["GITHUB_*"]), ignored_environment=["GITHUB_*"])
        now = {"PATH": "/usr/bin", "PARSER_PATHS": "/runner-b/web"}
        self.assertEqual([r["name"] for r in environment_changes(manifest, now)], ["PARSER_PATHS"])
        self.assertEqual(environment_changes(manifest, now, ["PARSER_PATHS"]), [])

    def test_a_record_without_the_variable_matches_a_run_that_ignores_it(self):
        manifest = dict(environment=environment({"PATH": "/usr/bin"}, []), ignored_environment=[])
        now = {"PATH": "/usr/bin", "PARSER_PATHS": "/x"}
        self.assertEqual(environment_changes(manifest, now, ["PARSER_PATHS"]), [])

    def test_other_variables_still_count(self):
        manifest = dict(environment=environment({"PATH": "/usr/bin", "TZ": "UTC"}, []), ignored_environment=[])
        changes = environment_changes(manifest, {"PATH": "/usr/bin", "TZ": "Europe/Paris"}, ["LIT_*"])
        self.assertEqual([r["name"] for r in changes], ["TZ"])


def decision(reuse, kind="changed"):
    return dict(reuse=reuse, reasons=[] if reuse else [dict(kind=kind)])


class StarvedReuse(unittest.TestCase):
    def test_records_for_most_files_and_no_reuse_is_starved(self):
        d = {f"f{i}": decision(False, "environment") for i in range(40)}
        d.update({f"n{i}": decision(False, "new") for i in range(10)})
        self.assertTrue(starved(d))

    def test_any_reuse_is_not_starved(self):
        d = {f"f{i}": decision(False) for i in range(40)}
        d["ok"] = decision(True)
        self.assertFalse(starved(d))

    def test_mostly_new_files_are_not_starved(self):
        d = {f"n{i}": decision(False, "new") for i in range(40)}
        d.update({f"f{i}": decision(False) for i in range(10)})
        self.assertFalse(starved(d))

    def test_small_suites_are_not_judged(self):
        self.assertFalse(starved({f"f{i}": decision(False) for i in range(5)}))


if __name__ == "__main__":
    unittest.main()
