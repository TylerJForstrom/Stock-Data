"""Manifest contract tests."""

import datetime as dt
import hashlib
import json

import pytest

from stock_data.manifest import read_manifest, write_manifest


def test_manifest_hashes_and_roundtrip(tmp_path):
    data = tmp_path / "dataset"
    data.mkdir()
    payload = b"ticker,close\nAAPL,1.0\n"
    (data / "prices.csv").write_bytes(payload)

    written = write_manifest(
        str(data),
        source_urls=["https://example.gov/b", "https://example.gov/a"],
        license_note="public domain",
        row_counts={"prices.csv": 1},
        generated_at=dt.datetime(2026, 7, 28, tzinfo=dt.UTC),
    )
    assert written["source_urls"] == ["https://example.gov/a", "https://example.gov/b"]
    entry = written["files"][0]
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry["rows"] == 1

    read_back = read_manifest(str(data))
    assert read_back == written


def test_unknown_schema_version_refused(tmp_path):
    data = tmp_path / "dataset"
    data.mkdir()
    (data / "manifest.json").write_text(json.dumps({"schema_version": "99.0"}))
    with pytest.raises(ValueError, match="unknown manifest schema_version"):
        read_manifest(str(data))


def test_manifest_excludes_itself_and_hidden_files(tmp_path):
    data = tmp_path / "dataset"
    data.mkdir()
    (data / "real.jsonl").write_text("{}\n")
    (data / ".tmp.partial").write_text("junk")
    written = write_manifest(str(data), source_urls=[], license_note="x")
    assert [f["name"] for f in written["files"]] == ["real.jsonl"]
