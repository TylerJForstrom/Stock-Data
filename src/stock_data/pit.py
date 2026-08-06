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
``provable_from: true``.

The boundary is computed PER SOURCE, not once for the dataset. A source added
to ``SOURCES`` later than the archive start has no events before its first
fetch (``symbols.snapshot`` deliberately emits no "added" storm for a
first-ever snapshot), so a single global boundary would open all of its rows
at a date on which the foundry had never fetched it — exactly the "today's
survivors dressed up as history" this boundary exists to prevent. So each
source's floor is ``max(global boundary, the date that source was first
observed)``, tracked by ``symbols.snapshot`` as ``first_observed`` in
``data/symbols/current/manifest.json``. The pit manifest publishes the whole
map as ``reconstructable_from_by_source``; the scalar ``reconstructable_from``
stays, as the conservative whole-dataset floor (the LATEST per-source floor),
so a consumer that only reads the scalar refuses too much rather than too
little. Consumers must refuse any earlier ``asof`` rather than serve rows the
archive cannot testify to.

Same-date ordering: two snapshot runs on one UTC day (the workflow has both a
``schedule:`` and a ``workflow_dispatch:``) emit two events for the same
identity key on the same date — ``added`` then ``removed``, or two ``changed``
events. A BACKWARD replay must un-apply those in reverse order, so the replay
orders events by ``(date, append position)`` and walks that descending.
``events.jsonl`` is append-only, so a line's position in the file is its
chronological order — the same property ``symbols.snapshot``'s dedupe already
rests on.

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

#: The event kinds the replay knows how to reverse. An unrecognized kind is a
#: refusal, never a no-op: silently skipping it publishes the post-change
#: record as having been in effect before the change ever happened.
REPLAYABLE_EVENTS = ("added", "removed", "changed")

#: Ordinal stamped by :func:`build` onto each parsed event (its line number in
#: events.jsonl). Never published — it lives on the event, not on the record —
#: it just lets the replay order same-date events after the stream has been
#: filtered down to one source.
SEQ_FIELD = "_seq"


class PitBuildError(RuntimeError):
    """Refusal: inputs unverifiable, malformed, or unable to testify."""


def _event_date(event: dict[str, Any]) -> str:
    """The event's date as an ISO string, or a refusal.

    Every date in the stream is load-bearing — it places the event on the
    timeline and it fixes the archive boundary — so an event that cannot say
    when it happened stops the build instead of escaping as a bare
    ``KeyError``/``ValueError`` past :func:`cli.main`'s refusal handler.
    """
    raw = event.get("date")
    if not isinstance(raw, str):
        raise PitBuildError(f"event has no date: {event!r}")
    try:
        dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise PitBuildError(f"event has an unparseable date {raw!r}: {event!r}") from exc
    return raw


