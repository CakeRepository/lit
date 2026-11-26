"""Read-only verification shared by the intake client and the protected merge check."""

import re

from . import github


PREFIX = "codex/ci/lit-v1/"
OID = r"[0-9a-f]{40}"
PULL_FIELDS = """fragment LitSource on PullRequest {
 id number state isDraft isCrossRepository headRefOid headRefName
 baseRefName mergeStateStatus reviewDecision isInMergeQueue mergeQueueEntry{position state headCommit{oid}} author{login}
 reviewThreads(first:100){nodes{isResolved} pageInfo{hasNextPage endCursor}}
 headRepository{nameWithOwner} mergeCommit{oid} autoMergeRequest{enabledAt}
 commits(last:1){nodes{commit{oid status{contexts{context state createdAt creator{login} targetUrl}}}}}
 }"""


class Ineligible(ValueError):
    """The pinned source no longer qualifies for this submission."""


class SourceResolution(Ineligible):
    def __init__(self, number):
        self.number = number
        super().__init__(
            "Source merge needs an author resolution or contains extra changes"
        )


def graphql(query, variables):
    result = github.graphql(query, variables, raise_on_errors=False)
    if result.get("errors") or not result.get("data"):
        raise RuntimeError("GitHub could not verify intake state")
    return result["data"]


def pin_ref(number):
    return f"refs/heads/lit/ready/{number}"


def reject_ref(number):
    return f"refs/heads/lit/rejected/{number}"


def pull_many(repository, numbers):
    """Read every source and its pin, bounding request size independently of batch size."""
    owner, name = repository.split("/")
    numbers = list(dict.fromkeys(numbers))
    if any(type(n) is not int or not 1 <= n < 2**31 for n in numbers):
        raise Ineligible("Invalid source PR number")
    result = {}
    for start in range(0, len(numbers), 25):
        group = numbers[start : start + 25]
        fields = " ".join(
            f"p{n}:pullRequest(number:{n}){{...LitSource}} "
            f'r{n}:ref(qualifiedName:"{pin_ref(n)}"){{target{{oid}}}}'
            for n in group
        )
        data = graphql(
            "query($owner:String!,$repo:String!){repository(owner:$owner,name:$repo){"
            + fields
            + "}} "
            + PULL_FIELDS,
            dict(owner=owner, repo=name),
        )["repository"]
        for n in group:
            pr, ref = data[f"p{n}"], data[f"r{n}"]
            if pr is not None:
                if pr["number"] != n:
                    raise Ineligible("Unexpected source PR in GitHub response")
                pr["unresolvedReview"] = has_unresolved_review(owner, name, pr)
            result[n] = pr, ref["target"]["oid"] if ref else None
    return result


def has_unresolved_review(owner, name, pr):
    threads = pr.pop("reviewThreads")
    while True:
        if any(not t["isResolved"] for t in threads["nodes"]):
            return True
        page = threads["pageInfo"]
        if not page["hasNextPage"]:
            return False
        next_pr = graphql(
            """query($o:String!,$r:String!,$n:Int!,$after:String!){
              repository(owner:$o,name:$r){pullRequest(number:$n){headRefOid
              reviewThreads(first:100,after:$after){nodes{isResolved} pageInfo{hasNextPage endCursor}}}}}""",
            dict(o=owner, r=name, n=pr["number"], after=page["endCursor"]),
        )["repository"]["pullRequest"]
        if not next_pr or next_pr["headRefOid"] != pr["headRefOid"]:
            raise Ineligible("Source changed while reading its review threads")
        threads = next_pr["reviewThreads"]


def pull(repository, number):
    return pull_many(repository, [number])[number]


def trusted_status(repository, pr, head, context):
    commit = pr["commits"]["nodes"][-1]["commit"]
    if commit["oid"] != head:
        return None
    return next(
        (
            s
            for s in (commit.get("status") or {}).get("contexts", [])
            if s["context"] == context
            and (s.get("creator") or {}).get("login") == "github-actions"
            and re.fullmatch(
                re.escape("https://github.com/" + repository + "/actions/runs/")
                + r"[1-9]\d*",
                s.get("targetUrl") or "",
            )
        ),
        None,
    )


