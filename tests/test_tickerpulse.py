"""The foundry may publish TickerPulse's derived metrics, never its raw content."""

from __future__ import annotations

import gzip
import json

import pytest

from stock_data.manifest import PUBLIC_DOMAIN_NOTE
from stock_data.tickerpulse import (
    ALLOWED_FIELDS,
    FORBIDDEN_FIELDS,
    LICENSE_NOTE,
    TickerPulseError,
    read_archive,
    snapshot,
)


def _archive(root, dataset, day, rows):
    directory = root / dataset
    directory.mkdir(parents=True, exist_ok=True)
    with gzip.open(directory / f"{day}.jsonl.gz", "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _trend_row(**overrides):
    row = {
        "ticker": "AAPL",
        "mentions": 44,
        "sentiment_avg": -0.17,
        "platforms": {"stocktwits": 20},
        # Everything below is raw social content or personal data and must not
        # cross into the public foundry.
        "top_posts": [
            {
                "author": "BigPlaySnipers",
                "text": "I've been trading for a long time...",
                "id": "stocktwits:660554336",
                "platform": "stocktwits",
            }
        ],
        "name": "Apple Inc.",
    }
    row.update(overrides)
    return row


def test_raw_social_content_and_author_identity_never_reach_the_foundry(tmp_path):
    """The whole reason this collector projects instead of copying.

    Every ticker_trends row upstream carries top_posts: verbatim post text, the
    author's handle, and the platform post id. Copying a row would publish
    scraped content and personal identifiers into a public git history on the
    next cron push, permanently.
    """
    archive = tmp_path / "archive"
    _archive(archive, "ticker_trends", "2026-08-01", [_trend_row()])

    (day,) = read_archive(str(archive), "ticker_trends").values()
    (published,) = day

    assert published["ticker"] == "AAPL"
    assert published["mentions"] == 44
    for field in ("top_posts", "author", "text", "id", "name"):
        assert field not in published, f"{field} must not be published"

    snapshot(str(tmp_path / "data"), str(archive))
    raw = (tmp_path / "data" / "sentiment" / "ticker_trends" / "2026-08-01.jsonl").read_text()
    assert "BigPlaySnipers" not in raw
    assert "been trading for a long time" not in raw
    assert "stocktwits:660554336" not in raw


def test_projection_is_an_allowlist_so_a_new_upstream_field_is_not_published(tmp_path):
    """A field nobody has reviewed must default to NOT published."""
    archive = tmp_path / "archive"
    _archive(
        archive,
        "ticker_trends",
        "2026-08-01",
        [_trend_row(some_future_field="whatever TickerPulse adds next")],
    )
    (day,) = read_archive(str(archive), "ticker_trends").values()
    assert "some_future_field" not in day[0]


def test_widening_the_allowlist_into_forbidden_territory_raises(tmp_path, monkeypatch):
    """The second barrier: if someone edits the allowlist, fail loudly."""
    monkeypatch.setitem(ALLOWED_FIELDS, "ticker_trends", ("ticker", "top_posts"))
    archive = tmp_path / "archive"
    _archive(archive, "ticker_trends", "2026-08-01", [_trend_row()])
    with pytest.raises(TickerPulseError, match="raw social content"):
        read_archive(str(archive), "ticker_trends")


def test_manifest_states_honest_provenance_not_public_domain(tmp_path):
    """These are derived from social posts, not a US-government work."""
    archive = tmp_path / "archive"
    _archive(archive, "ticker_buckets", "2026-08-01", [{"ticker": "AAPL", "mentions": 3}])
    snapshot(str(tmp_path / "data"), str(archive))

    manifest = json.loads(
        (tmp_path / "data" / "sentiment" / "ticker_buckets" / "manifest.json").read_text()
    )
    assert manifest["license_note"] == LICENSE_NOTE
    assert manifest["license_note"] != PUBLIC_DOMAIN_NOTE
    assert "17 USC 105" not in manifest["license_note"]
    assert manifest["schema_version"] == "1.0"
    assert manifest["producer"] == "TickerPulse"
    assert set(manifest["published_fields"]) <= set(ALLOWED_FIELDS["ticker_buckets"])
    assert not (set(manifest["published_fields"]) & FORBIDDEN_FIELDS)
    assert [f["name"] for f in manifest["files"]] == ["2026-08-01.jsonl"]


def test_a_row_without_a_ticker_is_refused(tmp_path):
    archive = tmp_path / "archive"
    _archive(archive, "ticker_buckets", "2026-08-01", [{"mentions": 3}])
    with pytest.raises(TickerPulseError, match="no ticker"):
        read_archive(str(archive), "ticker_buckets")


def test_the_daily_clock_actually_mirrors_sentiment():
    """Wiring, not just capability: an unwired collector archives nothing.

    Sentiment is named in ECOSYSTEM.md Rule 4 as unbackfillable, and TickerPulse
    had been committing daily since 2026-07-21 with no consumer at all.
    """
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "daily-snapshot.yml"
    ).read_text(encoding="utf-8")

    assert "repository: TylerJForstrom/TickerPulse" in workflow
    assert "stock-data sentiment-snapshot --data-dir data --archive-dir tickerpulse/archive" in (
        workflow
    )
    # The gate runs before collection, and is bootstrap-guarded so the first run
    # is not red for having no prior archive.
    assert "data/sentiment/ticker_buckets" in workflow
    gate = workflow.index("check-staleness --max-age-days 3 data/sentiment/ticker_buckets")
    work = workflow.index("sentiment-snapshot")
    assert gate < work, "the staleness gate must run before collection"
