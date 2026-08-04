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
