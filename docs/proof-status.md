# What lit has proven on real PRs

Checked Nov 26 2026 against flowdevs-io/FlowRMM, the first repository that lands through lit. "Proven" means
measured on real PRs and repeatable; "not yet" means measured and it doesn't hold; "unproven" means built and
tested only on synthetic data so far. Update this page when a number changes.

| Claim | Status | Evidence |
|---|---|---|
| lit combines concurrent PRs into the same tree they really produced | Proven | 100 merged FlowRMM PRs (#2773 to #2914), replayed in 133 windows of 2, 5, 10 and 25 consecutive PRs. Every window gave the tree main had after those PRs, none held a PR. 89 of the 100 PR heads lacked the PR merged just before them, so these were truly concurrent changes. |
| Assembly is fast | Proven for assembly | 25 real PRs assembled in about 7 s, including Python start-up. This is lit's own work only, not CI. |
| Independent PRs skip three-way merging | Proven | 21 of the 99 pairs went through the disjoint fast-import path, all with the right tree. |
| Declaration-level merges resolve overlaps Git can't | Unproven on real PRs | No window in this history needed one. `tests/test_end_to_end.py` checks them end to end on synthetic PRs against a local stand-in for GitHub; it found a gap with renamed functions, fixed in #38. |
| One queue run lands many PRs | Proven once | Batch #2912 landed #2908, #2909 and #2910 with one merge-group run instead of three. The other six batches (#2840, #2845, #2873, #2894, #2905, #2911) held one PR each. |
| PRs land faster | Not yet | Green to main for batched PRs: median 70.0 min (9 PRs). PRs that went through GitHub's queue alone the same day: median 46.9 min (15 PRs). Not a clean comparison (different hours and queue depth), but there is no speed-up to claim. The two-minute goal is far off. |
| A failing batch is split to find the PR that broke it | Unproven on real PRs | No FlowRMM batch has failed yet. `tests/test_end_to_end.py` covers a PR that fails alone and one that fails only when combined, on synthetic PRs. |
| `--max-batch auto` sizes batches from the red share | Unproven | No batch has been sized by it yet. |
| Test records skip work that can't have changed | Partly | Merge-group runs on batches #2912 and #2894: the portal suite ran 34 of 1,182 files and reused 1,148 (97%), 84 s. The FlowPSA SQL suite ran 256 of 438 and 239 of 437 files (about 43% reused). Audits ran in each. |
| Signed test results from other machines count as proof | Unproven | Built (proof pack/sign/take, audit book) but not used in FlowRMM: no signing machines are set up. |
| Land re-runs only the tests where changes overlap | Unproven | Still in shadow mode. No FlowRMM merge used its decisions. |
| lit's actions leave a self-hosted runner cleanable | Proven in CI | The `action-hygiene` job runs both actions as root in FlowRMM's batcher image over a checkout owned by another user. See [the Nov 25 incident](incidents/2026-11-25-runner-breakage.md). |

## How to check again

The tree replay needs only a bare copy of the repository, shallow is fine, and pushes nothing:

```sh
git clone --bare --depth 400 https://github.com/OWNER/REPO.git replay.git
python -m benchmarks.replay_history replay.git --merges 100 --sizes 2,5,10,25
```

It exits 1 if any window's tree differs from the branch. Timings and reuse counts come from the repository's
own PR records and merge-group job logs.
