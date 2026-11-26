"""Submit a tested batch for review through a protected GitHub branch."""

import json
import os
from pathlib import Path
import sys
import tempfile

from . import github
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
            destination = github.git_destination(live["repository"])
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
    # A snapshot collected by --prs, --label or --min-reviews has no merge queue:
    # a member must still match those rules instead of still being queued.
    left = (
        "no longer matches the collected selection"
        if receipt["snapshot"].get("selection")
        else "left the merge queue"
    )
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
            raise ValueError(f"PR #{number} {left}; recollect and test")
    if not remaining or tree == repo.out("rev-parse", target + "^{tree}"):
        raise ValueError("The target already contains the batch")
    return remaining, included, tree


def graphql_allowed():
    api = os.environ.get("LIT_GITHUB_API", "auto")
    if api not in ("auto", "graphql", "rest"):
        raise ValueError("LIT_GITHUB_API must be auto, graphql or rest")
    return api


PRIOR_QUERY = """query($owner:String!,$name:String!,$base:String!,$head:String!){
  repository(owner:$owner,name:$name){pullRequests(baseRefName:$base,headRefName:$head,
    states:[OPEN,CLOSED,MERGED],first:100){nodes{number state headRefOid isCrossRepository url}}}}"""

CREATE_QUERY = """mutation($repo:ID!,$base:String!,$head:String!,$title:String!,$body:String!,$draft:Boolean!){
  createPullRequest(input:{repositoryId:$repo,baseRefName:$base,headRefName:$head,
    title:$title,body:$body,draft:$draft}){pullRequest{url}}}"""


def prior_pulls(repository, branch, head):
    """Earlier PRs from this topic branch: number, state, headRefOid, isCrossRepository, url."""
    api = graphql_allowed()
    if api != "rest":
        try:
            owner, name = repository.split("/")
            found = github.graphql(
                PRIOR_QUERY, dict(owner=owner, name=name, base=branch, head=head)
            )
            return found["data"]["repository"]["pullRequests"]["nodes"]
        except github.GitHubError:
            if api == "graphql":
                raise
            print("lit: GitHub GraphQL refused; reading PRs over REST", file=sys.stderr)
    owner = repository.split("/")[0]
    rows = github.rest(
        "GET",
        f"repos/{repository}/pulls",
        dict(head=f"{owner}:{head}", base=branch, state="all", per_page=100),
    )
    return [
        dict(
            number=p["number"],
            state="MERGED" if p.get("merged_at") else p["state"].upper(),
            headRefOid=p["head"]["sha"],
            isCrossRepository=((p["head"].get("repo") or {}).get("full_name") != repository),
            url=p["html_url"],
        )
        for p in rows
        if p["head"]["ref"] == head
    ]


def create_pull(repository, branch, head, title, body_path, *, draft=True):
    api = graphql_allowed()
    body = Path(body_path).read_text(encoding="utf8")
    if target := os.environ.get("LIT_PARITY_LAND_TARGET"):
        payload = dict(base=branch, head=head, title=title, body=body, draft=draft)
        (Path(target) / "lit-draft.json").write_text(json.dumps(payload), encoding="utf8")
        return f"https://github.com/{repository}/pull/parity"
    if api != "rest":
        try:
            owner, name = repository.split("/")
            repo_id = github.graphql(
                "query($o:String!,$n:String!){repository(owner:$o,name:$n){id}}",
                dict(o=owner, n=name),
            )["data"]["repository"]["id"]
            made = github.graphql(
                CREATE_QUERY,
                dict(repo=repo_id, base=branch, head=head, title=title, body=body, draft=draft),
            )
            return made["data"]["createPullRequest"]["pullRequest"]["url"]
        except github.GitHubError:
            if api == "graphql":
                raise
            print("lit: GitHub GraphQL refused; opening the PR over REST", file=sys.stderr)
    created = github.rest(
        "POST",
        f"repos/{repository}/pulls",
        body=dict(title=title, head=head, base=branch, draft=draft, body=body),
    )
    return created["html_url"]


def propose(args, read_queue):
    receipt = json.loads(args.receipt.read_text(encoding="utf8"))
    snapshot, final = receipt["snapshot"], receipt["final"]
    repository, branch = snapshot["repository"], snapshot["branch"]
    repo = Repo(args.repo)
    head = args.head
    repo.run("check-ref-format", "--branch", head)
    if head == branch or head.startswith(("refs/", "gh-readonly-queue/")):
        raise ValueError("Choose a new topic branch for the batch PR")
    selection = snapshot.get("selection")
    if selection:
        # Collected without a merge queue: recheck the same rules, as land does.
        live = read_queue(repository, branch, selection)
    else:
        live = read_queue(repository, branch)
    destination = github.git_destination(repository)
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
    prior = prior_pulls(repository, branch, head)
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
    route = "for review"
    if not selection:
        route += " and validation through the protected merge queue"
    body = f"lit combined {numbers} {route}. Each source PR head is a parent in the candidate history.\n\nLocal validation passed on commit `{final}` and tree `{tree}`.\n"
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
        url = create_pull(
            repository, branch, head, args.title or f"lit batch for {numbers}", path
        )
    print(url)
