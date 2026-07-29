"""Command-line entry points for the foundry jobs."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from . import corporate_actions, events, manifest, symbols


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-data", description=__doc__)
    # Shared parent so --data-dir works BEFORE or AFTER the subcommand: the
    # workflows call the subcommand-first order, and this exact arg-order
    # mismatch silently killed every scheduled run once already.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--data-dir",
        default=os.environ.get("STOCK_DATA_DIR", "data"),
        help="output root for public-domain datasets (default: ./data)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "snapshot-symbols",
        parents=[shared],
        help="archive symbol directories and diff events",
    )

    actions = sub.add_parser(
        "corporate-actions",
        parents=[shared],
        help="reconstruct dividends/splits from XBRL",
    )
    actions.add_argument("--tickers", nargs="+", required=True)

    ev = sub.add_parser(
        "events",
        parents=[shared],
        help="8-K red flags + delisting forms across the universe",
    )
    ev.add_argument("--limit", type=int, default=None, help="cap the CIK sweep (testing)")

    freshness = sub.add_parser(
        "check-staleness",
        help="fail when one or more dataset manifests are older than the allowed age",
    )
    freshness.add_argument(
        "--max-age-days",
        type=float,
        required=True,
        help="maximum allowed manifest age in days",
    )
    freshness.add_argument("dataset_dirs", nargs="+", metavar="DATASET_DIR")

    args = parser.parse_args(argv)
    if args.command == "snapshot-symbols":
        counts, failures = symbols.snapshot(args.data_dir)
        for source, count in sorted(counts.items()):
            print(f"{source}: {count} events")
        for failure in failures:
            print(f"FAILURE {failure}", file=sys.stderr)
        if not counts and failures:
            return 1  # every source failed: the archive is frozen, go red
        return 0
    if args.command == "corporate-actions":
        out = corporate_actions.run(args.data_dir, args.tickers)
        print(f"wrote {out}")
        return 0
    if args.command == "events":
        stats = events.collect(args.data_dir, limit=args.limit)
        print(f"events: {stats}")
        return 0
    if args.command == "check-staleness":
        if args.max_age_days < 0:
            parser.error("--max-age-days must be non-negative")
        threshold = dt.timedelta(days=args.max_age_days)
        now = dt.datetime.now(dt.UTC)
        stale = False
        for dataset_dir in args.dataset_dirs:
            try:
                generated = manifest.manifest_generated_at(dataset_dir)
            except (OSError, ValueError) as exc:
                print(f"STALE {dataset_dir}: {exc}", file=sys.stderr)
                stale = True
                continue
            age = now - generated
            detail = (
                f"generated {generated.isoformat()} "
                f"({age.total_seconds() / 86400:.2f} days old; "
                f"maximum {args.max_age_days:g})"
            )
            if age > threshold:
                print(f"STALE {dataset_dir}: {detail}", file=sys.stderr)
                stale = True
            else:
                print(f"FRESH {dataset_dir}: {detail}")
        return int(stale)
    return 2


if __name__ == "__main__":
    sys.exit(main())
