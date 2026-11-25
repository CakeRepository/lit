import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time

from .parallel import (
    disjoint_deltas,
    import_chain,
    ordered_map,
    replace_blobs,
    tree_entries,
    worker_count,
)
from .structure import choose, has_driver, resolve_file


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf8"
    ) as f:
        json.dump(data, f, indent=2, ensure_ascii=True)
        f.write("\n")
    os.replace(f.name, path)


def git(*paths):
    """git with the given repositories trusted.

    CI containers often check a repository out as one user and run as another
    (root in a runner's container); git then refuses the repository as of
    "dubious ownership" and every lit call fails. The directory lit was pointed
    at is the one being worked on, so trusting it is no wider than the run."""
    argv = ["git"]
    for path in paths:
        path = Path(path).resolve()
        # A local clone reads the source through its git directory, which git checks by that path.
        for trusted in (path, path / ".git"):
            argv += ["-c", f"safe.directory={trusted}"]
    return argv


def shared_clone(source, destination):
    """argv for a no-checkout clone sharing `source`'s objects, trusting `source`.

    A local clone reads the source through a separate upload-pack, which does not
    inherit -c settings, so that process is told to trust the source itself."""
    source = Path(source).resolve()
    pack = " ".join(shlex.quote(a) for a in [*git(source), "upload-pack"])
    return [*git(source), "clone", "-q", "--shared", "--no-checkout",
            "--upload-pack", pack, str(source), str(destination)]


class Repo:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def run(self, *args, data=None, env=None, check=True):
        proc = subprocess.run(
            [
                *git(self.path),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "-C",
                self.path,
                *args,
            ],
            input=data,
            capture_output=True,
            env={**os.environ, **(env or {})},
        )
        if check and proc.returncode:
            raise RuntimeError(
                f"git {args[0]}: {proc.stderr.decode(errors='replace').strip()}"
            )
        return proc

    def out(self, *args, **kw):
        return self.run(*args, **kw).stdout.decode().strip()

    def blobs(self, objects):
        """Read a bounded set of blob revisions in one binary-safe Git call."""
        return self.objects(objects, b"blob")

    def objects(self, objects, kind):
        queries = b"".join(os.fsencode(obj) + b"\0" for obj in objects)
        data = self.run("cat-file", "-Z", "--batch", data=queries).stdout
        contents, offset = [], 0
        for obj in objects:
            end = data.index(b"\0", offset)
            header = data[offset:end].split()
            if len(header) != 3 or header[1] != kind:
                raise ValueError("Unexpected Git object type in batch response")
            if kind == b"commit" and header[0] != obj.encode("ascii"):
                raise ValueError("Unexpected commit in Git batch response")
            offset = end + 1 + int(header[2])
            if data[offset : offset + 1] != b"\0":
                raise ValueError("Incomplete Git object stream")
            contents.append(data[end + 1 : offset])
            offset += 1
        if offset != len(data):
            raise ValueError("Unexpected trailing Git object data")
        return contents

    def commit(self, tree, ours, pr, timestamp=None):
        identity = dict(
            GIT_AUTHOR_NAME="Concurrent Merge",
            GIT_COMMITTER_NAME="Concurrent Merge",
            GIT_AUTHOR_EMAIL="batch@localhost",
            GIT_COMMITTER_EMAIL="batch@localhost",
        )
        if timestamp is not None:
            identity["GIT_AUTHOR_DATE"] = identity["GIT_COMMITTER_DATE"] = (
                f"@{timestamp} +0000"
            )
        msg = commit_message(pr)
        parents = ["-p", ours]
        if ours != pr["head"]:
            parents.extend(["-p", pr["head"]])
        return self.out("commit-tree", tree, *parents, env=identity, data=msg.encode())

    def paths(self, before, after):
        data = self.run(
            "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", before, after
        ).stdout
        return set(os.fsdecode(p) for p in data.split(b"\0") if p)


def parse_merge(output):
    tree, _, rest = output.partition(b"\0")
    entries = {}
    records = iter(rest.split(b"\0"))
    for row in records:
        if not row:
            break
        meta, path = row.split(b"\t", 1)
        mode, blob, stage = meta.decode().split()
        entries.setdefault(os.fsdecode(path), {})[stage] = [mode, blob]
    messages = []
    for count in records:
        if not count:
            break
        paths = [os.fsdecode(next(records)) for _ in range(int(count))]
        kind = next(records).decode()
        message = next(records).decode(errors="replace")
        messages.append(dict(kind=kind, paths=paths, message=message))
    return tree.decode(), entries, messages


