# Nov 25 2026: lit's batcher left FlowRMM's orchestrator runners unable to start jobs

## What broke

FlowRMM runs lit's batcher action (`CakeRepository/lit/actions/batcher`) in a container job
(`python:3.13-trixie`) on its self-hosted orchestrator runners. A container job runs its steps as root, while
the runner downloads the action into `_work/_actions/CakeRepository/lit/<sha>` as its own, non-root user.

At lit 82516cc the action ran `python3 -c 'from concurrent_merge.cli import main ...'` from that folder.
Importing lit wrote `concurrent_merge/__pycache__/*.pyc` into it, owned by root. Before its next job the runner
deletes and re-downloads `_work/_actions`; it could not delete root-owned files, so every later job on that
runner failed at Set up job with access denied. The batcher's first successful passes broke flowrmm-orch-03
(21:24 UTC), orch-02 (21:31) and orch-01 (21:44), which stalled every FlowRMM PR whose checks need an
orchestrator, including the fix itself (FlowRMM #2914), until the folders were deleted by hand as root.

## Why lit's own CI didn't catch it

- lit's CI ran unit tests on a GitHub-hosted VM as the runner's own user. Nothing ran the actions at all,
  let alone as root in a container over folders owned by someone else.
- A hosted VM is thrown away after each job, so a file the runner can't delete never hurts the next job.
  The failure only exists on a persistent runner, which lit never tested on.
- The tests added for the action fixes (#33, #34) checked the action files' text, not what running them
  leaves behind.

## What was still exposed after the first fix (lit#34, `python3 -B`)

The new test (`tests/test_action_hygiene.py`) fails on e3f34cb too:

- `cached-run` stops bytecode only for lit's own process. A test command that imports lit's modules (or any
  Python under the action folder) still wrote root-owned `__pycache__` there.
- The batcher's Git cache defaults to `$HOME/.cache/lit`, which in a container job is the runner's
  `_work/_temp/_github_home`. It was created root-owned on every pass.

## Guards now in lit

- `actions/lib/guard.sh`, sourced by both actions before lit runs: sets `PYTHONDONTWRITEBYTECODE` for every
  Python process the step starts, lists lit's code folders before the step and on exit removes anything new
  (with a warning), gives anything not owned by the runner's user back to that user, fails the step with the
  exact cleanup command if something still can't be removed, and hands files the step created under the
  runner's temp and home folders back to their owner.
- `tests/test_action_hygiene.py` runs each action's own step script from its `action.yml` over a copy of lit
  that belongs to another user, and fails if the folder changes or anything is left the runner's user couldn't
  delete. It fails on 82516cc and e3f34cb and passes now.
- The `action-hygiene` CI job runs those tests as root in FlowRMM's own batcher image, then runs both actions
  through GitHub's composite runner over a checkout owned by uid 1001 and fails on any root-owned leftover.
