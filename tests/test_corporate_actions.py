"""Unit tests for the XBRL corporate-actions reconstruction.

Fixtures mirror the real companyfacts JSON shapes observed in live probes
(AAPL splits and pre/post-split restated dividends, JNJ-style Q4 derivation).
"""

import datetime as dt

from stock_data.corporate_actions import (
    SplitEvent,
    classify_span,
    cumulative_split_factor,
    extract_aggregate_fallback,
    extract_dividends,
    extract_for_company,
    extract_splits,
)


def facts_shell(us_gaap: dict, dei: dict | None = None) -> dict:
    return {"facts": {"us-gaap": us_gaap, "dei": dei or {}}}


def dps_fact(start: str, end: str, val: float, filed: str) -> dict:
    return {"start": start, "end": end, "val": val, "filed": filed, "form": "10-K"}


def test_split_extraction_dedupes_repeated_filings():
    us_gaap = {
        "StockholdersEquityNoteStockSplitConversionRatio1": {
            "units": {
                "pure": [
                    {"end": "2014-06-06", "val": 7, "filed": "2014-07-23"},
                    {"end": "2014-06-06", "val": 7, "filed": "2014-10-27"},
                    {"end": "2020-08-28", "val": 4, "filed": "2020-10-30"},
                ]
            }
        }
    }
    events = extract_splits(facts_shell(us_gaap))
    assert [(e.effective_date.isoformat(), e.ratio) for e in events] == [
        ("2014-06-06", 7.0),
        ("2020-08-28", 4.0),
    ]


def test_reverse_split_kept_verbatim():
    us_gaap = {
        "StockholdersEquityNoteStockSplitConversionRatio1": {
            "units": {"pure": [{"end": "2021-08-02", "val": 0.125, "filed": "2021-10-26"}]}
        }
    }
    events = extract_splits(facts_shell(us_gaap))
    assert events[0].ratio == 0.125  # GE's 1-for-8, encoded as post/pre multiplier


def test_cumulative_split_factor_applies_only_later_splits():
    splits = [
        SplitEvent(dt.date(2014, 6, 6), 7.0, "high", dt.date(2014, 7, 23)),
        SplitEvent(dt.date(2020, 8, 28), 4.0, "high", dt.date(2020, 10, 30)),
    ]
    assert cumulative_split_factor(splits, dt.date(2014, 4, 24)) == 28.0
    assert cumulative_split_factor(splits, dt.date(2015, 1, 1)) == 4.0
    assert cumulative_split_factor(splits, dt.date(2021, 1, 1)) == 1.0


def test_pre_and_post_split_restatements_agree_after_normalization():
    # AAPL FQ2-2014 observed as 3.05 (filed pre-7:1) and 0.44 (filed post-7:1,
    # pre-4:1). Both should normalize to ~0.109 on the current basis and the
    # latest-filed record should win without a disagreement flag.
    us_gaap = {
        "CommonStockDividendsPerShareDeclared": {
            "units": {
                "USD/shares": [
                    dps_fact("2013-12-29", "2014-03-29", 3.05, "2014-04-24"),
                    dps_fact("2013-12-29", "2014-03-29", 0.44, "2014-07-23"),
                ]
            }
        }
    }
    splits = extract_splits(
        facts_shell(
            {
                "StockholdersEquityNoteStockSplitConversionRatio1": {
                    "units": {
                        "pure": [
                            {"end": "2014-06-06", "val": 7, "filed": "2014-07-23"},
                            {"end": "2020-08-28", "val": 4, "filed": "2020-10-30"},
                        ]
                    }
                }
            }
        )
    )
    records = extract_dividends(facts_shell(us_gaap), splits)
    assert len(records) == 1
    assert abs(records[0].dps - 0.44 / 4.0) < 1e-9
    assert "restatement_disagreement" not in records[0].flags