def exact_line_union(repo, stages, blobs, expected):
    """Prove the clean Git result contains every independent input hunk verbatim."""

    def lines(blob):
        chunks = blob.split(b"\n")
        return [line + b"\n" for line in chunks[:-1]] + (
            [chunks[-1]] if chunks[-1] else []
        )

    original = lines(blobs[0])

    def apply(edits):
        cursor, result = 0, []
        for start, end, replacement in edits:
            if start < cursor:
                return None
            result.extend(original[cursor:start])
            result.extend(replacement)
            cursor = end
        return b"".join([*result, *original[cursor:]])

    all_edits = set()
    for stage, blob in zip(("2", "3"), blobs[1:]):
        diff = repo.run(
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--text",
            "-U0",
            stages["1"][1],
            stages[stage][1],
        ).stdout
        edits, revised = [], lines(blob)
        for match in re.finditer(
            rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, re.M
        ):
            old_n, new_n = int(match[2] or 1), int(match[4] or 1)
            old, new = int(match[1]) - bool(old_n), int(match[3]) - bool(new_n)
            edits.append((old, old + old_n, tuple(revised[new : new + new_n])))
        if apply(edits) != blob:
            return False
        all_edits.update(edits)
    combined = sorted(all_edits)
    if any(a[0] == b[0] for a, b in zip(combined, combined[1:])):
        return False
    return apply(combined) == expected


