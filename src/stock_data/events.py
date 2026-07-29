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
import json
import os

from .http import FairAccessSession, atomic_write_text
from .manifest import PUBLIC_DOMAIN_NOTE, write_manifest

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


def collect(data_dir: str, session: FairAccessSession | None = None,
            limit: int | None = None, log=print) -> dict[str, int]:
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
    state = {"week": week, "done": []}
    if os.path.exists(state_path):
        try:
            loaded = json.load(open(state_path, encoding="utf-8"))
            if loaded.get("week") == week:
                state = loaded
        except (json.JSONDecodeError, OSError):
            pass
    done: set[int] = set(state.get("done", []))

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
            _write_events(events_path, all_events)
            atomic_write_text(state_path, json.dumps({"week": week, "done": sorted(done)}))

    _write_events(events_path, all_events)
    atomic_write_text(state_path, json.dumps({"week": week, "done": sorted(done)}))
    write_manifest(
        out_dir,
        source_urls=[SUBMISSIONS_URL.format(cik=0)],
        license_note=PUBLIC_DOMAIN_NOTE,
        extra={"flagged_item_codes": FLAGGED_ITEMS, "delisting_forms": sorted(DELISTING_FORMS)},
    )
    return stats


def _write_events(path: str, events: list[dict]) -> None:
    events = sorted(events, key=lambda e: (e.get("filing_date") or "", e["cik"]))
    payload = "".join(json.dumps(e, sort_keys=True) + "\n" for e in events)
    tmp = path + ".tmp"
    with gzip.GzipFile(tmp, "wb", mtime=0) as handle:
        handle.write(payload.encode("utf-8"))
    os.replace(tmp, path)
