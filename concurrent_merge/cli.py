import argparse
import json
import os
import shlex
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from . import github
from .engine import Engine, Repo, shared_clone, write_json
from .inputs import valid_agent, verified_changes
from .proposal import propose


def split_repository(repository):
    owner, name = repository.split("/")
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", s) for s in (owner, name)):
        raise ValueError("Expected a GitHub owner/repository")
    return owner, name


def graphql_pages(query, repository, branch):
    """Yield each page of a repository query, holding the target commit fixed."""
    owner, name = split_repository(repository)
    variables = dict(owner=owner, name=name, branch=branch, ref="refs/heads/" + branch)
    base, cursor = None, None
    while True:
        result = github.graphql(query, dict(variables, after=cursor))
        data = (result.get("data") or {}).get("repository")
        if data is None:
            raise ValueError(f"Repository {repository} not found")
        if not data["ref"]:
            raise ValueError(f"Branch {branch} not found in {repository}")
        target = data["ref"]["target"]["oid"]
        if base and base != target:
            raise ValueError("Target advanced during collection; collect again")
        base = target
        info = yield base, data
        if not info or not info["hasNextPage"]:
            return
        cursor = info["endCursor"]


def agent_name(body, pattern):
    """The change's agent from its PR body, or None to keep the login."""
    if not pattern or not body:
        return None
    match = re.search(pattern, body, re.MULTILINE)
    if not match:
        return None
    name = (match.group(1) if match.re.groups else match.group(0)).strip()
    return name if valid_agent(name) else None


def change_row(number, head, author, body, author_pattern):
    row = dict(number=number, head=head, author=author)
    agent = agent_name(body, author_pattern)
    if agent and agent != author:
        row["agent"] = agent
    return row


QUEUE_QUERY = """query($owner:String!,$name:String!,$branch:String!,$ref:String!,$after:String){
    repository(owner:$owner,name:$name){ref(qualifiedName:$ref){target{oid}}
      mergeQueue(branch:$branch){entries(first:100,after:$after){pageInfo{hasNextPage endCursor}
        nodes{state headCommit{oid tree{oid}} pullRequest{number headRefOid baseRefName isDraft state author{login} body}}}}}}"""


def queue(repository, branch, author_pattern=None):
    changes, groups = {}, {}
    pages = graphql_pages(QUEUE_QUERY, repository, branch)
    try:
        base, data = next(pages)
    except github.GitHubError:
        # GitHub's merge queue has no REST API, so there is nothing to fall back to.
        raise ValueError(
            "Could not read the merge queue: GitHub GraphQL refused the request. "
            "Pick the changes with --prs, --label or --min-reviews, which also work over REST"
        )
    while True:
        if not data["mergeQueue"]:
            raise ValueError(
                "This branch has no GitHub merge queue. "
                "Pick the changes with --prs, --label or --min-reviews instead"
            )
        entries = data["mergeQueue"]["entries"]
        for item in entries["nodes"]:
            p = item["pullRequest"]
            if p["state"] == "OPEN" and not p["isDraft"] and p["baseRefName"] == branch:
                n = p["number"]
                change = change_row(
                    n,
                    p["headRefOid"],
                    (p["author"] or {}).get("login", "ghost"),
                    p.get("body"),
                    author_pattern,
                )
                if n in changes and changes[n] != change:
                    raise ValueError("PR changed during collection; collect again")
                changes[n] = change
                if commit := item.get("headCommit"):
                    groups[n] = dict(
                        number=n,
                        head=commit["oid"],
                        tree=commit["tree"]["oid"],
                        state=item["state"],
                    )
        try:
            base, data = pages.send(entries["pageInfo"])
        except StopIteration:
            return dict(
                repository=repository,
                branch=branch,
                base=base,
                changes=list(changes.values()),
                groups=list(groups.values()),
            )


PULLS_QUERY = """query($owner:String!,$name:String!,$branch:String!,$ref:String!,$after:String){
    repository(owner:$owner,name:$name){ref(qualifiedName:$ref){target{oid}}
      pullRequests(states:OPEN,baseRefName:$branch,first:50,after:$after,orderBy:{field:CREATED_AT,direction:ASC}){
        pageInfo{hasNextPage endCursor}
        nodes{number headRefOid baseRefName isDraft author{login} authorAssociation body
          labels(first:100){nodes{name}}
          latestOpinionatedReviews(first:100){nodes{state author{login} commit{oid}}}}}}}"""


def approvals(pr):
    """Distinct reviewers whose latest review approves the PR's current head."""
    return len(
        {
            (r["author"] or {}).get("login")
            for r in pr["latestOpinionatedReviews"]["nodes"]
            if r["state"] == "APPROVED"
            and r["author"]
            and (r["commit"] or {}).get("oid") == pr["headRefOid"]
        }
    )


TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")


def selection_of(prs=None, label=None, min_reviews=0, hold_outside=False, allow_outside=()):
    selection = {}
    if hold_outside:
        selection["hold_outside"] = True
        if allow_outside:
            if any(type(n) is not int or n <= 0 for n in allow_outside):
                raise ValueError("PR numbers must be positive integers")
            selection["allow_outside"] = sorted(set(allow_outside))
    elif allow_outside:
        raise ValueError("--allow-outside needs --hold-outside")
    if prs:
        if any(type(n) is not int or n <= 0 for n in prs):
            raise ValueError("PR numbers must be positive integers")
        selection["prs"] = sorted(set(prs))
    if label:
        selection["label"] = label
    if min_reviews:
        if type(min_reviews) is not int or min_reviews < 0:
            raise ValueError("--min-reviews must be a positive integer")
        selection["min_reviews"] = min_reviews
    return selection


