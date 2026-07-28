"""Unit tests for symbol-directory canonicalization and event diffing."""

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
