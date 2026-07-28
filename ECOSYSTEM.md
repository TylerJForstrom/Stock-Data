# The Ecosystem Contract

Canonical copy lives in **Stock-Data** (the hub every project reads from).
Other repos carry a pointer to this file. Any agent (human or AI) working in
any member repo reads this before cross-project work.

## Roles

| Project | Role | Talks to |
|---|---|---|
| **Stock-Data** | Data foundry: the ONLY project that fetches external sources. Archives point-in-time snapshots, reconstructs corporate actions, publishes versioned datasets. | Sources → publishes artifacts |
| **Stock-Grader** | System of record for grading methodology. Consumes foundry artifacts, produces score panels and research dossiers. Home of the backtest evaluator and its attestations. | Foundry artifacts → publishes reports/panels |
| **TickerPulse** | Sentiment/attention signal source. Its derived per-ticker daily metrics are archived by the foundry (raw social content is not). | Sources → foundry archives its metrics |
| **Stock Market Simulation** | Forward/execution layer (paper trading behind risk gates) AND test oracle: generates tapes with known injected signal/null to calibrate the backtest harness's power and false-positive rate. It does not validate alpha. | Consumes panels; feeds test tapes to backtest |
| **Stock-Rater** | Decision pending: archive, or revive as presentation layer rendering Stock-Grader output. Not a second grading engine. | — |

## Rules

1. **Artifacts, not imports.** Projects integrate only through published
   datasets carrying `manifest.json` with `schema_version`, source URLs,
   sha256s, row counts, and a license note. Consumers refuse unknown schema
   versions. No repo imports another repo's code.
2. **One-direction DAG.** sources → foundry → grader → backtest → forward.
   No cycles. TickerPulse metrics enter through the foundry, never directly
   into the grader. Sim test-tapes feed only the backtest harness's
   calibration, never production data.
3. **Provenance everywhere.** Every artifact records its source hashes; every
   score panel records the grader's git SHA and config/universe fingerprints.
   Two outputs are comparable only when fingerprints match.
4. **Forward-only clocks run first.** Anything that cannot be backfilled
   (PIT symbol snapshots, sentiment metrics, paper-trade journal) starts
   before anything that can. Every day of delay is unrecoverable data.
5. **Licensing split.** US-government public-domain data and derivations may
   live in public repos. FINRA, Tiingo, Stooq, and anything derived from
   restricted data stays in gitignored vaults or private storage. Decide
   placement before first write — public git history is forever.
6. **Honesty labels travel with data.** Reconstructed/approximate/derived
   values carry flags; granularity limits (e.g. fiscal-period dividends, no
   ex-dates) are stated in manifests; nothing is presented as investment
   advice.

## Current sequencing (2026-07-28)

1. Stock-Grader §0–§1 (stabilize + data integrity) — in progress (Codex).
2. FoundryProvider adapter in Stock-Grader (consume symbols + corporate
   actions artifacts) — queued after §1.
3. Foundry: weekly submissions.zip job (8-K events, vintage manifests), then
   quarterly delisted-price reconstruction (13F implied + terminal prices).
4. Grader §6 backtest harness against that panel, calibrated by sim-generated
   known-answer tapes; trial ledger for multiple-testing control.
5. Forward clocks: TickerPulse sentiment archive (URGENT — 30-day pruning is
   destroying data), paper-trade journal via the sim's risk-gated adapter.

## Decision log

- 2026-07-28: Stock-Grader is the system of record for methodology.
- 2026-07-28: Owner decision — Stock-Rater is ARCHIVED. No further work; the
  repo/folder may be kept or deleted at the owner's convenience. Its two ideas
  worth harvesting into Stock-Data: exact-accession-byte archiving and
  bitemporal lineage tracking.
