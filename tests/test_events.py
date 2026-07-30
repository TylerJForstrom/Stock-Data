"""Event-extraction tests against the live submissions JSON shape."""

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

    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    dataset = tmp_path / "symbols" / "current"
    dataset.mkdir(parents=True)
    (dataset / "x.jsonl").write_text("{}\n")
    write_manifest(
        str(dataset), source_urls=[], license_note="test",
        extra={"last_success": {"nasdaqlisted": today}},
    )
    code = main(["check-staleness", "--max-age-days", "2", str(dataset)])
    assert code == 0
    assert "FRESH" in capsys.readouterr().out
