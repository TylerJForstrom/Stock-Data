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
