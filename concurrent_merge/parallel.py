"""Compare independent Git deltas on one runner and compose commuting changes."""

from concurrent.futures import ThreadPoolExecutor
import os
import uuid


def worker_count(jobs=None):
    if jobs is None:
        available = getattr(os, "process_cpu_count", os.cpu_count)()
        return min(8, available or 1)
    if jobs < 1:
        raise ValueError("jobs must be positive")
    return jobs


def ordered_map(fn, items, jobs):
    if len(items) <= 1 or jobs == 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(len(items), jobs)) as workers:
        return list(workers.map(fn, items))


def commit_parents(repo, oids):
    data = "".join(oid + "\n" for oid in oids).encode("ascii")
    lines = repo.out(
        "rev-list", "--no-walk=unsorted", "--parents", "--stdin", data=data
    )
    parents = {}
    for line in lines.splitlines():
        head, *ancestors = line.split()
        parents[head] = ancestors
    return parents


def tree_entries(repo, tree, paths):
    """Read exact modes and IDs for named paths within the platform argv limit."""
    entries = {}
    chunk, size = [], 0

    def read():
        rows = repo.run(
            "--literal-pathspecs", "ls-tree", "-z", tree, "--", *chunk
        ).stdout
        for row in rows.split(b"\0"):
            if row:
                meta, path = row.split(b"\t", 1)
                mode, _, oid = meta.split()
                entries[path] = (mode, oid)

    for path in paths:
        if chunk and size + len(path) + 4 > 12000:
            read()
            chunk, size = [], 0
        chunk.append(os.fsdecode(path))
        size += len(path) + 4
    if chunk:
        read()
    return entries


def fork_points(repo, base, group):
    """Find unshared linear topic histories ending at an ancestor of the target."""
    forks = {p["head"]: p["patch_base"] for p in group if p.get("patch_base")}
    if forks:
        return forks if len(forks) == len(group) else None
    data = "".join(p["head"] + "\n" for p in group) + "^" + base + "\n"
    rows = repo.out("rev-list", "--parents", "--stdin", data=data.encode())
    parents = {}
    for row in rows.splitlines():
        head, *previous = row.split()
        if len(previous) != 1:
            return None
        parents[head] = previous[0]
    visited = set()
    for pr in group:
        node = pr["head"]
        if node not in parents:
            return None
        while node in parents:
            if node in visited:
                return None
            visited.add(node)
            node = parents[node]
        forks[pr["head"]] = node
    return forks


def compatible_additions(repo, fork, target, added):
    # Deletions on the target can trigger Git's implicit directory renames.
    # Also check for target edits above or below a proposed new file.
    raw = repo.run(
        "diff-tree",
        "--no-commit-id",
        "-r",
        "--no-renames",
        "--ignore-submodules=none",
        "--name-status",
        "-z",
        fork,
        target,
    ).stdout
    fields = iter(raw.split(b"\0"))
    parents = set()
    for name in added:
        while b"/" in name:
            name = name.rsplit(b"/", 1)[0]
            parents.add(name)
    for status in fields:
        if not status:
            break
        path = next(fields)
        if status == b"D" or path in parents or path in added:
            return False
        while b"/" in path:
            path = path.rsplit(b"/", 1)[0]
            if path in added:
                return False
    return True


