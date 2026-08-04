# The Ecosystem Contract

Canonical copy lives in **Stock-Data** (the hub every project reads from).
Other repos carry a pointer to this file. Any agent (human or AI) working in
any member repo reads this before cross-project work.

## Roles

| Project | Role | Talks to |
|---|---|---|
| **Stock-Data** | Data foundry: the ONLY project that fetches external sources. Archives point-in-time snapshots, reconstructs corporate actions, publishes versioned datasets. | Sources → publishes artifacts |
| **Stock-Grader** | System of record for grading methodology. Consumes foundry artifacts, produces score panels and research dossiers. Home of the backtest evaluator and its attestations. | Foundry artifacts → publishes reports/panels |
| **Stock-Vault** | Private twin of the foundry: collectors for sources whose terms forbid redistribution (Massive/Polygon EOD, IBKR borrow, Finnhub recs, SSGA, FINRA, delisted cohorts). ALSO the ecosystem's execution layer — the ONLY order path: the Alpaca **paper** trader (`src/stock_vault/paper.py`, paper endpoint only per rule 8, run by `paper-trader.yml`) with its append-only journal, plus the shadow/control arms that replay it. **Must never be made public.** | Restricted sources → private archives; reads Grader panels as artifacts; paper orders → Alpaca paper endpoint only |
| **TickerPulse** | Sentiment/attention signal source. Its derived per-ticker daily metrics are archived by the foundry (raw social content is not). | Sources → foundry archives its metrics |
| **Stock Market Simulation** | Test oracle ONLY: generates tapes with known injected signal/null and exports calibration panels (`scripts/export_calibration_panels.py`) that calibrate the backtest harness's power and false-positive rate (the grader's power table). It does not validate alpha, and it is NOT the execution layer: its `AlpacaPaperAdapter` (`src/sms/trading/paper.py`) is dormant code wired to no workflow — production execution runs from Stock-Vault (see decision log, 2026-08-04). | Calibration tapes/panels → backtest calibration only |
| ~~**Stock-Rater**~~ | **ARCHIVED** by owner decision 2026-07-28; the repository no longer exists. Do not revive it as a second grading engine. If a presentation layer is ever wanted, that is a new decision. | — |

**Membership note (2026-08-01).** `Portfolio-Insight-Copilot` is a public repository on the same
account that consumes a Stock Market Simulation sample run. It is **not** an ecosystem member: it
imports no member's artifacts through a manifest, publishes none, and carries no clock. It is listed
here only so the next reader does not have to re-derive that. If it is ever wired to Stock-Grader
output it becomes a presentation layer and needs a row above, a manifest contract, and a licensing
review — a Finnhub-derived panel may not reach it.
**Archived 2026-08-03** per the audit recommendation: read-only, still publicly visible as a
portfolio piece; unarchive is a one-click reversal if it is ever demoed or wired up again.

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
7. **Ticker symbology: canonical = the SEC dash form** (`BRK-B`). Four live
   spellings of one class share exist — SEC/panels `BRK-B`, Polygon and
   TickerPulse `BRK.B`, IB `BRK B`, FINRA `BRKB` — and every adapter boundary
   canonicalizes (or variant-matches) on read so all of them resolve to one
   issuer. Because code imports are forbidden (rule 1), each repo carries its
   own copy of the ~20-line helper: Stock-Data `src/stock_data/tickers.py`,
   Stock-Grader `src/stock_grader/data/symbols.py`, Stock-Vault
   `src/stock_vault/tickers.py`; the copies must agree on the canonical form.
   The no-separator FINRA form is never a blind variant — a squashed class
   share can spell a different issuer's real ticker — so no-separator joins go
   through an ambiguity-guarded index (Stock-Vault `build_squash_index`).