def eligibility(repository, branch, pr, head, context):
    if not pr or pr["state"] != "OPEN" or pr["isDraft"] or pr["baseRefName"] != branch:
        return "source is closed, draft, missing, or retargeted"
    if (pr.get("headRepository") or {}).get("nameWithOwner") != repository or pr[
        "isCrossRepository"
    ]:
        return "source is from another repository"
    if pr["headRefName"].startswith((PREFIX, "lit/")) or pr["headRefOid"] != head:
        return "source revision changed or is an intake branch"
    if pr["unresolvedReview"]:
        return "source has unresolved review threads"
    if pr["isInMergeQueue"] or pr["autoMergeRequest"]:
        return "source already has an individual merge request"
    if (
        pr["reviewDecision"] not in (None, "APPROVED")
        or pr["mergeStateStatus"] != "CLEAN"
    ):
        return (
            "source review or branch requirements are incomplete ("
            + pr["mergeStateStatus"]
            + ")"
        )
    if (trusted_status(repository, pr, head, context) or {}).get("state") != "SUCCESS":
        return "required status is missing or unsuccessful on the pinned commit"
    return None


def verify_sources(repository, branch, members, context):
    if not members:
        raise Ineligible("A batch needs at least one pinned source")
    sources = pull_many(repository, (n for n, _ in members))
    green = {}
    for number, head in members:
        pr, pin = sources[number]
        if pin != head:
            raise Ineligible(f"#{number} readiness was withdrawn or replaced")
        reason = eligibility(repository, branch, pr, head, context)
        if reason:
            raise Ineligible(f"#{number}: {reason}")
        green[number] = trusted_status(repository, pr, head, context)["createdAt"]
    return green


def batch_name(base, tip):
    return f"{PREFIX}{base}/{tip}"


def manifest(repo, name, head, target):
    """Derive membership from the Git graph and reproduce every submitted tree."""
    m = re.fullmatch(re.escape(PREFIX) + f"({OID})/({OID})", name)
    if not m or m[2] != head:
        raise Ineligible("Batch name does not identify its exact commit")
    base = m[1]
    repo.run("merge-base", "--is-ancestor", base, target)
    rows = repo.out(
        "rev-list", "--first-parent", "--reverse", "--parents", base + ".." + head
    )
    parent, chain = base, []
    for line in rows.splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[1] != parent:
            raise Ineligible("Batch history is not a continuous chain of source merges")
        parent = fields[0]
        chain.append(fields)
    if not chain or parent != head:
        raise Ineligible("Empty or incomplete batch")
    members, numbers = [], set()
    for start in range(0, len(chain), 128):
        chunk = chain[start : start + 128]
        commits = repo.objects([oid for oid, _, _ in chunk], b"commit")
        pairs = "".join(f"{left} {right}\n" for _, left, right in chunk)
        # --stdin reports each pair's own clean/conflict flag. Git can exit zero
        # even when one pair conflicts. Bind each result to the submitted tree.
        data = repo.run(
            "merge-tree",
            "--stdin",
            "-z",
            "--no-messages",
            "--name-only",
            data=pairs.encode("ascii"),
        ).stdout
        results = data.split(b"\0\0")
        if results.pop() != b"" or len(results) != len(chunk):
            raise Ineligible("Incomplete Git merge verification")
        for (_, _, source), commit, result in zip(chunk, commits, results, strict=True):
            headers, _, raw_message = commit.partition(b"\n\n")
            tree = headers.split(b"\n", 1)[0].removeprefix(b"tree ")
            if not re.fullmatch(rb"[0-9a-f]{40}", tree) or b"\0" in commit:
                raise Ineligible("Invalid batch commit")
            match = re.fullmatch(
                r"Batch PR #(\d+)\n\nPull-Request: #\1\nPR-Author: [^\n]+\nPR-Head: ("
                + OID
                + r")",
                raw_message.decode("utf8").rstrip("\n"),
            )
            if not match or match[2] != source or int(match[1]) in numbers:
                raise Ineligible("Batch commit does not identify a unique source PR")
            number = int(match[1])
            if result != b"1\0" + tree:
                raise SourceResolution(number)
            numbers.add(number)
            members.append((number, source))
    return members
