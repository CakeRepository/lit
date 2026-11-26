# lit

Test several reviewed PRs in one checkout and land the tested commit with one Git ref update. The engine works with ordinary Git objects. Each PR keeps its original head as a parent and has trailers identifying its number and author.

What lit has and hasn't proven on real PRs so far, with the numbers, is in [docs/proof-status.md](docs/proof-status.md).

## For agents

lit is built to be driven by coding agents. Many agents can open PRs at once; lit tests them together on one runner, lands the passing ones as ordinary Git commits, and hands each failure or overlap back to the change that caused it.

1. Install it: `python -m pip install git+https://github.com/CakeRepository/lit.git` (Python 3.12+, Git 2.48+). The command is `concurrent-merge`, and `concurrent-merge --help` lists every subcommand.
2. Snapshot what is ready: `concurrent-merge collect OWNER/REPO --branch main --repo batch.git --out queue.json` reads the GitHub merge queue. A repository without one picks its changes with `--prs 133 134 135`, `--label lit-ready` or `--min-reviews 2` (see "Repositories without a merge queue"). Any other source of ready changes can write the same JSON shown below.
3. Test the batch: `concurrent-merge run queue.json --repo batch.git --state batch-state --test ./your-test-command`. Exit 0 from your command accepts a candidate and exit 1 starts bisection.
4. Read the receipt: `run` prints the path of `result.json`. It lists the passing subset, and `attempts.json` beside it lists every candidate and failure. A held change has a conflict file under `batch-state/conflicts` with the exact Git blobs to fix.
5. Fix a conflict: write the agreed file bytes and record your vote with `concurrent-merge agree` (below). Rerun step 3.
6. Land: `concurrent-merge land` pushes the tested commit with a lease, or `concurrent-merge propose` opens a PR for repositories that require GitHub's merge queue.
7. Teach lit your language: if lit reports that it has no reader for your files, open an issue and send a reader (see [CONTRIBUTING.md](CONTRIBUTING.md)).
8. Skip tests that cannot be affected: `trace-units` and `cached-units` record what each test file read and rerun only files whose inputs changed (see "One footprint per test file").

Every command reads and writes plain files and Git refs, so an agent can inspect each step with `cat`, `jq` and `git`.

## One binary

