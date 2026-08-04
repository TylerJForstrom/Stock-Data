"""Point-in-time interval tables: lookup must equal replay, provably.

The pit dataset retires consumer-side event replay, so the one property that
matters is exact equivalence: slicing the interval table at any date yields the
same membership as replaying the event stream backward from the current
snapshot. That is asserted here twice — over a fixture archive exercising
added/removed/changed/boundary cases, and over the repository's REAL archive at
every archived event date (the burn-in gate the migration is conditioned on).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_data import pit
from stock_data.manifest import write_manifest
from stock_data.symbols import _KEYS, SOURCES

REPO_DATA = Path(__file__).resolve().parent.parent / "data"

EXCHANGE_CURRENT = [
    {"cik": 320193, "ticker": "AAPL", "title": "Apple Inc.", "exchange": "Nasdaq"},
    {"cik": 777, "ticker": "NEWCO", "title": "New Co", "exchange": "Nasdaq"},
    {"cik": 900, "ticker": "OLDN", "title": "New Name", "exchange": "NYSE"},
]

EVENTS = [
    {
        "date": "2026-07-20",
        "source": "sec_company_tickers_exchange",
        "event": "removed",
        "record": {"cik": 555, "ticker": "DEADCO", "title": "Dead Co", "exchange": "NYSE"},
    },
    {
        "date": "2026-07-22",
        "source": "sec_company_tickers_exchange",
        "event": "changed",
        "record": {"cik": 900, "ticker": "OLDN", "title": "New Name", "exchange": "NYSE"},
        "previous": {"cik": 900, "ticker": "OLDN", "title": "Old Name", "exchange": "NYSE"},
    },
    {
        "date": "2026-07-25",
        "source": "sec_company_tickers_exchange",
        "event": "added",
        "record": {"cik": 777, "ticker": "NEWCO", "title": "New Co", "exchange": "Nasdaq"},
    },
]

BOUNDARY = "2026-07-19"  # earliest event 2026-07-20, minus one day


def write_fixture(root: Path, *, events: list[dict] | None = None) -> Path:
    """A data dir with all four current snapshots, an event stream, manifests."""
    current_dir = root / "symbols" / "current"
    events_dir = root / "symbols" / "events"
    current_dir.mkdir(parents=True)
    events_dir.mkdir(parents=True)
    per_source: dict[str, list[dict]] = {
        "sec_company_tickers_exchange": EXCHANGE_CURRENT,
        "sec_company_tickers": [
            {"cik": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        ],
        "nasdaqlisted": [
            {"ticker": "AAPL", "name": "Apple", "category": "Q", "etf": "N", "test_issue": "N"},
        ],
        "otherlisted": [],
    }
    for source, rows in per_source.items():
        (current_dir / f"{source}.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
        )
    stream = EVENTS if events is None else events
    (events_dir / "events.jsonl").write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in stream), encoding="utf-8"
    )
    for directory in (current_dir, events_dir):
        write_manifest(str(directory), source_urls=[], license_note="test")
    return root


def slice_members(intervals: list[dict], asof: str) -> list[dict]:
    """Date-range lookup: the consumer side of the contract, in four lines."""
    return [
        {k: v for k, v in row.items() if k not in pit.INTERVAL_FIELDS}
        for row in intervals
        if row["valid_from"] <= asof and (row["valid_to"] is None or asof < row["valid_to"])
    ]


def canon(rows) -> set[str]:
    return {json.dumps(r, sort_keys=True) for r in rows}


def test_intervals_cover_added_removed_changed_and_boundary(tmp_path):
    write_fixture(tmp_path / "data")
    counts = pit.build(str(tmp_path / "data"), log=lambda *_: None)
    assert counts["sec_company_tickers_exchange.jsonl"] == 5
    rows = [
        json.loads(line)
        for line in (tmp_path / "data" / "symbols" / "pit" / "sec_company_tickers_exchange.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    by = {(r["ticker"], r["valid_from"]): r for r in rows}
    # Baseline survivor: open interval, membership NOT provable before the boundary.
    aapl = by[("AAPL", BOUNDARY)]
    assert aapl["valid_to"] is None and aapl["provable_from"] is False
    # Delisted at 07-20: half-open interval ends ON the event date.
    dead = by[("DEADCO", BOUNDARY)]
    assert dead["valid_to"] == "2026-07-20" and dead["provable_from"] is False
    # Changed at 07-22: previous state closes, new state opens provably.
    old = by[("OLDN", BOUNDARY)]
    assert old["title"] == "Old Name" and old["valid_to"] == "2026-07-22"
    new = by[("OLDN", "2026-07-22")]
    assert new["title"] == "New Name" and new["valid_to"] is None
    assert new["provable_from"] is True
    # Added at 07-25: provable open interval.
    newco = by[("NEWCO", "2026-07-25")]
    assert newco["valid_to"] is None and newco["provable_from"] is True


def test_lookup_equals_replay_on_every_fixture_date(tmp_path):
    write_fixture(tmp_path / "data")
    pit.build(str(tmp_path / "data"), log=lambda *_: None)
    rows = [
        json.loads(line)
        for line in (tmp_path / "data" / "symbols" / "pit" / "sec_company_tickers_exchange.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    # Every day of the fixture window, not just event dates: between-event days
    # must resolve to the state opened by the preceding event.
    for day in range(19, 28):
        asof = f"2026-07-{day:02d}"
        replayed = pit.members_asof(
            EXCHANGE_CURRENT, EVENTS, asof, _KEYS["sec_company_tickers_exchange"]
        )
        assert canon(slice_members(rows, asof)) == canon(replayed.values()), asof


TWO_RUNS_CURRENT = [
    {"cik": 320193, "ticker": "AAPL", "title": "Apple Inc.", "exchange": "Nasdaq"},
    {"cik": 1711570, "ticker": "UROY", "title": "Uranium Royalty Corp.", "exchange": "Nasdaq"},
]

GWSO = {"cik": 1430300, "ticker": "GWSO", "title": "Global Warming Solutions", "exchange": "OTC"}
UROY_A = TWO_RUNS_CURRENT[1]
UROY_B = {**UROY_A, "title": "Uranium Royalty Corp. /CA"}

# Two snapshot runs on one UTC day (the workflow has a schedule AND a
# workflow_dispatch), in the order they were appended to events.jsonl. Run 1
# diffs the 07-28 baseline; run 2 diffs run 1's baseline and cancels it out.
TWO_RUNS_ONE_DAY = [
    {
        "date": "2026-07-29",
        "source": "sec_company_tickers_exchange",
        "event": "added",
        "record": GWSO,
    },
    {
        "date": "2026-07-29",
        "source": "sec_company_tickers_exchange",
        "event": "changed",
        "record": UROY_B,
        "previous": UROY_A,
    },
    {
        "date": "2026-07-29",
        "source": "sec_company_tickers_exchange",
        "event": "removed",
        "record": GWSO,
    },
    {
        "date": "2026-07-29",
        "source": "sec_company_tickers_exchange",
        "event": "changed",
        "record": UROY_A,
        "previous": UROY_B,
    },
]


def test_same_date_events_are_un_applied_in_reverse_append_order():
    """Two runs on one day cancel out; the backward replay must cancel them back.

    The 2026-07-28 baseline had no GWSO and titled UROY without the "/CA"
    suffix. Run 1 added GWSO and renamed UROY; run 2 undid both, so the current
    snapshot matches the baseline again. Un-applying those four same-date events
    in APPEND order (what a stable sort with reverse=True does to equal keys)
    re-applies each pair's first half and lands on the opposite of the truth:
    a GWSO listing that never existed and a title the directory never carried.
    """
    replayed = pit.members_asof(
        TWO_RUNS_CURRENT,
        TWO_RUNS_ONE_DAY,
        "2026-07-28",
        _KEYS["sec_company_tickers_exchange"],
    )
    assert (1430300, "GWSO") not in replayed, "phantom listing fabricated at the boundary"
    assert replayed[(1711570, "UROY")]["title"] == "Uranium Royalty Corp."
    assert canon(replayed.values()) == canon(TWO_RUNS_CURRENT)


def test_same_date_collisions_do_not_reach_the_published_table(tmp_path):
    """The published interval table carries no phantom boundary row."""
    root = write_fixture(tmp_path / "data", events=TWO_RUNS_ONE_DAY)
    # write_fixture's exchange snapshot is the generic one; use the two-run pair.
    current_path = root / "symbols" / "current" / "sec_company_tickers_exchange.jsonl"
    current_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in TWO_RUNS_CURRENT),
        encoding="utf-8",
    )
    write_manifest(str(root / "symbols" / "current"), source_urls=[], license_note="test")
    pit.build(str(root), log=lambda *_: None)
    rows = [
        json.loads(line)
        for line in (root / "symbols" / "pit" / "sec_company_tickers_exchange.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert not [r for r in rows if r["ticker"] == "GWSO"], "phantom listing published"
    boundary_uroy = [r for r in rows if r["ticker"] == "UROY" and r["valid_from"] == "2026-07-28"]
    assert [r["title"] for r in boundary_uroy] == ["Uranium Royalty Corp."]


def test_sources_without_events_publish_boundary_intervals(tmp_path):
    write_fixture(tmp_path / "data")
    pit.build(str(tmp_path / "data"), log=lambda *_: None)
    pit_dir = tmp_path / "data" / "symbols" / "pit"
    nasdaq = [
        json.loads(line)
        for line in (pit_dir / "nasdaqlisted.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(nasdaq) == 1
    assert nasdaq[0]["valid_from"] == BOUNDARY  # global boundary, same for all sources
    assert nasdaq[0]["valid_to"] is None and nasdaq[0]["provable_from"] is False
    assert (pit_dir / "otherlisted.jsonl").read_text(encoding="utf-8") == ""


def test_source_first_observed_after_the_archive_start_gets_its_own_floor(tmp_path, monkeypatch):
    """A source added later must not claim the archive-wide boundary.

    ``symbols.snapshot`` deliberately emits NO events for a first-ever snapshot
    ("a baseline, not thousands of added events"), so a newly added source
    contributes nothing to the global ``min(date)`` and, under one global
    boundary, publishes every current row as directory state from 2026-07-19 —
    months before the foundry ever fetched it. Its floor is its own
    first-observed date instead, and the scalar the manifest publishes is the
    conservative latest floor.
    """
    root = write_fixture(tmp_path / "data")
    current_dir = root / "symbols" / "current"
    (current_dir / "newsource.jsonl").write_text(
        json.dumps({"ticker": "NEW", "name": "New Co"}, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_manifest(
        str(current_dir),
        source_urls=[],
        license_note="test",
        extra={"first_observed": {"newsource": "2026-10-01"}},
    )
    monkeypatch.setattr(pit, "SOURCES", {**SOURCES, "newsource": "https://example.test/new"})
    monkeypatch.setattr(pit, "_KEYS", {**_KEYS, "newsource": ("ticker",)})

    pit.build(str(root), log=lambda *_: None)
    pit_dir = root / "symbols" / "pit"
    rows = [
        json.loads(line)
        for line in (pit_dir / "newsource.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["valid_from"] for r in rows] == ["2026-10-01"], (
        "a source's rows must not predate the day it was first fetched"
    )
    manifest = json.loads((pit_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reconstructable_from_by_source"]["newsource"] == "2026-10-01"
    assert manifest["reconstructable_from_by_source"]["sec_company_tickers"] == BOUNDARY
    assert manifest["reconstructable_from"] == "2026-10-01"  # conservative: latest floor


def test_manifest_publishes_boundary_and_input_hashes(tmp_path):
    write_fixture(tmp_path / "data")
    pit.build(str(tmp_path / "data"), log=lambda *_: None)
    manifest = json.loads(
        (tmp_path / "data" / "symbols" / "pit" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["reconstructable_from"] == BOUNDARY
    assert set(manifest["input_sha256"]) == {"data/symbols/events/events.jsonl"} | {
        f"data/symbols/current/{source}.jsonl" for source in SOURCES
    }
    names = {entry["name"]: entry for entry in manifest["files"]}
    assert names["sec_company_tickers_exchange.jsonl"]["rows"] == 5


def test_rebuild_is_byte_deterministic(tmp_path):
    write_fixture(tmp_path / "data")
    pit_dir = tmp_path / "data" / "symbols" / "pit"
    pit.build(str(tmp_path / "data"), log=lambda *_: None)
    first = {p.name: p.read_bytes() for p in pit_dir.iterdir() if p.name != "manifest.json"}
    pit.build(str(tmp_path / "data"), log=lambda *_: None)
    second = {p.name: p.read_bytes() for p in pit_dir.iterdir() if p.name != "manifest.json"}
    assert first == second


def test_refuses_hash_mismatched_inputs(tmp_path):
    root = write_fixture(tmp_path / "data")
    snapshot = root / "symbols" / "current" / "sec_company_tickers_exchange.jsonl"
    snapshot.write_text(snapshot.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(pit.PitBuildError, match="sha256 mismatch"):
        pit.build(str(root), log=lambda *_: None)
    assert not (root / "symbols" / "pit").exists()  # refusal publishes nothing


def test_refuses_empty_event_archive(tmp_path):
    root = write_fixture(tmp_path / "data", events=[])
    with pytest.raises(pit.PitBuildError, match="empty"):
        pit.build(str(root), log=lambda *_: None)


def test_refuses_records_carrying_reserved_interval_fields():
    with pytest.raises(pit.PitBuildError, match="reserved interval field"):
        pit.build_intervals(
            [{"ticker": "X", "valid_from": "2001-01-01"}], [], "2026-07-19", ("ticker",)
        )


def test_refuses_an_unreplayable_event_kind(tmp_path):
    """A fourth event kind must stop the build, not vanish into a no-op.

    Skipping it publishes the POST-change record as the directory state back to
    the boundary, with provable_from silent about it — a smooth, confident,
    wrong history that no hash check downstream can see.
    """
    renamed = [
        {
            "date": "2026-07-20",
            "source": "sec_company_tickers_exchange",
            "event": "renamed",
            "record": EXCHANGE_CURRENT[0],
            "previous": {**EXCHANGE_CURRENT[0], "ticker": "APPL"},
        }
    ]
    root = write_fixture(tmp_path / "data", events=renamed)
    with pytest.raises(pit.PitBuildError, match="unrecognized event kind 'renamed'"):
        pit.build(str(root), log=lambda *_: None)
    assert not (root / "symbols" / "pit").exists()


def test_refuses_a_changed_event_that_cannot_be_reversed(tmp_path):
    unreversible = [
        {
            "date": "2026-07-20",
            "source": "sec_company_tickers_exchange",
            "event": "changed",
            "record": EXCHANGE_CURRENT[0],
        }
    ]
    root = write_fixture(tmp_path / "data", events=unreversible)
    with pytest.raises(pit.PitBuildError, match="no previous state"):
        pit.build(str(root), log=lambda *_: None)


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ({"source": "sec_company_tickers_exchange", "event": "added", "record": {}}, "no date"),
        (
            {
                "date": "2026-13-99",
                "source": "sec_company_tickers_exchange",
                "event": "added",
                "record": {},
            },
            "unparseable date",
        ),
    ],
)
def test_refuses_events_that_cannot_be_placed_on_the_timeline(tmp_path, event, message):
    """A dateless or unparseable event is a refusal, not a raw traceback.

    ``cli.main`` catches only :class:`PitBuildError`, so a bare KeyError or
    ValueError escapes the refusal path and reports as a crash rather than as
    the honest "this input cannot testify" it is.
    """
    root = write_fixture(tmp_path / "data", events=[event])
    with pytest.raises(pit.PitBuildError, match=message):
        pit.build(str(root), log=lambda *_: None)


def test_cli_refuses_a_malformed_event_stream(tmp_path, capsys):
    from stock_data.cli import main

    root = write_fixture(
        tmp_path / "data",
        events=[{"source": "nasdaqlisted", "event": "added", "record": {"ticker": "X"}}],
    )
    assert main(["build-pit", "--data-dir", str(root)]) == 1
    assert "FAILURE" in capsys.readouterr().err


def test_cli_build_pit_publishes_and_reports_failures(tmp_path, capsys):
    from stock_data.cli import main

    write_fixture(tmp_path / "data")
    assert main(["build-pit", "--data-dir", str(tmp_path / "data")]) == 0
    assert (tmp_path / "data" / "symbols" / "pit" / "manifest.json").exists()
    (tmp_path / "data" / "symbols" / "events" / "events.jsonl").write_text("", encoding="utf-8")
    assert main(["build-pit", "--data-dir", str(tmp_path / "data")]) == 1
    assert "FAILURE" in capsys.readouterr().err


# -- the burn-in gate: the REAL archive, every archived event date --------------


@pytest.mark.skipif(
    not (REPO_DATA / "symbols" / "events" / "events.jsonl").exists(),
    reason="repository data archive not present",
)
def test_real_archive_lookup_equals_replay_at_every_event_date():
    """Table slice == replayed membership for every event date in the archive.

    This is the condition the ecosystem migration is gated on: consumers may
    retire their replay copies only while this equivalence holds over the real
    event stream. It runs against a fresh in-memory build so it also covers
    event/snapshot pairs newer than the committed pit tables.
    """
    events = [
        json.loads(line)
        for line in (REPO_DATA / "symbols" / "events" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert events, "real archive unexpectedly empty"
    earliest = min(str(e["date"]) for e in events)
    import datetime as dt

    boundary = (dt.date.fromisoformat(earliest) - dt.timedelta(days=1)).isoformat()
    for source in sorted(SOURCES):
        current = [
            json.loads(line)
            for line in (REPO_DATA / "symbols" / "current" / f"{source}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        source_events = [e for e in events if e.get("source") == source]
        intervals = pit.build_intervals(current, source_events, boundary, _KEYS[source])
        dates = sorted({boundary} | {str(e["date"]) for e in source_events})
        for asof in dates:
            replayed = pit.members_asof(current, source_events, asof, _KEYS[source])
            assert canon(slice_members(intervals, asof)) == canon(replayed.values()), (
                f"{source} diverges at {asof}"
            )


REPO_ROOT = REPO_DATA.parent


def _git(*args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _archived_snapshot_commits() -> dict[str, str]:
    """``{snapshot_date: sha}`` for the LAST snapshot commit of each date.

    ``git log`` is newest-first, so the first commit seen for a date is that
    day's final state — which is exactly what a replay to that date must equal.
    """
    by_date: dict[str, str] = {}
    for sha in _git("log", "--format=%H", "--", "data/symbols/current").split():
        manifest = json.loads(_git("show", f"{sha}:data/symbols/current/manifest.json"))
        date = manifest.get("snapshot_date")
        if isinstance(date, str):
            by_date.setdefault(date, sha)
    return by_date


def _git_history_available() -> bool:
    try:
        return _git("rev-parse", "--is-shallow-repository").strip() == "false"
    except Exception:  # noqa: BLE001 - no git, no worktree, no history: skip
        return False


@pytest.mark.skipif(
    not (REPO_DATA / "symbols" / "events" / "events.jsonl").exists()
    or not _git_history_available(),
    reason="repository archive or full git history not present",
)
def test_real_archive_replay_reconstructs_the_archived_snapshots():
    """GROUND TRUTH: the replay must reproduce the snapshots actually committed.

    Every other test in this file compares the interval table against
    ``members_asof`` — so a wrong replay stays invisible, because both sides
    are the same function. This one steps outside the implementation entirely:
    for each archived snapshot date it reads the directory state that was
    really committed that day and asserts the backward replay lands on it,
    record for record.

    It is the only assertion here that can see a same-date ordering bug: the
    archive holds two snapshot commits on 2026-07-29, 2026-08-02 and
    2026-08-04, and un-applying those days' paired events in append order
    fabricates memberships (e.g. CIK 1430300/GWSO at the 2026-07-28 boundary,
    which the f5445b6 baseline provably does not contain).
    """
    events = [
        json.loads(line)
        for line in (REPO_DATA / "symbols" / "events" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert events, "real archive unexpectedly empty"
    snapshots = _archived_snapshot_commits()
    assert len(snapshots) >= 2, "need at least two archived snapshot dates to compare"

    checked = 0
    for source in sorted(SOURCES):
        key_fields = _KEYS[source]
        current = [
            json.loads(line)
            for line in (REPO_DATA / "symbols" / "current" / f"{source}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        source_events = [e for e in events if e.get("source") == source]
        for asof, sha in sorted(snapshots.items()):
            try:
                blob = _git("show", f"{sha}:data/symbols/current/{source}.jsonl")
            except Exception:  # noqa: BLE001 - source not archived at that commit
                continue
            truth = [json.loads(line) for line in blob.splitlines() if line.strip()]
            replayed = pit.members_asof(current, source_events, asof, key_fields)
            assert canon(replayed.values()) == canon(truth), (
                f"{source} replayed to {asof} does not match the snapshot "
                f"committed that day ({sha[:7]})"
            )
            checked += 1
    assert checked >= len(SOURCES) * 2


@pytest.mark.skipif(
    not (REPO_DATA / "symbols" / "pit" / "manifest.json").exists(),
    reason="pit dataset not yet published",
)
def test_committed_pit_tables_match_a_fresh_rebuild(tmp_path):
    """The published artifact must never lag its inputs.

    The daily workflow rebuilds pit/ in the same run that advances current/ and
    events/, so a divergence here means someone changed the inputs without
    republishing — exactly the desync a consumer's hash check cannot see,
    because a stale table hashes correctly against its own stale manifest.
    """
    import shutil

    published = {
        f"{source}.jsonl": (REPO_DATA / "symbols" / "pit" / f"{source}.jsonl").read_bytes()
        for source in SOURCES
    }
    (tmp_path / "symbols").mkdir(parents=True)
    shutil.copytree(REPO_DATA / "symbols" / "current", tmp_path / "symbols" / "current")
    shutil.copytree(REPO_DATA / "symbols" / "events", tmp_path / "symbols" / "events")
    pit.build(str(tmp_path), log=lambda *_: None)
    rebuilt = {
        f"{source}.jsonl": (tmp_path / "symbols" / "pit" / f"{source}.jsonl").read_bytes()
        for source in SOURCES
    }
    assert published == rebuilt
