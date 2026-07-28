"""Tests for FINRA settlement-date candidates and the vault watermark."""

import datetime as dt

from stock_data.finra import _watermark, candidate_settlement_dates


def test_candidate_dates_are_mid_and_end_of_month_business_days():
    dates = candidate_settlement_dates(dt.date(2026, 6, 1), dt.date(2026, 7, 20))
    assert dt.date(2026, 6, 15) in dates  # Monday
    assert dt.date(2026, 6, 30) in dates  # Tuesday
    assert dt.date(2026, 7, 15) in dates  # Wednesday
    assert all(d.weekday() < 5 for d in dates)
    assert all(dt.date(2026, 6, 1) <= d <= dt.date(2026, 7, 20) for d in dates)


def test_watermark_reads_newest_settlement_file(tmp_path):
    out = tmp_path / "finra_short_interest"
    out.mkdir()
    (out / "shrt20260615.csv").write_text("x")
    (out / "shrt20260715.csv").write_text("x")
    (out / "LICENSE_NOTE.json").write_text("{}")
    assert _watermark(str(out)) == dt.date(2026, 7, 15)


def test_watermark_empty_dir(tmp_path):
    assert _watermark(str(tmp_path / "missing")) is None
