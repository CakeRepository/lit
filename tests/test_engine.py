import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from unittest.mock import patch

from concurrent_merge.cli import land, main, verified_changes
from concurrent_merge.engine import Engine, Repo
from concurrent_merge.proposal import matching_group, proposal_members, propose
from concurrent_merge.structure import outline_merge
from concurrent_merge.projection import preview, project


class BatchTests(unittest.TestCase):
    def test_live_prediction_discards_a_head_admitted_during_assembly(self):
        source = self.pr(21, {"candidate.txt": b"candidate\n"})
        snapshot = dict(
            repository="owner/repo",
            branch="main",
            base=self.base,
            changes=[],
            groups=[],
        )
        pr = dict(
            state="OPEN",
            baseRefName="main",
            isCrossRepository=False,
            headRepository={"nameWithOwner": "owner/repo"},
            isInMergeQueue=False,
            headRefOid=source["head"],
            author={"login": "alice"},
        )
        with patch(
            "concurrent_merge.projection.pull_many",
            side_effect=[
                {21: (pr, None)},
                {21: ({**pr, "isInMergeQueue": True}, None)},
            ],
        ):
            with self.assertRaisesRegex(ValueError, "entered the queue"):
                preview(self.repo, "owner/repo", "main", [21], lambda *_: snapshot)
        self.assertEqual(self.repo.out("rev-parse", "main"), self.base)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = Repo(self.root / "repo.git")
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", self.repo.path], check=True
        )
        self.repo.run("config", "user.name", "Test Author")
        self.repo.run("config", "user.email", "author@localhost")
        self.engine = Engine(self.repo, self.root / "state")
        self.base = self.commit(
            None,
            {"test.txt": b"base\n", "m.ps1": b"function A { 0 }; function B { 0 }\n"},
        )
        self.repo.run("update-ref", "refs/heads/main", self.base)

    def commit(self, parent, changes):
        env = {"GIT_INDEX_FILE": str(self.root / "scratch-index")}
        if parent:
            self.repo.run("read-tree", parent, env=env)
        for file, data in changes.items():
            if data is None:
                record = ("0 " + "0" * 40 + "\t" + file + "\0").encode()
                self.repo.run(
                    "update-index", "-z", "--index-info", data=record, env=env
                )
            else:
                blob = self.repo.out("hash-object", "-w", "--stdin", data=data)
                self.repo.run(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    blob,
                    file,
                    env=env,
                )
        tree = self.repo.out("write-tree", env=env)
        args = ["-p", parent] if parent else []
        return self.repo.out("commit-tree", tree, *args, "-m", "change")

    def pr(self, n, files, parent=None):
        return dict(
            number=n, author="same-login", head=self.commit(parent or self.base, files)
        )

    def test_projection_only_uses_the_observed_queue_prefix(self):
        ready = self.pr(1, {"earlier": b"before"})
        waiting = self.pr(2, {"unbuilt": b"pending"})
        source = self.pr(3, {"next": b"after"})
        anchor = self.repo.commit(
            self.repo.out("rev-parse", ready["head"] + "^{tree}"), self.base, ready
        )
        group = dict(number=1, head=anchor, state="AWAITING_CHECKS")
        metadata = dict(base=self.base, repository="owner/repo", branch="main")
        queue = dict(metadata, changes=[ready, waiting, source], groups=[group])
        sources = dict(metadata, changes=[source])
        refs_before = self.repo.out("show-ref")
        prediction = project(self.repo, queue, sources)
        expected = self.commit(anchor, {"next": b"after"})
        landing = self.repo.commit(
            self.repo.out("rev-parse", expected + "^{tree}"), anchor, source
        )
        scored = project(self.repo, queue, sources, landing)
        self.assertEqual(prediction["from_queue"], scored["from_queue"])
        self.assertEqual(prediction["preceding_prs_outside_anchor"], [2])
        self.assertTrue(scored["observed"]["queue_tree_matched"])
        self.assertTrue(scored["observed"]["all_sources_in_landing"])
        self.assertFalse(scored["observed"]["main_tree_matched"])
        with self.assertRaisesRegex(ValueError, "Interleaved"):
            project(self.repo, dict(queue, changes=[ready, source, waiting]), sources)
        with self.assertRaisesRegex(ValueError, "predecessor"):
            project(
                self.repo, dict(queue, groups=[dict(group, head=self.base)]), sources
            )
        self.assertEqual(self.repo.out("show-ref"), refs_before)

    def test_structural_merge_and_one_test(self):
        a = self.pr(1, {"m.ps1": b"function A { 1 }; function B { 0 }\n"})
        b = self.pr(2, {"m.ps1": b"function A { 0 }; function B { 2 }\n"})
        tested = []

        def check(oid):
            self.assertEqual(
                self.repo.out("show", oid + ":m.ps1"),
                "function A { 1 }; function B { 2 }",
            )
            tested.append(oid)
            return "pass"

        final, accepted = self.engine.batch(self.base, [a, b], check)
        self.assertEqual((accepted, tested), ({1, 2}, [final]))
        self.assertEqual(
            self.engine.history[0]["resolved"][0]["method"], "powershell-outline"
        )
        for p in (a, b):
            self.repo.run("merge-base", "--is-ancestor", p["head"], final)
        self.assertEqual(self.repo.out("rev-parse", "main"), self.base)

    def test_conflict_in_a_language_without_a_reader_invites_one(self):
        base = self.commit(self.base, {"main.go": b"package main\nfunc A() {}\n"})
        a = self.pr(1, {"main.go": b"package main\nfunc A() { a() }\n"}, base)
        b = self.pr(2, {"main.go": b"package main\nfunc A() { b() }\n"}, base)
        with patch("sys.stderr") as err:
            _, accepted = self.engine.batch(base, [a, b], lambda _: "pass")
        self.assertEqual(accepted, {1})
        key = self.engine.history[0]["pending"][0]["conflicts"][0]
        issue = json.loads((self.engine.state / "conflicts" / (key + ".json")).read_text())
        self.assertIn("no reader for .go files", issue["hint"])
        self.assertIn("github.com/CakeRepository/lit/issues", issue["hint"])
        printed = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertEqual(printed.count("no reader for .go"), 1)

    def test_two_agents_same_login_must_agree(self):
        a = self.pr(1, {"test.txt": b"A"})
        b = self.pr(2, {"test.txt": b"B", "paired.txt": b"B"})
        c = self.pr(3, {"independent.txt": b"C"})
        changes = [a, b, c]

        def rerun():
            engine = Engine(self.repo, self.engine.state)
            final, accepted = engine.batch(self.base, changes, lambda _: "pass")
            return engine, final, accepted

        engine, final, accepted = rerun()
        self.assertEqual(accepted, {1, 3})
        self.assertNotIn("paired.txt", self.repo.out("ls-tree", "--name-only", final))
        key = engine.history[0]["pending"][0]["conflicts"][0]
        issue = json.loads((engine.state / "conflicts" / (key + ".json")).read_text())
        self.assertEqual(len(issue["authors"]), 2)
        left, right = issue["authors"]
        engine.agree(key, left, b"A+B")
        self.assertEqual(rerun()[2], {1, 3})
        engine.agree(key, right, b"A")
        self.assertEqual(rerun()[2], {1, 3})
        engine.agree(key, right, b"A+B")
        _, final, accepted = rerun()
        self.assertEqual(accepted, {1, 2, 3})
        self.assertEqual(self.repo.out("show", final + ":test.txt"), "A+B")

    def test_split_rechecks_interactions_and_recovers_held_change(self):
        bad = self.pr(1, {"test.txt": b"bad"})
        good = self.pr(2, {"test.txt": b"good"})
        a = self.pr(3, {"a": b"A"})
        b = self.pr(4, {"b": b"B"})

        def test(oid):
            names = self.repo.out("ls-tree", "--name-only", oid).splitlines()
            failed = self.repo.out("show", oid + ":test.txt") == "bad" or {
                "a",
                "b",
            } <= set(names)
            return "fail" if failed else "pass"

        final, accepted = self.engine.batch(self.base, [bad, good, a, b], test)
        self.assertEqual(accepted, {2, 3})
        self.assertEqual(test(final), "pass")
        self.assertEqual(self.repo.out("show", final + ":test.txt"), "good")
        self.assertEqual(
            {a["returned"]["number"] for a in self.engine.history if "returned" in a},
            {1, 4},
        )

    def test_blame_sends_back_the_named_change_without_splitting(self):
        changes = [self.pr(n, {f"f{n}": b"x" if n != 3 else b"bad"}) for n in (1, 2, 3, 4)]
        tested = []

        def test(oid):
            tested.append(oid)
            names = self.repo.out("ls-tree", "--name-only", oid).splitlines()
            if "f3" in names:
                return ("fail", {"eject": ["3"]})
            return "pass"

        final, accepted = self.engine.batch(self.base, changes, test)
        self.assertEqual(accepted, {1, 2, 4})
        self.assertEqual(len(tested), 2)  # the group once, then the rest once
        self.assertEqual(
            [r["number"] for e in self.engine.history for r in e.get("returned_by_blame", [])], [3]
        )

    def test_split_reuses_only_exact_failed_commits_within_one_batch(self):
        first = self.pr(1, {"visible": b"change"})
        second = self.pr(2, {})
        checked = []

        def check(oid):
            checked.append(oid)
            return (
                "fail"
                if self.repo.out("rev-parse", oid + "^2") == second["head"]
                else "pass"
            )

        final, passed = self.engine.batch(self.base, [first, second], check)
        full, prefix, suffix = self.engine.history
        self.assertEqual(full["tree"], prefix["tree"])
        self.assertEqual(full["candidate"], suffix["candidate"])
        self.assertEqual(suffix["result"], "known-failure")
        self.assertEqual(suffix["returned"]["number"], 2)
        self.assertEqual(checked, [full["candidate"], final])
        self.assertEqual(passed, {1})

        _, passed = self.engine.batch(self.base, [first, second], lambda _: "pass")
        self.assertEqual(passed, {1, 2})

    def test_stacked_head_cannot_reintroduce_failed_dependency(self):
        a = self.pr(1, {"broken": b"x"})
        b = self.pr(2, {"other": b"y"}, parent=a["head"])
        c = self.pr(3, {"good": b"z"})
        ordered = verified_changes(self.repo, dict(base=self.base, changes=[b, a, c]))
        self.assertEqual(next(p for p in ordered if p["number"] == 2)["requires"], [1])

        def test(oid):
            return (
                "fail"
                if "broken" in self.repo.out("ls-tree", "--name-only", oid)
                else "pass"
            )

        _, accepted = self.engine.batch(self.base, ordered, test)
        self.assertEqual(accepted, {3})

    def test_disjoint_history_matches_three_way_after_target_advances(self):
        # The first PR has two commits. The second only modifies an existing
        # file whose blob and mode are still identical on the newer target.
        prep = self.commit(self.base, {"test.txt": b"draft"})
        a = self.pr(1, {"test.txt": b"final"}, parent=prep)
        b = self.pr(2, {"m.ps1": b"42"})
        target = self.commit(self.base, {"other": b"already landed"})
        trees = []

        def check(oid):
            trees.append(self.repo.out("rev-parse", oid + "^{tree}"))
            return "pass"

        tip, ready = self.engine.batch(target, [a, b], check)
        self.assertEqual(ready, {1, 2})
        self.assertEqual(self.engine.history[0]["assembly"], "disjoint")
        self.assertEqual(self.repo.out("rev-parse", tip + "^2"), b["head"])
        self.assertEqual(self.repo.out("rev-parse", tip + "^1^2"), a["head"])
        self.assertEqual(self.repo.out("rev-parse", tip + "^1^1"), target)
        self.assertEqual(
            self.repo.out("for-each-ref", "refs/concurrent-merge/scratch"), ""
        )
        with patch("concurrent_merge.engine.disjoint_deltas", return_value=None):
            Engine(self.repo, self.engine.state).batch(target, [a, b], check)
        self.assertEqual(trees[0], trees[1])
        changed = self.commit(target, {"test.txt": b"from main"})
        _, ready = self.engine.batch(changed, [a, b], lambda _: "pass")
        self.assertEqual(self.engine.history[-1]["assembly"], "three-way")
        self.assertEqual(ready, {2})

    def test_disjoint_additions_preserve_names_and_bisect(self):
        a = self.pr(1, {"nested/a b-é.txt": b"A"})
        b = self.pr(2, {"nested/b": b"B"})

        def check(oid):
            return (
                "fail"
                if self.repo.run(
                    "cat-file", "-e", oid + ":nested/b", check=False
                ).returncode
                == 0
                else "pass"
            )

        final, ready = self.engine.batch(self.base, [a, b], check)
        self.assertEqual(self.engine.history[0]["assembly"], "disjoint")
        self.assertEqual(ready, {1})
        self.assertEqual(self.repo.out("show", final + ":nested/a b-é.txt"), "A")
        collision = self.pr(3, {"nested": b"file"})
        _, ready = self.engine.batch(self.base, [a, collision], lambda _: "pass")
        self.assertEqual(self.engine.history[-1]["assembly"], "three-way")
        self.assertEqual(ready, {1})

    def test_overlapping_files_use_concurrent_structural_workers(self):
        names = ("dir/a.txt", "dir/b.txt")
        base = self.commit(self.base, dict.fromkeys(names, b"original"))
        a = self.pr(1, dict.fromkeys(names, b"left"), parent=base)
        b = self.pr(2, dict.fromkeys(names, b"right"), parent=base)
        gate = threading.Barrier(2, timeout=10)

        def resolve(path, blobs):
            gate.wait()
            return b"agreed " + path.encode(), "test-driver"

        with patch("concurrent_merge.engine.resolve_file", side_effect=resolve):
            engine = Engine(self.repo, self.engine.state, jobs=2)
            final, ready = engine.batch(base, [a, b], lambda _: "pass")
        self.assertEqual(ready, {1, 2})
        self.assertEqual(engine.history[0]["assembly"], "three-way")
        for path in names:
            self.assertEqual(
                self.repo.out("show", final + ":" + path), "agreed " + path
            )

    def test_stale_addition_and_directory_rename(self):
        base = self.commit(self.base, {"old/readme": b"original"})
        a = self.pr(1, {"old/new": b"added"}, parent=base)
        b = self.pr(2, {"test.txt": b"existing edit"}, parent=base)
        target = self.commit(base, {"unrelated": b"target-only"})
        tip, ready = self.engine.batch(target, [a, b], lambda _: "pass")
        self.assertEqual(self.engine.history[-1]["assembly"], "disjoint")
        self.assertEqual(ready, {1, 2})
        self.assertEqual(self.repo.out("show", tip + ":old/new"), "added")

        moved = self.commit(target, {"old/readme": None, "moved/readme": b"original"})
        tip, ready = self.engine.batch(moved, [a, b], lambda _: "pass")
        self.assertEqual(self.engine.history[-1]["assembly"], "three-way")
        with patch("concurrent_merge.engine.disjoint_deltas", return_value=None):
            reference, expected = Engine(self.repo, self.engine.state).batch(
                moved, [a, b], lambda _: "pass"
            )
        self.assertEqual(ready, expected)
        self.assertEqual(self.repo.paths(tip, reference), set())

    def test_hidden_common_branch_history_uses_three_way(self):
        common = self.commit(self.base, {"test.txt": b"intermediate"})
        a = self.pr(1, {"test.txt": b"base\n", "one": b"A"}, parent=common)
        b = self.pr(2, {"test.txt": b"base\n", "two": b"B"}, parent=common)
        tip, ready = self.engine.batch(self.base, [a, b], lambda _: "pass")
        self.assertEqual(self.engine.history[-1]["assembly"], "three-way")
        self.assertEqual(ready, {1, 2})
        self.assertEqual(self.repo.out("show", tip + ":test.txt"), "base")

    def test_disjoint_edits_verified_without_launching_driver(self):
        source = b"A\n1\n2\n3\nZ"
        self.base = self.commit(self.base, {"code.py": source})
        a = self.pr(1, {"code.py": source.replace(b"A", b"first")})
        b = self.pr(2, {"code.py": source.replace(b"Z", b"last")})
        with patch(
            "concurrent_merge.engine.resolve_file", return_value=(None, None)
        ) as driver:
            final, accepted = self.engine.batch(self.base, [a, b], lambda _: "pass")
        driver.assert_not_called()
        self.assertEqual(accepted, {1, 2})
        self.assertEqual(
            self.repo.run("show", final + ":code.py").stdout, b"first\n1\n2\n3\nlast"
        )

    def test_delete_modify_requires_a_revised_pr(self):
        a = self.pr(1, {"test.txt": None})
        b = self.pr(2, {"test.txt": b"modified"})
        _, accepted = self.engine.batch(self.base, [a, b], lambda _: "pass")
        self.assertEqual(accepted, {1})
        key = self.engine.history[0]["pending"][0]["conflicts"][0]
        issue = json.loads(
            (self.engine.state / "conflicts" / (key + ".json")).read_text()
        )
        self.assertIsNone(issue["mode"])
        self.assertIn("CONFLICT (modify/delete)", issue["kinds"])

    def test_cli_runs_exact_checkout_and_stale_target_refuses_landing(self):
        pr = self.pr(1, {"test.txt": b"tested\n"})
        snapshot = dict(
            repository="example/repo", branch="main", base=self.base, changes=[pr]
        )
        path = self.root / "queue.json"
        path.write_text(json.dumps(snapshot))
        args = [
            "tool",
            "run",
            str(path),
            "--repo",
            str(self.repo.path),
            "--state",
            str(self.engine.state),
            "--test",
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('test.txt').read_text()=='tested\\n'",
        ]
        with patch.object(sys, "argv", args):
            self.assertEqual(main(), 0)
        (receipt,) = self.engine.state.glob("runs/*/result.json")
        record = json.loads(receipt.read_text())
        self.assertEqual(record["tested"], [1])
        self.assertEqual(self.repo.out("rev-parse", "main"), self.base)
        with patch(
            "concurrent_merge.cli.queue", return_value={**snapshot, "base": pr["head"]}
        ):
            with self.assertRaisesRegex(ValueError, "eligibility changed"):
                land(Namespace(receipt=receipt, repo=self.repo.path))

    def test_proposal_only_accepts_queued_heads_or_heads_already_on_the_target(self):
        first = self.pr(1, {"new-one": b"one"})
        second = self.pr(2, {"new-two": b"two"})
        changes = [first, second]
        final, accepted = self.engine.batch(self.base, changes, lambda _: "pass")
        snapshot = dict(base=self.base, changes=changes)
        record = dict(snapshot=snapshot, tested=sorted(accepted), final=final)
        ref = "refs/concurrent-merge/tested/test-proposal"
        self.repo.run("update-ref", ref, final)

        def verify(target, queued):
            return proposal_members(
                self.repo, record, ref, dict(base=target, changes=queued)
            )

        self.assertEqual(verify(self.base, changes)[:2], ([1, 2], []))
        self.assertEqual(verify(first["head"], [second])[:2], ([2], [1]))
        with self.assertRaisesRegex(ValueError, "left the merge queue"):
            verify(self.base, [first])
        with self.assertRaisesRegex(ValueError, "changed revision"):
            verify(self.base, [first, {**second, "head": first["head"]}])
        with self.assertRaisesRegex(ValueError, "tested tree"):
            verify(self.commit(first["head"], {"third": b"later"}), [second])
        with self.assertRaisesRegex(ValueError, "already contains"):
            verify(final, [])
        tree = self.repo.out("rev-parse", final + "^{tree}")
        same_files = self.repo.out("commit-tree", tree, "-p", self.base, "-m", "copy")
        group = dict(number=2, head=final, tree=tree, state="AWAITING_CHECKS")
        self.assertEqual(
            matching_group(self.repo, record, dict(base=self.base, groups=[group])),
            group,
        )
        self.assertIsNone(
            matching_group(
                self.repo,
                record,
                dict(base=self.base, groups=[{**group, "head": same_files}]),
            )
        )
        self.assertIsNone(
            matching_group(
                self.repo,
                record,
                dict(base=self.base, groups=[{**group, "state": "UNMERGEABLE"}]),
            )
        )

    def test_propose_creates_only_a_topic_ref_and_reuses_the_open_draft(self):
        remote = Repo(self.root / "remote.git")
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", remote.path], check=True
        )
        destination = "https://github.com/example/repo.git"
        self.repo.run(
            "config", "url." + remote.path.as_uri() + ".insteadOf", destination
        )
        self.repo.run("push", destination, self.base + ":refs/heads/main")
        pr = self.pr(1, {"topic": b"change"})
        final, accepted = self.engine.batch(self.base, [pr], lambda _: "pass")
        snapshot = dict(
            repository="example/repo", branch="main", base=self.base, changes=[pr]
        )
        receipt = self.root / "batch-one" / "result.json"
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps(dict(snapshot=snapshot, final=final, tested=list(accepted)))
        )
        self.repo.run("update-ref", "refs/concurrent-merge/tested/batch-one", final)
        args = Namespace(
            receipt=receipt,
            repo=self.repo.path,
            head="topic/batch",
            title=None,
            notes=None,
        )
        native = dict(
            number=1,
            head=final,
            tree=self.repo.out("rev-parse", final + "^{tree}"),
            state="AWAITING_CHECKS",
        )
        with patch("concurrent_merge.proposal.prior_pulls", return_value=[]) as gh:
            propose(args, lambda *_: {**snapshot, "groups": [native]})
            self.assertEqual(gh.call_count, 1)
        self.assertFalse(remote.out("for-each-ref", "refs/heads/topic/batch"))
        url = "https://github.com/example/repo/pull/2"
        def graphql(query, variables=None, raise_on_errors=True):
            if "createPullRequest" in query:
                return dict(data=dict(createPullRequest=dict(pullRequest=dict(url=url))))
            if "pullRequests" in query:
                return dict(data=dict(repository=dict(pullRequests=dict(nodes=[]))))
            return dict(data=dict(repository=dict(id="R_1")))

        with patch("concurrent_merge.github.graphql", side_effect=graphql) as gh:
            propose(args, lambda *_: snapshot)
            create = [c for c in gh.call_args_list if "createPullRequest" in c.args[0]]
            self.assertEqual(len(create), 1)
            self.assertIn("draft:true", create[0].args[0].replace(" ", ""))
        self.assertEqual(remote.out("rev-parse", "refs/heads/main"), self.base)
        self.assertEqual(remote.out("rev-parse", "refs/heads/topic/batch"), final)
        existing = dict(
            isCrossRepository=False, state="OPEN", headRefOid=final, url=url
        )
        with patch(
            "concurrent_merge.proposal.prior_pulls", return_value=[existing]
        ) as gh:
            propose(args, lambda *_: snapshot)
            self.assertEqual(gh.call_count, 1)

    def test_unicode_class_methods_crlf_bom_and_overlapping_change(self):
        base = "# 😀\r\nclass Foo {\r\n[int] A() { return 1 }\r\n[int] B() { return 2 }\r\n}\r\n"

        def encode(s):
            return b"\xef\xbb\xbf" + s.encode()

        left, right = (
            base.replace("return 1", "return 3"),
            base.replace("return 2", "return 4"),
        )
        merge = lambda *texts: outline_merge("m.ps1", list(map(encode, texts)))
        self.assertEqual(merge(base, left, right), encode(left.replace("return 2", "return 4")))
        self.assertIsNone(merge(base, left, base.replace("return 1", "return 5")))

    def test_blocks_inside_one_function_go_back_to_the_author(self):
        base = b"function Get-Value { if ($x) { return 1 } else { return 2 } }"
        left = base.replace(b"return 1", b"return 3")
        right = base.replace(b"return 2", b"return 4")
        self.assertIsNone(outline_merge("m.ps1", [base, left, right]))