def open_pulls_graphql(repository, branch):
    nodes = []
    pages = graphql_pages(PULLS_QUERY, repository, branch)
    base, data = next(pages)
    while True:
        found = data["pullRequests"]
        nodes.extend(found["nodes"])
        try:
            base, data = pages.send(found["pageInfo"])
        except StopIteration:
            return base, nodes


def rest(path, **query):
    return github.rest("GET", path, query)


def rest_pages(path, **query):
    return github.rest_pages(path, query)


def rest_target(owner, name, branch):
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch:
        raise ValueError("Unsupported branch name")
    try:
        ref = rest(f"repos/{owner}/{name}/git/ref/heads/{branch}")
    except github.GitHubError:
        raise ValueError(f"Branch {branch} not found in {owner}/{name}")
    if ref["object"]["type"] != "commit":
        raise ValueError(f"Branch {branch} does not point at a commit")
    return ref["object"]["sha"]


def latest_reviews(owner, name, number):
    """Each reviewer's latest approval or change request, as GraphQL's latestOpinionatedReviews."""
    latest = {}
    for review in rest_pages(f"repos/{owner}/{name}/pulls/{number}/reviews"):
        login = (review.get("user") or {}).get("login")
        if not login:
            continue
        if review["state"] in ("APPROVED", "CHANGES_REQUESTED"):
            latest[login] = dict(
                state=review["state"],
                author={"login": login},
                commit={"oid": review.get("commit_id")},
            )
        elif review["state"] == "DISMISSED":
            latest.pop(login, None)
    return list(latest.values())


def open_pulls_rest(repository, branch, reviews):
    """The same PR records as PULLS_QUERY, from GitHub's REST API."""
    owner, name = split_repository(repository)
    base = rest_target(owner, name, branch)
    nodes = []
    for pr in rest_pages(f"repos/{owner}/{name}/pulls", state="open", base=branch):
        nodes.append(
            dict(
                number=pr["number"],
                headRefOid=pr["head"]["sha"],
                baseRefName=pr["base"]["ref"],
                isDraft=bool(pr.get("draft")),
                author={"login": pr["user"]["login"]} if pr.get("user") else None,
                authorAssociation=pr.get("author_association") or "NONE",
                body=pr.get("body") or "",
                labels={"nodes": [{"name": l["name"]} for l in pr.get("labels", [])]},
                latestOpinionatedReviews={
                    "nodes": latest_reviews(owner, name, pr["number"]) if reviews else []
                },
            )
        )
    if rest_target(owner, name, branch) != base:
        raise ValueError("Target advanced during collection; collect again")
    return base, nodes


def open_pulls(repository, branch, reviews=False):
    """Open PRs into the branch and its target commit.

    LIT_GITHUB_API picks the transport: graphql, rest, or auto (default), which
    tries GraphQL and falls back to REST when GitHub or a proxy refuses it."""
    api = os.environ.get("LIT_GITHUB_API", "auto")
    if api not in ("auto", "graphql", "rest"):
        raise ValueError("LIT_GITHUB_API must be auto, graphql or rest")
    if api != "rest":
        try:
            return open_pulls_graphql(repository, branch)
        except github.GitHubError:
            if api == "graphql":
                raise
            print("lit: GitHub GraphQL refused; reading PRs over REST", file=sys.stderr)
    return open_pulls_rest(repository, branch, reviews)


def pulls(repository, branch, selection, author_pattern=None):
    """Snapshot open PRs picked by number, label or approvals, for repos without a merge queue."""
    if not set(selection) - {"hold_outside", "allow_outside"}:
        raise ValueError("Pick changes with --prs, --label or --min-reviews")
    wanted = set(selection.get("prs", ()))
    allowed = set(selection.get("allow_outside", ()))
    outside = []
    label, minimum = selection.get("label"), selection.get("min_reviews", 0)
    changes, seen = {}, set()
    base, nodes = open_pulls(repository, branch, reviews=bool(minimum))
    for p in nodes:
        n = p["number"]
        if wanted and n not in wanted:
            continue
        seen.add(n)
        labels = {node["name"] for node in p["labels"]["nodes"]}
        if p["isDraft"] or p["baseRefName"] != branch:
            continue
        if label and label not in labels:
            continue
        if approvals(p) < minimum:
            continue
        if (
            selection.get("hold_outside")
            and p.get("authorAssociation", "NONE") not in TRUSTED
            and n not in allowed
        ):
            outside.append(n)
            continue
        change = change_row(
            n,
            p["headRefOid"],
            (p["author"] or {}).get("login", "ghost"),
            p.get("body"),
            author_pattern,
        )
        if n in changes and changes[n] != change:
            raise ValueError("PR changed during collection; collect again")
        changes[n] = change
    if missing := sorted(wanted - seen):
        raise ValueError(
            f"Not open against {branch}: " + ", ".join(f"#{n}" for n in missing)
        )
    if outside:
        print(
            "lit: held for review, from outside contributors: "
            + ", ".join(f"#{n}" for n in sorted(outside))
            + " (after review, collect again with --allow-outside)",
            file=sys.stderr,
        )
    if wanted and (skipped := sorted(wanted - changes.keys() - set(outside))):
        raise ValueError(
            "Draft or not matching the other rules: "
            + ", ".join(f"#{n}" for n in skipped)
        )
    return dict(
        repository=repository,
        branch=branch,
        base=base,
        selection=selection,
        changes=sorted(changes.values(), key=lambda p: p["number"]),
        groups=[],
    )


