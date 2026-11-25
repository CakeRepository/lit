from datetime import datetime, timedelta, timezone
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from concurrent_merge import proof
from concurrent_merge.unit_store import cached_units
from tests.test_unit_store import TEST, traced

COMMIT, TREE = "a" * 40, "b" * 40
IMAGE = "debian@sha256:" + "c" * 64


def record(commit=COMMIT, name="tests/test_a.py", exit_code=0, read=None, seconds=1.0):
    tracked = {"src/a.py": read} if read else {}
    return dict(kind="lit-unit-footprints", commit=commit, tracked=tracked, external={},
                units={name: dict(exit_code=exit_code, incomplete=[], seconds=seconds,
                                  tracked=sorted(tracked))})


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class ProofTests(unittest.TestCase):
    """Keys here are throwaway test keys made in a temporary folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.keys = {name: self.key(name) for name in ("bench", "laptop")}
        self.allowed = self.root / "allowed_signers"
        self.allowed.write_text(f"bench@lit namespaces=\"lit-proof\" {self.keys['bench'].with_suffix('.pub').read_text()}")

    def key(self, name):
        path = self.root / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True)
        return path

    def packed(self, store=None, **kw):
        store = store or self.root / "store"
        unit = store / "unit" / "units"
        unit.mkdir(parents=True, exist_ok=True)
        (unit / "a.json").write_text(json.dumps(record()))
        (unit / "old.json").write_text(json.dumps(record(commit="d" * 40, name="tests/test_old.py")))
        (unit / "failed.json").write_text(json.dumps(record(name="tests/test_f.py", exit_code=1)))
        out = proof.pack(store, ["unit"], repository="o/r", commit=COMMIT, tree=TREE, image=IMAGE,
                         out=self.root / "p" / "proof.json.gz", **kw)
        return out, proof.sign(out, self.keys["bench"])

    def check(self, path, sig, **kw):
        options = dict(repository="o/r", commits=[COMMIT], image=IMAGE)
        options.update(kw)
        return proof.verify(path, sig, self.allowed, **options)

    def test_a_signed_proof_carries_only_this_commits_passing_records(self):
        path, sig = self.packed()
        payload = self.check(path, sig)
        self.assertEqual(payload["signer"], "bench@lit")
        self.assertTrue(payload["fingerprint"].startswith("SHA256:"))
        self.assertEqual(list(payload["jobs"]["unit"]["units"]), ["tests/test_a.py"])
        store = self.root / "taken"
        self.assertEqual(proof.unpack(payload, store), 1)
        self.assertEqual(len(list((store / "unit" / "units").glob("*.json"))), 1)

    def test_every_mismatch_is_rejected(self):
        path, sig = self.packed()
        now = datetime.now(timezone.utc)
        cases = {
            "other repository": dict(repository="o/other"),
            "other commit": dict(commits=["e" * 40]),
            "other image": dict(image="debian@sha256:" + "f" * 64),
            "too old": dict(now=now + timedelta(hours=73)),
            "revoked key": dict(revoked=[self.check(path, sig)["fingerprint"]]),
        }
        for label, kw in cases.items():
            with self.subTest(label), self.assertRaises(proof.Rejected):
                self.check(path, sig, **kw)

    def test_a_key_not_listed_or_changed_contents_are_rejected(self):
        path, _ = self.packed()
        laptop = proof.sign(path, self.keys["laptop"])
        with self.assertRaisesRegex(proof.Rejected, "allowed signer"):
            self.check(path, laptop)
        path, sig = self.packed()
        payload = json.loads(gzip.decompress(path.read_bytes()))
        payload["jobs"]["unit"]["units"]["tests/test_b.py"] = record(name="tests/test_b.py")
        path.write_bytes(gzip.compress(json.dumps(payload).encode()))
        with self.assertRaisesRegex(proof.Rejected, "does not match"):
            self.check(path, sig)

    def test_a_failing_record_smuggled_into_a_signed_proof_is_rejected(self):
        store = self.root / "store"
        path, _ = self.packed(store)
        payload = json.loads(gzip.decompress(path.read_bytes()))
        payload["jobs"]["unit"]["units"]["tests/test_f.py"] = record(name="tests/test_f.py", exit_code=1)
        path.write_bytes(gzip.compress(json.dumps(payload).encode()))
        sig = proof.sign(path, self.keys["bench"])
        with self.assertRaisesRegex(proof.Rejected, "not a passing record"):
            self.check(path, sig)

    def test_signers_come_from_the_target_branch_not_the_change(self):
        repo = self.root / "repo"
        run = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@l", *a],
                                        check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / ".lit").mkdir()
        (repo / ".lit" / "allowed_signers").write_text(self.allowed.read_text())
        run("add", "-A"); run("commit", "-qm", "signers")
        run("checkout", "-qb", "pr")
        (repo / ".lit" / "allowed_signers").write_text(
            f"laptop@lit {self.keys['laptop'].with_suffix('.pub').read_text()}")
        run("commit", "-qam", "add my own key")
        path, _ = self.packed()
        laptop = proof.sign(path, self.keys["laptop"])
        allowed = proof.signers_at(repo, "main", self.root / "from-main")
        with self.assertRaises(proof.Rejected):
            proof.verify(path, laptop, allowed, repository="o/r", commits=[COMMIT])


@unittest.skipUnless(shutil.which("ssh-keygen") and shutil.which("strace") and not traced(),
                     "needs ssh-keygen, strace and no outer tracer")
class ProofRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work, self.store, self.marker = self.root / "work", self.root / "store", self.root / "marker"
        self.git("init", "-q", "-b", "main", str(self.work), cwd=self.root)
        files = {"README.md": "x\n"}
        for name in ("a", "b"):
            files[f"src/{name}.py"] = "ok\n"
            files[f"tests/test_{name}.py"] = TEST.format(name=name)
        files["tests/test_ab.py"] = """import os, sys
