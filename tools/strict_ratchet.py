#!/usr/bin/env python3
"""Ratchet for `mypy --strict src`: the count may fall, never rise.

Why a ratchet and not a gate: the repo's configured mypy settings (see
[tool.mypy] in pyproject.toml) are CLEAN and enforced as a hard CI gate. Full
`--strict` is a different, stricter bar that this repo does not meet yet --
26 findings, almost entirely `Missing type arguments for generic type` on bare
`dict`/`tuple` annotations plus a handful of unannotated inner helpers. Fixing
those touches real signatures in the point-in-time replay and the XBRL
reconstruction, which deserves its own reviewed change, not a drive-by.

So instead of leaving strict mode as advisory noise nobody reads, the count is
pinned here. New code cannot add strict findings, and the number can only be
edited downward. That turns "we should get strict one day" into a measurable
balance that shrinks.

Deliberately scoped to src/ only. Under --strict the test suite reports 162
findings, ~95 of them "annotate this test function" -- a number that would
swamp the signal from the package itself and make the ratchet meaningless.

The count is tied to the mypy version pinned in pyproject.toml's dev extra; a
mypy upgrade that changes it will fail here and ask for a conscious re-baseline.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "tools" / "strict_baseline.json"
TARGET = "src"


def count_errors() -> tuple[int, str]:
    """Run mypy --strict and return (error count, full output)."""
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", TARGET],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    # Trust mypy's own summary line rather than counting "error:" substrings,
    # which would also catch the word inside a diagnostic's quoted source text.
    match = re.search(r"^Found (\d+) errors? in ", output, re.MULTILINE)
    if match:
        return int(match.group(1)), output
    if re.search(r"^Success: no issues found", output, re.MULTILINE):
        return 0, output
    print("could not parse mypy output:\n" + output, file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    allowed = int(baseline["max_strict_errors"])
    actual, output = count_errors()

    if actual > allowed:
        print(output)
        print(
            f"STRICT RATCHET BROKEN: `mypy --strict {TARGET}` reports {actual} "
            f"findings, up from the recorded {allowed}.\n"
            f"This change adds {actual - allowed} new strict finding(s). Fix them "
            f"rather than raising the baseline -- the number is only allowed to go down.",
            file=sys.stderr,
        )
        return 1

    if actual < allowed:
        print(
            f"STRICT RATCHET IMPROVED: {actual} findings, below the recorded {allowed}.\n"
            f"Tighten the ratchet so the gain cannot be given back: set "
            f'"max_strict_errors": {actual} in {BASELINE_PATH.relative_to(ROOT)}.',
            file=sys.stderr,
        )
        return 1

    print(f"strict ratchet holding: {actual} findings in {TARGET}/ (baseline {allowed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