def collect(repository, branch, selection=None, author_pattern=None):
    if selection:
        return pulls(repository, branch, selection, author_pattern)
    return queue(repository, branch, author_pattern)


def run_test(repo, oid, command, directory):
    # Each test sees a clean checkout of exactly the candidate being recorded.
    with tempfile.TemporaryDirectory(prefix="candidate-") as temporary:
        work = Repo(temporary)
        subprocess.run(
            shared_clone(repo.path, temporary),
            check=True,
        )
        work.run("remote", "remove", "origin")
        work.run("checkout", "-q", "--detach", oid)
        with (directory / (oid + ".log")).open("wb") as f:
            rc = subprocess.run(
                command, cwd=temporary, stdout=f, stderr=subprocess.STDOUT
            ).returncode
        if rc not in (0, 1):
            raise RuntimeError(
                f"Test infrastructure exited {rc}. See {directory / (oid + '.log')}"
            )
        return "pass" if rc == 0 else "fail"


def test_snapshot(args):
    if not args.test:
        raise ValueError("Supply the full suite command after --test")
    snapshot = json.loads(args.snapshot.read_text(encoding="utf8"))
    repo = Repo(args.repo)
    changes = verified_changes(repo, snapshot, args.jobs)
    engine = Engine(repo, args.state, jobs=args.jobs)
    parent = args.state / "runs"
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(dir=parent, prefix="batch-"))
    receipt = directory / "result.json"

    def test(oid):
        return run_test(repo, oid, args.test, directory)

    try:
        final, passed = engine.batch(snapshot["base"], changes, test)
    finally:
        write_json(directory / "attempts.json", engine.history)
    if passed:
        repo.run("update-ref", "refs/concurrent-merge/tested/" + directory.name, final)
    write_json(
        receipt,
        dict(snapshot=snapshot, final=final, tested=sorted(passed), command=args.test),
    )
    if args.shadow_store:
        write_shadow(repo, snapshot, args.shadow_store, args.shadow_job, directory, args.shadow_proofs,
                     args.shadow_discover, args.shadow_strip)
    print(receipt.resolve())
    return 0 if len(passed) == len(changes) else 1


def write_shadow(repo, snapshot, store, job, directory, proofs=None, discover=None, strip=""):
    """Record beside a run what Frontier would have decided. Never changes the run's outcome."""
    from .shadow import shadow

    try:
        result = shadow(
            repo, snapshot, store, job,
            receipt=json.loads((directory / "result.json").read_text(encoding="utf8")),
            attempts=json.loads((directory / "attempts.json").read_text(encoding="utf8")),
            proofs=proofs,
            discover=discover,
            strip=strip,
        )
    except Exception as exc:  # the shadow is advisory: report it, keep the run as it was
        result = dict(kind="lit-shadow-decision", error=f"{type(exc).__name__}: {exc}")
        print(f"lit: shadow decision failed: {exc}", file=sys.stderr)
    write_json(directory / "shadow.json", result)


def record_footprint(args):
    from .footprint import trace

    if not args.test:
        raise ValueError("Supply the job command after --test")
    manifest = trace(
        Repo(args.repo),
        args.commit,
        args.test,
        log=args.log,
        ignore_env=args.ignore_env,
    )
    write_json(args.out, manifest)
    print(
        json.dumps(
            dict(
                commit=manifest["commit"],
                exit_code=manifest["exit_code"],
                seconds=manifest["seconds"],
                tracked=len(manifest["tracked"]),
                listings=len(manifest["listings"]),
                absent=len(manifest["absent"]),
                external=len(manifest["external"]),
                incomplete=manifest["incomplete"],
                network=bool(manifest["network"]),
                reads_git_metadata=manifest["reads_git_metadata"],
            ),
            indent=2,
        )
    )
    return 0


def record_units(args):
    from .footprint import RUN_SPECIFIC_ENV, trace_units

    each = None
    if args.each:
        if not args.units_file or args.test or args.pattern:
            raise ValueError("--each takes --units-file and no --pattern or --test")
        template = shlex.split(args.each)
        if not any("{}" in part for part in template):
            raise ValueError("--each needs {} where the unit name goes")
        names = [
            line.strip()
            for line in args.units_file.read_text(encoding="utf8").splitlines()
            if line.strip()
        ]
        each = {n: [part.replace("{}", n) for part in template] for n in names}
    elif not (args.test and args.pattern):
        raise ValueError("Give --pattern and the command after --test, or --each")
    ignore = args.ignore_env if args.strict_env else [*RUN_SPECIFIC_ENV, *args.ignore_env]
    workdir = args.workdir.resolve() if args.workdir else None
    repo = Repo(args.repo or workdir)
    commit = args.commit or repo.out("rev-parse", "HEAD")
    bundle = trace_units(
        repo, commit, args.test or None, args.pattern, each=each, cwd=args.cwd,
        log=args.log, ignore_env=ignore, workdir=workdir,
    )
    write_json(args.out, bundle)
    units = bundle["units"].values()
    print(
        json.dumps(
            dict(
                commit=bundle["commit"],
                exit_code=bundle["exit_code"],
                seconds=bundle["seconds"],
                units=len(bundle["units"]),
                failed=sum(u["exit_code"] != 0 for u in units),
                incomplete=sum(bool(u["incomplete"]) for u in units),
                network=sum(bool(u["network"]) for u in units),
                unit_seconds=round(sum(u["seconds"] or 0 for u in units), 1),
            ),
            indent=2,
        )
    )
    return 0


