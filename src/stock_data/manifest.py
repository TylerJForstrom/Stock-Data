"""Dataset manifests: every dataset directory carries a manifest.json.

The manifest is the contract with consumers (e.g. Stock-Grader's
FoundryProvider): schema version, where the data came from, when it was
fetched, content hashes, and the license note that travels with the data.
Consumers must refuse manifests with an unknown schema_version.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os

from .http import atomic_write_text

SCHEMA_VERSION = "1.0"

PUBLIC_DOMAIN_NOTE = (
    "US-government public-domain source data (17 USC 105); derived work, freely redistributable."
)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(
    dataset_dir: str,
    *,
    source_urls: list[str],
    license_note: str,
    row_counts: dict[str, int] | None = None,
    extra: dict[str, object] | None = None,
    generated_at: dt.datetime | None = None,
) -> dict[str, object]:
    """Hash every data file in ``dataset_dir`` and write manifest.json beside them."""
    generated = generated_at or dt.datetime.now(dt.UTC)
    files = []
    for name in sorted(os.listdir(dataset_dir)):
        path = os.path.join(dataset_dir, name)
        if name == "manifest.json" or name.startswith(".") or not os.path.isfile(path):
            continue
        entry: dict[str, object] = {
            "name": name,
            "sha256": _sha256(path),
            "bytes": os.path.getsize(path),
        }
        if row_counts and name in row_counts:
            entry["rows"] = row_counts[name]
        files.append(entry)
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_urls": sorted(source_urls),
        "license_note": license_note,
        "files": files,
    }
    if extra:
        manifest.update(extra)
    atomic_write_text(
        os.path.join(dataset_dir, "manifest.json"),
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def publish_staged_dataset(
    dataset_dir: str,
    staged: dict[str, bytes],
    *,
    source_urls: list[str],
    license_note: str,
    row_counts: dict[str, int] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Publish new file contents (basename -> bytes) plus a matching manifest.

    Writing data files and only then computing manifest.json leaves a window —
    file writes plus hashing — where a kill publishes data whose manifest
    sha256s do not match, and consumers hard-refuse the dataset until the next
    successful write. So EVERYTHING is staged first: the new contents, copies
    of existing data files this publish does not rewrite (the manifest must
    still cover them), and the manifest computed from those exact staged
    bytes. Only then do renames go back-to-back, manifest LAST so it never
    advertises a file that is not already published. A kill during writing or
    hashing changes nothing; a kill between the final renames desyncs only
    until the next publish rewrites every output and self-heals.
    """
    stage_dir = os.path.join(dataset_dir, ".staging")
    os.makedirs(stage_dir, exist_ok=True)
    for name in os.listdir(stage_dir):  # leftovers from a killed publish
        os.remove(os.path.join(stage_dir, name))
    for name in os.listdir(dataset_dir):
        path = os.path.join(dataset_dir, name)
        if name == "manifest.json" or name in staged or not os.path.isfile(path):
            continue
        if name.startswith("."):
            continue  # hidden files are never manifested (see write_manifest)
        with open(path, "rb") as src, open(os.path.join(stage_dir, name), "wb") as dst:
            dst.write(src.read())
    for name, payload in staged.items():
        with open(os.path.join(stage_dir, name), "wb") as handle:
            handle.write(payload)
    manifest = write_manifest(
        stage_dir,
        source_urls=source_urls,
        license_note=license_note,
        row_counts=row_counts,
        extra=extra,
    )
    # Carried-over copies are byte-identical to what is already published, so
    # only the caller's new files move: fewer renames, smaller window.
    for name in staged:
        os.replace(os.path.join(stage_dir, name), os.path.join(dataset_dir, name))
    os.replace(os.path.join(stage_dir, "manifest.json"), os.path.join(dataset_dir, "manifest.json"))
    for name in os.listdir(stage_dir):  # don't leave copies for workflows to commit
        os.remove(os.path.join(stage_dir, name))
    os.rmdir(stage_dir)
    return manifest


def read_manifest(dataset_dir: str) -> dict[str, object]:
    with open(os.path.join(dataset_dir, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unknown manifest schema_version {manifest.get('schema_version')!r} "
            f"(supported: {SCHEMA_VERSION})"
        )
    return manifest


def manifest_generated_at(dataset_dir: str) -> dt.datetime:
    """Return a dataset manifest's generation time as an aware UTC datetime."""
    manifest = read_manifest(dataset_dir)
    value = manifest.get("generated_at_utc")
    if not isinstance(value, str):
        raise ValueError(f"{dataset_dir}/manifest.json has no generated_at_utc timestamp")
    try:
        generated = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{dataset_dir}/manifest.json has invalid generated_at_utc {value!r}"
        ) from exc
    if generated.tzinfo is None:
        raise ValueError(f"{dataset_dir}/manifest.json generated_at_utc must include a timezone")
    return generated.astimezone(dt.UTC)
