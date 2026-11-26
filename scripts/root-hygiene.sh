#!/usr/bin/env bash
# Usage: scripts/root-hygiene.sh PATH/TO/lit
#
# A GitHub container job runs as root over folders that the runner's user owns
# on the host. Anything root leaves there cannot be removed by the runner, and
# the next job on that runner fails before it starts (Nov 25, 2026: Python's
# __pycache__ in the lit action's folder stopped every FlowRMM runner).
#
# This runs lit as root over a folder the current user owns, the same way, and
# fails if lit leaves anything there that only root can remove. It first checks
# that it would have caught the Nov 25 failure.
#
# LIT_HYGIENE_MODE=docker (default) runs root in a container. LIT_HYGIENE_MODE=direct
# is for a machine that is already root and has no Docker: the scratch folder is
# handed to uid LIT_HYGIENE_UID (default 1000) and lit runs as root beside it.
set -euo pipefail

lit=$(realpath "$1")
mode=${LIT_HYGIENE_MODE:-docker}
image=${LIT_HYGIENE_IMAGE:-python:3.12-bookworm}
checkout=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)

as_root() {
	if [ "$mode" = direct ]; then
		[ "$(id -u)" = 0 ] || { echo "direct mode needs root" >&2; exit 2; }
		(cd "$work" && PATH="$(dirname "$lit"):$PATH" GIT_CONFIG_COUNT=1 \
			GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' sh -ec "$1")
	else
		docker run --rm --user 0 -v "$work:/ws" -v "$lit:/usr/local/bin/lit:ro" -w /ws "$image" \
			sh -ec 'git config --global --add safe.directory "*"; '"$1"
	fi
}

cleanup() {
	if [ "$mode" = docker ]; then
		as_root "chown -R $(id -u):$(id -g) /ws" >/dev/null 2>&1 || true
	fi
	rm -rf "$work"
}
trap cleanup EXIT

root_owned() { find "$work" -uid 0 -print; }

git_() {
	GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=t \
		GIT_COMMITTER_EMAIL=t@example.com git "$@"
}

# The scratch batch, made by the folder's owner.
mkdir "$work/src"
git_ -C "$work/src" init -q -b main
echo a >"$work/src/a.txt"
git_ -C "$work/src" add . && git_ -C "$work/src" commit -qm base
base=$(git -C "$work/src" rev-parse HEAD)
git_ -C "$work/src" checkout -qb pr
echo b >"$work/src/b.txt"
git_ -C "$work/src" add . && git_ -C "$work/src" commit -qm "add b"
head=$(git -C "$work/src" rev-parse HEAD)
git_ clone -q --bare "$work/src" "$work/batch.git"
printf '{"repository":"example/repo","branch":"main","base":"%s","changes":[{"number":1,"author":"octocat","head":"%s"}]}\n' \
	"$base" "$head" >"$work/queue.json"
# An action checkout, as the runner lays one out.
mkdir -p "$work/action"
cp -R "$checkout/concurrent_merge" "$work/action/"
find "$work/action" -name __pycache__ -prune -exec rm -rf {} +

if [ "$mode" = direct ]; then
	chown -R "${LIT_HYGIENE_UID:-1000}:${LIT_HYGIENE_UID:-1000}" "$work"
fi
[ -z "$(root_owned)" ] || { echo "setup left root-owned files" >&2; exit 2; }

# 1. The check catches the Nov 25 failure: Python importing lit from the action
#    folder without -B writes root-owned bytecode there.
as_root 'cd action && python3 -c "import concurrent_merge.cli"'
if [ -z "$(find "$work/action" -name __pycache__ -uid 0 -print)" ]; then
	echo "self-check failed: running Python from the action folder left nothing for this test to see" >&2
	exit 2
fi
as_root 'find action -name __pycache__ -prune -exec rm -rf {} +'
[ -z "$(root_owned)" ] || { echo "could not reset after the self-check" >&2; root_owned >&2; exit 2; }
echo "self-check: the Nov 25 failure is caught"

# 2. lit as root: a full batch run that writes Git objects, refs and receipts
#    into folders the runner's user owns.
as_root 'export LIT_PYTHON=/lit-no-python
lit version
lit run queue.json --repo batch.git --state state --test test -f b.txt
lit run queue.json --repo batch.git --state state --test test -f b.txt'

left=$(root_owned)
if [ -n "$left" ]; then
	echo "lit left files only root can remove:" >&2
	echo "$left" | sed "s|$work/||" | head -50 >&2
	exit 1
fi
[ -s "$work/state/runs/$(ls "$work/state/runs" | head -1)/result.json" ] || { echo "no receipt" >&2; exit 1; }
echo "ok: lit ran as root and left nothing that only root can remove"
