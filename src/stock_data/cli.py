"""Command-line entry points for the foundry jobs."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from . import corporate_actions, events, finra, symbols


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-data", description=__doc__)
    # Shared parent so --data-dir/--vault-dir work BEFORE or AFTER the
    # subcommand: the workflows call the subcommand-first order, and this exact
    # arg-order mismatch silently killed every scheduled run once already.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--data-dir",
        default=os.environ.get("STOCK_DATA_DIR", "data"),
        help="output root for public-domain datasets (default: ./data)",
    )
    parser.add_argument(
        "--vault-dir",
        default=os.environ.get("STOCK_DATA_VAULT", "vault"),
        help="local-only root for restricted-license data (default: ./vault, gitignored)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("snapshot-symbols", parents=[shared], help="archive symbol directories and diff events")

    actions = sub.add_parser("corporate-actions", parents=[shared], help="reconstruct dividends/splits from XBRL")
    actions.add_argument("--tickers", nargs="+", required=True)

    ev = sub.add_parser("events", parents=[shared], help="8-K red flags + delisting forms across the universe")
    ev.add_argument("--limit", type=int, default=None, help="cap the CIK sweep (testing)")

    short_interest = sub.add_parser(
        "finra-short-interest", parents=[shared], help="download FINRA short interest to the local vault"
    )
    short_interest.add_argument(
        "--since", type=dt.date.fromisoformat, default=dt.date.today() - dt.timedelta(days=90)
    )

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
    if args.command == "finra-short-interest":
        paths = finra.fetch(args.vault_dir, args.since)
        print(f"fetched {len(paths)} new file(s) into the vault")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
