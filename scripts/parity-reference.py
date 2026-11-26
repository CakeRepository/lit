"""Run the checked-out Python reference with the same deterministic inputs as lit parity."""
import sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
epoch = sys.argv.pop(1)
if epoch and sys.argv[1:2] == ["cached-run"]:
    from concurrent_merge import cache
    cache.cached_run = partial(cache.cached_run,
                               clock=lambda: datetime.fromtimestamp(int(epoch), timezone.utc),
                               rng=lambda: 0.5)
if epoch and sys.argv[1:2] == ["intake"]:
    from concurrent_merge import intake, intake_audit

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(int(epoch), tz)

    intake.datetime = intake_audit.datetime = Clock
    intake.time = SimpleNamespace(sleep=lambda _: None)

from concurrent_merge.cli import main
sys.exit(main())