def _replay_order(source_events: list[dict[str, Any]]) -> list[tuple[tuple[str, int], dict]]:
    """Pair every event with its replay-order key ``(date, append position)``.

    A backward replay must un-apply same-date events in reverse order, and the
    only chronology finer than the date is the order they were appended in.
    :func:`build` stamps :data:`SEQ_FIELD` while the stream is still whole, so
    the ordering survives the per-source filtering; an unstamped list is taken
    at its word that it is already in file order. A stream that mixes the two
    has no single ordering and is refused rather than guessed at.
    """
    stamped = [event.get(SEQ_FIELD) for event in source_events]
    if any(s is not None for s in stamped) and any(s is None for s in stamped):
        raise PitBuildError(
            "event stream mixes sequenced and unsequenced events; same-date "
            "events cannot be ordered for a backward replay"
        )
    return [
        ((_event_date(event), position if seq is None else int(seq)), event)
        for position, (seq, event) in enumerate(zip(stamped, source_events, strict=True))
    ]


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
    here, beside the event producer. Events dated after ``asof`` are reversed
    NEWEST FIRST: additions removed, removals restored, changes reverted to
    their recorded previous state. Only this source's events participate.

    Ordering within a single date is NOT immaterial: a second snapshot run on
    the same UTC day appends a second event for the same identity key with the
    same date (``added`` then ``removed``, or two ``changed`` events), and
    un-applying those in append order rather than reverse-append order
    reinstates the very state the pair cancelled. ``source_events`` must
    therefore be in append order (or carry :data:`SEQ_FIELD`); see
    :func:`_replay_order`.
    """

    def key(record: dict[str, Any]) -> tuple:
        return tuple(record.get(f) for f in key_fields)

    members = {key(r): r for r in current}
    replayed = [(order, e) for order, e in _replay_order(source_events) if order[0] > asof]
    for _, event in sorted(replayed, key=lambda pair: pair[0], reverse=True):
        record = event.get("record") or {}
        kind = event.get("event")
        if kind == "added":
            members.pop(key(record), None)
        elif kind == "removed":
            members[key(record)] = record
        elif kind == "changed":
            previous = event.get("previous")
            if not previous:
                raise PitBuildError(
                    f"'changed' event carries no previous state, so it cannot be "
                    f"reversed: {event!r}"
                )
            members[key(record)] = previous
        else:
            raise PitBuildError(
                f"unrecognized event kind {kind!r} (replayable: "
                f"{list(REPLAYABLE_EVENTS)}); refusing to publish a history that "
                f"silently ignores it: {event!r}"
            )
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

    ``boundary`` is THIS source's floor (see :func:`build`), not necessarily
    the archive-wide one.
    """
    breakpoints = sorted({boundary} | {_event_date(e) for e in source_events})
    if breakpoints[0] != boundary:
        raise PitBuildError(
            f"event dated {breakpoints[0]} precedes this source's archive boundary {boundary}"
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


def _first_observed(current_dir: str) -> dict[str, str]:
    """``{source: first date the foundry ever snapshotted it}``, if recorded.

    Written by :func:`symbols.snapshot` on a source's first-ever successful
    fetch and carried forward from then on. A source snapshotted before this
    was tracked has no entry; its floor stays the archive-wide boundary, which
    is honest for it (it was being fetched throughout the archive) and is
    exactly what was published before per-source floors existed.
    """
    manifest = read_manifest(current_dir)
    recorded = manifest.get("first_observed")
    if not isinstance(recorded, dict):
        return {}
    observed: dict[str, str] = {}
    for source, date in recorded.items():
        if not isinstance(date, str):
            raise PitBuildError(f"first_observed[{source!r}] is not a date: {date!r}")
        try:
            dt.date.fromisoformat(date)
        except ValueError as exc:
            raise PitBuildError(
                f"first_observed[{source!r}] is an unparseable date {date!r}"
            ) from exc
        observed[str(source)] = date
    return observed


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
    # Stamp append order while the stream is whole: it is the only chronology
    # finer than the date, and the per-source filtering below would otherwise
    # leave same-date events unorderable.
    for position, event in enumerate(events):
        event[SEQ_FIELD] = position
    earliest = min(_event_date(e) for e in events)
    boundary = (dt.date.fromisoformat(earliest) - dt.timedelta(days=1)).isoformat()
    first_observed = _first_observed(current_dir)

    staged: dict[str, bytes] = {}
    row_counts: dict[str, int] = {}
    boundaries: dict[str, str] = {}
    input_sha256: dict[str, str] = {"data/symbols/events/events.jsonl": events_sha}
    for source in sorted(SOURCES):
        current, current_sha = _verified_records(current_dir, f"{source}.jsonl")
        input_sha256[f"data/symbols/current/{source}.jsonl"] = current_sha
        source_events = [e for e in events if e.get("source") == source]
        # A source first fetched after the archive started cannot testify to
        # the archive-wide boundary; its own first-observed date is the floor.
        source_boundary = max(boundary, first_observed.get(source, boundary))
        boundaries[source] = source_boundary
        intervals = build_intervals(current, source_events, source_boundary, _KEYS[source])
        name = f"{source}.jsonl"
        staged[name] = "".join(json.dumps(row, sort_keys=True) + "\n" for row in intervals).encode(
            "utf-8"
        )
        row_counts[name] = len(intervals)
        log(
            f"pit/{name}: {len(intervals)} intervals "
            f"({len(source_events)} events replayed, from {source_boundary})"
        )

    publish_staged_dataset(
        pit_dir,
        staged,
        source_urls=list(SOURCES.values()),
        license_note=PUBLIC_DOMAIN_NOTE,
        row_counts=row_counts,
        extra={
            # Conservative scalar: the LATEST per-source floor, so a consumer
            # that reads only this refuses more than it must, never less.
            "reconstructable_from": max(boundaries.values()),
            "reconstructable_from_by_source": boundaries,
            "reconstructable_from_note": (
                "reconstructable_from_by_source is the honest per-source floor: "
                "max(archive boundary, the date that source was first snapshotted). "
                "The scalar reconstructable_from is the latest of those — the "
                "conservative whole-dataset floor for consumers that do not read "
                "the per-source map. Refuse any asof earlier than a source's floor."
            ),
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
            "event_ordering": (
                "events are un-applied newest-first by (date, position in the "
                "append-only events.jsonl); two snapshot runs on one UTC day emit "
                "two same-date events for one identity key, and the second must be "
                "reversed first"
            ),
            "input_sha256": input_sha256,
        },
    )
    return row_counts
