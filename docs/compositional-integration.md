# Compositional integration: the model

This is the model behind `concurrent_merge/frontier.py`, its simulator
(`frontier_sim.py`) and its benchmark (`frontier_bench.py`). It states what lit
assumes, what it guarantees, what each step costs, and what has only been
measured on synthetic workloads. It tracks issue #9.

## Objects

- **Main** is a map from input keys to content digests. An input is a file, or
  one function of a file (`path#function`) where lit recorded symbols.
- A **change** `c` has a write set `W(c)`: the keys it sets, each to a new
  digest or to nothing (a delete).
- A **test unit** `u` is anything lit runs and records as pass or fail on its
  own: a test file, a test, a job.
- A **certificate** binds one passing run of `u` to the digest of every input
  that run read: `cert = (u, {key: digest})`. Lit keeps the newest `KEEP` (16)
  certificates per unit, plus the newest one that proves main.
- A certificate **proves** `u` on a tree `T` when `T` agrees with it on every
  key it lists. `Frontier.proves(u, members)` checks this for `T` = main plus
  the admitted changes in `members`.

Two indexes answer every question, and no step compares one change against
another:

- `writer`: key → the one admitted change writing it.
- `readers`: key → the units with a kept certificate reading it.

From them:

- `cone(c)` is the set of units with a kept certificate reading something in
  `W(c)`.
- `touching(u)` is the set of admitted changes writing something a kept
  certificate of `u` read.
- `needs(G)` is the cone of `G` minus the units that certificates prove on main
  plus `G`.
- `components()` is a union-find over changes linked by a shared unit.

## Assumptions

Each certificate is only as good as these.

1. **Determinism.** A unit that reads the same content takes the same path,
   reads nothing else and gives the same result. A run on another tree that
   agrees on every recorded input would pass too.
2. **Complete footprints.** A certificate lists every input the run read.
   Lit's tracer marks a run incomplete when it can't see all reads (for
   example `io_uring`); such a run must not become a certificate.
3. **Same environment.** The runner, toolchain and anything else outside the
   recorded inputs are the same. A change of environment invalidates every
   certificate taken in the old one; the in-memory model does not track
   this yet.
4. **New tests have no certificate.** A unit that has never passed proves
   nothing, so it is unproven wherever it is read.

Under these assumptions a certificate is observed evidence that the unit passes
on any tree agreeing with it. It is **not** a proof that the code is correct.
A flaky unit, or a read the tracer missed, can make it wrong.

## Safety

`Frontier.land_if(G, v)` is the only checked way to move main. It changes
nothing and returns a reason unless all of these hold:

- **S1, compare-and-swap.** Main is still at version `v`, the version the
  scheduler read when it decided. Every landing bumps `version`. A decision
  taken on stale evidence can lose a race but can never move main. In Git the
  same rule is the push's lease on the tested base (see "Still open").
- **S2, membership.** Every change in `G` is still admitted (not ejected, not
  landed).
- **S3, proof.** `needs(G)` is empty: every unit with a kept certificate reading
  something `G` writes has a certificate matching main plus `G` on everything
  it read.

**Invariant.** If every unit is proven on main before a checked landing, every
unit is proven on main after it.

*Why.* Take any unit `u`. If none of its kept certificates read a key in
`W(G)`, the certificate that proved it on main read only keys the landing
leaves unchanged, so it still matches. Otherwise `u` is in the cone of `G`,
and S3 found a certificate matching main plus `G`, which is the new main.
Trimming keeps the newest certificate proving main, so trimming cannot break
the invariant either.

Indexing every kept certificate, not just the newest run, matters here. A
unit's newest run may be on a change whose code reads other inputs, while an
older certificate is what proves it on main. PR #19 fixed exactly that gap.

Two more rules follow from the indexes:

- **Write-write conflicts are refused at admission.** Two admitted changes
  never write the same key. The later one gets a conflict packet an agent can
  act on.
