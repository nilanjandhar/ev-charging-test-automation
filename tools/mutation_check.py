#!/usr/bin/env python3
"""Does this suite actually catch a bug, or does it just describe the code?

A green suite proves nothing on its own. This script answers the only question
that matters about a test suite: if someone broke the service, would anything go
red — and would the red thing be the one you would want to read?

It copies `service/` to a scratch directory, applies one small mutation at a
time, points pytest at the mutated copy via `pythonpath`, and records which tests
died. `service/` itself is never touched.

Usage:
    python tools/mutation_check.py            # run every mutant
    python tools/mutation_check.py --list     # show them without running
    python tools/mutation_check.py -k offline # run mutants matching a substring

A mutant that no test kills is a gap in the suite, and the script exits non-zero
so that is not something you can skim past.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO_ROOT / "service"

#: Layers to run. Deliberately the merge gate and nothing else: a mutant that is
#: only caught by a nightly job is a mutant that reaches main.
GATE_MARKERS = "not e2e and not perf and not ui and not slow"


@dataclass(frozen=True)
class Mutant:
    name: str
    path: str
    before: str
    after: str
    rationale: str

    def apply(self, root: Path) -> None:
        target = root / self.path
        source = target.read_text()
        if self.before not in source:
            raise SystemExit(
                f"mutant {self.name!r} no longer applies: {self.before!r} not found in {self.path}."
                " The service changed — update tools/mutation_check.py."
            )
        target.write_text(source.replace(self.before, self.after, 1))


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "threshold-raised",
        "app/scoring.py",
        "FLAGGING_THRESHOLD = 60.0",
        "FLAGGING_THRESHOLD = 65.0",
        "the 'make the score more sensitive' ticket — re-flags the whole fleet",
    ),
    Mutant(
        "flag-boundary-inclusive",
        "app/scoring.py",
        "return score < FLAGGING_THRESHOLD",
        "return score <= FLAGGING_THRESHOLD",
        "one keystroke. Decides whether a dead station at exactly 60.0 is flagged (R2)",
    ),
    Mutant(
        "offline-penalty-weakened",
        "app/scoring.py",
        "OFFLINE_PENALTY = 40.0",
        "OFFLINE_PENALTY = 35.0",
        "offline stations quietly score higher and drop off the worklist",
    ),
    Mutant(
        "error-penalty-reduced",
        "app/scoring.py",
        "ERROR_PENALTY_PER = 5.0",
        "ERROR_PENALTY_PER = 4.0",
        "erroring stations look healthier than they are",
    ),
    Mutant(
        "error-cap-raised",
        "app/scoring.py",
        "ERROR_PENALTY_CAP = 30.0",
        "ERROR_PENALTY_CAP = 40.0",
        "changes where the R4 saturation blind spot begins",
    ),
    Mutant(
        "latency-divisor-changed",
        "app/scoring.py",
        "LATENCY_DIVISOR = 20.0",
        "LATENCY_DIVISOR = 25.0",
        "latency contributes less; slow stations score higher",
    ),
    Mutant(
        "latency-cap-raised",
        "app/scoring.py",
        "LATENCY_PENALTY_CAP = 20.0",
        "LATENCY_PENALTY_CAP = 25.0",
        "moves the point where latency stops mattering",
    ),
    Mutant(
        "rounding-precision",
        "app/scoring.py",
        "return round(max(score, 0.0), 2)",
        "return round(max(score, 0.0), 1)",
        "the R15 rounding surface — would silently change flags at the boundary",
    ),
    Mutant(
        "recency-inverted",
        "app/routers/stations.py",
        ".order_by(StationReport.timestamp.desc())",
        ".order_by(StationReport.timestamp.asc())",
        "station status starts reporting the *oldest* report. The worst realistic bug",
    ),
    Mutant(
        "metrics-connectivity-flipped",
        "app/routers/metrics.py",
        'if r.connectivity_status == "online"',
        'if r.connectivity_status != "online"',
        "online/offline counts swap on the dashboard",
    ),
    Mutant(
        "ingest-args-swapped",
        "app/routers/reports.py",
        "latency_ms=payload.latency_ms,\n        error_count=payload.error_count,",
        "latency_ms=payload.error_count,\n        error_count=int(payload.latency_ms),",
        "a copy-paste slip at the call site — the scoring function itself is untouched",
    ),
    Mutant(
        "flag-stored-inverted",
        "app/routers/reports.py",
        "flagged = is_flagged(score)",
        "flagged = not is_flagged(score)",
        "score is right, the stored flag is wrong — only cross-field checks catch this",
    ),
    Mutant(
        "offline-branch-dropped",
        "app/scoring.py",
        'if connectivity_status == "offline":\n        score -= OFFLINE_PENALTY',
        'if connectivity_status == "offline_DISABLED":\n        score -= OFFLINE_PENALTY',
        "connectivity stops affecting the score at all",
    ),
    Mutant(
        "poor-hygiene-filter-inverted",
        "app/routers/stations.py",
        ".filter(StationReport.flagged == True)",
        ".filter(StationReport.flagged == False)",
        "the worklist shows healthy stations and hides the broken ones",
    ),
    Mutant(
        "metrics-errors-over-all-history",
        "app/routers/metrics.py",
        "total_errors = sum(r.error_count for r in latest_reports)",
        "total_errors = sum(r.error_count for r in db.query(StationReport).all())",
        "superseded reports leak into the network error total",
    ),
    Mutant(
        "metrics-counts-all-history",
        "app/routers/metrics.py",
        "total = len(latest_reports)",
        "total = len(latest_reports) + 0 * db.query(StationReport).count()",
        "a no-op control: this mutation changes nothing, so nothing should die",
    ),
)


def run_gate(mutant_root: Path) -> tuple[bool, list[str]]:
    """Run the gate layers against a mutated copy. Returns (killed, failing tests)."""
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            GATE_MARKERS,
            "-o",
            f"pythonpath=. {mutant_root}",
            "-q",
            "--no-header",
            "-p",
            "no:randomly",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    failing = sorted(
        {
            match.group(1)
            for match in re.finditer(r"^(?:FAILED|ERROR) (\S+)", process.stdout, re.MULTILINE)
        }
    )
    # An XPASS under `strict=True` is reported as a failure too, which is the
    # point of the strict markers: fixing a known bug must also break the build.
    return process.returncode != 0, failing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list mutants and exit")
    parser.add_argument("-k", dest="pattern", default="", help="only mutants matching this")
    args = parser.parse_args()

    selected = [m for m in MUTANTS if args.pattern in m.name]
    if args.list:
        for mutant in selected:
            print(f"{mutant.name:32} {mutant.path:28} {mutant.rationale}")
        return 0

    control = [m for m in selected if m.name == "metrics-counts-all-history"]
    survivors: list[Mutant] = []

    print(f"running {len(selected)} mutants against: pytest -m '{GATE_MARKERS}'\n")
    for mutant in selected:
        with tempfile.TemporaryDirectory(prefix="mutant-") as scratch:
            mutant_root = Path(scratch) / "service"
            shutil.copytree(SERVICE_DIR, mutant_root)
            mutant.apply(mutant_root)
            killed, failing = run_gate(mutant_root)

        is_control = mutant in control
        expected_kill = not is_control
        ok = killed == expected_kill
        verdict = "KILLED " if killed else "SURVIVED"
        marker = "ok " if ok else "!! "
        print(f"{marker}{verdict}  {mutant.name}")
        print(f"          {mutant.rationale}")
        if failing:
            for test in failing[:4]:
                print(f"          - {test}")
            if len(failing) > 4:
                print(f"          - ... and {len(failing) - 4} more")
        print()
        if not ok:
            survivors.append(mutant)

    if survivors:
        print("GAPS — these mutants behaved unexpectedly:")
        for mutant in survivors:
            print(f"  - {mutant.name}: {mutant.rationale}")
        return 1

    print(f"every mutant behaved as expected ({len(selected)} checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
