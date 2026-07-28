"""Daily symbol-directory archiver: the point-in-time universe record.

Fetches the SEC ticker/CIK maps and the official exchange symbol directories,
canonicalizes them into sorted JSONL (stable git diffs), and diffs against the
previous snapshot to emit listing/delisting/change events. Run daily; each
day's diff is a free corporate-events stream and, over time, an honest
point-in-time universe archive.

Canonical record keys per source:
- sec_company_tickers:          cik, ticker, title
- sec_company_tickers_exchange: cik, ticker, title, exchange
- nasdaqlisted:                 ticker, name, category, etf, test_issue
- otherlisted:                  ticker, name, exchange, etf, test_issue
"""

from __future__ import annotations

import datetime as dt
import json
import os

from .http import FairAccessSession, atomic_write_text
from .manifest import PUBLIC_DOMAIN_NOTE, write_manifest

SOURCES: dict[str, str] = {
    "sec_company_tickers": "https://www.sec.gov/files/company_tickers.json",
    "sec_company_tickers_exchange": "https://www.sec.gov/files/company_tickers_exchange.json",
    "nasdaqlisted": "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
    "otherlisted": "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt",
}


def canonicalize_sec_company_tickers(raw: str) -> list[dict[str, object]]:
    """company_tickers.json: {"0": {"cik_str":..., "ticker":..., "title":...}, ...}

    The outer numeric keys shift between publications, so we discard them.
    """
    payload = json.loads(raw)
    records = [
        {"cik": int(row["cik_str"]), "ticker": row["ticker"], "title": row["title"]}
        for row in payload.values()
    ]
    records.sort(key=lambda r: (r["cik"], r["ticker"]))
    return records


def canonicalize_sec_company_tickers_exchange(raw: str) -> list[dict[str, object]]:
    """company_tickers_exchange.json is columnar: {"fields": [...], "data": [[...], ...]}."""
    payload = json.loads(raw)
    fields = payload["fields"]
    records = []
    for row in payload["data"]:
        record = dict(zip(fields, row, strict=True))
        records.append(
            {
                "cik": int(record["cik"]),
                "ticker": record.get("ticker"),
                "title": record.get("name"),
                "exchange": record.get("exchange"),
            }
        )
    records.sort(key=lambda r: (r["cik"], r["ticker"] or ""))
    return records


def _canonicalize_symdir(raw: str, fields: dict[str, str]) -> list[dict[str, object]]:
    """Nasdaq Trader pipe-delimited files, with a 'File Creation Time' trailer row."""
    lines = [line for line in raw.splitlines() if line.strip()]
    header = lines[0].split("|")
    records = []
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        # strict=False: tolerate ragged rows rather than losing the whole file
        row = dict(zip(header, line.split("|"), strict=False))
        record: dict[str, object] = {}
        for out_key, in_key in fields.items():
            record[out_key] = row.get(in_key, "")
        if record.get("test_issue") == "Y":
            continue  # test issues are exchange plumbing, never real listings
        records.append(record)
    records.sort(key=lambda r: str(r["ticker"]))
    return records


def canonicalize_nasdaqlisted(raw: str) -> list[dict[str, object]]:
    return _canonicalize_symdir(
        raw,
        {
            "ticker": "Symbol",
            "name": "Security Name",
            "category": "Market Category",
            "etf": "ETF",
            "test_issue": "Test Issue",
        },
    )


def canonicalize_otherlisted(raw: str) -> list[dict[str, object]]:
    return _canonicalize_symdir(
        raw,
        {
            "ticker": "ACT Symbol",
            "name": "Security Name",
            "exchange": "Exchange",
            "etf": "ETF",
            "test_issue": "Test Issue",
        },
    )


_CANONICALIZERS = {
    "sec_company_tickers": canonicalize_sec_company_tickers,
    "sec_company_tickers_exchange": canonicalize_sec_company_tickers_exchange,
    "nasdaqlisted": canonicalize_nasdaqlisted,
    "otherlisted": canonicalize_otherlisted,
}

# The identity key per source; other fields changing is a "changed" event.
_KEYS = {
    "sec_company_tickers": ("cik", "ticker"),
    "sec_company_tickers_exchange": ("cik", "ticker"),
    "nasdaqlisted": ("ticker",),
    "otherlisted": ("ticker",),
}


def _to_jsonl(records: list[dict[str, object]]) -> str:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)


