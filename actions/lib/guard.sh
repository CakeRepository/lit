# lit's runner guard. Every lit action step sources this file before it runs lit.
#
# Why: a self-hosted runner deletes and re-downloads its _work/_actions folder before each job. In a container
# job lit runs as root, while that folder belongs to the runner's own user. Any file lit leaves there is
# root-owned, the runner can't delete it, and every later job on that runner fails at "Set up job" (FlowRMM,
# Nov 25 2026: root-owned __pycache__ broke all three orchestrator runners).
#
# What it does:
#   - tells Python to write no bytecode, in lit and in every Python process lit starts;
#   - lists lit's own code folders (concurrent_merge/ and actions/) before the step, and on exit removes
#     anything new in them, gives anything not owned by the runner's user back to that user, warns, and fails
#     the step if something still can't be removed, naming the command that fixes the runner;
#   - hands files the step created under the runner's temp and home folders back to the folder's owner, so the
#     runner can clean them too.
# A job that runs as the runner's own user (no container) has nothing to hand back and nothing changes.

export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="${RUNNER_TEMP:-/tmp}/lit-pycache"

lit_guard_root="$(cd "$GITHUB_ACTION_PATH/../.." && pwd -P)"
lit_guard_owner="$(stat -c %u "$lit_guard_root")"
lit_guard_group="$(stat -c %g "$lit_guard_root")"
lit_guard_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/lit-guard.XXXXXX")"
lit_guard_list() { (cd "$lit_guard_root" && find concurrent_merge actions -print | LC_ALL=C sort); }
lit_guard_list > "$lit_guard_dir/before"
touch "$lit_guard_dir/marker"

lit_guard_finish() {
  local status=$? new left d
  set +e +o pipefail
  lit_guard_list > "$lit_guard_dir/after"
  # Only the topmost new path of each new tree: removing it removes what is under it.
  new="$(LC_ALL=C comm -13 "$lit_guard_dir/before" "$lit_guard_dir/after" | awk 'p=="" || index($0, p "/") != 1 { print; p=$0 }')"
  if [ -n "$new" ]; then
    echo "::warning title=lit wrote into its action folder::lit removed what it left in $lit_guard_root, so the runner can still clean it: $(echo "$new" | head -5 | tr '\n' ' ')"
    (cd "$lit_guard_root" && echo "$new" | while IFS= read -r p; do rm -rf -- "$p"; done)
  fi
  if [ "$(id -u)" = "0" ]; then
    (cd "$lit_guard_root" && find concurrent_merge actions ! -uid "$lit_guard_owner" -exec chown -h "$lit_guard_owner:$lit_guard_group" {} +)
  fi
  left="$(cd "$lit_guard_root" && find concurrent_merge actions ! -uid "$lit_guard_owner" -print | head -5)"
  if [ -n "$left" ]; then
    echo "::error title=lit left files the runner can't remove::These are not owned by the runner's user, so this runner's next job will fail at Set up job: $(echo "$left" | tr '\n' ' '). As root on the runner, run: rm -rf $lit_guard_root"
    status=1
  fi
  # Hand back what this step created under the runner's own temp and home folders.
  if [ "$(id -u)" = "0" ]; then
    for d in "${RUNNER_TEMP:-}" "${HOME:-}"; do
      [ -n "$d" ] && [ -d "$d" ] || continue
      [ "$(stat -c %u "$d")" != "0" ] || continue
      find "$d" -newer "$lit_guard_dir/marker" -uid 0 -exec chown -h "$(stat -c %u:%g "$d")" {} + 2>/dev/null
    done
  fi
  rm -rf "$lit_guard_dir"
  exit "$status"
}
trap lit_guard_finish EXIT