def test_q4_derivation_exact():
    # JNJ-style: three tagged quarters at 0.5 plus FY total 2.2 -> derived Q4 0.7.
    us_gaap = {
        "CommonStockDividendsPerShareCashPaid": {
            "units": {
                "USD/shares": [
                    dps_fact("2023-01-01", "2023-04-02", 0.5, "2023-05-01"),
                    dps_fact("2023-04-03", "2023-07-02", 0.5, "2023-08-01"),
                    dps_fact("2023-07-03", "2023-10-01", 0.5, "2023-11-01"),
                    dps_fact("2023-01-01", "2023-12-31", 2.2, "2024-02-15"),
                ]
            }
        }
    }
    records = extract_dividends(facts_shell(us_gaap), splits=[])
    derived = [r for r in records if r.derived]
    assert len(derived) == 1
    assert abs(derived[0].dps - 0.7) < 1e-9
    assert derived[0].span_type == "quarterly"
    assert derived[0].period_end == dt.date(2023, 12, 31)
    assert derived[0].period_start == dt.date(2023, 10, 2)  # no overlap with Q3


def test_q4_not_fabricated_when_inputs_inconsistent():
    us_gaap = {
        "CommonStockDividendsPerShareCashPaid": {
            "units": {
                "USD/shares": [
                    dps_fact("2023-01-01", "2023-04-02", 1.0, "2023-05-01"),
                    dps_fact("2023-04-03", "2023-07-02", 1.0, "2023-08-01"),
                    dps_fact("2023-07-03", "2023-10-01", 1.0, "2023-11-01"),
                    dps_fact("2023-01-01", "2023-12-31", 2.0, "2024-02-15"),  # < Q1+Q2+Q3
                ]
            }
        }
    }
    records = extract_dividends(facts_shell(us_gaap), splits=[])
    assert not [r for r in records if r.derived]


def test_aggregate_fallback_divides_by_nearby_shares():
    us_gaap = {
        "PaymentsOfDividendsCommonStock": {
            "units": {"USD": [dps_fact("2023-01-01", "2023-04-01", 5_000_000, "2023-05-01")]}
        }
    }
    dei = {
        "EntityCommonStockSharesOutstanding": {
            "units": {"shares": [{"end": "2023-04-15", "val": 10_000_000, "filed": "2023-05-01"}]}
        }
    }
    records = extract_aggregate_fallback(facts_shell(us_gaap, dei), splits=[])
    assert len(records) == 1
    assert abs(records[0].dps - 0.5) < 1e-9
    assert records[0].approximate


def test_fallback_engages_only_when_per_share_coverage_thin():
    us_gaap = {
        "CommonStockDividendsPerShareCashPaid": {
            "units": {
                "USD/shares": [
                    dps_fact("2023-01-01", "2023-04-02", 0.5, "2023-05-01"),
                    dps_fact("2023-04-03", "2023-07-02", 0.5, "2023-08-01"),
                    dps_fact("2023-07-03", "2023-10-01", 0.5, "2023-11-01"),
                    dps_fact("2022-01-01", "2022-04-02", 0.4, "2022-05-01"),
                ]
            }
        },
        "PaymentsOfDividendsCommonStock": {
            "units": {"USD": [dps_fact("2023-01-01", "2023-04-01", 5_000_000, "2023-05-01")]}
        },
    }
    dei = {
        "EntityCommonStockSharesOutstanding": {
            "units": {"shares": [{"end": "2023-04-15", "val": 10_000_000, "filed": "2023-05-01"}]}
        }
    }
    dividends, _ = extract_for_company(facts_shell(us_gaap, dei))
    assert not [r for r in dividends if r.approximate]


def test_aggregate_fallback_dedupes_rereported_facts():
    # The same Q1 cash-flow fact appears in the Q1 10-Q and again as the
    # comparative column of next year's 10-Q; only one record may result.
    us_gaap = {
        "PaymentsOfDividendsCommonStock": {
            "units": {
                "USD": [
                    dps_fact("2023-01-01", "2023-04-01", 5_000_000, "2023-05-01"),
                    dps_fact("2023-01-01", "2023-04-01", 5_000_000, "2024-05-01"),
                ]
            }
        }
    }
    dei = {
        "EntityCommonStockSharesOutstanding": {
            "units": {"shares": [{"end": "2023-04-15", "val": 10_000_000, "filed": "2023-05-01"}]}
        }
    }
    records = extract_aggregate_fallback(facts_shell(us_gaap, dei), splits=[])
    assert len(records) == 1


