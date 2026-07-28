"""FINRA bi-monthly equity short interest → local vault ONLY.

FINRA's terms are non-commercial use and prohibit end-user redistribution, so
this fetcher refuses to write anywhere except the gitignored ``vault/``
directory. Do not relax that: this repository is public.

Files follow ``shrtYYYYMMDD.csv`` keyed by settlement date (verified pattern,
e.g. shrt20260715.csv). Settlement dates are approximately the 15th and the
last business day of each month; we probe candidate dates and skip 404s, which
makes the job a self-healing watermark catch-up.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess

from .http import FairAccessSession, FetchError, atomic_write_bytes, atomic_write_text

FILE_URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{date}.csv"
LICENSE_NOTE = (
    "FINRA data: non-commercial internal use only; redistribution prohibited. "
    "This file must never leave the local vault."
)


def candidate_settlement_dates(since: dt.date, until: dt.date) -> list[dt.date]:
    """Mid-month and month-end business days, the bi-monthly settlement pattern."""

    def business_day_on_or_before(day: dt.date) -> dt.date:
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        return day

    dates = []
    cursor = dt.date(since.year, since.month, 1)
    while cursor <= until:
        mid = business_day_on_or_before(dt.date(cursor.year, cursor.month, 15))
        next_month = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        month_end = business_day_on_or_before(next_month - dt.timedelta(days=1))
        for day in (mid, month_end):
            if since <= day <= until:
                dates.append(day)
        cursor = next_month
    return dates


def _assert_vault_ignored(vault_dir: str) -> None:
    """Refuse to run if the vault would be committed (public repo safety)."""
    repo_dir = os.path.dirname(os.path.abspath(vault_dir))
    probe = os.path.join(vault_dir, "probe")
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", probe],
            cwd=repo_dir,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Fail CLOSED: we could not verify, so we do not download.
        raise RuntimeError(
            f"could not verify {vault_dir} is gitignored ({exc!r}); refusing to "
            "download restricted FINRA data without that guarantee"
        ) from exc
    if result.returncode == 0:
        return  # explicitly gitignored
    if result.returncode == 1:
        raise RuntimeError(
            f"{vault_dir} is not gitignored — refusing to download restricted "
            "FINRA data into a directory that could be committed to a public repo"
        )
    # Exit code 128: not a git repository / path outside the work tree — the
    # safest configuration of all, since nothing can be committed from there.
    return


def _watermark(out_dir: str) -> dt.date | None:
    """Settlement date of the newest already-downloaded file, if any."""
    newest = None
    if os.path.isdir(out_dir):
        for name in os.listdir(out_dir):
            if name.startswith("shrt") and name.endswith(".csv") and len(name) == 16:
                try:
                    day = dt.datetime.strptime(name[4:12], "%Y%m%d").date()
                except ValueError:
                    continue
                newest = day if newest is None or day > newest else newest
    return newest


def fetch(vault_dir: str, since: dt.date, session: FairAccessSession | None = None) -> list[str]:
    """Download all missing settlement files since ``since``. Returns new paths.

    Self-healing catch-up: if the vault already holds files, the probe window
    extends back to just after the newest one, so a gap between runs longer
    than the requested window cannot silently skip settlement dates.
    """
    _assert_vault_ignored(vault_dir)
    session = session or FairAccessSession()
    out_dir = os.path.join(vault_dir, "finra_short_interest")
    os.makedirs(out_dir, exist_ok=True)
    newest = _watermark(out_dir)
    if newest is not None:
        since = min(since, newest + dt.timedelta(days=1))
    today = dt.date.today()
    fetched = []
    for day in candidate_settlement_dates(since, today):
        stamp = day.strftime("%Y%m%d")
        path = os.path.join(out_dir, f"shrt{stamp}.csv")
        if os.path.exists(path):
            continue
        try:
            response = session.get(FILE_URL.format(date=stamp))
        except FetchError as exc:
            if getattr(exc, "not_found", False):
                continue  # not a real settlement date, or not published yet
            raise
        atomic_write_bytes(path, response.content)
        fetched.append(path)
    atomic_write_text(
        os.path.join(out_dir, "LICENSE_NOTE.json"),
        json.dumps({"license_note": LICENSE_NOTE}, indent=2) + "\n",
    )
    return fetched
