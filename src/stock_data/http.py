"""Throttled, retrying HTTP session honouring SEC fair-access rules.

One session serves every fetcher so per-host throttles are global to the
process. SEC policy: max 10 req/s regardless of machine count, declared
User-Agent with contact, and temporary blocks for violators — we default to
8 req/s for sec.gov hosts and stay polite everywhere else.
"""

from __future__ import annotations

import os
import time

import requests

USER_AGENT = "Stock-Data/0.1 (Tyler Forstrom; tylerjamesforstrom@gmail.com)"

# Minimum seconds between requests, per host suffix. First match wins.
_HOST_INTERVALS: list[tuple[str, float]] = [
    ("sec.gov", 1.0 / 8.0),
    ("nasdaqtrader.com", 0.5),
    ("treasury.gov", 0.5),
]
_DEFAULT_INTERVAL = 0.25

_RETRY_STATUSES = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """A fetch failed after all retry attempts."""


class FairAccessSession:
    """requests.Session wrapper: per-host throttle, retries with backoff."""

    def __init__(self, max_attempts: int = 4, timeout: float = 60.0) -> None:
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._last_request: dict[str, float] = {}
        self.max_attempts = max_attempts
        self.timeout = timeout

    @staticmethod
    def _interval_for(host: str) -> float:
        for suffix, interval in _HOST_INTERVALS:
            if host.endswith(suffix):
                return interval
        return _DEFAULT_INTERVAL

    def _throttle(self, host: str) -> None:
        interval = self._interval_for(host)
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request[host] = time.monotonic()

    def get(self, url: str, *, ok_statuses: frozenset[int] = frozenset({200})) -> requests.Response:
        """GET with throttle and retry. Raises FetchError after exhaustion.

        A status in ``ok_statuses`` returns immediately; 404 raises FetchError
        with ``not_found=True`` set on the exception without retrying (missing
        files are a normal outcome for date-pattern probing).
        """
        host = requests.utils.urlparse(url).netloc
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            self._throttle(host)
            try:
                response = self._session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code in ok_statuses:
                    return response
                if response.status_code == 404:
                    error = FetchError(f"404 for {url}")
                    error.not_found = True  # type: ignore[attr-defined]
                    raise error
                if response.status_code not in _RETRY_STATUSES:
                    raise FetchError(f"HTTP {response.status_code} for {url}")
                last_error = FetchError(f"HTTP {response.status_code} for {url}")
            if attempt + 1 < self.max_attempts:
                time.sleep(min(2.0**attempt, 30.0))
        raise FetchError(f"failed after {self.max_attempts} attempts: {url}") from last_error


def atomic_write_bytes(path: str, payload: bytes) -> None:
    """Write via temp file + os.replace so readers never see partial files."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".tmp.{os.path.basename(path)}.{os.getpid()}")
    with open(tmp, "wb") as handle:
        handle.write(payload)
    os.replace(tmp, path)


def atomic_write_text(path: str, payload: str) -> None:
    atomic_write_bytes(path, payload.encode("utf-8"))
