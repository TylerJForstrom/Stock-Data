"""Point-in-time membership intervals: the event replay, published as data.

The ecosystem's PIT membership promise used to be delivered by an ALGORITHM —
a backward event replay that each consumer re-typed for itself (Stock-Grader's
``FoundryDataSource.universe(asof)`` / ``symbol_directory(asof)``, and
Stock-Vault's ``_CikMaps`` mirror of them). Duplicated replay code drifts, and
"artifacts, not imports" (ECOSYSTEM.md rule 1) forbids the import that would
deduplicate it — so the replay now runs exactly once, HERE at the producer,
next to the event stream it replays, and the result is published as interval
tables under ``data/symbols/pit/``. Consumers do a hash-verified date-range
lookup instead of carrying replay code.

One table per symbol-directory source (``sec_company_tickers``,
``sec_company_tickers_exchange``, ``nasdaqlisted``, ``otherlisted``): the
sources have different record shapes and identity keys, and different
consumers replay different sources, so a single merged table would serve none
of them faithfully.

Interval semantics (half-open, matching the replay exactly):
    a row ``{**record, valid_from, valid_to, provable_from}`` asserts the
    record was the directory's state for its identity key on every date D with
    ``valid_from <= D < valid_to``; ``valid_to: null`` means still current.
    Directory membership at ``asof`` is the set of rows whose interval covers
    ``asof`` — equal, by construction and by test, to replaying the event
    stream backward from the current snapshot to ``asof``.

Honesty at the archive boundary: an event dated D proves snapshots existed on
D-1 and D, so the earliest reconstructable date is ``earliest_event - 1 day``
(2026-07-28 for this archive). Rows already present at that boundary carry
``valid_from = boundary`` and ``provable_from: false`` — the archive can prove
they were members ON the boundary date, but not when they actually became
members. Rows opened by an archived added/changed event carry
``provable_from: true``. The dataset manifest publishes the boundary as
``reconstructable_from``; consumers must refuse any earlier ``asof`` rather
than serve today's survivors dressed up as history.

The tables are rebuilt from scratch on every publish (they are a pure function
of ``current/`` + ``events/``), byte-deterministically, through the same
atomic staged-publish path as every other dataset.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from typing import Any

from .manifest import PUBLIC_DOMAIN_NOTE, publish_staged_dataset, read_manifest
from .symbols import _KEYS, SOURCES

#: Fields this module adds to each published record. A source record carrying
#: one of these names would be silently clobbered, so the build refuses it.
INTERVAL_FIELDS = ("valid_from", "valid_to", "provable_from")


class PitBuildError(RuntimeError):
    """Refusal: inputs unverifiable, malformed, or unable to testify."""


def _verified_records(dataset_dir: str, name: str) -> tuple[list[dict[str, Any]], str]:
    """Read one JSONL input, verifying its sha256 against its own manifest.

    The producer eats the same contract it publishes: building the pit tables
    from bytes the dataset manifest does not vouch for would launder an
    integrity failure into a freshly-manifested artifact.
    """
    manifest = read_manifest(dataset_dir)  # refuses unknown schema_version
    files = manifest.get("files")
    entry = None
    if isinstance(files, list):
        entry = next((f for f in files if f.get("name") == name), None)
    if entry is None:
        raise PitBuildError(f"{name} is not listed in {dataset_dir}/manifest.json")
    path = os.path.join(dataset_dir, name)
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError as exc:
        raise PitBuildError(f"missing input file: {path}") from exc
    digest = hashlib.sha256(blob).hexdigest()
    if digest != entry.get("sha256"):
        raise PitBuildError(
            f"sha256 mismatch for {path}: manifest says {entry.get('sha256')!r}, "
            f"file hashes to {digest!r}; refusing to build pit tables from it"
        )
    records = [json.loads(line) for line in blob.decode("utf-8").splitlines() if line.strip()]
    return records, digest


def members_asof(
    current: list[dict[str, Any]],
    source_events: list[dict[str, Any]],
    asof: str,
    key_fields: tuple[str, ...],
) -> dict[tuple, dict[str, Any]]:
    """Directory state at ``asof``: backward replay from the current snapshot.

    This is THE replay — the algorithm consumers used to carry — kept only
    here, beside the event producer. Events dated after ``asof`` are reversed:
    additions removed, removals restored, changes reverted to their recorded
    previous state. Only this source's events participate; ordering within a
    single date is immaterial because the daily diff emits at most one event
    per identity key per date.
    """

    def key(record: dict[str, Any]) -> tuple:
        return tuple(record.get(f) for f in key_fields)

    members = {key(r): r for r in current}
    replayed = [e for e in source_events if str(e.get("date", "")) > asof]
    for event in sorted(replayed, key=lambda e: str(e.get("date", "")), reverse=True):
        record = event.get("record") or {}
        kind = event.get("event")
        if kind == "added":
            members.pop(key(record), None)
        elif kind == "removed":
            members[key(record)] = record
        elif kind == "changed" and event.get("previous"):
            members[key(record)] = event["previous"]
    return members


def build_intervals(
    current: list[dict[str, Any]],
    source_events: list[dict[str, Any]],
    boundary: str,
    key_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Interval rows for one source, exactly equivalent to the replay.

    Membership is piecewise constant between event dates, so the state is
    replayed at every breakpoint (the boundary plus each event date) and
    consecutive states are diffed into half-open intervals. Deriving every
    breakpoint state from :func:`members_asof` itself — rather than from a
    second, forward interpretation of the events — makes lookup-equals-replay
    true by construction; the tests assert it anyway.
    """
    breakpoints = sorted({boundary} | {str(e["date"]) for e in source_events})
    if breakpoints[0] != boundary:
        raise PitBuildError(
            f"event dated {breakpoints[0]} precedes the archive boundary {boundary}"
        )
    intervals: list[dict[str, Any]] = []
    open_rows: dict[tuple, dict[str, Any]] = {}
    previous: dict[tuple, dict[str, Any]] = {}
    for date in breakpoints:
        state = members_asof(current, source_events, date, key_fields)
        for k, record in previous.items():
            if state.get(k) != record:
                open_rows.pop(k)["valid_to"] = date
        for k, record in state.items():
            if previous.get(k) != record:
                clash = sorted(set(record) & set(INTERVAL_FIELDS))
                if clash:
                    raise PitBuildError(
                        f"source record carries reserved interval field(s) {clash}: {record!r}"
                    )
                row = {
                    **record,
                    "valid_from": date,
                    "valid_to": None,
                    "provable_from": date != boundary,
                }
                open_rows[k] = row
                intervals.append(row)
        previous = state

    def sort_key(row: dict[str, Any]) -> tuple:
        identity = tuple(str(row.get(f)) for f in key_fields)
        return (identity, row["valid_from"], row["valid_to"] or "9999-12-31")

    return sorted(intervals, key=sort_key)


