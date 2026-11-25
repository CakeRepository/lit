"""Submit a tested batch for review through a protected GitHub branch."""

import json
from pathlib import Path
import subprocess
import tempfile

from .engine import Repo, parse_merge


def matching_group(repo, receipt, live):
    """Recognize an existing native group covering every tested input."""
    tree = repo.out("rev-parse", receipt["final"] + "^{tree}")
    heads = {
        p["head"]
        for p in receipt["snapshot"]["changes"]
        if p["number"] in receipt["tested"]
    }
    heads.add(live["base"])
    for group in live.get("groups", []):
        if group["tree"] != tree or group["state"] not in {
            "AWAITING_CHECKS",
            "MERGEABLE",
            "LOCKED",
        }:
            continue
        head = group["head"]
        if repo.run("cat-file", "-e", head + "^{commit}", check=False).returncode:
            destination = "https://github.com/" + live["repository"] + ".git"
            if repo.run(
                "fetch", "--no-tags", destination, head, check=False
            ).returncode:
                continue
        if repo.out("rev-parse", head + "^{tree}") == tree and all(
            repo.run("merge-base", "--is-ancestor", oid, head, check=False).returncode
            == 0
            for oid in heads
        ):
            return group
    return None


def proposal_members(repo, receipt, tested_ref, live):
    snapshot = receipt["snapshot"]
    base, final = snapshot["base"], receipt["final"]
    tested = set(receipt["tested"])
    changes = {pr["number"]: pr["head"] for pr in snapshot["changes"]}
    if (
        not tested
        or not tested <= changes.keys()
        or repo.out("rev-parse", tested_ref) != final
    ):
        raise ValueError("No matching tested batch ref")
    repo.run("merge-base", "--is-ancestor", base, final)
    target = live["base"]
    tree = repo.out("rev-parse", final + "^{tree}")
    if target != base:
        repo.run("merge-base", "--is-ancestor", base, target)
        merged = repo.run(
            "merge-tree", "--write-tree", "--messages", "-z", target, final, check=False
        )
        if merged.returncode or parse_merge(merged.stdout)[0] != tree:
            raise ValueError(
                "The current target does not merge to the tested tree; recollect and test"
            )
    queued = {pr["number"]: pr["head"] for pr in live["changes"]}
    remaining, included = [], []
    for number in sorted(tested):
        oid = changes[number]
        repo.run("merge-base", "--is-ancestor", oid, final)
        if number in queued:
            if queued[number] != oid:
                raise ValueError(f"PR #{number} changed revision; recollect and test")
            remaining.append(number)
        elif (
            repo.run("merge-base", "--is-ancestor", oid, target, check=False).returncode
            == 0
        ):
            included.append(number)
        else:
            raise ValueError(f"PR #{number} left the merge queue; recollect and test")
    if not remaining or tree == repo.out("rev-parse", target + "^{tree}"):
        raise ValueError("The target already contains the batch")
    return remaining, included, tree


def propose(args, read_queue):
    receipt = json.loads(args.receipt.read_text(encoding="utf8"))
    snapshot, final = receipt["snapshot"], receipt["final"]
    repository, branch = snapshot["repository"], snapshot["branch"]
    repo = Repo(args.repo)
    head = args.head
    repo.run("check-ref-format", "--branch", head)
    if head == branch or head.startswith(("refs/", "gh-readonly-queue/")):
        raise ValueError("Choose a new topic branch for the batch PR")
    live = read_queue(repository, branch)
    destination = "https://github.com/" + repository + ".git"
    if live["base"] != snapshot["base"]:
        repo.run("fetch", "--no-tags", destination, "refs/heads/" + branch)
    remaining, included, tree = proposal_members(
        repo, receipt, "refs/concurrent-merge/tested/" + args.receipt.parent.name, live
    )
    ref = "refs/heads/" + head
    repo.run("check-ref-format", ref)
    remote = repo.out("ls-remote", "--heads", destination, ref)
    if remote and remote.split()[0] != final:
        raise ValueError("That topic branch exists at another revision")
    gh = ["gh", "pr"]
    prior = json.loads(
        subprocess.check_output(
            [
                *gh,
                "list",
                "--repo",
                repository,
                "--base",
                branch,
                "--head",
                head,
                "--state",
                "all",
                "--json",
                "state,headRefOid,isCrossRepository,url",
            ]
        )
    )
    previous = [p for p in prior if not p["isCrossRepository"]]
    if previous:
        for p in previous:
            if p["state"] == "OPEN" and p["headRefOid"] == final:
                print(p["url"])
                return
        raise ValueError("A previous batch PR already used this branch")
    if group := matching_group(repo, receipt, live):
        print(
            f"GitHub already has a matching merge group for PR #{group['number']} ({group['state']}). https://github.com/{repository}/commit/{group['head']}"
        )
        return
    numbers = ", ".join(f"#{n}" for n in remaining)
    body = f"lit combined {numbers} for review and validation through the protected merge queue. Each source PR head is a parent in the candidate history.\n\nLocal validation passed on commit `{final}` and tree `{tree}`.\n"
    if included:
        body += (
            "\nThe target already contains "
            + ", ".join(f"#{n}" for n in included)
            + ".\n"
        )
    if args.notes:
        body += "\n" + args.notes.read_text(encoding="utf8").strip() + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "body.md"
        path.write_text(body, encoding="utf8")
        if not remote:
            # The empty lease makes this a create-only push, even if another
            # process creates the branch between ls-remote and push.
            repo.run(
                "push",
                "--force-with-lease=" + ref + ":",
                destination,
                final + ":" + ref,
            )
        url = subprocess.check_output(
            [
                *gh,
                "create",
                "--repo",
                repository,
                "--base",
                branch,
                "--head",
                head,
                "--draft",
                "--title",
                args.title or f"lit batch for {numbers}",
                "--body-file",
                str(path),
            ],
            text=True,
        ).strip()
    print(url)
