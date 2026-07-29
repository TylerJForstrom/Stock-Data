"""CLI contract tests, including subcommand-first workflow arguments."""

import datetime as dt

from stock_data.cli import main
from stock_data.manifest import write_manifest


def _dataset(tmp_path, name, generated_at):
    dataset = tmp_path / name
    dataset.mkdir()
    write_manifest(
        str(dataset),
        source_urls=[],
        license_note="test",
        generated_at=generated_at,
    )
    return dataset


def test_snapshot_accepts_data_dir_after_subcommand(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        "stock_data.cli.symbols.snapshot",
        lambda data_dir: (called.append(data_dir) or {}, []),
    )

    assert main(["snapshot-symbols", "--data-dir", str(tmp_path)]) == 0
    assert called == [str(tmp_path)]


def test_check_staleness_accepts_subcommand_first_args(tmp_path, capsys):
    fresh = _dataset(tmp_path, "fresh", dt.datetime.now(dt.UTC) + dt.timedelta(days=1))

    result = main(
        [
            "check-staleness",
            "--max-age-days",
            "2",
            str(fresh),
        ]
    )

    assert result == 0
    assert f"FRESH {fresh}" in capsys.readouterr().out


def test_check_staleness_fails_old_or_missing_datasets(tmp_path, capsys):
    old = _dataset(tmp_path, "old", dt.datetime.now(dt.UTC) - dt.timedelta(days=3))
    missing = tmp_path / "missing"

    result = main(
        [
            "check-staleness",
            "--max-age-days",
            "2",
            str(old),
            str(missing),
        ]
    )

    assert result == 1
    stderr = capsys.readouterr().err
    assert f"STALE {old}" in stderr
    assert f"STALE {missing}" in stderr
