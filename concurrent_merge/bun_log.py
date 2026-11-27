"""Score a plan of skippable test files against a real `bun test` run.

Outside a terminal, bun prints a file's path, then a colon, only before that
file's failures, so the files named that way are the ones that failed. Under
GitHub Actions it prints every file's path that way, as a log group
("::group::path:" ... "::endgroup::"), so a file counts as failed only when a
failure line follows its path. A plan from `cached-units --mode plan` lists
the files lit would have skipped; any of them that failed in the real run is an
unsound skip.
"""

import json
from pathlib import Path
import re

HEADER = re.compile(r"^(?:::group::)?(\S[^\s:]*\.[cm]?[jt]sx?):$")
FAILED = ("(fail)", "# Unhandled error", "error: ")


def failed_files(text):
    current, failed = None, set()
    for line in text.splitlines():
        match = HEADER.match(line)
        if match:
            current = match.group(1).removeprefix("./")
        elif line == "::endgroup::":
            current = None
        elif current and line.startswith(FAILED):
            failed.add(current)
    return failed


def score(plan, logs):
    failed = set()
    for log in logs:
        failed |= failed_files(Path(log).read_text(encoding="utf8", errors="replace"))
    reusable = set(plan["reusable"])
    return dict(
        commit=plan["commit"],
        units=plan["units"],
        predicted_reusable=len(reusable),
        seconds_predicted_reusable=plan["seconds_predicted_reusable"],
        failed_in_run=len(failed),
        unsound=sorted(reusable & failed),
        first_reasons=plan["first_reasons"],
    )


def main_score(args):
    result = score(json.loads(Path(args.plan).read_text(encoding="utf8")), args.log)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")
    print(json.dumps(result, indent=2))
    return 0