def test_aggregate_fallback_differences_ytd_chain():
    # Standard 10-Q YTD reporting: 3/6/9-month facts plus the FY figure must
    # yield four discrete quarters via differencing.
    us_gaap = {
        "PaymentsOfDividendsCommonStock": {
            "units": {
                "USD": [
                    dps_fact("2023-01-01", "2023-04-01", 5_000_000, "2023-05-01"),
                    dps_fact("2023-01-01", "2023-07-01", 11_000_000, "2023-08-01"),
                    dps_fact("2023-01-01", "2023-09-30", 18_000_000, "2023-11-01"),
                    dps_fact("2023-01-01", "2023-12-31", 26_000_000, "2024-02-15"),
                ]
            }
        }
    }
    dei = {
        "EntityCommonStockSharesOutstanding": {
            "units": {
                "shares": [
                    {"end": "2023-04-15", "val": 10_000_000, "filed": "2023-05-01"},
                    {"end": "2023-07-15", "val": 10_000_000, "filed": "2023-08-01"},
                    {"end": "2023-10-15", "val": 10_000_000, "filed": "2023-11-01"},
                    {"end": "2024-01-15", "val": 10_000_000, "filed": "2024-02-15"},
                ]
            }
        }
    }
    records = extract_aggregate_fallback(facts_shell(us_gaap, dei), splits=[])
    assert [round(r.dps, 3) for r in records] == [0.5, 0.6, 0.7, 0.8]
    assert all(r.span_type == "quarterly" for r in records)


def test_aggregate_fallback_split_factor_keyed_on_shares_date():
    # Reverse split 1-for-8 between the quarter end and the matched share
    # count: the post-split share count is already on the current basis, so no
    # further adjustment may be applied (the bug divided by 0.125 -> 8x DPS).
    from stock_data.corporate_actions import SplitEvent as SE

    splits = [SE(dt.date(2024, 5, 15), 0.125, "high", dt.date(2024, 6, 1))]
    us_gaap = {
        "PaymentsOfDividendsCommonStock": {
            "units": {"USD": [dps_fact("2024-01-01", "2024-03-31", 1_000_000, "2024-05-01")]}
        }
    }
    dei = {
        "EntityCommonStockSharesOutstanding": {
            "units": {"shares": [{"end": "2024-06-30", "val": 1_000_000, "filed": "2024-07-30"}]}
        }
    }
    records = extract_aggregate_fallback(facts_shell(us_gaap, dei), splits=splits)
    assert len(records) == 1
    assert abs(records[0].dps - 1.0) < 1e-9  # post-split shares: current basis already


def test_fallback_never_stacks_on_semiannual_per_share_records():
    # Foreign-private-issuer style: semiannual per-share tags plus quarterly
    # cash-flow facts. The fallback must not add quarters inside covered halves.
    us_gaap = {
        "CommonStockDividendsPerShareCashPaid": {
            "units": {
                "USD/shares": [
                    dps_fact("2023-01-01", "2023-06-30", 1.0, "2023-08-01"),
                    dps_fact("2023-07-01", "2023-12-31", 1.0, "2024-02-15"),
                ]
            }
        },
        "PaymentsOfDividendsCommonStock": {
            "units": {
                "USD": [
                    dps_fact("2023-01-01", "2023-03-31", 5_000_000, "2023-05-01"),
                    dps_fact("2023-01-01", "2023-06-30", 10_000_000, "2023-08-01"),
                ]
            }
        },
    }
    dei = {
        "EntityCommonStockSharesOutstanding": {
            "units": {"shares": [{"end": "2023-04-15", "val": 10_000_000, "filed": "2023-05-01"}]}
        }
    }
    dividends, _ = extract_for_company(facts_shell(us_gaap, dei))
    assert not [r for r in dividends if r.approximate]
    assert abs(sum(r.dps for r in dividends) - 2.0) < 1e-9


