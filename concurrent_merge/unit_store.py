"""Run a test job's files against a store of each file's newest passing footprint.

shadow (the default) runs every file and records, for each one the store said
could be skipped, whether it really passed. enforce runs only the files whose
inputs changed, that have no passing footprint, or that an audit picks, and
reports the rest as reused. plan runs nothing and lists the files enforce would
skip, so a job's own run can score them (see bun_log.score).

Every file that runs is traced, and a passing, complete trace replaces that
file's footprint, so the store follows main one commit at a time. As with
cached-run, the store must live where the job cannot write it.
"""

from datetime import datetime, timezone
from fnmatch import fnmatchcase
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import re
import subprocess
import tempfile
import time

from .engine import Repo, write_json
from . import footprint
from . import symbols as symbol_tables
from .footprint import (
    Listings,
    compare,
    environment_changes,
    trace_units,
    unit_manifest,
    workspace,
)

MODES = ("shadow", "enforce", "plan")


def job_dir(store, job):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", job) or job in (".", ".."):
        raise ValueError("Job names use letters, digits, '.', '_' and '-'")
    return Path(store) / job


def unit_path(directory, name):
    return directory / "units" / (sha256(name.encode("utf8")).hexdigest()[:32] + ".json")


def slice_unit(bundle, name):
    """A one-unit bundle holding only what that unit's comparison needs."""
    unit = bundle["units"][name]
    return dict(
        {k: bundle[k] for k in bundle if k not in ("units", "tracked", "external",
                                                     "listing_children", "each")},
        each={name: bundle["each"][name]} if bundle.get("each") else None,
        tracked={p: bundle["tracked"][p] for p in unit["tracked"]},
        external={p: bundle["external"][p] for p in unit["external"]},
        listing_children={
            d: bundle["listing_children"][d] for d in unit["listings"]
            if d in bundle.get("listing_children", {})
        },
        units={name: unit},
    )


