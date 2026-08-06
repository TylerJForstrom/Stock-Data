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
from collections.abc import Mapping, Sequence
from typing import NotRequired, TypedDict

from .http import FairAccessSession, Fetcher, atomic_write_text
from .manifest import PUBLIC_DOMAIN_NOTE, publish_staged_dataset, write_manifest

# One canonicalized directory row. Read-only (Mapping, not dict) because
# diff_records only ever reads its inputs -- and because dict's value type is
# invariant, so a plain dict[str, object] parameter would reject the
# dict[str, str] rows callers naturally build.
Record = Mapping[str, object]

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


class ListingEvent(TypedDict):
    """One line of data/symbols/events/events.jsonl.

    Spelled out as a TypedDict because this stream is a published contract:
    Stock-Grader replays it to rebuild point-in-time membership, so a typo in
    a key name here is a silent consumer break rather than a local mistake.
    """

    date: str
    source: str
    event: str
    record: Record
    previous: NotRequired[Record]


def diff_records(
    source: str,
    previous: Sequence[Record],
    current: Sequence[Record],
    as_of: str,
) -> list[ListingEvent]:
    """Set-diff on the identity key; value changes on surviving keys are 'changed'."""
    key_fields = _KEYS[source]

    def key(r: Record) -> tuple:
        return tuple(r.get(f) for f in key_fields)

    prev_map = {key(r): r for r in previous}
    cur_map = {key(r): r for r in current}
    events: list[ListingEvent] = []
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


def snapshot(data_dir: str, session: Fetcher | None = None) -> tuple[dict[str, int], list[str]]:
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
    events_dir = os.path.join(symbols_dir, "events")
    os.makedirs(current_dir, exist_ok=True)
    os.makedirs(events_dir, exist_ok=True)
    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")

    event_counts: dict[str, int] = {}
    failures: list[str] = []
    all_events: list[ListingEvent] = []
    staged: dict[str, str] = {}  # snapshot basename -> new content
    # Per-source success watermarks survive failed runs: the staleness gate
    # reads these, so a single permanently broken source goes red instead of
    # hiding behind the manifest's always-fresh write timestamp.
    last_success: dict[str, str] = {}
    # A source's first-ever snapshot date is its point-in-time floor: before
    # it, the foundry never fetched the source, so nothing can be reconstructed
    # for it (pit.build reads this). It is recorded ONLY on the first-ever
    # snapshot — for a source already being archived when this began, the true
    # date is unknown and asserting today's would be a lie in the other
    # direction — and carried forward verbatim from then on.
    first_observed: dict[str, str] = {}
    previous_manifest_path = os.path.join(current_dir, "manifest.json")
    if os.path.exists(previous_manifest_path):
        try:
            with open(previous_manifest_path, encoding="utf-8") as handle:
                previous_manifest = json.load(handle)
            carried = previous_manifest.get("last_success")
            if isinstance(carried, dict):
                last_success = {str(k): str(v) for k, v in carried.items()}
            carried_first = previous_manifest.get("first_observed")
            if isinstance(carried_first, dict):
                first_observed = {str(k): str(v) for k, v in carried_first.items()}
        except (json.JSONDecodeError, OSError):
            pass
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
        staged[f"{source}.jsonl"] = _to_jsonl(records)
        all_events.extend(events)
        event_counts[source] = len(events)
        last_success[source] = today
        if not had_previous:
            first_observed.setdefault(source, today)

    events_path = os.path.join(events_dir, "events.jsonl")
    existing = ""
    if os.path.exists(events_path):
        with open(events_path, encoding="utf-8") as handle:
            existing = handle.read()
        # Manifest hashes are over the published bytes, so normalize legacy
        # Windows checkouts before hashing even when this run emits no events.
        existing = "".join(line + "\n" for line in existing.splitlines())
    if all_events:
        # Date-agnostic dedupe, but only against each entity's LATEST event: a
        # crash retried on a later UTC day re-emits the same diffs with a
        # different date field, and comparing without the date drops those
        # replays. Matching against ALL history would also swallow genuine
        # repeats — a relisting identical to an old "added", a second
        # delisting, a field oscillating back — so an intervening opposite
        # event makes an identical repeat real again.
        def _identity(line: str) -> tuple[str, tuple]:
            """(dateless canonical line, entity key) for one event line."""
            event = json.loads(line)
            event.pop("date", None)
            record = event.get("record") or {}
            # A retired source's lines outlive its _KEYS entry (the file is
            # append-only); fall back to whole-record identity for those.
            key_fields = _KEYS.get(event["source"])
            if key_fields is None:
                entity = (event["source"], tuple(sorted(record.items())))
            else:
                entity = (event["source"], tuple(record.get(f) for f in key_fields))
            return json.dumps(event, sort_keys=True), entity

        # The events file is append-only, so file order is chronological.
        latest: dict[tuple, str] = {}
        for line in existing.splitlines():
            if line.strip():
                dateless, entity = _identity(line)
                latest[entity] = dateless
        new_lines = []
        for line in (json.dumps(e, sort_keys=True) for e in all_events):
            dateless, entity = _identity(line)
            if latest.get(entity) == dateless:
                continue  # exact replay of this entity's newest event
            latest[entity] = dateless
            new_lines.append(line)
        if new_lines:
            existing += "".join(line + "\n" for line in new_lines)
    atomic_write_text(events_path, existing)

    write_manifest(
        events_dir,
        source_urls=list(SOURCES.values()),
        license_note=PUBLIC_DOMAIN_NOTE,
        row_counts={"events.jsonl": sum(bool(line.strip()) for line in existing.splitlines())},
        extra={"last_checked_date": today},
    )

    # Only after events are durable do the baselines advance. Baselines and
    # their manifest are staged together and renamed in succession, manifest
    # last (see publish_staged_dataset), so a kill mid-publish cannot leave
    # snapshots whose manifest sha256s no longer match.
    publish_staged_dataset(
        current_dir,
        {name: content.encode("utf-8") for name, content in staged.items()},
        source_urls=list(SOURCES.values()),
        license_note=PUBLIC_DOMAIN_NOTE,
        row_counts=None,
        extra={
            "snapshot_date": today,
            "failures": failures,
            "last_success": last_success,
            "first_observed": first_observed,
        },
    )
    # Heartbeat updates every run — scheduled workflows are disabled by GitHub
    # after 60 days without commits, so the workflow commits this even when
    # nothing else changed.
    atomic_write_text(
        os.path.join(data_dir, "heartbeat.txt"),
        f"last_run_utc: {dt.datetime.now(dt.UTC).isoformat()}\nfailures: {failures or 'none'}\n",
    )
    return event_counts, failures
