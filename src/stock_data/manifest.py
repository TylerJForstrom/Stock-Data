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


def read_manifest(dataset_dir: str) -> dict[str, object]:
    with open(os.path.join(dataset_dir, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unknown manifest schema_version {manifest.get('schema_version')!r} "
            f"(supported: {SCHEMA_VERSION})"
        )
    return manifest