def select_units(args):
    from .footprint import load_units, select

    workdir = args.workdir.resolve() if args.workdir else None
    repo = Repo(args.repo or workdir)
    bundle = load_units(args.bundle)
    result = select(
        repo,
        bundle,
        args.candidate or repo.out("rev-parse", "HEAD"),
        env=dict(os.environ) if args.environment else None,
        host=args.host,
        allow_network=args.allow_network,
        workdir=workdir,
        discover=args.discover,
        strip=args.strip,
        listings=args.listings,
    )
    if args.out:
        write_json(args.out, result)
    if args.list:
        args.list.write_text("".join(name + "\n" for name in result["run"]), encoding="utf8")
    print(
        json.dumps(
            {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in result.items()},
            indent=2,
        )
    )
    return 0


def shard_spec(text):
    try:
        index, total = (int(part) for part in text.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError("use I/N, for example 2/4")
    if not 1 <= index <= total:
        raise argparse.ArgumentTypeError("I must be between 1 and N")
    return index, total


def run_cached_units(args):
    from .footprint import RUN_SPECIFIC_ENV
    from .unit_store import cached_units

    ignore = args.ignore_env if args.strict_env else [*RUN_SPECIFIC_ENV, *args.ignore_env]
    names = None
    if args.units_file:
        names = [
            line.strip()
            for line in args.units_file.read_text(encoding="utf8").splitlines()
            if line.strip()
        ]
    elif not args.discover:
        raise ValueError("Give --units-file or --discover")
    result = cached_units(
        args.workdir or Path.cwd(),
        args.job,
        args.store,
        shlex.split(args.each),
        names=names,
        discover=args.discover,
        strip=args.strip,
        cwd=args.cwd,
        mode=args.mode,
        allow_network=args.allow_network,
        listings=args.listings,
        audit_rate=args.audit_rate,
        ignore_env=ignore,
        log=args.log,
        shard=args.shard[0] - 1,
        shards=args.shard[1],
        pinned=[
            line.strip()
            for line in (args.pin_file.read_text(encoding="utf8").splitlines() if args.pin_file else [])
            if line.strip()
        ],
        symbols=args.symbols or (),
        scratch=args.scratch or (),
        batch=shlex.split(args.batch) if args.batch else None,
        proofs=args.proof_store, host=not args.no_host, audit_book=args.audit_book,
    )
    if args.receipt:
        write_json(args.receipt, result)
    if args.list:
        args.list.write_text(
            "".join(n + "\n" for n in sorted(result["decisions"])), encoding="utf8"
        )
    summary = {k: v for k, v in result.items() if k not in ("decisions", "failed", "reusable")}
    summary["failed"] = len(result["failed"])
    print(json.dumps(summary, indent=2))
    return result["exit_code"]


def run_cached(args):
    from .cache import cached_run
    from .footprint import RUN_SPECIFIC_ENV

    if not args.strict_env:
        args.ignore_env = [*RUN_SPECIFIC_ENV, *args.ignore_env]
    if not args.test:
        raise ValueError("Supply the job command after --test")
    try:
        result = cached_run(
            args.workdir,
            args.job,
            args.store,
            args.test,
            mode=args.mode,
            ignore_env=args.ignore_env,
            allow_network=args.allow_network,
            audit_rate=args.audit_rate,
            log=args.log,
        )
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        # Never let the recorder decide a check: run the job as it would run without lit.
        print(f"lit: {exc}; running the job without a footprint", file=sys.stderr)
        return subprocess.run(args.test, cwd=args.workdir).returncode
    if args.receipt:
        write_json(args.receipt, result)
    if result["decision"] == "reused":
        print(f"lit: reused {result['prior']} from {result['prior_commit']}")
    elif result.get("predicted_reuse"):
        verdict = "matched" if result["sound"] else "DID NOT MATCH"
        print(f"lit: {result['prior']} predicted this result and the run {verdict}")
    return result["exit_code"]


def predict_reuse(args):
    from .footprint import load, predict, verify

    repo, manifest = Repo(args.repo), load(args.footprint)
    options = dict(
        env=dict(os.environ) if args.environment else None,
        host=args.host,
        allow_network=args.allow_network,
    )
    results = []
    for candidate in args.candidate:
        if args.verify:
            result, _ = verify(repo, manifest, candidate, **options)
        else:
            result = predict(repo, manifest, candidate, **options)
        results.append(result)
    if args.out:
        write_json(args.out, results)
    print(json.dumps(results, indent=2))
    if args.verify:
        return 0 if all(r["sound"] for r in results) else 1
    return 0 if all(r["verdict"] == "reusable" for r in results) else 1


def land(args):
    receipt = json.loads(args.receipt.read_text(encoding="utf8"))
    snapshot = receipt["snapshot"]
    repo = Repo(args.repo)
    base, final = snapshot["base"], receipt["final"]
    tested = set(receipt["tested"])
    members = {p["number"] for p in snapshot["changes"]}
    if not tested or not tested <= members or final != repo.out(
        "rev-parse", "refs/concurrent-merge/tested/" + args.receipt.parent.name
    ):
        raise ValueError("No matching tested batch ref")
    repo.run("merge-base", "--is-ancestor", base, final)
    live = collect(snapshot["repository"], snapshot["branch"], snapshot.get("selection"))
    ready = {p["number"]: p["head"] for p in live["changes"]}
    if live["base"] != base or any(
        ready.get(p["number"]) != p["head"]
        for p in snapshot["changes"]
        if p["number"] in receipt["tested"]
    ):
        raise ValueError("Base or queue eligibility changed; collect and test again")
    ref = "refs/heads/" + snapshot["branch"]
    repo.run("check-ref-format", ref)
    destination = github.git_destination(snapshot["repository"])
    publish(repo, destination, ref, base, final)
    print(final)


def publish(repo, destination, ref, base, final):
    """Move `ref` at `destination` from `base` to `final`, or leave it as it is.

    This is the landing's compare-and-swap: the push carries `base` as the value the
    remote ref must still hold, so a batch tested on a main that has since moved never
    lands, even if the move happened after the eligibility check just before it. A lost
    race is a ValueError (collect and test again); any other push failure stays an error
    of its own, so a network or permission problem is not reported as a race."""
    proc = repo.run(
        "push", "--porcelain", "--force-with-lease=" + ref + ":" + base,
        destination, final + ":" + ref, check=False,
    )
    if proc.returncode:
        said = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
        if "stale info" in said:
            raise ValueError(
                f"{ref} moved since the batch was tested on {base}; collect and test again"
            )
        raise RuntimeError(f"git push: {proc.stderr.decode(errors='replace').strip()}")


def max_batch_option(text):
    """--max-batch: "auto" or a whole number of PRs."""
    if text == "auto":
        return text
    if not re.fullmatch(r"[1-9]\d*", text):
        raise argparse.ArgumentTypeError("expected auto or a positive whole number")
    return int(text)


def intake_options(parser):
    parser.add_argument("repository")
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--context", required=True, help="Required status a PR head must pass to be batched"
    )
    parser.add_argument(
        "--original-prs-only",
        action="store_true",
        help="Queue the original PRs one by one instead of a combined batch PR",
    )
    parser.add_argument(
        "--max-batch",
        type=max_batch_option,
        metavar="N|auto",
        help="Most ready PRs in one fresh batch; auto sizes it from the recent share of red PRs",
    )
    parser.epilog = "Then an action: ready N..., drain [--no-wait], tick [--label L], withdraw N HEAD, report N"
    return parser


