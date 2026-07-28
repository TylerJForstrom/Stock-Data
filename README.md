# Stock-Data

A **data foundry**: this repository archives and derives equity datasets from
free primary sources, aiming for industry-standard quality through point-in-time
discipline, full provenance, and honest limitation labeling — rather than
through paid feeds.

It is the companion data layer for
[Stock-Grader](https://github.com/TylerJForstrom/Stock-Grader); every dataset
ships with a `manifest.json` (schema version, source URLs, fetch timestamp,
sha256, row counts, license note) so downstream consumers can verify what they
are reading.

## Datasets

| Dataset | Source | Cadence | Location |
|---|---|---|---|
| Symbol directories + listing/delisting events | SEC `company_tickers*.json`, Nasdaq Trader symbol directories | daily (git-scraped) | `data/symbols/` |
| Corporate actions (dividends per share, splits) | Reconstructed from SEC XBRL companyfacts | on demand / quarterly | `data/corporate_actions/` |
| FINRA short interest | FINRA bi-monthly files | bi-monthly | `vault/` (local only, never committed) |

The daily symbol-directory snapshot is the point-in-time universe archive: each
day's diff is a free listing/delisting/ticker-change event stream. It only
covers dates after the archiver started — which is why it runs first.

### Corporate-actions reconstruction (how and why it works)

SEC XBRL companyfacts carries per-share dividend tags
(`CommonStockDividendsPerShare{Declared,CashPaid}`, `DividendsPayableAmountPerShare`)
and the split tag (`StockholdersEquityNoteStockSplitConversionRatio1`).
Verified properties this pipeline relies on:

- Filers switch dividend tags mid-history, so tags are UNIONed.
- Fiscal Q4 is systematically absent and is derived as FY − (Q1+Q2+Q3).
- The same fiscal period appears with pre-split and post-split restated values
  across filings, so every per-share fact is normalized by the cumulative split
  factor keyed on its `filed` date — split extraction is a hard dependency of
  dividend extraction, not an optional extra.
- Ex-dividend dates are NOT in XBRL (numeric facts only). The reconstructed
  series is fiscal-period-granular: suitable for quarterly total-return work
  and shareholder-yield metrics, NOT for daily benchmark-grade total returns.

## Licensing split (important)

This is a public repository. Only US-government public-domain data (SEC,
Treasury, exchange reference directories) and datasets computed from it are
committed here. **Restricted-license data — FINRA short interest, Tiingo/Stooq
price caches — is written only to `vault/`, which is gitignored and must never
be committed or otherwise redistributed.**

## Usage

```bash
pip install -e ".[dev]"
stock-data snapshot-symbols            # archive today's symbol directories + diff events
stock-data corporate-actions --tickers AAPL MSFT JNJ
stock-data finra-short-interest --since 2026-01-01   # writes to vault/ only
```

Scheduled GitHub Actions run the daily snapshot (see
`.github/workflows/daily-snapshot.yml`). Every scheduled run commits a
heartbeat even when sources are unchanged, because GitHub disables cron
workflows after 60 days without commits.

## Fair access

All SEC requests go through one throttled session with a declared User-Agent,
per SEC's automated-access guidance. Bulk files are preferred over per-CIK
loops wherever possible.

## Disclaimer

This project archives and transforms public data. Nothing in it is investment
advice.
