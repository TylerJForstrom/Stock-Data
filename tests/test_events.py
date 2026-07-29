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