def main():
    p = argparse.ArgumentParser(
        prog="concurrent-merge",
        description="Batch reviewed GitHub PRs and test their combined Git tree.",
    )
    cmds = p.add_subparsers(dest="action", required=True)
    c = cmds.add_parser(
        "collect",
        help="Snapshot the ready changes (the GitHub merge queue, or PRs picked by number, "
        "label or approvals) and fetch their pinned inputs into a bare copy",
    )
    c.add_argument("repository")
    c.add_argument("--branch", default="main")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--repo", type=Path, required=True)
    c.add_argument(
        "--prs",
        nargs="+",
        type=int,
        metavar="N",
        help="Collect these open PRs instead of the merge queue",
    )
    c.add_argument(
        "--label", help="Collect open PRs carrying this label instead of the merge queue"
    )
    c.add_argument(
        "--min-reviews",
        type=int,
        default=0,
        metavar="K",
        help="Collect open PRs with at least K approvals on their current head",
    )
    c.add_argument(
        "--hold-outside",
        action="store_true",
        help="Hold PRs whose authors have no write access (not owner, member or collaborator) "
        "until they are reviewed and passed with --allow-outside",
    )
    c.add_argument(
        "--allow-outside",
        nargs="+",
        type=int,
        default=(),
        metavar="N",
        help="With --hold-outside: outside contributors' PRs that passed review",
    )
    c.add_argument(
        "--author-pattern",
        metavar="REGEX",
        help="Read each change's agent from its PR body (group 1 of the first match); "
        "unmatched PRs keep their GitHub login",
    )
    intake_cmd = cmds.add_parser(
        "intake",
        help="Batch ready PRs ahead of GitHub's merge queue (ready, drain, tick, withdraw, report)",
    )
    intake_options(intake_cmd)
    cmds.add_parser(
        "proof",
        help="Pack, sign, push, verify and take signed test results (concurrent-merge proof -h)",
        add_help=False,
    )
    preview = cmds.add_parser(
        "preview", help="Predict the combined tree before original PRs enter the queue"
    )
    preview.add_argument("repository")
    preview.add_argument("numbers", nargs="+", type=int)
    preview.add_argument("--branch", default="main")
    preview.add_argument("--repo", type=Path, required=True)
    preview.add_argument("--out", type=Path, required=True)
    run = cmds.add_parser("run", help="Build and test a queue snapshot locally")
    run.add_argument("snapshot", type=Path)
    run.add_argument(
        "--jobs",
        type=int,
        help="Concurrent Git and structural comparison workers on this runner (default: up to 8)",
    )
    run.add_argument(
        "--shadow-store",
        type=Path,
        help="Also write shadow.json: what Frontier would have decided from this unit store "
        "(advisory, changes nothing)",
    )
    run.add_argument("--shadow-job", help="The store's job for --shadow-store")
    run.add_argument(
        "--shadow-proofs",
        type=Path,
        help="A store that 'proof take' filled with signed records (each PR's own CI run): "
        "the shadow counts them as certificates too",
    )
    run.add_argument(
        "--shadow-discover", action="append", metavar="GLOB",
        help="Test files as cached-units finds them, for --shadow-store: any without a passing "
        "footprint is reported",
    )
    run.add_argument("--shadow-strip", default="", help="Prefix --shadow-discover removes to name a unit")
    run.add_argument("--test", nargs=argparse.REMAINDER, required=True)
    agree = cmds.add_parser(
        "agree", help="Record one participant's vote for exact replacement file bytes"
    )
    agree.add_argument("conflict")
    agree.add_argument("participant")
    agree.add_argument("file", type=Path)
    for sub in [run, agree]:
        sub.add_argument("--state", type=Path, required=True)
        sub.add_argument("--repo", type=Path, required=True)
    footprint = cmds.add_parser(
        "trace",
        help="Run a test job in a fresh checkout and record the inputs it consumed",
    )
    footprint.add_argument("--repo", type=Path, required=True)
    footprint.add_argument("--commit", required=True)
    footprint.add_argument("--out", type=Path, required=True)
    footprint.add_argument("--log", type=Path, help="Write the job's output here")
    footprint.add_argument(
        "--ignore-env",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Leave matching environment variables out of the record (repeatable)",
    )
    footprint.add_argument("--test", nargs=argparse.REMAINDER, required=True)
    units = cmds.add_parser(
        "trace-units",
        help="Trace a command that starts one process per test file; keep a footprint per file",
    )
    units.add_argument("--repo", type=Path)
    units.add_argument("--commit")
    units.add_argument(
        "--workdir", type=Path, help="Trace in this clean checkout instead of a fresh one"
    )
    units.add_argument(
        "--pattern", help="Regex on a process's argv that starts a unit; group 1 names it"
    )
    units.add_argument(
        "--each",
        metavar="TEMPLATE",
        help="Run each unit as its own traced command, with {} for the unit name",
    )
    units.add_argument("--units-file", type=Path, help="Unit names for --each, one per line")
    units.add_argument("--cwd", default="", help="Directory in the checkout to run --each in")
    units.add_argument("--out", type=Path, required=True)
    units.add_argument("--log", type=Path, help="Write the command's output here")
    units.add_argument("--ignore-env", action="append", default=[], metavar="PATTERN")
    units.add_argument("--strict-env", action="store_true")
    units.add_argument("--test", nargs=argparse.REMAINDER)
    pick = cmds.add_parser(
        "select", help="List the units a candidate must run; the rest have passing footprints"
    )
    pick.add_argument("bundle", type=Path)
    pick.add_argument("--repo", type=Path)
    pick.add_argument("--candidate")
    pick.add_argument("--workdir", type=Path)
    pick.add_argument(
        "--discover",
        action="append",
        metavar="GLOB",
        help="Unit files in the candidate tree; one with no footprint runs (repeatable)",
    )
    pick.add_argument("--strip", default="", help="Prefix to remove from a found path to get its unit name")
    pick.add_argument(
        "--listings",
        choices=("exact", "names"),
        default="exact",
        help="names: a directory change matters only for names a unit looked up there, "
        "unless the unit opened most of the directory",
    )
    pick.add_argument("--environment", action="store_true")
    pick.add_argument("--host", action="store_true")
    pick.add_argument("--allow-network", action="store_true")
    pick.add_argument("--out", type=Path)
    pick.add_argument("--list", type=Path, help="Write the units to run here, one per line")
    store_units = cmds.add_parser(
        "cached-units",
        help="Run a job's test files against a store of each file's newest passing footprint",
    )
    store_units.add_argument("--store", type=Path, required=True)
    store_units.add_argument("--job", required=True)
    store_units.add_argument("--workdir", type=Path)
    store_units.add_argument("--cwd", default="", help="Directory in the checkout to run each file in")
    store_units.add_argument("--each", required=True, metavar="TEMPLATE",
                             help="Command for one file, with {} for its name")
    store_units.add_argument(
        "--batch", metavar="TEMPLATE",
        help="In enforce mode, run the files that must run as one untraced command, where the "
        "part with {} repeats per file; only if it fails, run them one by one traced",
    )
    store_units.add_argument("--units-file", type=Path)
    store_units.add_argument("--discover", action="append", metavar="GLOB")
    store_units.add_argument("--strip", default="")
    store_units.add_argument("--proof-store", type=Path, action="append", default=[],
                             help="Another store whose records also count, such as a pull request's own")
    store_units.add_argument("--audit-book", type=Path,
                             help="Signers' audit record: rerun proof files at each signer's share and record the result")
    store_units.add_argument("--no-host", action="store_true",
                             help="Do not compare files outside the checkout")
    store_units.add_argument("--mode", choices=("shadow", "enforce", "plan"), default="shadow")
    store_units.add_argument("--audit-rate", type=float, default=0.1)
    store_units.add_argument("--allow-network", action="store_true")
    store_units.add_argument("--listings", choices=("exact", "names"), default="names")
    store_units.add_argument("--ignore-env", action="append", default=[], metavar="PATTERN")
    store_units.add_argument("--strict-env", action="store_true")
    store_units.add_argument("--log", type=Path)
    store_units.add_argument("--receipt", type=Path)
    store_units.add_argument("--list", type=Path, help="Write the files that had to run here")
    store_units.add_argument(
        "--shard",
        type=shard_spec,
        default=(1, 1),
        metavar="I/N",
        help="Run only shard I of N of the files that must run, balanced by recorded seconds",
    )
    store_units.add_argument(
        "--pin-file", type=Path,
        help="Unit names, one per line, that always run on shard 1",
    )
    store_units.add_argument(
        "--symbols", action="append", metavar="PATTERN",
        help="Keep per-function records for JS/TS files matching PATTERN (repeatable); "
        "put {lcov} in --each where bun's coverage arguments go",
    )
    store_units.add_argument(
        "--scratch", action="append", metavar="DIR",
        help="Untracked directory (repository path) where tests keep runtime data; nothing "
        "read or written there counts as an input (repeatable)",
    )
    dotnet_units = cmds.add_parser(
        "dotnet-units",
        help="Run a .NET test project's classes against a store of each class's last passing program map",
    )
    dotnet_units.add_argument("--store", type=Path, required=True)
    dotnet_units.add_argument("--job", required=True)
    dotnet_units.add_argument("--workdir", type=Path, default=Path("."))
    dotnet_units.add_argument("--tests-dll", type=Path, required=True,
                              help="The built test assembly; its folder holds the project's own assemblies")
    dotnet_units.add_argument("--run", required=True, metavar="TEMPLATE",
                              help="The dotnet test command (with --no-build), with {filter} where lit puts "
                              "--filter and the classes to run")
    dotnet_units.add_argument("--mode", choices=("enforce", "refresh", "sweep", "plan"), default="enforce")
    dotnet_units.add_argument("--jobs", type=int, help="classes recorded at once (refresh, sweep)")
    dotnet_units.add_argument("--audit-rate", type=float, default=0.0)
    dotnet_units.add_argument("--log", type=Path)
    dotnet_units.add_argument("--receipt", type=Path)
    dotnet_units.add_argument("--list", type=Path, help="Write the classes that had to run here")
    blame_cmd = cmds.add_parser(
        "blame",
        help="Name the change a failed combined run's failing test files could have seen",
    )
    blame_cmd.add_argument("receipts", type=Path, nargs="+", help="cached-units receipts of the run")
    blame_cmd.add_argument("--workdir", type=Path, default=Path("."))
    blame_cmd.add_argument(
        "--change", action="append", required=True, metavar="ID=BASE..HEAD",
        help="One change of the group and the commits that bound it (repeatable)",
    )
    blame_cmd.add_argument("--out", type=Path)
    score_bun = cmds.add_parser(
        "score-bun",
        help="Score a cached-units plan against the logs of a real bun test run",
    )
    score_bun.add_argument("plan", type=Path, help="Receipt of cached-units --mode plan")
    score_bun.add_argument("--log", type=Path, action="append", required=True)
    score_bun.add_argument("--out", type=Path)
    reuse = cmds.add_parser(
        "reuse",
        help="Predict whether candidate commits would reuse a recorded job result",
    )
    reuse.add_argument("footprint", type=Path)
    reuse.add_argument("--repo", type=Path, required=True)
    reuse.add_argument("--candidate", nargs="+", required=True)
    reuse.add_argument(
        "--environment",
        action="store_true",
        help="Also compare this process's environment with the record",
    )
    reuse.add_argument(
        "--host",
        action="store_true",
        help="Also compare files read outside the checkout on this machine",
    )
    reuse.add_argument("--allow-network", action="store_true")
    reuse.add_argument(
        "--verify",
        action="store_true",
        help="Also run the job on each candidate and score the prediction",
    )
    reuse.add_argument("--out", type=Path)
    cached = cmds.add_parser(
        "cached-run",
        help="Run a CI job and compare it with earlier footprints on this runner",
    )
    cached.add_argument("--store", type=Path, required=True)
    cached.add_argument("--job", required=True)
    cached.add_argument("--workdir", type=Path, default=Path("."))
    cached.add_argument("--mode", choices=["shadow", "enforce"], default="shadow")
    cached.add_argument(
        "--audit-rate",
        type=float,
        default=0.1,
        help="In enforce mode, the fraction of predicted reuses that still run",
    )
    cached.add_argument("--ignore-env", action="append", default=[], metavar="PATTERN")
    cached.add_argument(
        "--strict-env",
        action="store_true",
        help="Also record per-run CI variables such as GITHUB_SHA and GITHUB_RUN_ID",
    )
    cached.add_argument("--allow-network", action="store_true")
    cached.add_argument("--log", type=Path)
    cached.add_argument("--receipt", type=Path)
    cached.add_argument("--test", nargs=argparse.REMAINDER, required=True)
    cache_report = cmds.add_parser(
        "cache-report", help="Summarize shadow and enforced reuse for a store"
    )
    cache_report.add_argument("--store", type=Path, required=True)
    cache_report.add_argument("--job")
    shadow_cmd = cmds.add_parser(
        "shadow",
        help="Say what Frontier would decide for a snapshot from a unit store, without running anything",
    )
    shadow_cmd.add_argument("snapshot", type=Path)
    shadow_cmd.add_argument("--repo", type=Path, required=True)
    shadow_cmd.add_argument("--store", type=Path, required=True)
    shadow_cmd.add_argument("--job", required=True)
    shadow_cmd.add_argument(
        "--result", type=Path, help="A run's result.json, to compare with what actually passed"
    )
    shadow_cmd.add_argument(
        "--proofs", type=Path, help="A store that 'proof take' filled: signed records count as certificates"
    )
    shadow_cmd.add_argument(
        "--discover", action="append", metavar="GLOB",
        help="Test files as cached-units finds them: any without a passing footprint is reported",
    )
    shadow_cmd.add_argument("--strip", default="", help="Prefix to remove from a found path to get its unit name")
    shadow_cmd.add_argument("--out", type=Path)
    publish = cmds.add_parser(
        "land", help="Push a locally tested result to its original GitHub target"
    )
    publish.add_argument("receipt", type=Path)
    publish.add_argument("--repo", type=Path, required=True)
    draft = cmds.add_parser(
        "propose",
        help="Open a tested batch as a draft PR through the existing GitHub review process",
    )
    draft.add_argument("receipt", type=Path)
    draft.add_argument("--repo", type=Path, required=True)
    draft.add_argument("--head", required=True, help="New topic branch for this batch")
    draft.add_argument("--title")
    draft.add_argument(
        "--notes",
        type=Path,
        help="Additional Markdown to include in the PR description",
    )
    if sys.argv[1:2] == ["proof"]:
        from . import proof

        return proof.main(sys.argv[2:])
    if sys.argv[1:2] == ["intake"]:
        # The intake has its own actions and options; this layer only names the repository.
        q = intake_options(argparse.ArgumentParser(prog="concurrent-merge intake"))
        known, rest = q.parse_known_args(sys.argv[2:])
        from . import intake

        intake.main(
            rest,
            repository=known.repository,
            branch=known.branch,
            context=known.context,
            original_prs_only=known.original_prs_only,
            max_batch=known.max_batch,
        )
        return 0
    args = p.parse_args()
    try:
        if args.action == "collect":
            if args.author_pattern:
                try:
                    re.compile(args.author_pattern)
                except re.error as exc:
                    raise ValueError(f"--author-pattern is not a valid regex: {exc}")
            snapshot = collect(
                args.repository,
                args.branch,
                selection_of(
                    args.prs, args.label, args.min_reviews, args.hold_outside, args.allow_outside
                ),
                args.author_pattern,
            )
            repo = Repo(args.repo)
            if repo.out("rev-parse", "--is-bare-repository") != "true":
                raise ValueError("Use a dedicated bare clone")
            specs = [
                "refs/heads/" + args.branch,
                *(
                    "refs/pull/" + str(p["number"]) + "/head"
                    for p in snapshot["changes"]
                ),
            ]
            repo.run(
                "fetch",
                "--no-tags",
                "--stdin",
                "https://github.com/" + args.repository + ".git",
                data=("\n".join(specs) + "\n").encode(),
            )
            verified_changes(repo, snapshot)
            oids = {snapshot["base"], *(p["head"] for p in snapshot["changes"])}
            pins = "".join(
                f"update refs/concurrent-merge/inputs/{oid} {oid}\n" for oid in oids
            )
            repo.run("update-ref", "--stdin", data=pins.encode())
            write_json(args.out, snapshot)
        elif args.action == "preview":
            from concurrent_merge.projection import preview as predict

            evidence = predict(
                Repo(args.repo), args.repository, args.branch, args.numbers, queue
            )
            write_json(args.out, evidence)
            print(json.dumps(evidence["prediction"], indent=2))
        elif args.action == "run":
            if args.shadow_store and not args.shadow_job:
                raise ValueError("--shadow-store needs --shadow-job")
            return test_snapshot(args)
        elif args.action == "shadow":
            from .shadow import shadow

            receipt = attempts = None
            if args.result:
                receipt = json.loads(args.result.read_text(encoding="utf8"))
                history = args.result.parent / "attempts.json"
                attempts = json.loads(history.read_text(encoding="utf8")) if history.exists() else None
            snapshot = json.loads(args.snapshot.read_text(encoding="utf8"))
            if receipt and receipt.get("snapshot") != snapshot:
                raise ValueError("--result is from another snapshot")
            result = shadow(Repo(args.repo), snapshot, args.store, args.job, receipt, attempts,
                            proofs=args.proofs, discover=args.discover, strip=args.strip)
            if args.out:
                write_json(args.out, result)
            print(json.dumps(dict(result["decision"], **({"outcome": result["outcome"]} if receipt else {})),
                             indent=2))
        elif args.action == "agree":
            Engine(Repo(args.repo), args.state).agree(
                args.conflict, args.participant, args.file.read_bytes()
            )
        elif args.action == "trace":
            return record_footprint(args)
        elif args.action == "trace-units":
            return record_units(args)
        elif args.action == "select":
            return select_units(args)
        elif args.action == "cached-units":
            return run_cached_units(args)
        elif args.action == "dotnet-units":
            from .dotnet_units import run_dotnet_units

            log = open(args.log, "a", encoding="utf8") if args.log else None
            try:
                row = run_dotnet_units(
                    args.store, args.job, args.workdir, args.tests_dll, shlex.split(args.run),
                    mode=args.mode, audit_rate=args.audit_rate, log=log, receipt=args.receipt, listing=args.list,
                    jobs=args.jobs,
                )
            finally:
                if log:
                    log.close()
            print(json.dumps({k: row[k] for k in ("mode", "units", "ran", "reused", "audited", "exit_code",
                                                  "seconds_ran", "reasons")}))
            return row["exit_code"]
        elif args.action == "blame":
            from .blame import changed_paths, failed_reads, verdict

            changes = {}
            for spec in args.change:
                cid, _, span = spec.partition("=")
                base, _, head = span.partition("..")
                if not (cid and base and head):
                    raise ValueError("--change takes ID=BASE..HEAD")
                changes[cid] = changed_paths(args.workdir, base, head)
            result = verdict(failed_reads(args.receipts), changes)
            if args.out:
                write_json(args.out, result)
            print(json.dumps({k: result[k] for k in ("action", "eject", "bisect", "rerun")}))
        elif args.action == "score-bun":
            from .bun_log import main_score

            return main_score(args)
        elif args.action == "reuse":
            return predict_reuse(args)
        elif args.action == "cached-run":
            return run_cached(args)
        elif args.action == "cache-report":
            from .cache import report

            print(json.dumps(report(args.store, args.job), indent=2))
        elif args.action == "propose":
            propose(args, collect)
        else:
            land(args)
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0
