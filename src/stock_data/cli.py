"""Command-line entry points for the foundry jobs."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from . import corporate_actions, finra, symbols


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-data", description=__doc__)
    parser.add_argument(
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

    sub.add_parser("snapshot-symbols", help="archive symbol directories and diff events")

    actions = sub.add_parser("corporate-actions", help="reconstruct dividends/splits from XBRL")
    actions.add_argument("--tickers", nargs="+", required=True)

    short_interest = sub.add_parser(
        "finra-short-interest", help="download FINRA short interest to the local vault"
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
    if args.command == "finra-short-interest":
        paths = finra.fetch(args.vault_dir, args.since)
        print(f"fetched {len(paths)} new file(s) into the vault")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