- **Overlaps are the only new work.** A unit that read inputs of two or more
  changes in `G` saw a tree no single change's run saw, so only those units
  need a combined run. Every other unit is proven by its own change's run.

The tests in `tests/test_frontier.py` (`SafetyTests`) check these properties.
That includes a randomized test that races stale decisions against landings,
varies units' read sets, and checks the invariant against an oracle that reads
certificates directly rather than through the indexes.

## Liveness (conditional)

Progress is promised only where there is eligible work, resources and valid
evidence:

- **L1, independent work isn't blocked.** A change whose cone shares no unit
  with a blocked change can land on its own passes. A change whose tests never
  pass holds back only the changes it shares units with.
- **L2, no starvation.** Each scheduling policy in the simulator starts from
  the oldest waiting change.
- **L3, capacity.** If changes arrive faster than the merge runner can prove
  their overlaps, the backlog grows without bound. The model doesn't promise
  that main advances under overload. The simulator's `max_waiting` is one
  form of backpressure: an arrival that would exceed it is told to come back
  later, and its wait still counts from when it first came.

## Cost of each step

Let `|W|` be a change's write count, `R` the most keys one certificate lists,
`r(k)` the number of units reading key `k`, and `K = KEEP`.

| step | cost |
|---|---|
| `admit(c)` | `O(|W(c)|)` |
| `record(u)` | `O(R)`, or `O(K·R)` when it trims |
| `cone(c)` | `O(Σ r(k) for k in W(c))` |
| `touching(u)` | `O(K·R)` |
| `needs(G)` | `O(|cone(G)| · K · R)` |
| `components()` over `n` changes | `O(Σ |cone(c)| · K · R + n log n)` |
| `land(G)` | `O(Σ |W(c)|)` |

None of these grows with the number of changes in flight. Only the
change's own cone and the overlaps it really has count. Hot keys (high `r(k)`)
are the cost driver: a change to a function every unit reads has a cone of
every unit.

## What has been measured

All of this is synthetic. None of it is a claim about production throughput
or real Git landings.

- `python -m concurrent_merge.frontier_bench N`: admit, prove and land per
  change as up to `N` changes are in flight. It stays under about 100 µs per
  change to 100,000.
- `python -m concurrent_merge.frontier_bench grouping N`: `components()` cost
  per change as one component grows to most of what is in flight.
- `python -m concurrent_merge.frontier_sim PER_HOUR HOURS`: one stream through
  dependency, fixed and adaptive batching, with p50, p95, p99 and longest wait.
  At 200 changes an hour, dependency scheduling waited 211 s at p50 against
  685 s for fixed batches of 8.
- `frontier_sim.sweep` and `max_waiting`: past capacity, on a mid-size program
  with 4 cores, going from 120 to 3,200 changes an hour raised the peak
  backlog from 10 to 479 and p99 from 153 s to 3,071 s. Most of the extra load
  was refused as same-input conflicts (2 against 2,343). Capping in-flight
  changes at 80 cut refusals to 675 but raised p99 to 6,025 s. Backpressure
  trades collisions for waiting; it doesn't add capacity.
- Hot spots (`hot`, the share of writes drawn by popularity) widen cones and
  multiply overlap runs and conflicts.

## Still open

- **Frontier decisions on real candidates.** `concurrent-merge land` already
  lands by compare-and-swap: its push carries the tested base as the value
  main must still hold (`cli.publish`, tested against a local remote in
  `tests/test_landing.py`), so a batch tested on a main that moved never
  lands. What is open is feeding Frontier's certificates and components into
  `collect`, `run` and `land`, without weakening required checks or
  approvals.
- **Environment in certificates.** Bind certificates to the environment that
  ran them (assumption 3).
- **Overload, further.** Retries of refused changes, flaky units and several
  schedulers contending for the landing compare-and-swap are not modelled
  yet.
- **Release bridges.** Promote one immutable main snapshot through
  Lab → Early Release → Production, each stage with its own evidence. A
  failure downstream holds that promotion only, never main integration.
