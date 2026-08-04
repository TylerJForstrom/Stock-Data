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
| Symbol directory snapshots | SEC `company_tickers*.json`, Nasdaq Trader symbol directories | daily (git-scraped) | `data/symbols/current/` |
| Listing/delisting/change events | Daily symbol-directory diffs | daily (git-scraped) | `data/symbols/events/` |
| Point-in-time membership intervals | Derived: the event stream replayed at the producer | daily (rebuilt from the above) | `data/symbols/pit/` |
| Corporate actions (dividends per share, splits) | Reconstructed from SEC XBRL companyfacts | on demand / quarterly | `data/corporate_actions/` |

The daily symbol-directory snapshot is the point-in-time universe archive: each
day's diff is a free listing/delisting/ticker-change event stream. Each event
retains its documented top-level JSON object in
`data/symbols/events/events.jsonl`, beside that dataset's own `manifest.json`.
The archive only
covers dates after the archiver started — which is why it runs first.

`data/symbols/pit/` is that archive made directly queryable: one interval
table per symbol-directory source (`{**record, valid_from, valid_to,
provable_from}`, half-open `valid_from <= date < valid_to`, `valid_to: null` =
still current), rebuilt daily by replaying the event stream once here at the
producer. Consumers answer "who was listed on date D" with a hash-verified
date-range lookup instead of carrying their own replay implementation. The
manifest's `reconstructable_from` is the honest floor: rows with
`provable_from: false` were already present at the archive boundary and their
true start date is unknowable from this data — consumers must refuse earlier
`asof` dates rather than treat today's survivors as history.

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
committed here. Restricted-license collection and storage belongs in the
private Stock-Vault repository; that data must never be committed here or
otherwise redistributed.

## Usage

```bash
pip install -e ".[dev]"
stock-data snapshot-symbols            # archive today's symbol directories + diff events
stock-data corporate-actions --tickers AAPL MSFT JNJ
```

Scheduled GitHub Actions run the daily symbol snapshot and weekly SEC-event
sweep. Before updating, they fail red when the prior snapshot is more than two
days old or the prior weekly artifact is more than eight days old. Collection
and commit steps still run after that alert so the archives self-heal. Every
daily run commits a heartbeat even when sources are unchanged, because GitHub
disables cron workflows after 60 days without commits.

## Fair access

All SEC requests go through one throttled session with a declared User-Agent,
per SEC's automated-access guidance. Bulk files are preferred over per-CIK
loops wherever possible.

## Disclaimer

This project archives and transforms public data. Nothing in it is investment
advice.