class Engine:
    def __init__(self, repo, state, jobs=None):
        self.jobs = worker_count(jobs)
        self.repo = repo
        self.state = Path(state)
        self.owners = {}
        self.history = []

    def merge(self, ours, pr, owners, timestamp=None):
        base = pr.get("patch_base")
        args = ["--merge-base=" + base] if base else []
        proc = self.repo.run(
            "merge-tree",
            "--write-tree",
            "--messages",
            "-z",
            *args,
            ours,
            pr["head"],
            check=False,
        )
        if proc.returncode not in (0, 1):
            raise RuntimeError(
                "git merge-tree: " + proc.stderr.decode(errors="replace")
            )
        tree, entries, messages = parse_merge(proc.stdout)
        fixes, pending, updates = [], [], []
        ordinary, special = set(), set()
        for msg in messages:
            paths = msg["paths"]
            if msg["kind"] == "CONFLICT (contents)" and len(paths) == 1:
                ordinary.update(paths)
            elif msg["kind"].startswith("CONFLICT"):
                special.update(paths)
        line_conflicts = set(ordinary)
        # Git matches paths across renames. Verify clean concurrent edits on
        # supported files as well as resolving textual conflicts.
        clean_paths = {
            m["paths"][0]
            for m in messages
            if m["kind"] == "Auto-merging" and len(m["paths"]) == 1
        }
        clean_paths = {
            p
            for p in clean_paths - entries.keys() - special
            if has_driver(p)
        }
        if clean_paths:
            bases = (
                [base]
                if base
                else self.repo.out("merge-base", "--all", ours, pr["head"]).splitlines()
            )
            if len(bases) == 1:
                paths = sorted(os.fsencode(p) for p in clean_paths)
                snapshots = ordered_map(
                    lambda rev: tree_entries(self.repo, rev, paths),
                    [bases[0], ours, pr["head"]],
                    self.jobs,
                )
                for path in clean_paths:
                    raw = os.fsencode(path)
                    stages = {}
                    for i, snapshot in enumerate(snapshots, 1):
                        if raw in snapshot:
                            mode, oid = snapshot[raw]
                            if mode in (b"100644", b"100755"):
                                stages[str(i)] = [mode.decode(), oid.decode()]
                    if len(stages) == 3 and len({r[1] for r in stages.values()}) == 3:
                        entries[path] = stages
                        ordinary.add(path)
        if entries or special or proc.returncode:
            # Rename/delete, file/directory and mode conflicts require a revised PR.
            def compare_file(path):
                stages = entries.get(path, {})
                mode = None
                if path in ordinary - special and set(stages) == {"1", "2", "3"}:
                    modes = [stages[str(i)][0] for i in (1, 2, 3)]
                    if all(m in ("100644", "100755") for m in modes):
                        mode = choose(*modes)
                content = why = None
                if mode:
                    objects = [stages[str(i)][1] for i in (1, 2, 3)]
                    clean = path not in line_conflicts
                    if clean:
                        objects.append(tree + ":" + path)
                    inputs = self.repo.blobs(objects)
                    if clean:
                        git_result = inputs.pop()
                        if exact_line_union(self.repo, stages, inputs, git_result):
                            return None
                    content, why = resolve_file(path, inputs)
                    if clean:
                        if content == git_result:
                            return None
                        content = None
                prior = owners.get(path)
                if not prior:
                    previous = self.repo.out(
                        "log", "-1", "--format=%H %an", ours, "--", path
                    )
                    prior = {"base:" + (previous or ours)}
                authors = sorted({participant(pr), *prior})
                conflict = dict(
                    path=path,
                    incoming={
                        "number": pr["number"],
                        "head": pr["head"],
                        "author": pr["author"],
                    },
                    stages=stages,
                    authors=authors,
                    mode=mode,
                    kinds=sorted(m["kind"] for m in messages if path in m["paths"]),
                )
                key = hashlib.sha256(
                    json.dumps(conflict, sort_keys=True).encode()
                ).hexdigest()
                if content is not None:
                    blob = self.repo.out("hash-object", "-w", "--stdin", data=content)
                else:
                    blob = self.approved_blob(key, authors) if mode else None
                    why = "agent-agreement"
                return path, mode, blob, why, key, conflict

            paths = sorted(set(entries) | special)
            for result in ordered_map(compare_file, paths, self.jobs):
                if result is None:
                    continue
                path, mode, blob, why, key, conflict = result
                if blob:
                    updates.append((path, mode, blob))
                    if path in line_conflicts or why == "agent-agreement":
                        fixes.append(dict(id=key, path=path, method=why))
                else:
                    self.repo.run(
                        "update-ref",
                        f"refs/concurrent-merge/conflicts/{key}/ours",
                        ours,
                    )
                    self.repo.run(
                        "update-ref",
                        f"refs/concurrent-merge/conflicts/{key}/incoming",
                        pr["head"],
                    )
                    write_json(
                        self.state / "conflicts" / f"{key}.json",
                        {**conflict, "id": key, "ours": ours},
                    )
                    pending.append(key)
            if pending:
                return None, fixes, pending
            if updates:
                tree = replace_blobs(self.repo, tree, updates)
            elif proc.returncode:
                raise RuntimeError("Unaccounted Git conflict")
        return self.repo.commit(tree, ours, pr, timestamp), fixes, []

    def approved_blob(self, key, authors):
        votes = []
        for author in authors:
            vote = self.vote_path(key, author)
            if not vote.exists():
                return None
            votes.append(json.loads(vote.read_text())["blob"])
        if len(set(votes)) != 1:
            return None
        if self.repo.out("cat-file", "-t", votes[0]) != "blob":
            raise ValueError("Approved object must be a Git blob")
        return votes[0]

    def vote_path(self, key, author):
        return (
            self.state
            / "votes"
            / key
            / (hashlib.sha256(author.encode()).hexdigest() + ".json")
        )

    def agree(self, key, author, content):
        if not re.fullmatch("[0-9a-f]{64}", key):
            raise ValueError("Invalid conflict id")
        issue = json.loads((self.state / "conflicts" / f"{key}.json").read_text())
        if not issue["mode"] or author not in issue["authors"]:
            raise ValueError(
                "This vote requires a listed author and a regular file content conflict"
            )
        blob = self.repo.out("hash-object", "-w", "--stdin", data=content)
        # Keep approved content alive through Git garbage collection.
        self.repo.run(
            "update-ref",
            f"refs/concurrent-merge/votes/{key}/{blob}",
            blob,
        )
        write_json(self.vote_path(key, author), dict(author=author, blob=blob))

    def batch(self, base, changes, test, accepted=()):
        """Split on test failure. The right half always starts from the last passing commit."""
        final = base
        accepted = set(accepted)
        # Rebuilding a prefix during this split must reproduce its commit IDs
        # even if the test takes hours or the prefix takes another assembly path.
        timestamp = int(time.time())
        failed_commits = set()

        def attempt(group):
            nonlocal final
            ours = final
            owners = self.owners.copy()
            selected, pending, fixes = [], [], []
            available = accepted.copy()
            deltas = disjoint_deltas(self.repo, ours, group, available, self.jobs)
            if deltas is not None:
                ours = import_chain(
                    self.repo, ours, group, deltas, commit_message, timestamp
                )
                selected.extend(group)
                for pr in group:
                    for raw_path, _, _ in deltas[pr["head"]]:
                        path = os.fsdecode(raw_path)
                        owners[path] = set(owners.get(path, ())) | {participant(pr)}
            else:
                for pr in group:
                    missing = sorted(set(pr.get("requires", ())) - available)
                    if missing:
                        pending.append(
                            dict(
                                number=pr["number"],
                                author=pr["author"],
                                requires=missing,
                            )
                        )
                        continue
                    head, resolved, clashes = self.merge(ours, pr, owners, timestamp)
                    fixes.extend(resolved)
                    if clashes:
                        pending.append(
                            dict(
                                number=pr["number"],
                                author=pr["author"],
                                conflicts=clashes,
                            )
                        )
                        continue
                    for path in self.repo.paths(ours, head):
                        owners[path] = set(owners.get(path, ())) | {participant(pr)}
                    ours = head
                    selected.append(pr)
                    available.add(pr["number"])
            entry = dict(
                assembly="disjoint" if deltas is not None else "three-way",
                base=final,
                candidate=ours,
                members=[p["number"] for p in selected],
                pending=pending,
                resolved=fixes,
                result="empty",
            )
            self.history.append(entry)
            if not selected:
                return
            entry["tree"] = self.repo.out("rev-parse", ours + "^{tree}")
            entry["result"] = "interrupted"
            if ours in failed_commits:
                outcome = "fail"
                entry["result"] = "known-failure"
            else:
                outcome = test(ours)
                # A failing test run may name the members its failing files could
                # have seen (blame.py): ("fail", {"eject": [numbers]}).
                if isinstance(outcome, tuple):
                    outcome, found = outcome
                    entry["blame"] = found
                if outcome not in ("pass", "fail", "model"):
                    raise ValueError("Unrecognized test result")
                entry["result"] = outcome
                if outcome == "fail":
                    failed_commits.add(ours)
            if outcome in ("pass", "model"):
                self.owners = owners
                final = ours
                accepted.update(p["number"] for p in selected)
            elif len(group) > 1 and _ejected(entry, selected):
                # The failing files could see only these members: send them back
                # and test the rest of the group, without splitting it.
                out = _ejected(entry, selected)
                entry["returned_by_blame"] = [
                    dict(number=p["number"], head=p["head"], author=p["author"], tested_on=entry["base"])
                    for p in selected if p["number"] in out
                ]
                attempt([p for p in group if p["number"] not in out])
            elif len(group) > 1:
                # A held change can become clean after its conflicting peer fails.
                mid = len(group) // 2
                attempt(group[:mid])
                attempt(group[mid:])
            else:
                entry["returned"] = {
                    "number": selected[0]["number"],
                    "head": selected[0]["head"],
                    "author": selected[0]["author"],
                    "tested_on": entry["base"],
                }

        attempt(changes)
        return final, accepted


def _ejected(entry, selected):
    """Members a blame verdict named, when they are some but not all of the group."""
    named = {int(n) for n in (entry.get("blame") or {}).get("eject", ())}
    members = {p["number"] for p in selected}
    return named if named and named < members else set()


def participant(pr):
    return f"PR#{pr['number']}@{pr['head']} ({pr['author']})"


def commit_message(pr):
    msg = f"Batch PR #{pr['number']}\n\nPull-Request: #{pr['number']}\nPR-Author: {pr['author']}\n"
    if pr.get("login"):
        msg += f"PR-Login: {pr['login']}\n"
    msg += f"PR-Head: {pr['head']}\n"
    for other in pr.get("included", []):
        msg += f"Included-PR: {participant(other)}\n"
    return msg