open(os.environ["LIT_TEST_MARKER"], "a").write("ab\\n")
sys.exit(0 if all("ok" in open(f"src/{n}.py").read() for n in "ab") else 1)
"""
        self.commit(files)
        env = patch.dict(os.environ, {"LIT_TEST_MARKER": str(self.marker)})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args],
                              cwd=cwd or self.work, check=True, capture_output=True).stdout.decode().strip()

    def commit(self, files):
        for path, text in files.items():
            (self.work / path).parent.mkdir(parents=True, exist_ok=True)
            (self.work / path).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")

    def units(self, store, **kw):
        self.marker.write_text("")
        result = cached_units(self.work, "unit", store, [sys.executable, "{}"],
                              discover=["tests/test_*.py"], log=os.devnull, **kw)
        return result, sorted(self.marker.read_text().split())

    def test_a_proof_pushed_from_one_machine_lets_the_merge_skip_what_it_covers(self):
        self.units(self.store)                               # main's records at the base
        bench = self.root / "bench-store"
        shutil.copytree(self.store, bench)
        self.git("checkout", "-q", "-b", "pr")
        self.commit({"src/a.py": "ok from the pr\n"})
        pr_head = self.git("rev-parse", "HEAD")
        self.units(bench, mode="enforce")                    # the signing machine runs the PR
        key = self.root / "bench"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        allowed = self.root / "allowed"
        allowed.write_text("bench@lit " + key.with_suffix(".pub").read_text())
        packed = proof.pack(bench, ["unit"], repository="o/r", commit=pr_head,
                            tree=self.git("rev-parse", "HEAD^{tree}"), image=IMAGE,
                            out=self.root / "out" / "proof.json.gz")
        sig = proof.sign(packed, key)
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        ref = proof.push(packed, sig, str(remote), self.work)
        self.assertTrue(ref.startswith(proof.REF_PREFIX + pr_head + "/"))
        self.assertEqual(proof.push(packed, sig, str(remote), self.work), ref)  # pushing again is a no-op

        self.git("checkout", "-q", "main")
        self.commit({"src/b.py": "ok from main\n"})
        self.units(self.store, mode="enforce")
        self.git("merge", "-q", "--no-edit", "pr")
        taken = self.root / "taken"
        accepted = proof.take(str(remote), self.work, [pr_head], taken, repository="o/r",
                              allowed_signers=allowed, image=IMAGE, log=lambda *_: None)
        self.assertEqual([a["signer"] for a in accepted], ["bench@lit"])
        proved, ran = self.units(self.store, mode="plan", host=False, proofs=[taken])
        self.assertEqual(ran, [])
        self.assertEqual(sorted(proved["decisions"]), ["tests/test_ab.py"])  # read both sides' changes
        self.assertEqual(proved["reusable_by_proof"], 1)
        nothing = proof.take(str(remote), self.work, [pr_head], self.root / "none", repository="o/other",
                             allowed_signers=allowed, log=lambda *_: None)
        self.assertEqual(nothing, [])

        # A new signer earns nothing yet: with an audit book, everything it vouched for reruns
        # here, and each clean rerun goes on its record.
        book = self.root / "book.json"
        audited, ran = self.units(self.store, mode="enforce", host=False, proofs=[taken], audit_book=book)
        self.assertEqual(audited["audited"], 1)                  # tests/test_a.py, from the proof
        self.assertEqual(ran, ["a", "ab"])
        self.assertEqual(audited["signer_audits"], {"bench@lit": dict(ok=1, wrong=0)})
        self.assertEqual(proof.Book.load(book).data["bench@lit"]["clean"], 1)


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class AgreementTests(unittest.TestCase):
    """Throwaway keys for three machines; two of them are allowed to sign."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.allowed = self.root / "allowed_signers"
        self.keys = {}
        for name in ("bench", "laptop", "ci"):
            key = self.root / name
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)], check=True)
            self.keys[name] = key
            with self.allowed.open("a") as out:
                out.write(f"{name}@lit {key.with_suffix('.pub').read_text()}")
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def publish(self, signer, **unit):
        store = self.root / f"store-{signer}"
        (store / "unit" / "units").mkdir(parents=True, exist_ok=True)
        (store / "unit" / "units" / "a.json").write_text(json.dumps(record(**unit)))
        out = proof.pack(store, ["unit"], repository="o/r", commit=COMMIT, tree=TREE, image=IMAGE,
                         out=self.root / f"p-{signer}" / "proof.json.gz")
        proof.push(out, proof.sign(out, self.keys[signer]), str(self.remote), self.repo)

    def take(self, into="taken", **kw):
        log = []
        accepted = proof.take(str(self.remote), self.repo, [COMMIT], self.root / into, repository="o/r",
                              allowed_signers=self.allowed, image=IMAGE, log=log.append, **kw)
        return accepted, proof.vouchers(self.root / into, "unit", "tests/test_a.py"), log

    def test_what_two_machines_must_report_alike_leaves_out_timing(self):
        same = proof.agreement_key(record(read="x", seconds=1.0), "tests/test_a.py")
        self.assertEqual(same, proof.agreement_key(record(read="x", seconds=9.0), "tests/test_a.py"))
        self.assertNotEqual(same, proof.agreement_key(record(read="y"), "tests/test_a.py"))
        self.assertNotEqual(same, proof.agreement_key(record(read="x", exit_code=1), "tests/test_a.py"))

    def test_one_signer_is_not_enough_when_two_are_required(self):
        self.publish("bench", read="x")
        accepted, vouched, _ = self.take(quorum=2)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(vouched, [])
        self.publish("laptop", read="x", seconds=3.0)
        _, vouched, _ = self.take(into="taken2", quorum=2)
        self.assertEqual(vouched, ["bench@lit", "laptop@lit"])

    def test_signers_who_disagree_about_a_file_leave_it_to_run(self):
        self.publish("bench", read="x")
        self.publish("laptop", read="y")
        _, vouched, log = self.take()
        self.assertEqual(vouched, [])
        self.assertTrue(any("disagree" in line for line in log))

    def test_a_failed_audit_drops_what_that_signer_signed_before_it(self):
        self.publish("bench", read="x")
        book = proof.Book()
        book.record("bench@lit", False, now=datetime.now(timezone.utc) + timedelta(seconds=1))
        accepted, vouched, log = self.take(book=book)
        self.assertEqual((accepted, vouched), ([], []))
        self.assertTrue(any("failed an audit" in line for line in log))

    def test_each_clean_streak_halves_a_signers_share_and_a_miss_resets_it(self):
        book = proof.Book(step=2, floor=0.2)
        self.assertEqual(book.share("bench@lit"), 1)
        for expected in (1, 0.5, 0.5, 0.25, 0.25, 0.2):
            book.record("bench@lit", True)
            self.assertEqual(book.share("bench@lit"), expected)
        book.record("bench@lit", False)
        self.assertEqual(book.share("bench@lit"), 1)
        self.assertIsNotNone(book.dropped_before("bench@lit"))
        self.assertEqual(book.share("laptop@lit"), 1)

    def test_the_book_round_trips_and_will_not_overwrite_a_newer_one(self):
        path, other = self.root / "book.json", self.root / "other.json"
        proof.book_pull(str(self.remote), self.repo, path)
        proof.book_pull(str(self.remote), self.repo, other)
        book = proof.Book.load(path)
        book.record("bench@lit", True)
        book.save(path)
        proof.book_push(path, str(self.remote), self.repo)
        self.assertEqual(proof.book_pull(str(self.remote), self.repo, self.root / "again.json").data["bench@lit"]["clean"], 1)
        stale = proof.Book.load(other)
        stale.record("bench@lit", False)
        stale.save(other)
        with self.assertRaises(subprocess.CalledProcessError):
            proof.book_push(other, str(self.remote), self.repo)
