"""Reconstruct corporate actions (dividends per share, splits) from SEC XBRL.

Verified properties of companyfacts this module is built around (probed live
2026-07-28 across 14 issuers; see docs in the Stock-Grader repo):

- Filers switch per-share dividend tags mid-history → tags must be UNIONed.
- Fiscal Q4 per-share dividends appear only inside the FY figure → derive
  Q4 = FY − (Q1+Q2+Q3).
- The same fiscal period appears with pre-split and post-split restated values
  across filings → every per-share fact must be normalized by the cumulative
  split factor keyed on its *filed* date. Split extraction is therefore a hard
  dependency of dividend extraction.
- Ex-dividend dates are not present (numeric facts only): output is
  fiscal-period-granular, on the current (fully split-adjusted) share basis.
- Split ratios are reliably tagged (StockholdersEquityNoteStockSplitConversionRatio1)
  but reverse-split encoding is filer-chosen (GE tagged 1-for-8 as 0.125), so
  each event's direction is cross-checked against the dei share-count series
  and inverted (with a flag) when the observed jump contradicts it.
- Cash-flow dividend facts in 10-Qs are year-to-date durations, so the
  aggregate fallback derives discrete quarters by differencing the YTD chain.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from dataclasses import dataclass, field

import pandas as pd

from .http import FairAccessSession, atomic_write_text
from .manifest import PUBLIC_DOMAIN_NOTE, write_manifest

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

# Priority order: cash-paid is the cleanest "what shareholders got" measure.
DPS_TAGS = (
    "CommonStockDividendsPerShareCashPaid",
    "CommonStockDividendsPerShareDeclared",
    "DividendsPayableAmountPerShare",
)
SPLIT_TAG = "StockholdersEquityNoteStockSplitConversionRatio1"
AGGREGATE_DIVIDEND_TAGS = ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends")

# Same-ratio facts chained within this window are one split event re-reported.
_SPLIT_CLUSTER_DAYS = 370
# Tolerated disagreement between restatements of the same period, after basis
# normalization (beyond it we keep the chosen record and flag).
_RESTATEMENT_TOLERANCE = 0.015
# Two periods describe the same stretch when they overlap by at least this many
# days (fiscal-calendar boundaries differ from cash-flow-calendar boundaries by
# a few days; adjacent quarters overlap by at most a few days).
_MATERIAL_OVERLAP_DAYS = 20


@dataclass
class SplitEvent:
    effective_date: dt.date  # best guess: earliest sighting's period end
    ratio: float  # post/pre share multiplier; <1 means reverse split
    confidence: str  # "high" | "low"
    filed: dt.date
    flags: list[str] = field(default_factory=list)


@dataclass
class DividendRecord:
    period_start: dt.date | None  # None for instant (payable-amount) facts
    period_end: dt.date
    span_type: str  # monthly | quarterly | semiannual | annual | instant | other
    dps: float  # current (fully split-adjusted) basis
    source_tag: str
    filed: dt.date
    derived: bool = False
    approximate: bool = False
    flags: list[str] = field(default_factory=list)

    def interval(self) -> tuple[dt.date, dt.date]:
        return (self.period_start or self.period_end, self.period_end)


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value)


def _iter_facts(facts: dict, tag: str, units: tuple[str, ...]) -> list[dict]:
    node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
    if not node:
        return []
    rows: list[dict] = []
    for unit_name, items in node.get("units", {}).items():
        if unit_name not in units:
            continue
        for item in items:
            row = dict(item)
            row["_unit"] = unit_name
            rows.append(row)
    return rows


def _shares_series(facts: dict) -> list[tuple[dt.date, float]]:
    items = (
        facts.get("facts", {})
        .get("dei", {})
        .get("EntityCommonStockSharesOutstanding", {})
        .get("units", {})
        .get("shares", [])
    )
    series = []
    for item in items:
        end = _parse_date(item.get("end"))
        val = item.get("val")
        if end and val:
            series.append((end, float(val)))
    series.sort()
    return series


def _shares_near(
    series: list[tuple[dt.date, float]], target: dt.date, max_days: int = 180
) -> tuple[dt.date, float] | None:
    best: tuple[int, dt.date, float] | None = None
    for end, val in series:
        distance = abs((end - target).days)
        if distance <= max_days and (best is None or distance < best[0]):
            best = (distance, end, val)
    return (best[1], best[2]) if best else None


def classify_span(start: dt.date | None, end: dt.date) -> str:
    if start is None:
        return "instant"
    days = (end - start).days
    if 25 <= days <= 35:
        return "monthly"
    if 70 <= days <= 100:
        return "quarterly"
    if 150 <= days <= 200:
        return "semiannual"
    if 330 <= days <= 400:
        return "annual"
    return "other"


def _overlap_days(a: tuple[dt.date, dt.date], b: tuple[dt.date, dt.date]) -> int:
    start = max(a[0], b[0])
    end = min(a[1], b[1])
    return max(0, (end - start).days + 1)


def extract_splits(facts: dict) -> list[SplitEvent]:
    """Cluster repeated split facts into events; check direction vs share counts.

    Clustering chains: a candidate joins an event when it falls within the
    window of the event's *latest* member (re-tagged comparatives can stretch
    beyond one window from the first sighting), while two genuinely distinct
    same-ratio splits years apart stay separate.
    """
    raw = _iter_facts(facts, SPLIT_TAG, units=("pure",))
    candidates: list[tuple[float, dt.date, dt.date, str]] = []
    for item in raw:
        val = item.get("val")
        end = _parse_date(item.get("end"))
        filed = _parse_date(item.get("filed"))
        if not val or val <= 0 or end is None or filed is None or val == 1:
            continue
        start = _parse_date(item.get("start"))
        # Duration-typed facts locate the event only within the period.
        confidence = "high" if (start is None or (end - start).days <= 45) else "low"
        if val > 100 or val < 0.005:
            confidence = "low"
        candidates.append((float(val), end, filed, confidence))

    clusters: list[dict] = []
    for val, end, filed, confidence in sorted(candidates, key=lambda c: c[1]):
        merged = False
        for cluster in clusters:
            if (
                abs(cluster["ratio"] - val) < 1e-9
                and (end - cluster["max_end"]).days <= _SPLIT_CLUSTER_DAYS
            ):
                cluster["max_end"] = max(cluster["max_end"], end)
                cluster["min_end"] = min(cluster["min_end"], end)
                cluster["filed"] = min(cluster["filed"], filed)
                if confidence == "high":
                    cluster["confidence"] = "high"
                merged = True
                break
        if not merged:
            clusters.append(
                {
                    "ratio": val,
                    "min_end": end,
                    "max_end": end,
                    "filed": filed,
                    "confidence": confidence,
                }
            )

    shares = _shares_series(facts)
    events = []
    for cluster in sorted(clusters, key=lambda c: c["min_end"]):
        event = SplitEvent(
            effective_date=cluster["min_end"],
            ratio=cluster["ratio"],
            confidence=cluster["confidence"],
            filed=cluster["filed"],
        )
        _check_direction(event, shares)
        events.append(event)
    return events


def _check_direction(event: SplitEvent, shares: list[tuple[dt.date, float]]) -> None:
    """Reverse-split encoding is filer-chosen: verify against the share-count jump."""
    before = _shares_near(
        [s for s in shares if s[0] <= event.effective_date - dt.timedelta(days=10)],
        event.effective_date,
        max_days=270,
    )
    after = _shares_near(
        [s for s in shares if s[0] >= event.effective_date + dt.timedelta(days=10)],
        event.effective_date,
        max_days=270,
    )
    if not before or not after or before[1] <= 0 or after[1] <= 0:
        return
    observed = after[1] / before[1]
    as_tagged = abs(math.log(observed / event.ratio))
    inverted = abs(math.log(observed * event.ratio))
    if inverted < as_tagged - math.log(2):  # decisively closer to the inverse
        event.ratio = 1.0 / event.ratio
        event.flags.append("direction_inverted_by_share_check")


def cumulative_split_factor(splits: list[SplitEvent], basis_date: dt.date) -> float:
    """Multiplier converting a per-share value on ``basis_date``'s share basis
    to the current basis: the product of every later split's ratio."""
    factor = 1.0
    for event in splits:
        if event.effective_date > basis_date:
            factor *= event.ratio
    return factor


