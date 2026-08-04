"""Workflow-contract tests: cross-coverage staleness net and action pinning.

These assert on the workflow YAML as text, the same style as the sentiment
wiring test in test_tickerpulse.py: cheap, and they fail the moment a
refactor silently drops a gate or reintroduces a floating action tag.
"""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_daily_snapshot_cross_covers_the_corporate_actions_clock():
    """The weekly sweep's clock is checked daily (cross-coverage): a disabled
    weekly-corporate-actions workflow must not fail silently. Guarded the same
    way as that workflow's own gate — armed only once the manifest declares an
    all-listed sweep — so the legacy 3-ticker dataset cannot trip the alarm.
    """
    workflow = (WORKFLOWS / "daily-snapshot.yml").read_text(encoding="utf-8")

    gate = workflow.index("check-staleness --max-age-days 9 data/corporate_actions")
    work = workflow.index("snapshot-symbols")
    assert gate < work, "the cross-coverage gate must run before collection"
    assert '"tickers_failed"' in workflow, "bootstrap guard must match the weekly gate"


def _run_block(workflow: str, step_name: str) -> str:
    """The `run:` script of one named step, as text."""
    steps = workflow.split("      - ")
    step = next(s for s in steps if s.startswith(f"name: {step_name}"))
    return step[step.index("run: |") :]


def test_no_freshness_clock_can_short_circuit_the_others():
    """Each staleness clock must report independently.

    `check-staleness` returns 1 whenever a dataset is stale, and GitHub runs
    `run:` blocks under `bash -e`, so chaining the checks means the first red
    clock aborts the step and every later one is skipped — silencing the
    cross-coverage clocks (weekly events, sentiment's forward-only clock,
    corporate actions) exactly when something is already broken and a second,
    independent clock stopping would matter most.
    """
    workflow = (WORKFLOWS / "daily-snapshot.yml").read_text(encoding="utf-8")
    block = _run_block(workflow, "Check prior snapshot freshness")

    checks = [line.strip() for line in block.splitlines() if "check-staleness" in line]
    assert len(checks) >= 5, "the cross-coverage net lost a clock"
    for line in checks:
        assert line.endswith("|| rc=1"), (
            f"clock can abort the step and hide the ones after it: {line}"
        )
    assert block.rstrip().endswith("exit $rc"), "the step must exit on the aggregated result"


def test_every_workflow_action_is_pinned_to_a_commit_sha():
    """Floating tags (@v4) are the standard supply-chain path: a rewritten tag
    executes attacker code inside jobs holding push-capable tokens. Every
    `uses:` must pin a 40-hex commit SHA with a human-readable tag comment.
    """
    pinned = re.compile(r"uses:\s*\S+@[0-9a-f]{40}\s+#\s*\S+")
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "uses:" not in line:
                continue
            assert pinned.search(line), (
                f"{path.name}:{line_number} is not SHA-pinned: {line.strip()}"
            )