8. **Money boundary.** The ecosystem's only order path is Stock-Vault's
   Alpaca **paper** trader, and it trades exclusively against the paper
   endpoint. This is a constitutional invariant, stated behaviorally: no
   refactor, configuration option, credential, environment variable, or
   workflow may create a code path from any member repo to a live brokerage
   account. Today the invariant is enforced by the hardcoded `PAPER_BASE` in
   Stock-Vault `src/stock_vault/paper.py` ("there is deliberately no
   configuration that reaches a live account") — the constant may be
   refactored; the behavior may not. Un-stopping this boundary requires a
   promotion-gate pass under the declared promotion policy AND a new dated
   decision-log entry below — never a quiet code change.

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
   *(2026-08-04: the last clause is superseded — the journal runs from
   Stock-Vault's `paper.py`, not the sim; see the decision log. The original
   text is kept as the historical record.)*

## Decision log

- 2026-07-28: Stock-Grader is the system of record for methodology.
- 2026-07-28: Owner decision — Stock-Rater is ARCHIVED. No further work; the
  repo/folder may be kept or deleted at the owner's convenience. Its two ideas
  worth harvesting into Stock-Data: exact-accession-byte archiving and
  bitemporal lineage tracking.
- **2026-08-02** — Derived backtest panels (per-row forward returns computed
  from Massive free-tier EOD closes and stockanalysis.com delisted histories)
  live in the PRIVATE vault under `data/backtest_panels/<profile>/`, per
  licensing rule 5. The public grader repo commits only aggregate statistics:
  the backtest markdown, build accounting, and the ledger line.
- **2026-08-03** — Shadow paper arms (M3) consume grader frozen panels and the
  vault EOD archive and write vault journals: sources → foundry → grader →
  forward, same one-direction DAG, no new cycle, no code import in either
  direction. Gap surfaced: `frozen_scores/` panels carry NO manifest.json —
  consumers gate on the panel's own `schema_version` column; a per-profile
  manifest is follow-up work for the grader.
- **2026-08-03** — Repo-security pass: `protect-main` rulesets (block branch
  deletion and non-fast-forward pushes on `main`) are active on all four
  PUBLIC repos; Dependabot (pip + github-actions, weekly) and vulnerability
  alerts enabled ecosystem-wide. **Residual gap:** the rulesets API returns
  403 for the PRIVATE repos (Stock-Vault, Stock-Market-Sim) on the free plan,
  so their `main` branches remain force-pushable and deletable by anyone with
  push access — the vault journals' append-only guarantee rests on token
  hygiene until a plan upgrade (or making the repo public, which Stock-Vault
  must never do) closes it.
- **2026-08-03** — DECLINED: using the sim as a differential test oracle for
  the code that replays `paper.target_portfolio` (the long-standing "sim as
  oracle for the shadow replay" variant). Three reasons. (1) The replay is
  Stock-Vault's `shadow.py`, which imports and calls `target_portfolio`
  directly; its economic core is ~50 lines of next-close ± 5 bps arithmetic
  already covered by 19 targeted tests, including rules-parity (a monkeypatch
  proves shadow calls the pre-registered function), byte-identical rebuild,
  the rebalance band, no-margin failed legs, splits, and stale write-downs.
  (2) Structural mismatch: the sim's `Position.quantity` is int whole shares
  while shadow fills fractional quantities from notional sizing
  (`qty = spend / fill_px`), so an oracle comparison needs either a core sim
  type change or rounding that makes disagreements ambiguous. (3) Rule 1
  forbids cross-repo code imports, and an artifact handshake is
  disproportionate machinery for a ~50-line target. The idea's defensible
  kernel — generative invariant checking of shadow's float accounting — is
  captured instead as hypothesis property tests inside Stock-Vault, mirroring
  the sim's test PATTERN, not its code. Do not resurface the oracle variant.
- **2026-08-04** — Roles corrected: the execution layer is Stock-Vault, not
  the sim. Production paper trading has run from Stock-Vault's `paper.py`
  (Alpaca paper account, append-only journal, shadow/control arms, workflow
  `paper-trader.yml`) since 2026-07-30. The sim's `AlpacaPaperAdapter`
  (`src/sms/trading/paper.py`) is dormant code wired to no workflow and must
  not be mistaken for the order path. The sim's role is oracle-only:
  known-IC calibration tapes and panels
  (`scripts/export_calibration_panels.py`) behind the grader's power table
  (Stock-Grader `docs/calibration/power_table_2026-08-03.md`). The Roles
  table above was edited accordingly; this entry records the change. (This
  pass also merged the file's two accidental `## Decision log` headings into
  one section; every entry was kept verbatim.)
- **2026-08-04** — DELIBERATE STOP recorded: the live-broker bridge and the
  portfolio/risk-construction layer (position sizing beyond the
  pre-registered paper rules, portfolio optimization, tax-lot accounting)
  are OUT OF SCOPE for every ecosystem repo. This is a decision, not a
  backlog item. By the system's own calibration (the power table above), the
  forward significance gate is structurally closed below 11 matured monthly
  periods and cannot realistically pass before ~mid-2027; building execution
  or portfolio machinery before the evidence can exist would be structure
  ahead of proof. The stop holds until a promotion gate declared under
  Stock-Grader's versioned promotion policy — referenced by NAME
  (PROMOTION-POLICY v1, landing in the same batch as this entry; its
  research-ledger declaration carries the binding hash) — passes on
  schedule. Revisiting this decision, for any reason including a passed
  gate, requires a NEW dated entry in this log; no code, configuration, or
  workflow change may un-stop it silently.
- **2026-08-04** — Constitutional invariant adopted as rule 8 (money
  boundary): the vault's paper trader trades only against the Alpaca paper
  endpoint. Worded behaviorally on purpose — the hardcoded `PAPER_BASE` in
  `paper.py` is today's enforcement mechanism, not the invariant itself, so
  a legitimate refactor of the constant is not a violation, while ANY code
  path that reaches a live account is, regardless of mechanism.