def stored(directory, name):
    try:
        bundle = json.loads(unit_path(directory, name).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None
    if bundle.get("kind") != "lit-unit-footprints" or name not in bundle.get("units", {}):
        return None
    return bundle


def symbols_path(directory, oid):
    return directory / "symbols" / (oid + ".json")


def stored_symbols(directory, oid):
    try:
        return json.loads(symbols_path(directory, oid).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None


def discover_units(entries, patterns, strip):
    return sorted(
        path[len(strip):]
        for path, entry in entries.items()
        if entry[1] == "blob"
        and entry[0] != "untracked"
        and path.startswith(strip)
        and any(fnmatchcase(path, pattern) for pattern in patterns)
    )


def plan(directory, names, commit, entries, workdir, *, allow_network, listings, env, scratch=(),
         host=True, ignore_env=()):
    listed = Listings(entries)
    decisions, kept, loaded = {}, {}, {}
    for name in names:
        bundle = stored(directory, name)
        if bundle is None:
            decisions[name] = dict(reuse=False, reasons=[dict(kind="new")])
            continue
        reasons, _ = compare(
            unit_manifest(bundle, name), commit, entries, host=host,
            allow_network=allow_network, workdir=workdir, listed=listed,
            listings=listings,
        )
        reasons += environment_changes(bundle, env, ignore_env)
        if sorted(bundle.get("scratch") or ()) != sorted(scratch):
            reasons.append(dict(kind="scratch", before=bundle.get("scratch") or [], now=sorted(scratch)))
        kept[name] = {
            path: (bundle["tracked"][path][1], ran)
            for path, ran in (bundle["units"][name].get("symbols") or {}).items()
            if path in bundle["tracked"]
        }
        loaded[name] = set(bundle["tracked"]) | set(bundle.get("untracked") or ())
        decisions[name] = dict(
            reasons=reasons,
            prior_commit=bundle["commit"],
            prior_seconds=bundle["units"][name]["seconds"] or 0,
        )
    # Per-function records: a changed file a unit read can still leave it reusable
    # when none of the functions it ran changed (see symbols.py). Parse each file once.
    wanted = sorted({
        r["path"] for n, d in decisions.items() if n in kept for r in d["reasons"]
        if r["kind"] == "changed" and r.get("path") in kept[n]
    })
    current = symbol_tables.declarations(workdir, wanted) if wanted else {}
    parsed = dict(current)
    exists = lambda path: (entries.get(path) or (None, None))[1] == "blob"
    versions = {}
    for name, d in decisions.items():
        if "reuse" in d:
            continue
        reasons = []
        for r in d["reasons"]:
            record = kept[name].get(r.get("path")) if r["kind"] == "changed" else None
            if record and record[0] not in versions:
                versions[record[0]] = stored_symbols(directory, record[0])
            modules = symbol_tables.Modules(workdir, exists, loaded[name], parsed).ask(r["path"]) if record else None
            if record and symbol_tables.unaffected(versions[record[0]], record[1], current.get(r["path"]), modules):
                d.setdefault("unchanged_functions", []).append(r["path"])
                continue
            reasons.append(r)
        d["reuse"] = not reasons
        d["reasons"] = reasons[:10]
    return decisions


def shard_of(names, weights, shard, shards, pinned=()):
    """Deal names heaviest first to the least-loaded shard; keep those of one shard.

    Every shard computes the same assignment from the same weights, so shards
    need no coordination. Ties go by name, then to the lowest shard number.
    Pinned names (tests that must not run beside each other) all go to shard 0.
    """
    pinned = set(pinned)
    loads = [0.0] * shards
    mine = [n for n in names if n in pinned] if shard == 0 else []
    loads[0] = sum(weights.get(n, 0.1) for n in names if n in pinned)
    for name in sorted((n for n in names if n not in pinned), key=lambda n: (-weights.get(n, 0.1), n)):
        target = loads.index(min(loads))
        loads[target] += weights.get(name, 0.1)
        if target == shard:
            mine.append(name)
    return sorted(mine)


def starved(decisions, floor=10):
    """True when most files have records yet none can be reused: a sign that
    something every record shares, such as an environment variable, changed,
    and the run quietly fell back to running everything."""
    if len(decisions) < floor or any(d["reuse"] for d in decisions.values()):
        return False
    recorded = sum(
        1 for d in decisions.values() if not any(r["kind"] == "new" for r in d["reasons"])
    )
    return recorded * 2 >= len(decisions)


def cached_units(
    workdir,
    job,
    store,
    template,
    *,
    names=None,
    discover=None,
    strip="",
    cwd="",
    mode="shadow",
    allow_network=False,
    listings="names",
    audit_rate=0.0,
    ignore_env=(),
    log=None,
    rng=None,
    shard=0,
    shards=1,
    pinned=(),
    symbols=(),
    scratch=(),
    batch=None,
    batch_report=None,
    proofs=(),
    audit_book=None,
    host=True,
):
    """proofs: further stores, such as the one a pull request's own CI left at
    its head. A file whose inputs here match its record in any of them, not only
    in store, counts as passing, so a merge of two sides that each passed runs
    only the files that read changes from both. A store a pull request wrote is
    only as trustworthy as that pull request's own CI verdict.

    audit_book: a file of each proof signer's audit record (proof.Book). A file
    reused from a proof whose store names its signers (proof take writes that)
    is rerun at the highest of those signers' shares, not audit_rate, while mode
    is enforce. Each rerun's pass or fail goes back into the book for every
    signer who vouched for it, so a clean signer is checked less over time and
    one wrong result drops the rest of that signer's earlier proofs.

    host: compare files outside the checkout too. Off when the check runs on a
    machine unlike the one that recorded, as a merge queue's proof does.

    batch: a command that runs many files in one process, where the part
    holding {} repeats once per file. In enforce mode the files that must run go
    through it once, untraced, so they share one process as a plain test run
    does. Only if it fails do they run one by one, traced, so each failure has
    its own record of what it read (for blame). Files run in a batch write no
    record: records a merge group writes are not saved anyway, and main's
    refresh traces every file.

    batch_report: how to read a failed batch's own output. "bun": bun names each
    file before its failures, so only the files it names run again, traced, and
    the rest count as passed. If the output does not end in bun's summary, or
    names no file or a file outside the batch, every file runs traced as before.

    scratch: untracked directories of runtime data, such as a server's data
    folder, whose contents are never inputs (see footprint.trace_units).

    symbols: path patterns whose JS/TS files get per-function records. The
    template's "{lcov}" part becomes bun's lcov coverage arguments when symbols
    is set, and is dropped otherwise."""
    if mode not in MODES:
        raise ValueError("mode is shadow, enforce or plan")
    if not any("{}" in part for part in template):
        raise ValueError("The unit command needs {} where the unit name goes")
    workdir = Path(workdir).resolve()
    repo = Repo(workdir)
    commit = repo.out("rev-parse", "HEAD")
    # Seeded by the commit, so every shard of one run audits the same files.
    rng = rng or random.Random(commit).random
    keep_empty = [d.strip("/") for d in scratch]
    directory = job_dir(store, job)
    started = datetime.now(timezone.utc)
    digests = Path(store) / "digests.json"
    try:
        footprint.DIGESTS.update(json.loads(digests.read_text(encoding="utf8")))
    except (OSError, ValueError):
        pass
    entries = workspace(repo, commit, workdir)
    if names is None:
        names = discover_units(entries, discover or [], strip)
    env = dict(os.environ)
    decisions = plan(
        directory, names, commit, entries, workdir,
        allow_network=allow_network, listings=listings, env=env,
        scratch=keep_empty, host=host, ignore_env=ignore_env,
    )
    for other in proofs:
        waiting = [n for n in names if not decisions[n]["reuse"]]
        if not waiting:
            break
        found = plan(
            job_dir(other, job), waiting, commit, entries, workdir,
            allow_network=allow_network, listings=listings, env=env,
            scratch=keep_empty, host=host, ignore_env=ignore_env,
        )
        for n in waiting:
            if found[n]["reuse"]:
                decisions[n] = dict(found[n], proof=str(other))
    book, vouched = None, {}
    if audit_book is not None:
        from .proof import Book, vouchers
        book = Book.load(audit_book)
        for n, d in decisions.items():
            if d["reuse"] and d.get("proof"):
                vouched[n] = vouchers(d["proof"], job, n)

    def rate(n):
        if vouched.get(n):
            return max([audit_rate] + [book.share(s) for s in vouched[n]])
        return audit_rate

    audited = {n for n, d in decisions.items() if d["reuse"] and rng() < rate(n)}
    if mode == "shadow":
        to_run = list(names)
    elif mode == "plan":
        to_run, audited = [], set()
    else:
        to_run = [n for n in names if not decisions[n]["reuse"] or n in audited]
    if shards > 1:
        weights = {n: d.get("prior_seconds") or 0.1 for n, d in decisions.items()}
        to_run = shard_of(to_run, weights, shard, shards, pinned)
    run_here = set(to_run)
    batched = None
    named = None
    if batch and mode == "enforce" and to_run:
        batched = run_batch(batch, sorted(to_run), workdir / cwd, env, log)
        if batched["exit_code"] == 0:
            to_run = []
        else:
            named = batch_failures(batch_report, batched.pop("output"), to_run)
            if named:
                to_run = sorted(named)
        batched.pop("output", None)
    with tempfile.TemporaryDirectory(prefix="lit-coverage-") as scratch:
        coverage = {}
        each = {}
        for i, n in enumerate(to_run):
            argv = []
            for part in template:
                if part == "{lcov}":
                    if symbols:
                        report_dir = Path(scratch) / str(i)
                        coverage[n] = str(report_dir / "lcov.info")
                        argv += ["--coverage", "--coverage-reporter=lcov",
                                 f"--coverage-dir={report_dir}"]
                    continue
                argv.append(part.replace("{}", n))
            each[n] = argv
        bundle = (
            trace_units(repo, commit, each=each, cwd=cwd, log=log,
                        ignore_env=ignore_env, workdir=workdir, scratch=keep_empty)
            if each else None
        )
        ran = bundle["units"] if bundle else {}
        if symbols and ran:
            records, tables = symbol_tables.unit_records(
                workdir, cwd, ran, coverage, symbols, bundle["tracked"]
            )
            for name, kept in records.items():
                ran[name]["symbols"] = kept
            for oid, table in tables.items():
                if not symbols_path(directory, oid).exists():
                    write_json(symbols_path(directory, oid), table)
    for name, unit in ran.items():
        if unit["exit_code"] == 0 and not unit["incomplete"]:
            write_json(unit_path(directory, name), slice_unit(bundle, name))
    unsound = sorted(
        n for n in ran if decisions[n]["reuse"] and ran[n]["exit_code"] != 0
    )
    failed = sorted(n for n, u in ran.items() if u["exit_code"] != 0)
    checked = {}
    if book is not None and mode == "enforce":
        for n in sorted(audited & set(vouched) & run_here):
            if n in ran:
                ok = ran[n]["exit_code"] == 0
            else:
                ok = batched is not None and (batched["exit_code"] == 0 or bool(named))
            for signer in vouched[n]:
                book.record(signer, ok)
                checked.setdefault(signer, dict(ok=0, wrong=0))["ok" if ok else "wrong"] += 1
        if checked:
            book.save(audit_book)
    predicted = [n for n in names if decisions[n]["reuse"]]
    row = dict(
        job=directory.name,
        mode=mode,
        shard=f"{shard + 1}/{shards}",
        commit=commit,
        started=started.isoformat(),
        units=len(names),
        predicted_reusable=len(predicted),
        ran=len(batched["units"]) if batched else len(ran),
        batch=None if batched is None else dict(
            files=len(batched["units"]), seconds=batched["seconds"],
            exit_code=batched["exit_code"], traced_after=bool(to_run), traced=len(to_run),
        ),
        reused=0 if mode != "enforce" else len(predicted) - len(audited & set(predicted)),
        audited=len(audited),
        failed=failed[:200],
        unsound=unsound,
        seconds_ran=round(
            sum(u["seconds"] or 0 for u in ran.values())
            + (batched["seconds"] if batched else 0), 1,
        ),
        seconds_predicted_reusable=round(
            sum(ran[n]["seconds"] or 0 for n in predicted if n in ran)
            or sum(decisions[n]["prior_seconds"] for n in predicted),
            1,
        ),
        first_reasons=_tally(decisions),
        reusable_by_function=sum(
            1 for d in decisions.values() if d["reuse"] and d.get("unchanged_functions")
        ),
        reusable_by_proof=sum(1 for d in decisions.values() if d["reuse"] and d.get("proof")),
        signer_audits=checked,
        starved=starved(decisions),
        exit_code=1 if failed else 0,
    )
    # What each failing file read, so blame.py can name the change it saw.
    failed_reads = {
        n: dict(
            tracked=ran[n]["tracked"],
            listings=sorted(ran[n].get("listings") or ()),
            absent=ran[n].get("absent") or [],
            incomplete=ran[n].get("incomplete") or [],
        )
        for n in failed
    }
    ledger = directory / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    write_json(digests, dict(list(footprint.DIGESTS.items())[-200000:]))
    return dict(
        row,
        reusable=predicted,
        failed_reads=failed_reads,
        decisions={n: d for n, d in decisions.items() if not d["reuse"]},
    )


def run_batch(template, units, cwd, env, log):
    argv = []
    for part in template:
        argv += [part.replace("{}", n) for n in units] if "{}" in part else [part]
    clock = time.monotonic()
    with tempfile.TemporaryFile() as out:
        code = subprocess.run(argv, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT).returncode
        out.seek(0)
        output = out.read()
    if log:
        with open(log, "ab") as f:
            f.write(output)
    return dict(units=units, exit_code=code, seconds=round(time.monotonic() - clock, 3),
                output=output.decode("utf8", errors="replace"))


BUN_SUMMARY = re.compile(r"(?m)^Ran \d+ tests? across \d+ files?\.")


def batch_failures(report, output, units):
    """The files a failed batch's output names as failed, or None when it cannot be
    trusted to name them all (see cached_units' batch_report)."""
    if report != "bun" or not BUN_SUMMARY.search(output):
        return None
    from .bun_log import failed_files
    named = failed_files(output)
    return named if named and named <= set(units) else None


def _tally(decisions):
    counts = {}
    for d in decisions.values():
        kind = "reusable" if d["reuse"] else d["reasons"][0]["kind"]
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))
