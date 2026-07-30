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
            dataset_stale = False
            if age > threshold:
                print(f"STALE {dataset_dir}: {detail}", file=sys.stderr)
                dataset_stale = True
            # A failing SOURCE hides behind the manifest's always-fresh write
            # timestamp; per-source last_success watermarks do not.
            watermarks = manifest.read_manifest(dataset_dir).get("last_success")
            if isinstance(watermarks, dict):
                # Watermarks only exist for sources that succeeded at least
                # once — a collector broken from day one never writes one and
                # would stay green forever. last_success is published by the
                # symbols snapshotter, so its SOURCES map is the expected set.
                for source_name in sorted(set(symbols.SOURCES) - set(watermarks)):
                    print(
                        f"STALE {dataset_dir}: source {source_name} never "
                        f"succeeded (no last_success watermark)",
                        file=sys.stderr,
                    )
                    dataset_stale = True
                for source_name, success_date in sorted(watermarks.items()):
                    try:
                        success_age = now - dt.datetime.fromisoformat(
                            str(success_date)
                        ).replace(tzinfo=dt.UTC)
                    except ValueError:
                        print(
                            f"STALE {dataset_dir}: source {source_name} has "
                            f"unparseable last_success {success_date!r}",
                            file=sys.stderr,
                        )
                        dataset_stale = True
                        continue
                    if success_age > threshold + dt.timedelta(days=2):
                        print(
                            f"STALE {dataset_dir}: source {source_name} last "
                            f"succeeded {success_date} "
                            f"({success_age.total_seconds() / 86400:.1f} days ago)",
                            file=sys.stderr,
                        )
                        dataset_stale = True
            if dataset_stale:
                stale = True
            else:
                print(f"FRESH {dataset_dir}: {detail}")
        return int(stale)
    return 2


if __name__ == "__main__":
    sys.exit(main())
