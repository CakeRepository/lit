"""Run a CI job against a store of earlier footprints on the same trusted runner.

shadow (the default) always runs the job and records whether an earlier
passing footprint predicted the result. enforce skips the job when one does,
except for a random audited fraction that still runs.

The store must live where the job cannot write it, for example a directory
owned by another account on a self-hosted runner. A job that can edit the
store can forge its own reuse.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import re

from .engine import Repo, write_json
from . import footprint
from .footprint import load, predict, trace

MODES = ("shadow", "enforce")


def job_dir(store, job):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", job) or job in (".", ".."):
        raise ValueError("Job names use letters, digits, '.', '_' and '-'")
    return Path(store) / job


def footprints(directory, command, limit):
    found = []
    for path in sorted((directory / "footprints").glob("*.json"), reverse=True):
        try:
            manifest = load(path)
        except (ValueError, OSError, json.JSONDecodeError):
            continue
        if manifest["command"] == list(command) and manifest["exit_code"] == 0:
            found.append((path, manifest))
        if len(found) >= limit:
            break
    return found


def match(repo, workdir, commit, candidates, allow_network):
    checked = 0
    for path, manifest in candidates:
        checked += 1
        result = predict(
            repo,
            manifest,
            commit,
            env=dict(os.environ),
            host=True,
            allow_network=allow_network,
            workdir=workdir,
        )
        if result["verdict"] == "reusable":
            return path, manifest, checked
    return None, None, checked


def append(ledger, row):
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def prune(directory, keep):
    stored = sorted((directory / "footprints").glob("*.json"))
    for path in stored[: max(0, len(stored) - keep)]:
        path.unlink(missing_ok=True)


def cached_run(
    workdir,
    job,
    store,
    command,
    *,
    mode="shadow",
    ignore_env=(),
    allow_network=False,
    audit_rate=0.0,
    log=None,
    limit=50,
    keep=200,
    rng=random.random,
    clock=None,
):
    if mode not in MODES:
        raise ValueError("mode is shadow or enforce")
    workdir = Path(workdir).resolve()
    repo = Repo(workdir)
    commit = repo.out("rev-parse", "HEAD")
    directory = job_dir(store, job)
    started = clock() if clock is not None else datetime.now(timezone.utc)
    digests = Path(store) / "digests.json"
    try:
        footprint.DIGESTS.update(json.loads(digests.read_text(encoding="utf8")))
    except (OSError, ValueError):
        pass
    try:
        return run_once(
            repo, workdir, commit, directory, command, started, mode, ignore_env,
            allow_network, audit_rate, log, limit, keep, rng,
        )
    finally:
        cache = dict(list(footprint.DIGESTS.items())[-200000:])
        write_json(digests, cache)


def run_once(
    repo, workdir, commit, directory, command, started, mode, ignore_env,
    allow_network, audit_rate, log, limit, keep, rng,
):
    prior_path, prior, checked = match(
        repo, workdir, commit, footprints(directory, command, limit), allow_network
    )
    row = dict(
        job=directory.name,
        mode=mode,
        commit=commit,
        started=started.isoformat(),
        compared=checked,
        prior=prior_path.name if prior_path else None,
        prior_commit=prior["commit"] if prior else None,
    )
    audited = prior is not None and mode == "enforce" and rng() < audit_rate
    if prior is not None and mode == "enforce" and not audited:
        append(
            directory / "ledger.jsonl",
            dict(row, decision="reused", exit_code=0, seconds_saved=prior["seconds"]),
        )
        return dict(row, decision="reused", exit_code=0)
    manifest = trace(
        repo, commit, command, log=log, ignore_env=ignore_env, workdir=workdir
    )
    if manifest["exit_code"] == 0 and not manifest["incomplete"]:
        stamp = started.strftime("%Y%m%dT%H%M%S%fZ")
        write_json(directory / "footprints" / f"{stamp}-{commit[:12]}.json", manifest)
        prune(directory, keep)
    sound = prior is None or manifest["exit_code"] == prior["exit_code"]
    result = dict(
        row,
        decision="audited" if audited else "ran",
        exit_code=manifest["exit_code"],
        seconds=manifest["seconds"],
        predicted_reuse=prior is not None,
        sound=sound,
        incomplete=manifest["incomplete"],
        network=bool(manifest["network"]),
        reads_git_metadata=manifest["reads_git_metadata"],
    )
    append(directory / "ledger.jsonl", result)
    return result


def report(store, job=None):
    jobs = [job_dir(store, job)] if job else sorted(
        p for p in Path(store).iterdir() if (p / "ledger.jsonl").exists()
    )
    summary = {}
    for directory in jobs:
        rows = [
            json.loads(line)
            for line in (directory / "ledger.jsonl").read_text(encoding="utf8").splitlines()
            if line.strip()
        ]
        ran = [r for r in rows if r["decision"] in ("ran", "audited")]
        predicted = [r for r in ran if r.get("predicted_reuse")]
        summary[directory.name] = dict(
            runs=len(rows),
            executed=len(ran),
            reused=sum(r["decision"] == "reused" for r in rows),
            predicted_reusable=len(predicted),
            unsound=sum(not r["sound"] for r in predicted),
            seconds_executed=round(sum(r.get("seconds", 0) for r in ran), 1),
            seconds_predicted_reusable=round(
                sum(r["seconds"] for r in predicted if r["sound"]), 1
            ),
            seconds_saved=round(sum(r.get("seconds_saved", 0) for r in rows), 1),
            incomplete_runs=sum(bool(r.get("incomplete")) for r in ran),
        )
    return summary