def test_split_chain_clustering_and_distinct_events():
    # The same 3:1 split re-tagged in comparatives chains beyond one window of
    # the first sighting and must still merge into one event, while a genuine
    # second 3:1 split years later must stay distinct.
    us_gaap = {
        "StockholdersEquityNoteStockSplitConversionRatio1": {
            "units": {
                "pure": [
                    {"end": "2020-06-01", "val": 3, "filed": "2020-07-01"},
                    {"end": "2021-01-04", "val": 3, "filed": "2021-02-01"},  # within window
                    {"end": "2021-07-05", "val": 3, "filed": "2021-08-01"},  # chained via above
                    {"end": "2024-06-01", "val": 3, "filed": "2024-07-01"},  # distinct
                ]
            }
        }
    }
    events = extract_splits(facts_shell(us_gaap))
    assert [e.effective_date.year for e in events] == [2020, 2024]


def test_reverse_split_direction_corrected_by_share_check():
    # Filer tags a 1-for-8 reverse split as "8"; the share count collapses 8x,
    # so the ratio must be inverted and flagged.
    us_gaap = {
        "StockholdersEquityNoteStockSplitConversionRatio1": {
            "units": {"pure": [{"end": "2024-05-15", "val": 8, "filed": "2024-06-01"}]}
        }
    }
    dei = {
        "EntityCommonStockSharesOutstanding": {
            "units": {
                "shares": [
                    {"end": "2024-03-31", "val": 80_000_000, "filed": "2024-04-30"},
                    {"end": "2024-06-30", "val": 10_000_000, "filed": "2024-07-30"},
                ]
            }
        }
    }
    events = extract_splits(facts_shell(us_gaap, dei))
    assert len(events) == 1
    assert abs(events[0].ratio - 0.125) < 1e-9
    assert "direction_inverted_by_share_check" in events[0].flags


def test_overlapping_annual_records_derive_only_one_q4():
    # Two annual records for the same fiscal year (different tags surviving
    # period-key dedupe because boundaries differ) must not each derive a Q4.
    quarters = [
        dps_fact("2023-01-01", "2023-04-02", 0.5, "2023-05-01"),
        dps_fact("2023-04-03", "2023-07-02", 0.5, "2023-08-01"),
        dps_fact("2023-07-03", "2023-10-01", 0.5, "2023-11-01"),
    ]
    us_gaap = {
        "CommonStockDividendsPerShareCashPaid": {
            "units": {
                "USD/shares": [*quarters, dps_fact("2023-01-01", "2023-12-31", 2.2, "2024-02-15")]
            }
        },
        "CommonStockDividendsPerShareDeclared": {
            "units": {"USD/shares": [dps_fact("2023-01-02", "2023-12-30", 2.2, "2024-02-15")]}
        },
    }
    records = extract_dividends(facts_shell(us_gaap), splits=[])
    assert len([r for r in records if r.derived]) == 1


def test_q4_not_derived_when_monthly_records_cover_gap():
    # REIT-style monthly payer: monthlies inside the trailing gap mean deriving
    # a Q4 on top would double-count.
    us_gaap = {
        "CommonStockDividendsPerShareCashPaid": {
            "units": {
                "USD/shares": [
                    dps_fact("2023-01-01", "2023-04-02", 0.5, "2023-05-01"),
                    dps_fact("2023-04-03", "2023-07-02", 0.5, "2023-08-01"),
                    dps_fact("2023-07-03", "2023-10-01", 0.5, "2023-11-01"),
                    dps_fact("2023-10-02", "2023-11-01", 0.2, "2023-12-01"),
                    dps_fact("2023-01-01", "2023-12-31", 2.2, "2024-02-15"),
                ]
            }
        }
    }
    records = extract_dividends(facts_shell(us_gaap), splits=[])
    assert not [r for r in records if r.derived]


def test_classify_span():
    assert classify_span(dt.date(2023, 1, 1), dt.date(2023, 4, 1)) == "quarterly"
    assert classify_span(dt.date(2023, 1, 1), dt.date(2023, 12, 31)) == "annual"
    assert classify_span(dt.date(2023, 1, 1), dt.date(2023, 1, 29)) == "monthly"
    assert classify_span(None, dt.date(2023, 1, 29)) == "instant"
