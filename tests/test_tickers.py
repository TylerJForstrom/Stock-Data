"""Ticker symbology helper: canonical = SEC dash form (ECOSYSTEM.md rule 7)."""

from __future__ import annotations

import json

from stock_data import corporate_actions as ca
from stock_data.tickers import canonical_ticker, ticker_variants


def test_dot_dash_and_space_forms_all_tried():
    assert ticker_variants("BRK.B") == ("BRK.B", "BRK-B", "BRK B")
    assert ticker_variants("brk-b") == ("BRK-B", "BRK.B", "BRK B")
    assert ticker_variants("BRK B") == ("BRK B", "BRK-B", "BRK.B")
    assert ticker_variants(" aapl ") == ("AAPL",)


def test_all_live_symbologies_share_one_canonical_form():
    assert {canonical_ticker(s) for s in ("BRK-B", "BRK.B", "brk b")} == {"BRK-B"}


def test_squash_form_is_deliberately_not_a_variant():
    # FINRA's no-separator form can spell a DIFFERENT issuer's real ticker, so
    # it must come from an ambiguity-guarded index (Stock-Vault's
    # build_squash_index), never from blind variant expansion.
    for spelling in ("BRK-B", "BRK.B", "BRK B"):
        assert "BRKB" not in ticker_variants(spelling)


def test_resolve_ciks_bridges_every_spelling():
    """The SEC map stores BRK-B; dot and space callers still resolve to one CIK."""
    payload = {
        "0": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire"},
        "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
    }

    class Session:
        def get(self, url):
            return type("R", (), {"text": json.dumps(payload)})()

    resolved = ca.resolve_ciks(Session(), ["BRK.B", "BRK B", "brk-b", "AAPL", "GONE"])
    assert resolved["BRK.B"] == 1067983
    assert resolved["BRK B"] == 1067983
    assert resolved["BRK-B"] == 1067983
    assert resolved["AAPL"] == 320193
    assert "GONE" not in resolved
