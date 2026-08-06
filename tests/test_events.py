"""Event-extraction tests against the live submissions JSON shape."""

import hashlib

import pytest

from stock_data.events import DELISTING_FORMS, extract_events

PAYLOAD = {
    "cik": "320193",
    "filings": {
        "recent": {
            "form": ["8-K", "10-K", "8-K", "25-NSE", "8-K"],
            "filingDate": ["2026-01-05", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"],
            "accessionNumber": ["a1", "a2", "a3", "a4", "a5"],
            "items": ["2.02,9.01", "", "4.02,9.01", "", "4.01,5.02"],
        }
    },
}


def test_flagged_items_and_delisting_forms_extracted():
    events = extract_events(PAYLOAD)
    by_accession = {e["accession"]: e for e in events}
    assert "a1" not in by_accession  # routine earnings 8-K is not a red flag
    assert by_accession["a3"]["event"] == "non_reliance_on_financials"
    assert by_accession["a4"]["event"] == "delisting_or_deregistration"
    assert by_accession["a5"]["event"] == "auditor_change,officer_departure"
    assert all(e["cik"] == 320193 for e in events)
    assert all(e["filing_date"] for e in events)


def test_delisting_form_set_covers_deregistration_variants():
    assert {"25", "15", "15-12B"} <= DELISTING_FORMS


def test_staleness_gate_reads_per_source_watermarks(tmp_path, capsys):
    """A failing source must go STALE even while the manifest timestamp is fresh."""

    from stock_data.cli import main
    from stock_data.manifest import write_manifest

    dataset = tmp_path / "symbols" / "current"
    dataset.mkdir(parents=True)
    (dataset / "x.jsonl").write_text("{}\n")
    write_manifest(
        str(dataset),
        source_urls=[],
        license_note="test",
        extra={"last_success": {"nasdaqlisted": "2026-07-01", "sec_company_tickers": "2026-07-29"}},
    )
    code = main(["check-staleness", "--max-age-days", "2", str(dataset)])
    err = capsys.readouterr().err
    assert code == 1
    assert "nasdaqlisted" in err and "last succeeded 2026-07-01" in err


def test_staleness_gate_fresh_when_all_watermarks_current(tmp_path, capsys):
    import datetime as dt

    from stock_data.cli import main
    from stock_data.manifest import write_manifest
    from stock_data.symbols import SOURCES

    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    dataset = tmp_path / "symbols" / "current"
    dataset.mkdir(parents=True)
    (dataset / "x.jsonl").write_text("{}\n")
    write_manifest(
        str(dataset),
        source_urls=[],
        license_note="test",
        extra={"last_success": {source: today for source in SOURCES}},
    )
    code = main(["check-staleness", "--max-age-days", "2", str(dataset)])
    assert code == 0
    assert "FRESH" in capsys.readouterr().out


def test_staleness_gate_flags_source_that_never_succeeded(tmp_path, capsys):
    """A collector broken from day one has NO watermark; it must not stay green."""

    import datetime as dt

    from stock_data.cli import main
    from stock_data.manifest import write_manifest
    from stock_data.symbols import SOURCES

    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    watermarks = {source: today for source in SOURCES}
    del watermarks["otherlisted"]
    dataset = tmp_path / "symbols" / "current"
    dataset.mkdir(parents=True)
    (dataset / "x.jsonl").write_text("{}\n")
    write_manifest(
        str(dataset),
        source_urls=[],
        license_note="test",
        extra={"last_success": watermarks},
    )
    code = main(["check-staleness", "--max-age-days", "2", str(dataset)])
    err = capsys.readouterr().err
    assert code == 1
    assert "otherlisted" in err and "never succeeded" in err


def test_checkpoint_publishes_nothing_when_killed_before_renames(tmp_path, monkeypatch):
    """A kill while writing or hashing must leave the published dataset untouched
    and self-consistent (previously the events file was replaced first, leaving a
    stale manifest whose hash mismatch consumers hard-refuse)."""

    from stock_data import events, manifest
    from stock_data.manifest import read_manifest

    out_dir = tmp_path / "events"
    out_dir.mkdir()
    events_path = out_dir / "flagged_events.jsonl.gz"
    state_path = out_dir / ".progress.json"
    first = [
        {
            "cik": 1,
            "form": "25",
            "filing_date": "2026-01-01",
            "accession": "a1",
            "event": "delisting_or_deregistration",
            "items": None,
        }
    ]
    events._checkpoint(str(out_dir), str(events_path), str(state_path), first, "2026-W31", {1})
    published_events = events_path.read_bytes()
    published_state = state_path.read_bytes()

    def _boom(_path):
        raise RuntimeError("simulated kill during manifest hashing")

    monkeypatch.setattr(manifest, "_sha256", _boom)
    second = first + [dict(first[0], accession="a2", cik=2)]
    with pytest.raises(RuntimeError):
        events._checkpoint(
            str(out_dir), str(events_path), str(state_path), second, "2026-W31", {1, 2}
        )

    assert events_path.read_bytes() == published_events
    assert state_path.read_bytes() == published_state
    entry = read_manifest(str(out_dir))["files"][0]
    assert entry["name"] == "flagged_events.jsonl.gz"
    assert entry["sha256"] == hashlib.sha256(published_events).hexdigest()