def extract_dividends(facts: dict, splits: list[SplitEvent]) -> list[DividendRecord]:
    by_period: dict[tuple[dt.date | None, dt.date], list[DividendRecord]] = {}
    for tag in DPS_TAGS:
        for item in _iter_facts(facts, tag, units=("USD/shares", "pure")):
            val = item.get("val")
            end = _parse_date(item.get("end"))
            filed = _parse_date(item.get("filed"))
            if val is None or val < 0 or end is None or filed is None:
                continue
            start = _parse_date(item.get("start"))
            flags = []
            if item["_unit"] == "pure":
                flags.append("suspect_unit")
            factor = cumulative_split_factor(splits, filed)
            record = DividendRecord(
                period_start=start,
                period_end=end,
                span_type=classify_span(start, end),
                dps=float(val) / factor,
                source_tag=tag,
                filed=filed,
                flags=flags,
            )
            by_period.setdefault((start, end), []).append(record)

    deduped: list[DividendRecord] = []
    for records in by_period.values():
        # Tag priority first (cash-paid beats declared beats payable), then the
        # latest filing within the winning tag (restatements supersede).
        best_tag = min(DPS_TAGS.index(r.source_tag) for r in records)
        in_tag = [r for r in records if DPS_TAGS.index(r.source_tag) == best_tag]
        in_tag.sort(key=lambda r: r.filed)
        chosen = in_tag[-1]
        values = [r.dps for r in records]
        if max(values) > 0 and (max(values) - min(values)) / max(values) > _RESTATEMENT_TOLERANCE:
            chosen.flags.append("restatement_disagreement")
        deduped.append(chosen)

    deduped.extend(_derive_missing_quarters(deduped))
    deduped.sort(key=lambda r: (r.period_end, r.period_start or r.period_end))
    return deduped


