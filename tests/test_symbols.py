"""Unit tests for symbol-directory canonicalization, event diffing, and publishing."""

import hashlib
import json

from stock_data import symbols
from stock_data.manifest import read_manifest
from stock_data.symbols import (
    canonicalize_nasdaqlisted,
    canonicalize_sec_company_tickers,
    canonicalize_sec_company_tickers_exchange,
    diff_records,
)

NASDAQ_RAW = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
    "Round Lot Size|ETF|NextShares\n"
    "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
    "ZAZZT|Test Issue Co|Q|Y|N|100|N|N\n"
    "File Creation Time: 0728202607:35|||||||\n"
)


def test_nasdaq_canonicalization_drops_trailer_and_test_issues():
    records = canonicalize_nasdaqlisted(NASDAQ_RAW)
    assert [r["ticker"] for r in records] == ["AAPL"]
    assert records[0]["name"] == "Apple Inc. - Common Stock"


def test_sec_map_discards_volatile_outer_keys():
    raw = (
        '{"1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},'
        ' "0": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"}}'
    )
    records = canonicalize_sec_company_tickers(raw)
    assert [(r["cik"], r["ticker"]) for r in records] == [(320193, "AAPL"), (789019, "MSFT")]


def test_sec_exchange_map_is_columnar():
    raw = (
        '{"fields": ["cik", "name", "ticker", "exchange"],'
        ' "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]]}'
    )
    records = canonicalize_sec_company_tickers_exchange(raw)
    assert records == [
        {"cik": 320193, "ticker": "AAPL", "title": "Apple Inc.", "exchange": "Nasdaq"}
    ]


def test_diff_emits_added_removed_changed():
    previous = [
        {"ticker": "AAPL", "name": "Apple", "category": "Q", "etf": "N", "test_issue": "N"},
        {"ticker": "DEAD", "name": "Delisted Co", "category": "Q", "etf": "N", "test_issue": "N"},
    ]
    current = [
        {"ticker": "AAPL", "name": "Apple Inc.", "category": "Q", "etf": "N", "test_issue": "N"},
        {"ticker": "NEWCO", "name": "New Listing", "category": "Q", "etf": "N", "test_issue": "N"},
    ]
    events = diff_records("nasdaqlisted", previous, current, "2026-07-28")
    kinds = {e["event"]: e for e in events}
    assert kinds["added"]["record"]["ticker"] == "NEWCO"
    assert kinds["removed"]["record"]["ticker"] == "DEAD"
    assert kinds["changed"]["record"]["name"] == "Apple Inc."
    assert kinds["changed"]["previous"]["name"] == "Apple"


class _Response:
    def __init__(self, text):
        self.text = text


class _Session:
    def __init__(self, raw):
        self.raw = raw

    def get(self, _url):
        return _Response(self.raw)


def test_snapshot_publishes_listing_events_as_own_manifest_dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(symbols, "SOURCES", {"nasdaqlisted": "https://example.test/symbols"})

    symbols.snapshot(str(tmp_path), session=_Session(NASDAQ_RAW))
    events_dir = tmp_path / "symbols" / "events"
    events_path = events_dir / "events.jsonl"
    first_manifest = read_manifest(str(events_dir))

    assert events_path.read_text() == ""
    assert first_manifest["schema_version"] == "1.0"
    assert first_manifest["source_urls"] == ["https://example.test/symbols"]
    assert first_manifest["files"][0]["name"] == "events.jsonl"
    assert first_manifest["files"][0]["rows"] == 0

    updated_raw = NASDAQ_RAW.replace(
        "File Creation Time",
        "MSFT|Microsoft Corporation|Q|N|N|100|N|N\nFile Creation Time",
    )
    symbols.snapshot(str(tmp_path), session=_Session(updated_raw))

    rows = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert rows == [
        {
            "date": rows[0]["date"],
            "event": "added",
            "record": {
                "category": "Q",
                "etf": "N",
                "name": "Microsoft Corporation",
                "test_issue": "N",
                "ticker": "MSFT",
            },
            "source": "nasdaqlisted",
        }
    ]
    assert "data" not in rows[0]

    manifest = read_manifest(str(events_dir))
    entry = manifest["files"][0]
    assert entry["name"] == "events.jsonl"
    assert entry["rows"] == 1
    assert entry["sha256"] == hashlib.sha256(events_path.read_bytes()).hexdigest()

    events_path.write_bytes(events_path.read_bytes().replace(b"\n", b"\r\n"))
    symbols.snapshot(str(tmp_path), session=_Session(updated_raw))

    assert b"\r\n" not in events_path.read_bytes()
    normalized_manifest = read_manifest(str(events_dir))
    normalized_entry = normalized_manifest["files"][0]
    assert normalized_entry["bytes"] == len(events_path.read_bytes())
    assert normalized_entry["sha256"] == hashlib.sha256(events_path.read_bytes()).hexdigest()
