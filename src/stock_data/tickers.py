"""The foundry's own ticker-spelling helper. Canonical form = SEC dash form.

Class shares are written differently across the live symbologies: the SEC
ticker maps use ``BRK-B`` (the ecosystem's canonical form — see
ECOSYSTEM.md rule 7), Polygon and most humans write ``BRK.B``, and IB writes
``BRK B``. This is the copy-the-helper remedy — cross-repo imports are
forbidden, and Stock-Grader's ``ticker_variants`` / Stock-Vault's
``tickers.py`` and this module must agree that canonical is the dash form.

FINRA's no-separator form (``BRKB``) is deliberately NOT produced here: a
squashed class share can spell a different issuer's real ticker, so joins
against no-separator sources must go through an ambiguity-guarded index
(Stock-Vault's ``build_squash_index``), never blind variant expansion.
"""

from __future__ import annotations

__all__ = ["canonical_ticker", "ticker_variants"]


def canonical_ticker(ticker: str) -> str:
    """Canonical SEC dash form: ``BRK.B``, ``BRK B``, and ``brk-b`` -> ``BRK-B``."""
    text = ticker.upper().strip().replace(".", "-").replace(" ", "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text


def ticker_variants(ticker: str) -> tuple[str, ...]:
    """Ordered, de-duplicated spellings to try: as-given, dash, dot, space forms."""
    upper = ticker.upper().strip()
    canonical = canonical_ticker(upper)
    variants = [
        upper,
        canonical,
        canonical.replace("-", "."),
        canonical.replace("-", " "),
    ]
    seen: set[str] = set()
    ordered = []
    for candidate in variants:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered)