def disjoint_deltas(repo, base, group, accepted, jobs):
    """Return exact independent path edits, or None for the ordinary Git merge."""
    if len(group) < 2 or any(not set(p.get("requires", ())) <= accepted for p in group):
        return None
    heads = [p["head"] for p in group]
    if len(set(heads)) != len(heads):
        return None
    forks = fork_points(repo, base, group)
    if forks is None:
        return None

    def diff(chunk):
        data = "".join(f"{head} {forks[head]}\n" for head in chunk).encode("ascii")
        raw = repo.run(
            "diff-tree",
            "--stdin",
            "-r",
            "--always",
            "--raw",
            "--no-abbrev",
            "--no-renames",
            "--ignore-submodules=none",
            "-z",
            data=data,
        ).stdout
        records = iter(raw.split(b"\0"))
        deltas, preimages, additions = {}, {}, {}
        for header in records:
            if not header:
                break
            if not header.startswith(b":"):
                edits = []
                head = header.decode("ascii")
                deltas[head] = edits
                continue
            old_mode, mode, original, blob, status = header[1:].split()
            path = next(records)
            if (
                status not in (b"M", b"A")
                or mode not in (b"100644", b"100755")
                or old_mode not in (b"000000", b"100644", b"100755")
                or path.rsplit(b"/", 1)[-1] == b".gitattributes"
            ):
                return None
            if forks[head] != base:
                if status == b"A":
                    additions.setdefault(forks[head], set()).add(path)
                else:
                    preimages[path] = (old_mode, original)
            edits.append((path, mode, blob))
        if deltas.keys() != set(chunk):
            raise ValueError("Incomplete Git delta stream")
        if preimages and tree_entries(repo, base, preimages) != preimages:
            return None
        if not all(
            compatible_additions(repo, fork, base, paths)
            for fork, paths in additions.items()
        ):
            return None
        return deltas

    width = min(jobs, len(heads))
    chunks = [heads[i::width] for i in range(width)]
    files, directories, deltas = set(), set(), {}
    for part in ordered_map(diff, chunks, jobs):
        if part is None:
            return None
        for head, edits in part.items():
            for path, _, _ in edits:
                if path in files or path in directories:
                    return None
                prefix = path
                while b"/" in prefix:
                    prefix = prefix.rsplit(b"/", 1)[0]
                    if prefix in files:
                        return None
                    directories.add(prefix)
                files.add(path)
            deltas[head] = edits
    return deltas


def import_chain(repo, base, group, deltas, message, timestamp):
    """Write the same two-parent history in one Git process with a private ref."""
    ref = "refs/concurrent-merge/scratch/" + uuid.uuid4().hex
    ident = f"Concurrent Merge <batch@localhost> {timestamp} +0000"
    stream = bytearray()
    parent = base
    for index, pr in enumerate(group, 1):
        msg = message(pr).encode("utf8")
        stream.extend(
            f"commit {ref}\nmark :{index}\ncommitter {ident}\ndata {len(msg)}\n".encode()
        )
        stream.extend(msg)
        stream.extend(f"\nfrom {parent}\nmerge {pr['head']}\n".encode())
        for path, mode, oid in deltas[pr["head"]]:
            # Git's C-quoted paths are byte strings. This also handles tabs,
            # newlines, quotes and names outside the platform's text encoding.
            quoted = b'"' + b"".join(b"\\%03o" % c for c in path) + b'"'
            stream.extend(b"M " + mode + b" " + oid + b" " + quoted + b"\n")
        stream.extend(b"\n")
        parent = f":{index}"
    stream.extend(f"get-mark {parent}\nreset {ref}\n\ndone\n".encode())
    return repo.out(
        "fast-import", "--quiet", "--done", "--date-format=raw", data=stream
    )


def replace_blobs(repo, tree, updates):
    """Rebuild only directories containing resolved files, without a full index."""
    edits = {}
    for path, mode, blob in updates:
        parent = edits
        *parts, name = os.fsencode(path).split(b"/")
        for part in parts:
            parent = parent.setdefault(part, {})
        parent[name] = f"{mode} blob {blob}".encode()

    def rebuild(oid, replacements):
        rows = []
        for row in repo.run("ls-tree", "-z", oid).stdout.split(b"\0"):
            if not row:
                continue
            metadata, name = row.split(b"\t", 1)
            edit = replacements.pop(name, None)
            if isinstance(edit, dict):
                mode, kind, child = metadata.split()
                if kind != b"tree":
                    raise ValueError("Expected a Git subtree at the resolution path")
                metadata = b" ".join(
                    (mode, kind, rebuild(child.decode(), edit).encode())
                )
            elif edit is not None:
                metadata = edit
            rows.append(metadata + b"\t" + name + b"\0")
        if replacements:
            raise ValueError("Resolution path missing from Git merge tree")
        return repo.out("mktree", "-z", data=b"".join(rows))

    return rebuild(tree, edits)