lit is becoming one Rust binary, `lit`, with nothing else to install ([#35](https://github.com/CakeRepository/lit/issues/35)). It takes the same commands and options as `concurrent-merge`. A command that has not moved to Rust yet runs on the Python lit built into the binary, so for now the machine still needs Python 3.12 or later. The crate has no dependencies. Build it with Rust 1.80 or later:

```sh
cargo build --release
./target/release/lit version
```

Each `v*` tag publishes a release with a binary for Linux, macOS and Windows on amd64 and arm64, and their SHA-256 sums in `SHA256SUMS`. Check a download before you run it:

```sh
sha256sum --check --ignore-missing SHA256SUMS
```

`lit parity COMMAND ...` runs a command with the Python lit and with the Rust lit, each in its own copy of the current folder, and fails if their exit codes, output, files or Git refs differ. Times, durations, test logs and the random part of a run folder may differ; batch commits are made at `SOURCE_DATE_EPOCH`, which parity pins, so their IDs must match. A command moves to Rust only once its parity cases match. Until then parity runs Python twice, which checks that the command gives the same result from the same inputs.

The binary runs its Python from a private folder in the OS temp directory, never from the checkout, the action folder, `HOME` or `RUNNER_TEMP`. When it runs as root, as a GitHub container job does, it gives anything it created under the paths you name back to the owner of their folder, so the runner can still clean up. CI checks both with `scripts/root-hygiene.sh`.

## Run a batch

Use Python 3.12 or later and Git 2.48 or later. Collecting, testing, merging and landing need nothing else installed: lit is written with the Python standard library and plain Git, and talks to GitHub through its own small client. That client uses the token in `GH_TOKEN` or `GITHUB_TOKEN`, or else asks Git's credential helper for the GitHub host (`GITHUB_API_URL` points it at GitHub Enterprise). A few optional features still call outside tools when they are present: footprint tracing runs the job under Linux `strace`, and the .NET program map builds with the `dotnet` SDK. Per-function selection for JavaScript and TypeScript (`--symbols`) reads those files with lit's own tokenizer, so it needs no parser; it still relies on Bun to run your tests and write the `lcov` coverage report that says which functions ran (put `{lcov}` where `bun test` takes its coverage arguments).

```sh
python -m pip install .
git clone --bare https://github.com/OWNER/REPO.git batch.git
concurrent-merge collect OWNER/REPO --branch main --repo batch.git --out queue.json
concurrent-merge run queue.json --repo batch.git --state batch-state --test ./run-full-suite
```

Use the repository's full test command in place of `./run-full-suite`. The existing runner can invoke collection whenever it becomes available. One call reads every page of the GitHub merge queue immediately. Arrivals during validation belong to the next collection. An installation with a different readiness signal can supply the same snapshot format from its review system.

```json
{
  "repository": "OWNER/REPO",
  "branch": "main",
  "base": "<full target commit ID>",
  "changes": [
    {"number": 42, "author": "octocat", "head": "<full reviewed PR head ID>"}
  ]
}
```

`collect` fetches each PR's `refs/pull/N/head` into the bare copy, so a plain `git clone --bare` is enough.

### Repositories without a merge queue

When readiness is decided by people or a coordinator rather than a merge queue, name the changes:

```sh
concurrent-merge collect OWNER/REPO --repo batch.git --out queue.json --prs 133 134 135
concurrent-merge collect OWNER/REPO --repo batch.git --out queue.json --label lit-ready
concurrent-merge collect OWNER/REPO --repo batch.git --out queue.json --min-reviews 2
```

Each reads the open, non-draft PRs against `--branch` and their current heads. `--min-reviews K` counts distinct reviewers whose latest review approves the current head, so a push after approval drops a PR until it is approved again. The rules combine: `--label lit-ready --min-reviews 1` takes labelled PRs with at least one approval. A PR named with `--prs` that is closed, a draft, against another branch or outside the other rules stops the collection with an error rather than being skipped. The snapshot records the rules as `selection`, and `land` checks the same rules again before it pushes.

These rules, and the `land` that rechecks them, read GitHub over GraphQL and fall back to the REST API when GraphQL is refused, as some proxies and sandboxed agent sessions do. Set `LIT_GITHUB_API=rest` to skip GraphQL, or `LIT_GITHUB_API=graphql` to turn the fallback off. GitHub's merge queue has no REST API, so plain `collect` still needs GraphQL.

### Pull requests from outside contributors

`--hold-outside` keeps PRs whose authors have no write access (GitHub's author association is not owner, member or collaborator) out of the batch, and lists them so they can be reviewed first. After review, name the ones that passed with `--allow-outside 140 141`. Both are part of the snapshot's `selection`, so `land` holds the same PRs when it checks again.

### Several agents on one GitHub login

lit hands a failure or a conflict back to the change's author, which is its GitHub login by default. When several agents open PRs from one shared account, give each change an `agent`:

```json
{"number": 42, "author": "shared-bot", "agent": "reviewer-bot-2", "head": "<full reviewed PR head ID>"}
```

`collect --author-pattern REGEX` fills it from each PR body, from group 1 of the first match, for example `--author-pattern '^Bot: (.+)$'`. It works with the merge queue and with the rules above. A PR whose body has no match keeps its login. The agent appears in receipts, conflict files and votes, and the batch commit carries it as `PR-Author:` with the login kept as `PR-Login:`.

Each test runs in a fresh detached checkout. The test command owns its suite and runs with the runner's permissions. Exit 0 accepts that exact candidate. Exit 1 triggers bisection. Other exit codes stop the operation as an infrastructure error. When a half passes, the engine tests the next half on top of it. This catches changes that pass alone and fail together. A failed singleton goes back to its author with the base on which it failed. The batch receipt lists the passing subset, and its sibling `attempts.json` records every candidate, assembly method, held PR, and failure.

## Parallel assembly on one runner

During bisection, lit reuses a failure if it reconstructs the exact same commit in the same run. The receipt records each such decision as `known-failure`. Every accepted subset still has a passing check on its own commit.

Use `run --jobs 8` before `--test` to set the number of comparison workers. The default uses up to eight available CPUs. Workers execute native Git comparisons and independent file drivers concurrently. Git's existing tree object IDs allow the comparison to skip unchanged directories.

For independent PRs, lit reads the path deltas in parallel and writes the entire two-parent commit chain with one Git fast-import process. It verifies the branch histories, file modes, original blobs on an advanced target, and possible file/directory collisions. Stale additions also require a target history compatible with the original path. A shared path, dependency, deletion, rename, or unsupported file type sends the batch through three-way merging. The combined candidate goes through the same test and landing gate in either case.

`python -m benchmarks.runner` measures assembly on one runner, and `python -m benchmarks.indexed_diff` measures sparse comparison of two indexed files. Each reports setup and query costs separately.

## Structural merges and author agreement

Git handles paths, file types, modes, and renames. For a clean concurrent content merge, lit can verify Git's exact bytes by reconstructing all disjoint input hunks. When two changes overlap in one file, lit outlines the file with its own reader for that language and merges it declaration by declaration, so two agents can each add a function or each change a different method and both land. lit has readers, written in Python with no outside tools, for Python (`.py`, `.pyi`, `.pyw`), JavaScript and TypeScript (`.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.mts`, `.cts`), C# (`.cs`), PowerShell (`.ps1`, `.psm1`, `.psd1`), JSON (`.json`), YAML (`.yml`, `.yaml`) and Markdown (`.md`, `.markdown`). Units are matched by name: functions, classes and their members, imports, namespaces, JSON and YAML keys, Pester and JavaScript test blocks, and Markdown sections. A unit added right before or after one the other side renamed or rewrote keeps its place beside it. Text between units merges by whole lines only, a unit both sides changed differently is merged one level down or not at all, and the combined result must read back cleanly. A reader that isn't sure of a file declines it, and then Git's line merge applies and a real overlap goes back to the change that caused it. Applying a file resolution rebuilds only the Git directories on the affected paths.

When lit meets a code file in a language it has no reader for, it says so and asks agents to teach it: open an issue and send a PR that adds a reader. [CONTRIBUTING.md](CONTRIBUTING.md) shows how, with the shared tests every reader passes.

A pending PR contributes none of its files to the candidate. Other PRs continue. Each JSON file under `batch-state/conflicts` identifies the path, Git stage blobs and modes, incoming PR revision, and participants. An author can read an input with `git -C batch.git cat-file blob OBJECT_ID`. Agents exchange proposed replacement bytes through their existing communication channel, then each records its agreement through the local adapter.

```sh
concurrent-merge agree CONFLICT_ID "PARTICIPANT_FROM_JSON" agreed-file --repo batch.git --state batch-state
```

Run the snapshot again after agreement, or collect again if the target moved. Every listed participant must vote for the same blob. The votes bind to the conflicting inputs and the incoming revision. Distinct PRs retain distinct votes even when the same GitHub account opened both. Rename, delete, and type conflicts require a revised PR. The filesystem adapter assumes a trusted coordinator attributes each vote to its participant. Agent identity verification and network delivery belong to the hosting integration.

`tests/test_end_to_end.py` checks these claims end to end against a local stand-in for GitHub: nine concurrent PRs (a disjoint file, two functions changed in one file, a rename, two functions added at the same spot, a change that fails alone, one that fails only when combined and a real overlap) go through `collect`, `run` and `land`, followed by author agreement and the landing races. CI runs it against the Python package and against the binary.

## Landing

For a GitHub target that accepts an aggregate PR as its unit of review, submit the tested commit as a draft batch PR. This pushes one new topic branch. lit rechecks the queued source revisions and their ancestry. If the target advanced during validation, the combined tree must still match the tested tree byte for byte. Original heads already incorporated into the target are recorded separately. Repeating the command recovers an existing open PR for the same head. Mark the draft ready after review and use the repository's ordinary GitHub merge queue.

```sh
concurrent-merge propose batch-state/runs/BATCH/result.json --repo batch.git --head lit/batch-example
```

A batch collected with `--prs`, `--label` or `--min-reviews` has no merge queue to recheck, so `propose` rechecks the same rules instead, as `land` does, and the draft PR says it is for review rather than for the merge queue. Like `collect`, `propose` falls back to the REST API to find and open its PR when GitHub GraphQL is refused.

Before opening a new PR, lit looks for a native GitHub merge group with the exact tested tree, current base, and every tested source head in its history. A match returns the existing group for tracking and saves the duplicate submission. GitHub still owns the result of that group's checks.

`run` prints a receipt path and leaves the target alone. A partial batch returns exit 1 and records its passing subset. To publish that subset explicitly, use the receipt from the operator's trusted state directory.

```sh
concurrent-merge land batch-state/runs/BATCH/result.json --repo batch.git
```

`land` checks the retained tested ref, verifies ancestry, rereads queue membership and exact PR heads, and uses a lease on the original target SHA for one push. GitHub applies the repository's branch rules. The lease atomically guards the target ref. Use `propose` when a repository requires GitHub's own merge-queue transaction.

## Batch ahead of GitHub's merge queue

GitHub's merge queue tests a few groups at a time, so a repository that agents commit to all day lands far fewer PRs per hour than it receives. The batcher puts every ready PR into one batch PR, so one pass of the queue lands all of them.

A PR is ready when it carries the label (default `lit-ready`), comes from a branch in the same repository, passes the required check on its current head, and has no unresolved review threads. Only people with triage access can add a label, and a PR from a fork is never batched. Each pass pins ready heads under `refs/heads/lit/ready/N`, builds one batch PR, enqueues it, and on a failure splits the batch to find the PR that broke it. All state lives in refs under `refs/heads/lit/`, so a pass that stops anywhere is resumed by the next one.

```sh
concurrent-merge intake OWNER/REPO --context "CI / tests" tick --label lit-ready
```

`tick` never sleeps: it returns while GitHub checks a batch, and the next call carries on. Run it on a schedule and when the required check finishes. The `actions/batcher` action does this in a GitHub workflow:

```yaml
on:
  schedule: [{cron: "*/5 * * * *"}]
  workflow_run: {workflows: ["CI"], types: [completed]}
  workflow_dispatch:
concurrency: {group: lit-batcher, cancel-in-progress: false}
jobs:
  batch:
    runs-on: ubuntu-latest
    steps:
      - id: app
        uses: actions/create-github-app-token@v3  # pin a commit SHA
        with:
          client-id: ${{ secrets.LIT_APP_CLIENT_ID }}
          private-key: ${{ secrets.LIT_APP_PRIVATE_KEY }}
      - uses: CakeRepository/lit/actions/batcher@main  # pin a commit SHA
        with:
          token: ${{ steps.app.outputs.token }}
          context: CI / tests
```

With many agents, more PRs pass alone and fail together, and one batch of everything spends most of its runs halving. `--max-batch N` (the action's `max-batch` input) caps a fresh batch at the N lowest-numbered ready PRs; the rest stay pinned for the next one. `--max-batch auto` sizes each batch from the recent share of PRs that came back red, using Hwang's binary splitting: about one in ten red gives batches of 8, one in a hundred gives 64, and it starts at 32. The counts live in the intake's own record under `refs/heads/lit/active/BRANCH`.

Use a GitHub App token: branches and PRs created with the workflow's own `GITHUB_TOKEN` start no checks. The App needs read and write access to contents, pull requests and workflows, and read access to actions, checks, commit statuses and issues. The other intake actions are `ready N...` to pin named PRs, `drain [--no-wait]` to resume, `withdraw N HEAD` to take a pin back, and `report N` to count the workflow runs a landed batch used.

## Record what a test job consumed

`trace` runs one job under `strace` in a fresh detached checkout and writes a footprint. Every path the job opened, executed, inspected or listed before it wrote that path counts as an input. Checkout inputs are recorded as the traced commit's Git entries: file modes and blob IDs, directory listings, and paths the job looked for and did not find. Files read outside the checkout are hashed, and environment variables are recorded as hashes. Reads of the checkout's own `.git` mark the job as revision-specific, and network connections are listed.

```sh
concurrent-merge trace --repo batch.git --commit TESTED_SHA --out portal.footprint.json --test ./run-portal-tests
concurrent-merge reuse portal.footprint.json --repo batch.git --candidate NEXT_SHA [NEXT_SHA ...]
```

`reuse` compares each candidate's tree with the footprint and prints `reusable` with the expected exit code, or `run` with the inputs that differ. It executes nothing unless you ask. Add `--environment` to compare the current environment and `--host` to compare the toolchain files on this machine. Add `--verify` to run the job on each candidate as well and report whether the prediction matched the actual exit code. Use `--ignore-env PATTERN` at trace time for variables that legitimately change per run, such as a run ID, and only after confirming the job does not read them.

A footprint is an observation of one execution under Linux `strace`. It does not confine the job, and a later run can read data the earlier one did not. A prediction alone cannot authorize a protected GitHub status; the required checks still run.

### One footprint per test file

A large suite rarely reuses as a whole, because almost every change touches something one of its files reads. `trace-units` keeps a footprint for each test file instead, and `select` lists the files a candidate must run.

```sh
concurrent-merge trace-units --workdir . --cwd web --units-file test-files.txt \
  --each 'bun test --timeout=15000 ./{}' --out units.json
concurrent-merge select units.json --workdir . --discover 'web/*.test.*' --strip web/ \
  --host --list to-run.txt
```

With `--each`, every unit runs as its own command under its own trace, one after another. Alternatively, `--pattern REGEX --test COMMAND` traces one command and starts a unit at each process whose argument list matches the pattern, so any runner that starts one process per file works unchanged. A unit runs when it failed when recorded, when any input it read differs, or when a file matched by `--discover` has no footprint. Files that an earlier unit of the same recording wrote, such as bytecode or transpiler caches, are listed as shared state and are not treated as inputs. Running a selected subset still breaks any test that depends on what another file did first.

### Signed results from other machines

A machine that ran a job's files with `cached-units` can hand its passing records to CI, so the merge runs only what those records do not cover. Each proof holds the records written for one commit, the repository, the tree and the container image, and is signed with `ssh-keygen`:

```sh
concurrent-merge proof pack --store /var/lib/lit --job unit --repository OWNER/REPO --image "$IMAGE" --out proof.json.gz
concurrent-merge proof sign proof.json.gz --key ~/.ssh/lit_proof.pub   # an ssh-agent or hardware key works
concurrent-merge proof push proof.json.gz --remote https://github.com/OWNER/REPO.git
```

The proof is pushed as `refs/heads/lit/proof/<commit>/<id>`. CI takes every proof for the commits it is about to merge and gives the result to `cached-units --proof-store`:

```sh
concurrent-merge proof take --remote https://github.com/OWNER/REPO.git --repo . --repository OWNER/REPO \
  --commit "$PR_HEAD" --signers-at origin/main --image "$IMAGE" --into /tmp/proofs
```

A proof counts only when an allowed signer in `.lit/allowed_signers` signed it, read from the target branch so a change cannot add the key that vouches for it. It must also be for this repository, one of the given commits and the given image, and no older than `--max-age-hours` (72 by default). `--revoked` names key fingerprints to refuse. Anything else is skipped and those files run as usual. A signature says which machine made a claim, not that the claim is true: keep signing keys on machines that run tests in the same pinned image as CI, under an account the code under test cannot use, and keep an `--audit-rate` so CI still reruns a share of what proofs cover.

Trust is earned rather than granted by the key. Two things make one machine's word count for less until it has a record:

- **Agreement:** `take --quorum 2` uses a file's record only when at least two different signers report it alike: the same repository inputs with the same contents, the same directories listed, the same missing files and a pass. Timing and the machine itself are left out of the comparison. Signers that report a file differently leave it to run.
- **Audits:** `cached-units --audit-book book.json` reruns each file a proof covers at its signers' share instead of `--audit-rate`. A new signer's share is 1, so everything it vouches for reruns. Every 20 clean audits halve the share, down to 5%. One wrong result puts it back to 1 and drops every proof that signer made before then; `take --audit-book` skips those.

CI keeps the book at `refs/heads/lit/audits`, fetched before the run and pushed after it. The push fails rather than overwrite a newer book:

```sh
concurrent-merge proof book-pull book.json --remote https://github.com/OWNER/REPO.git
concurrent-merge proof take ... --quorum 2 --audit-book book.json --into /tmp/proofs
concurrent-merge cached-units ... --proof-store /tmp/proofs --audit-book book.json
concurrent-merge proof book-push book.json --remote https://github.com/OWNER/REPO.git
```

Run the jobs that write the book in one concurrency group, and let only CI's token push `refs/heads/lit/audits` (a ruleset can do that), because whoever can write the book can clear a signer's record.

## Predict a landing

To predict a landing before admission, run `concurrent-merge preview OWNER/REPO PR_NUMBER [PR_NUMBER ...] --repo batch.git --out observation.json`. It combines the source revisions with the latest assigned queue predecessor and records the source snapshot, queue snapshot, computed time and predicted tree. It also reports earlier entries whose commits are still unknown. After fetching the protected landing, compare it with the saved prediction using `python -m concurrent_merge.projection --repo batch.git --observation observation.json --landed LANDED_SHA`. Prediction uses local Git objects and reads GitHub without requesting CI or publishing a check.

To measure how often early assembly predicts the eventual protected tree, run `python -m concurrent_merge.projection --repo batch.git --queue archived-queue.json --sources ready.json --landed LANDED_SHA`. Use a queue snapshot from `collect` and exact source heads from the same observation. The replay builds trial Git objects on then-current main and on the latest assigned predecessor before the selected sources. It reports unresolved preceding entries, tree equality and source ancestry. It creates no tested ref or check status.

Run the engine regression tests with `python -m unittest discover -s tests -v`.

## Compositional integration (research)

`concurrent_merge/frontier.py` lands a change on its own test passes when no other change in flight shares a test unit with it, and runs only the overlaps when one does. [docs/compositional-integration.md](docs/compositional-integration.md) sets out its model, assumptions, safety invariant, costs and what is still open. Its simulator and benchmarks are synthetic and make no claim about production throughput.

To see what it would decide on your own batches without changing anything, pass a unit store to `run`:

```
concurrent-merge run queue.json --repo batch.git --state state \
  --shadow-store units --shadow-job suite --test python3 -m unittest
```

lit then writes `shadow.json` next to the run's `result.json`: the groups Frontier would have formed, the tests each would have had to run, and which changes it would have landed without running anything that the full suite then failed. `concurrent-merge shadow queue.json --repo batch.git --store units --job suite` gives the same decision without running anything. Add `--shadow-proofs DIR` (or `--proofs DIR`) with a store that `proof take` filled, and each PR's own signed CI run counts as evidence for it too.

Give the shadow the same `--discover` patterns (and `--strip`) as `cached-units`, or `--shadow-discover` and `--shadow-strip` on `run`, and it also lists test files with no passing footprint as `unrecorded`. A file a PR adds must run with that PR's group, and one already on main leaves nothing landing unrun. lit records its own suite this way, one process per file:

```
concurrent-merge cached-units --store units --job unit --workdir . \
  --each 'env PYTHONPATH=tests python3 -m unittest tests/{}' --discover 'tests/test_*.py' --strip tests/
```

Run each file on its own. `unittest discover -p FILE` opens every file in the directory, so every unit would read every test file.

## License

lit is licensed under the [Apache License, Version 2.0](LICENSE). Copies and derivative works must keep the [NOTICE](NOTICE) file.

## Inspired by

lit's code is its own. Its design was inspired by ideas from other version control work: entity-level merging in [Weave](https://github.com/Ataraxy-Labs/weave), the conflict representation in [Jujutsu](https://docs.jj-vcs.dev/latest/conflicts/), batch integration in [git-ai / graft](https://github.com/feder-cr/git-ai), and patch dependencies in [Pijul](https://pijul.org/manual/theory.html). None of their code is included, and lit does not need them installed.