def _from_jsonl(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def diff_records(
    source: str,
    previous: list[dict[str, object]],
    current: list[dict[str, object]],
    as_of: str,
) -> list[dict[str, object]]:
    """Set-diff on the identity key; value changes on surviving keys are 'changed'."""
    key_fields = _KEYS[source]

    def key(r: dict[str, object]) -> tuple:
        return tuple(r.get(f) for f in key_fields)

    prev_map = {key(r): r for r in previous}
    cur_map = {key(r): r for r in current}
    events = []
    for k in sorted(cur_map.keys() - prev_map.keys(), key=str):
        events.append({"date": as_of, "source": source, "event": "added", "record": cur_map[k]})
    for k in sorted(prev_map.keys() - cur_map.keys(), key=str):
        events.append({"date": as_of, "source": source, "event": "removed", "record": prev_map[k]})
    for k in sorted(cur_map.keys() & prev_map.keys(), key=str):
        if prev_map[k] != cur_map[k]:
            events.append(
                {
                    "date": as_of,
                    "source": source,
                    "event": "changed",
                    "record": cur_map[k],
                    "previous": prev_map[k],
                }
            )
    return events


def snapshot(
    data_dir: str, session: FairAccessSession | None = None
) -> tuple[dict[str, int], list[str]]:
    """Fetch all sources, write canonical snapshots, append diff events.

    Returns ({source: event_count}, failures). Any per-source failure — fetch,
    canonicalization, or an unreadable previous snapshot — is recorded and the
    other sources continue; a failed source's previous snapshot stays intact.

    Durability ordering: events are appended (idempotently) BEFORE the snapshot
    baselines are advanced. A crash between the two re-emits the same events on
    the next run, and the exact-line dedupe on append drops them — so events
    are never lost and never duplicated.
    """
    session = session or FairAccessSession()
    symbols_dir = os.path.join(data_dir, "symbols")
    current_dir = os.path.join(symbols_dir, "current")
    os.makedirs(current_dir, exist_ok=True)
    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")

    event_counts: dict[str, int] = {}
    failures: list[str] = []
    all_events: list[dict[str, object]] = []
    staged: list[tuple[str, str]] = []  # (snapshot_path, new_content)
    for source, url in SOURCES.items():
        try:
            raw = session.get(url).text
            records = _CANONICALIZERS[source](raw)
            if not records:
                failures.append(f"{source}: empty after canonicalization, snapshot kept")
                continue
            snapshot_path = os.path.join(current_dir, f"{source}.jsonl")
            previous: list[dict[str, object]] = []
            had_previous = os.path.exists(snapshot_path)
            if had_previous:
                with open(snapshot_path, encoding="utf-8") as handle:
                    previous = _from_jsonl(handle.read())
            # First-ever snapshot is a baseline, not thousands of "added" events.
            events = diff_records(source, previous, records, today) if had_previous else []
        except Exception as exc:  # noqa: BLE001 - keep other sources alive
            failures.append(f"{source}: {exc}")
            continue
        staged.append((snapshot_path, _to_jsonl(records)))
        all_events.extend(events)
        event_counts[source] = len(events)

    if all_events:
        events_path = os.path.join(symbols_dir, "events.jsonl")
        existing = ""
        if os.path.exists(events_path):
            with open(events_path, encoding="utf-8") as handle:
                existing = handle.read()
        seen_lines = set(existing.splitlines())
        new_lines = [
            line
            for line in (json.dumps(e, sort_keys=True) for e in all_events)
            if line not in seen_lines
        ]
        if new_lines:
            atomic_write_text(events_path, existing + "".join(line + "\n" for line in new_lines))

    # Only after events are durable do the baselines advance.
    for snapshot_path, content in staged:
        atomic_write_text(snapshot_path, content)

    write_manifest(
        current_dir,
        source_urls=list(SOURCES.values()),
        license_note=PUBLIC_DOMAIN_NOTE,
        row_counts=None,
        extra={"snapshot_date": today, "failures": failures},
    )
    # Heartbeat updates every run — scheduled workflows are disabled by GitHub
    # after 60 days without commits, so the workflow commits this even when
    # nothing else changed.
    atomic_write_text(
        os.path.join(data_dir, "heartbeat.txt"),
        f"last_run_utc: {dt.datetime.now(dt.UTC).isoformat()}\n"
        f"failures: {failures or 'none'}\n",
    )
    return event_counts, failures