def _derive_missing_quarters(records: list[DividendRecord]) -> list[DividendRecord]:
    """Q4 = FY − (Q1+Q2+Q3), verified exact on JNJ/PG/JPM for 17/17 years."""
    derived: list[DividendRecord] = []
    seen_annual_ends: list[dt.date] = []
    quarters = [r for r in records if r.span_type == "quarterly"]
    sub_quarterly = [r for r in records if r.span_type in ("monthly", "instant")]
    for annual in sorted(
        (r for r in records if r.span_type == "annual"), key=lambda r: (r.period_end, r.filed)
    ):
        if annual.period_start is None:
            continue
        # Overlapping annual records for the same fiscal year (different tags,
        # 52/53-week vs calendar conventions) must not each derive a Q4.
        if any(abs((annual.period_end - end).days) <= 10 for end in seen_annual_ends):
            continue
        inside = [
            q
            for q in quarters
            if q.period_start is not None
            and q.period_start >= annual.period_start - dt.timedelta(days=7)
            and q.period_end <= annual.period_end + dt.timedelta(days=7)
        ]
        if len(inside) != 3:
            continue
        gap_start = max(q.period_end for q in inside)
        gap_days = (annual.period_end - gap_start).days
        if not 60 <= gap_days <= 130:
            continue  # the uncovered stretch is not a clean trailing quarter
        # Monthly payers (REIT-style) and instant payable facts already cover
        # parts of the gap; deriving a Q4 on top would double-count.
        if any(
            gap_start < r.period_end <= annual.period_end + dt.timedelta(days=7)
            for r in sub_quarterly
        ):
            continue
        value = annual.dps - sum(q.dps for q in inside)
        flags = ["derived_q4"]
        if value < -0.005:
            continue  # inconsistent inputs; do not fabricate
        if value < 0:
            value, flags = 0.0, [*flags, "clamped_negative"]
        seen_annual_ends.append(annual.period_end)
        derived.append(
            DividendRecord(
                period_start=gap_start + dt.timedelta(days=1),
                period_end=annual.period_end,
                span_type="quarterly",
                dps=value,
                source_tag=annual.source_tag,
                filed=annual.filed,
                derived=True,
                flags=flags,
            )
        )
    return derived


def extract_aggregate_fallback(facts: dict, splits: list[SplitEvent]) -> list[DividendRecord]:
    """Approximate DPS = dividends paid ÷ shares outstanding.

    Cash-flow dividend facts in 10-Qs are year-to-date durations, so discrete
    quarters are derived by differencing the YTD chain within each fiscal year.
    The split factor is keyed on the matched *shares fact's* date: total cash
    paid is split-invariant; only the share count carries a basis.
    """
    shares = _shares_series(facts)
    if not shares:
        return []

    for tag in AGGREGATE_DIVIDEND_TAGS:
        by_period: dict[tuple[dt.date, dt.date], dict] = {}
        for item in _iter_facts(facts, tag, units=("USD",)):
            start = _parse_date(item.get("start"))
            end = _parse_date(item.get("end"))
            filed = _parse_date(item.get("filed"))
            val = item.get("val")
            if val is None or val < 0 or start is None or end is None or filed is None:
                continue
            key = (start, end)
            # Re-reported comparatives duplicate periods: latest filed wins.
            if key not in by_period or filed > by_period[key]["filed"]:
                by_period[key] = {"start": start, "end": end, "val": float(val), "filed": filed}
        if not by_period:
            continue

        # Group the YTD chain by shared fiscal-year start (few days tolerance).
        groups: list[list[dict]] = []
        for fact in sorted(by_period.values(), key=lambda f: (f["start"], f["end"])):
            placed = False
            for group in groups:
                if abs((fact["start"] - group[0]["start"]).days) <= 5:
                    group.append(fact)
                    placed = True
                    break
            if not placed:
                groups.append([fact])

        records: list[DividendRecord] = []
        for group in groups:
            group.sort(key=lambda f: f["end"])
            previous_end: dt.date | None = None
            previous_val = 0.0
            for fact in group:
                if previous_end is None:
                    period_start, value = fact["start"], fact["val"]
                else:
                    period_start = previous_end + dt.timedelta(days=1)
                    value = fact["val"] - previous_val
                previous_end, previous_val = fact["end"], fact["val"]
                if value < -0.005 * max(1.0, abs(previous_val)):
                    continue  # broken YTD chain; skip rather than fabricate
                span = classify_span(period_start, fact["end"])
                if span not in ("monthly", "quarterly", "semiannual"):
                    continue
                matched = _shares_near(shares, fact["end"])
                if not matched:
                    continue
                shares_date, share_count = matched
                factor = cumulative_split_factor(splits, shares_date)
                records.append(
                    DividendRecord(
                        period_start=period_start,
                        period_end=fact["end"],
                        span_type=span,
                        dps=(max(value, 0.0) / share_count) / factor,
                        source_tag=tag,
                        filed=fact["filed"],
                        approximate=True,
                        flags=["aggregate_fallback"],
                    )
                )
        if records:
            return records
    return []


