"""Corporate event flags from SEC submissions: 8-K items and delisting forms.

The per-CIK submissions JSON already carries an ``items`` field for every 8-K
(verified live: e.g. "2.02,9.01") plus the full form list — so the strongest
free red flags cost zero additional data sources:

    1.03  bankruptcy / receivership
    2.04  triggering of direct financial obligation (debt acceleration)
    3.01  delisting notice / listing-standard failure
    4.01  auditor change
    4.02  non-reliance on previously issued financials (restatement warning)
    5.02  departure of directors / principal officers

Forms 25 / 25-NSE / 15 mark exchange delisting and deregistration. Events are
emitted as one JSONL row per (cik, accession) with filing dates, so the panel
is point-in-time by construction.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
from collections.abc import Callable
from typing import Any

from .http import FairAccessSession, Fetcher
from .manifest import PUBLIC_DOMAIN_NOTE, publish_staged_dataset

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

FLAGGED_ITEMS = {
    "1.03": "bankruptcy_or_receivership",
    "2.04": "debt_acceleration",
    "3.01": "delisting_notice",
    "4.01": "auditor_change",
    "4.02": "non_reliance_on_financials",
    "5.02": "officer_departure",
}
DELISTING_FORMS = {"25", "25-NSE", "15", "15-12B", "15-12G", "15-15D"}


def extract_events(submissions: dict) -> list[dict]:
    """Pull flagged 8-K items and delisting forms from one submissions payload."""
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    items_list = recent.get("items", [])
    accessions = recent.get("accessionNumber", [])
    cik = int(submissions.get("cik", 0))
    events: list[dict] = []
    for index, form in enumerate(forms):
        date = dates[index] if index < len(dates) else None
        accession = accessions[index] if index < len(accessions) else None
        if form in DELISTING_FORMS:
            events.append(
                {
                    "cik": cik,
                    "form": form,
                    "filing_date": date,
                    "accession": accession,
                    "event": "delisting_or_deregistration",
                    "items": None,
                }
            )
            continue
        if not form.startswith("8-K"):
            continue
        raw_items = items_list[index] if index < len(items_list) else ""
        flagged = sorted(
            {
                FLAGGED_ITEMS[code.strip()]
                for code in (raw_items or "").split(",")
                if code.strip() in FLAGGED_ITEMS
            }
        )
        if flagged:
            events.append(
                {
                    "cik": cik,
                    "form": form,
                    "filing_date": date,
                    "accession": accession,
                    "event": ",".join(flagged),
                    "items": raw_items,
                }
            )
    return events


def _universe_ciks(data_dir: str, limit: int | None = None) -> list[int]:
    """CIKs from the symbols snapshot the daily job already maintains."""
    path = os.path.join(data_dir, "symbols", "current", "sec_company_tickers.jsonl")
    ciks: list[int] = []
    seen: set[int] = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            cik = int(json.loads(line)["cik"])
            if cik not in seen:
                seen.add(cik)
                ciks.append(cik)
    return ciks[:limit] if limit else ciks


def collect(
    data_dir: str,
    session: Fetcher | None = None,
    limit: int | None = None,
    log: Callable[[str], object] = print,
) -> dict[str, int]:
    """Fetch submissions per CIK and write the flagged-events dataset.

    Watermark: CIKs whose events were already extracted this ISO week are
    skipped (state file), so the weekly job resumes cleanly after
    interruption. ~10k CIKs at 8 req/s is ~25 minutes of polite traffic.
    """
    session = session or FairAccessSession()
    out_dir = os.path.join(data_dir, "events")
    os.makedirs(out_dir, exist_ok=True)
    week = dt.datetime.now(dt.UTC).strftime("%G-W%V")
    state_path = os.path.join(out_dir, ".progress.json")
    # dict[str, Any]: this is parsed JSON with mixed value types (a week string
    # beside a CIK list). Without the annotation mypy joins the literal's values
    # down to Sequence[str] and then reports the int set below as a type error.
    state: dict[str, Any] = {"week": week, "done": []}
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if loaded.get("week") == week:
                state = loaded
        except (json.JSONDecodeError, OSError):
            pass
    # int(): the watermark round-trips through JSON, and a resumed sweep must
    # compare like with like -- str CIKs here would silently match nothing and
    # re-fetch every filer already done this week.
    done: set[int] = {int(cik) for cik in state.get("done", [])}

    all_events: list[dict] = []
    events_path = os.path.join(out_dir, "flagged_events.jsonl.gz")
    if os.path.exists(events_path):
        with gzip.open(events_path, "rt", encoding="utf-8") as handle:
            all_events = [json.loads(line) for line in handle if line.strip()]
    known = {(e["cik"], e["accession"]) for e in all_events}

    stats = {"ciks": 0, "new_events": 0, "skipped": len(done)}
    for cik in _universe_ciks(data_dir, limit=limit):
        if cik in done:
            continue
        try:
            payload = session.get(SUBMISSIONS_URL.format(cik=cik)).json()
        except Exception as exc:  # noqa: BLE001 - one filer never kills the sweep
            log(f"  CIK {cik}: {exc}")
            continue
        for event in extract_events(payload):
            key = (event["cik"], event["accession"])
            if key not in known:
                known.add(key)
                all_events.append(event)
                stats["new_events"] += 1
        done.add(cik)
        stats["ciks"] += 1
        if stats["ciks"] % 500 == 0:
            log(f"  events: {stats['ciks']} CIKs swept, {stats['new_events']} new events")
            _checkpoint(out_dir, events_path, state_path, all_events, week, done)

    _checkpoint(out_dir, events_path, state_path, all_events, week, done)
    return stats


def _checkpoint(
    out_dir: str,
    events_path: str,
    state_path: str,
    all_events: list[dict],
    week: str,
    done: set[int],
) -> None:
    """Every checkpoint is self-consistent: file, progress, AND manifest.

    The workflow commits with ``if: always()``; a mid-sweep timeout that
    updated the events file but not its manifest would publish a dataset every
    compliant consumer refuses (hash mismatch) for up to a week. A partial-
    but-honestly-manifested sweep is fine; a desynced one is not — so all
    three outputs are staged first and renamed into place back-to-back with
    the manifest last (see publish_staged_dataset): the manifest is computed
    from the staged bytes BEFORE anything is published, a kill while writing
    or hashing changes nothing, and a kill between the final renames is
    healed by the next checkpoint rewriting all three.
    """
    publish_staged_dataset(
        out_dir,
        {
            os.path.basename(events_path): _events_payload(all_events),
            os.path.basename(state_path): json.dumps({"week": week, "done": sorted(done)}).encode(
                "utf-8"
            ),
        },
        source_urls=[SUBMISSIONS_URL.format(cik=0)],
        license_note=PUBLIC_DOMAIN_NOTE,
        extra={"flagged_item_codes": FLAGGED_ITEMS, "delisting_forms": sorted(DELISTING_FORMS)},
    )


def _events_payload(events: list[dict]) -> bytes:
    """Deterministic gzip bytes (mtime=0, no embedded name) for stable hashes."""
    events = sorted(events, key=lambda e: (e.get("filing_date") or "", e["cik"]))
    payload = "".join(json.dumps(e, sort_keys=True) + "\n" for e in events)
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(payload.encode("utf-8"))
    return buffer.getvalue()
