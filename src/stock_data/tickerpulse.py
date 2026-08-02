"""Archive TickerPulse's DERIVED per-ticker metrics in the public foundry.

ECOSYSTEM.md assigns TickerPulse this route: *"Its derived per-ticker daily
metrics are archived by the foundry (raw social content is not)."* The grader
must never read TickerPulse directly — sentiment enters through here or not at
all.

**Why this module projects an allowlist rather than copying rows.**
TickerPulse's own ``ticker_trends`` archive carries a ``top_posts`` field on
every row: verbatim post text, the author's handle, and the platform post id.
Copying a row into Stock-Data would publish scraped social content and personal
identifiers into a public git history, permanently, on the next cron push —
exactly the unrecoverable mistake ECOSYSTEM.md Rule 5 exists to prevent
("decide placement before first write — public git history is forever").

So each dataset declares the exact fields that may cross the boundary and
everything else is dropped. An allowlist, not a denylist: when TickerPulse adds
a field upstream, the default must be *not published*. A field that is not
listed here has never been reviewed, and unreviewed data does not leave.

The license note is deliberately NOT ``PUBLIC_DOMAIN_NOTE``. These numbers are
derived from public social posts, not from a US-government work, and claiming
17 USC 105 for them would be false. What is published is aggregate counts and
scores over public chatter — no post text, no author, no post id.
"""

from __future__ import annotations

import gzip
import json
import os
from typing import Any

from .manifest import publish_staged_dataset

#: Datasets to mirror, and the ONLY fields of each that may be published.
#:
#: ``ticker_buckets`` is hourly aggregate counts and is safe in full.
#: ``ticker_trends`` is the daily leaderboard; its ``top_posts``, and any future
#: field, stay behind.
ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    "ticker_buckets": (
        "ticker",
        "bucket_start",
        "bucket_minutes",
        "mentions",
        "engagement",
        "bull",
        "bear",
        "neutral",
        "sentiment_avg",
        "platforms",
    ),
    "ticker_trends": (
        "ticker",
        "mentions",
        "mentions_prev",
        "engagement",
        "bull",
        "bear",
        "neutral",
        "bull_bear_ratio",
        "sentiment_avg",
        "engagement_weighted_score",
        "breakout_score",
        "velocity",
        "share_of_voice",
        "phase",
        "window_hours",
        "platforms",
        "updated_at",
    ),
}

#: Fields that must never appear in a published row, checked after projection as
#: a second, explicit barrier. The allowlist already excludes them; this makes a
#: future edit that widens the allowlist fail loudly instead of silently
#: publishing content.
FORBIDDEN_FIELDS = frozenset({"top_posts", "author", "text", "id", "url", "name", "source"})

LICENSE_NOTE = (
    "Derived aggregate metrics computed by TickerPulse over public social posts "
    "(Reddit, StockTwits, Bluesky, Hacker News, finance RSS). Counts and scores only: "
    "no post text, author identity, or post identifier is included or redistributable. "
    "Not US-government public-domain data."
)

SOURCE_URL = "https://github.com/TylerJForstrom/TickerPulse"


class TickerPulseError(RuntimeError):
    """The upstream archive violated the publication contract."""


def _project(dataset: str, row: dict[str, Any]) -> dict[str, Any]:
    """Keep only reviewed fields, then prove nothing forbidden survived."""
    allowed = ALLOWED_FIELDS[dataset]
    projected = {key: row[key] for key in allowed if key in row}
    leaked = FORBIDDEN_FIELDS & set(projected)
    if leaked:
        raise TickerPulseError(
            f"{dataset}: refusing to publish {sorted(leaked)} — raw social content and "
            f"author identity must not enter the public foundry"
        )
    if not projected.get("ticker"):
        raise TickerPulseError(f"{dataset}: row has no ticker; refusing to publish it")
    return projected


def read_archive(archive_dir: str, dataset: str) -> dict[str, list[dict[str, Any]]]:
    """Read one TickerPulse dataset as ``{date: [projected rows]}``."""
    if dataset not in ALLOWED_FIELDS:
        raise ValueError(f"unsupported TickerPulse dataset: {dataset}")
    source = os.path.join(archive_dir, dataset)
    if not os.path.isdir(source):
        raise TickerPulseError(f"no TickerPulse archive at {source}")
    days: dict[str, list[dict[str, Any]]] = {}
    for name in sorted(os.listdir(source)):
        if not name.endswith(".jsonl.gz"):
            continue
        day = name[: -len(".jsonl.gz")]
        with gzip.open(os.path.join(source, name), "rt", encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        days[day] = [_project(dataset, row) for row in rows]
    return days


def snapshot(data_dir: str, archive_dir: str) -> dict[str, int]:
    """Publish every TickerPulse day into the foundry, one dataset per directory.

    Sentiment is a forward-only clock — ECOSYSTEM.md Rule 4 names it explicitly
    as unbackfillable — so this mirrors every day present upstream rather than
    only the newest, and re-publishing an unchanged day is a no-op at the byte
    level.
    """
    written: dict[str, int] = {}
    for dataset in sorted(ALLOWED_FIELDS):
        if not os.path.isdir(os.path.join(archive_dir, dataset)):
            # Upstream adds datasets at different times — ticker_trends began six
            # days after ticker_buckets. A dataset that does not exist yet is not
            # an error; one that exists and cannot be read still is.
            continue
        days = read_archive(archive_dir, dataset)
        if not days:
            continue
        staged: dict[str, bytes] = {}
        row_counts: dict[str, int] = {}
        for day, rows in days.items():
            name = f"{day}.jsonl"
            body = "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
            )
            staged[name] = body.encode("utf-8")
            row_counts[name] = len(rows)
        dataset_dir = os.path.join(data_dir, "sentiment", dataset)
        publish_staged_dataset(
            dataset_dir,
            staged,
            source_urls=[SOURCE_URL],
            license_note=LICENSE_NOTE,
            row_counts=row_counts,
            extra={
                "producer": "TickerPulse",
                "derivation": "aggregate counts and scores over public social posts",
                "published_fields": list(ALLOWED_FIELDS[dataset]),
                "excluded_fields_note": (
                    "post text, author identity and post identifiers are excluded by an "
                    "allowlist and must never be added"
                ),
                "first_day": min(days),
                "last_day": max(days),
            },
        )
        written[dataset] = sum(row_counts.values())
    return written
