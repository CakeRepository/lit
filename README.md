# lit

Test several reviewed PRs in one checkout and land the tested commit with one Git ref update. The engine works with ordinary Git objects. Each PR keeps its original head as a parent and has trailers identifying its number and author.

## For agents

lit is built to be driven by coding agents. Many agents can open PRs at once; lit tests them together on one runner, lands the passing ones as ordinary Git commits, and hands each failure or overlap back to the change that caused it.

1. Install it: `python -m pip install git+https://github.com/CakeRepository/lit.git` (Python 3.12+, Git 2.48+). The command is `concurrent-merge`, and `concurrent-merge --help` lists every subcommand.
2. Snapshot what is ready: `concurrent-merge collect OWNER/REPO --branch main --repo batch.git --out queue.json` reads the GitHub merge queue. A repository without one picks its changes with `--prs 133 134 135`, `--label lit-ready` or `--min-reviews 2` (see "Repositories without a merge queue"). Any other source of ready changes can write the same JSON shown below.
3. Test the batch: `concurrent-merge run queue.json --repo batch.git --state batch-state --test ./your-test-command`. Exit 0 from your command accepts a candidate and exit 1 starts bisection.
4. Read the receipt: `run` prints the path of `result.json`. It lists the passing subset, and `attempts.json` beside it lists every candidate and failure. A held change has a conflict file under `batch-state/conflicts` with the exact Git blobs to fix.
5. Fix a conflict: write the agreed file bytes and record your vote with `concurrent-merge agree` (below). Rerun step 3.
6. Land: `concurrent-merge land` pushes the tested commit with a lease, or `concurrent-merge propose` opens a PR for repositories that require GitHub's merge queue.
7. Skip tests that cannot be affected: `trace-units` and `cached-units` record what each test file read and rerun only files whose inputs changed (see "One footprint per test file").

Every command reads and writes plain files and Git refs, so an agent can inspect each step with `cat`, `jq` and `git`.

## Run a batch

Use Python 3.12 or later, Git 2.48 or later, and an authenticated GitHub CLI for queue access. PowerShell 7 enables the native PowerShell parser. Install `weave-driver` on PATH or pass `--weave /path/to/weave-driver` before the test command. lit is tested with [Weave v0.7.1](https://github.com/Ataraxy-Labs/weave/releases/tag/v0.7.1).

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

Git handles paths, file types, modes, and renames. For a clean concurrent content merge, lit can verify Git's exact bytes by reconstructing all disjoint input hunks. For files that need structural comparison it uses Weave on supported formats and the PowerShell parser on `.ps1`, `.psm1`, and `.psd1`. The PowerShell adapter matches functions, classes, members, named script blocks, and control-flow blocks such as if/else and try/catch. It preserves original text between declarations, checks for ambiguous identities, and parses the combined result. Applying a file resolution rebuilds only the Git directories on the affected paths.

A pending PR contributes none of its files to the candidate. Other PRs continue. Each JSON file under `batch-state/conflicts` identifies the path, Git stage blobs and modes, incoming PR revision, and participants. An author can read an input with `git -C batch.git cat-file blob OBJECT_ID`. Agents exchange proposed replacement bytes through their existing communication channel, then each records its agreement through the local adapter.

```sh
concurrent-merge agree CONFLICT_ID "PARTICIPANT_FROM_JSON" agreed-file --repo batch.git --state batch-state
```

Run the snapshot again after agreement, or collect again if the target moved. Every listed participant must vote for the same blob. The votes bind to the conflicting inputs and the incoming revision. Distinct PRs retain distinct votes even when the same GitHub account opened both. Rename, delete, and type conflicts require a revised PR. The filesystem adapter assumes a trusted coordinator attributes each vote to its participant. Agent identity verification and network delivery belong to the hosting integration.

## Landing

For a GitHub target that accepts an aggregate PR as its unit of review, submit the tested commit as a draft batch PR. This pushes one new topic branch. lit rechecks the queued source revisions and their ancestry. If the target advanced during validation, the combined tree must still match the tested tree byte for byte. Original heads already incorporated into the target are recorded separately. Repeating the command recovers an existing open PR for the same head. Mark the draft ready after review and use the repository's ordinary GitHub merge queue.

```sh
concurrent-merge propose batch-state/runs/BATCH/result.json --repo batch.git --head lit/batch-example
```

Before opening a new PR, lit looks for a native GitHub merge group with the exact tested tree, current base, and every tested source head in its history. A match returns the existing group for tracking and saves the duplicate submission. GitHub still owns the result of that group's checks.

`run` prints a receipt path and leaves the target alone. A partial batch returns exit 1 and records its passing subset. To publish that subset explicitly, use the receipt from the operator's trusted state directory.

```sh
concurrent-merge land batch-state/runs/BATCH/result.json --repo batch.git
```

`land` checks the retained tested ref, verifies ancestry, rereads queue membership and exact PR heads, and uses a lease on the original target SHA for one push. GitHub applies the repository's branch rules. The lease atomically guards the target ref. Use `propose` when a repository requires GitHub's own merge-queue transaction.

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

## Predict a landing

To predict a landing before admission, run `concurrent-merge preview OWNER/REPO PR_NUMBER [PR_NUMBER ...] --repo batch.git --out observation.json`. It combines the source revisions with the latest assigned queue predecessor and records the source snapshot, queue snapshot, computed time and predicted tree. It also reports earlier entries whose commits are still unknown. After fetching the protected landing, compare it with the saved prediction using `python -m concurrent_merge.projection --repo batch.git --observation observation.json --landed LANDED_SHA`. Prediction uses local Git objects and reads GitHub without requesting CI or publishing a check.

To measure how often early assembly predicts the eventual protected tree, run `python -m concurrent_merge.projection --repo batch.git --queue archived-queue.json --sources ready.json --landed LANDED_SHA`. Use a queue snapshot from `collect` and exact source heads from the same observation. The replay builds trial Git objects on then-current main and on the latest assigned predecessor before the selected sources. It reports unresolved preceding entries, tree equality and source ancestry. It creates no tested ref or check status.

Run the engine regression tests with `python -m unittest discover -s tests -v`.

## License

lit is licensed under the [Apache License, Version 2.0](LICENSE). Copies and derivative works must keep the [NOTICE](NOTICE) file.

## Design references

Weave provides the entity merge driver. Jujutsu's conflict representation informed the persisted input stages. Graft's batch integration and Pijul's treatment of patch dependencies informed the batch and dependency handling. [Weave](https://github.com/Ataraxy-Labs/weave) · [Jujutsu conflicts](https://docs.jj-vcs.dev/latest/conflicts/) · [git-ai / graft](https://github.com/feder-cr/git-ai) · [Pijul patch theory](https://pijul.org/manual/theory.html)