def extract_for_company(facts: dict) -> tuple[list[DividendRecord], list[SplitEvent]]:
    """Per-share records are authoritative; the aggregate fallback only fills
    periods they do not already cover (any span type, boundary-tolerant)."""
    splits = extract_splits(facts)
    dividends = extract_dividends(facts, splits)
    covered = [r.interval() for r in dividends]
    for candidate in extract_aggregate_fallback(facts, splits):
        if any(_overlap_days(candidate.interval(), c) >= _MATERIAL_OVERLAP_DAYS for c in covered):
            continue
        dividends.append(candidate)
    dividends.sort(key=lambda r: (r.period_end, r.period_start or r.period_end))
    return dividends, splits


def resolve_ciks(session: FairAccessSession, tickers: list[str]) -> dict[str, int]:
    payload = json.loads(session.get(TICKER_MAP_URL).text)
    lookup = {row["ticker"].upper(): int(row["cik_str"]) for row in payload.values()}
    resolved = {}
    for ticker in tickers:
        normalized = ticker.upper().replace(".", "-")
        cik = lookup.get(ticker.upper()) or lookup.get(normalized)
        if cik:
            resolved[ticker.upper()] = cik
    return resolved


def run(data_dir: str, tickers: list[str], session: FairAccessSession | None = None) -> str:
    """Fetch companyfacts per ticker, extract, write parquet + jsonl + manifest."""
    session = session or FairAccessSession()
    out_dir = os.path.join(data_dir, "corporate_actions")
    os.makedirs(out_dir, exist_ok=True)
    cik_map = resolve_ciks(session, tickers)

    dividend_rows: list[dict[str, object]] = []
    split_lines: list[str] = []
    unresolved = [t for t in tickers if t.upper() not in cik_map]
    for ticker, cik in sorted(cik_map.items()):
        facts = json.loads(session.get(COMPANYFACTS_URL.format(cik=cik)).text)
        dividends, splits = extract_for_company(facts)
        for record in dividends:
            dividend_rows.append(
                {
                    "ticker": ticker,
                    "cik": cik,
                    "period_start": record.period_start,
                    "period_end": record.period_end,
                    "span_type": record.span_type,
                    "dps_current_basis": record.dps,
                    "source_tag": record.source_tag,
                    "filed": record.filed,
                    "derived": record.derived,
                    "approximate": record.approximate,
                    "flags": ",".join(record.flags),
                }
            )
        for event in splits:
            split_lines.append(
                json.dumps(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "effective_date": event.effective_date.isoformat(),
                        "ratio": event.ratio,
                        "confidence": event.confidence,
                        "filed": event.filed.isoformat(),
                        "flags": event.flags,
                    },
                    sort_keys=True,
                )
            )

    dividends_path = os.path.join(out_dir, "dividends.parquet")
    pd.DataFrame(dividend_rows).to_parquet(dividends_path, index=False)
    atomic_write_text(
        os.path.join(out_dir, "splits.jsonl"),
        "\n".join(split_lines) + ("\n" if split_lines else ""),
    )
    write_manifest(
        out_dir,
        source_urls=[TICKER_MAP_URL, COMPANYFACTS_URL.format(cik=0)],
        license_note=PUBLIC_DOMAIN_NOTE,
        row_counts={"dividends.parquet": len(dividend_rows)},
        extra={
            "basis": "current_fully_split_adjusted",
            "granularity": "fiscal_period (no ex-dates in XBRL)",
            "tickers_requested": sorted(t.upper() for t in tickers),
            "tickers_unresolved": sorted(t.upper() for t in unresolved),
        },
    )
    return out_dir