def build(data_dir: str, log=print) -> dict[str, int]:
    """Replay ``data/symbols/events`` into ``data/symbols/pit/`` and publish.

    Returns {published file name: row count}. Refuses (raises
    :class:`PitBuildError`) rather than publishing anything when the inputs
    fail hash verification or the event archive is empty — an empty stream has
    no boundary date, so an interval table built from it could not say what it
    proves.
    """
    symbols_dir = os.path.join(data_dir, "symbols")
    current_dir = os.path.join(symbols_dir, "current")
    events_dir = os.path.join(symbols_dir, "events")
    pit_dir = os.path.join(symbols_dir, "pit")

    events, events_sha = _verified_records(events_dir, "events.jsonl")
    if not events:
        raise PitBuildError(
            "event archive is empty: no boundary date exists, so point-in-time "
            "membership cannot be published yet"
        )
    earliest = min(str(e.get("date", "")) for e in events)
    boundary = (dt.date.fromisoformat(earliest) - dt.timedelta(days=1)).isoformat()

    staged: dict[str, bytes] = {}
    row_counts: dict[str, int] = {}
    input_sha256: dict[str, str] = {"data/symbols/events/events.jsonl": events_sha}
    for source in sorted(SOURCES):
        current, current_sha = _verified_records(current_dir, f"{source}.jsonl")
        input_sha256[f"data/symbols/current/{source}.jsonl"] = current_sha
        source_events = [e for e in events if e.get("source") == source]
        intervals = build_intervals(current, source_events, boundary, _KEYS[source])
        name = f"{source}.jsonl"
        staged[name] = "".join(
            json.dumps(row, sort_keys=True) + "\n" for row in intervals
        ).encode("utf-8")
        row_counts[name] = len(intervals)
        log(f"pit/{name}: {len(intervals)} intervals ({len(source_events)} events replayed)")

    publish_staged_dataset(
        pit_dir,
        staged,
        source_urls=list(SOURCES.values()),
        license_note=PUBLIC_DOMAIN_NOTE,
        row_counts=row_counts,
        extra={
            "reconstructable_from": boundary,
            "interval_semantics": (
                "half-open: a row is the directory state for its identity key on "
                "every date D with valid_from <= D < valid_to; valid_to null means "
                "still current. Membership at asof = rows whose interval covers asof."
            ),
            "provable_from_note": (
                "false = the row was already present at the archive boundary "
                "(reconstructable_from); its true start predates the event archive "
                "and is NOT provable from this dataset. true = the interval opens "
                "with an archived event."
            ),
            "derivation": (
                "pure function of data/symbols/current + data/symbols/events: the "
                "event stream replayed backward from the current snapshot at every "
                "event date, diffed into intervals; rebuilt in full on every publish"
            ),
            "input_sha256": input_sha256,
        },
    )
    return row_counts
