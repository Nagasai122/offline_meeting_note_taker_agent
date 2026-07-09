"""Tests for the `meeting-agent reindex` CLI command (cli/main.py), the CLI
counterpart of POST /api/search/reindex in cli/web.py. Both call
cli.semantic_search.refresh_index with the same three paths and surface the
same "run meeting-agent setup" hint on failure -- this file exercises the CLI
side with refresh_index stubbed out (no real embedding model needed) via
typer's CliRunner, mirroring the project's existing preference for testing
CLI commands against stubs rather than real subprocesses/models."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

import cli.main as main_module

runner = CliRunner()


class _FakePaths:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.tmp_dir = data_dir
        self.models_dir = data_dir


class _FakePrivacy:
    tmp_audio_ttl_seconds = 3600


class _FakeSettings:
    """Minimal enough for both `reindex` itself AND cli.main's `_startup`
    callback, which runs before every subcommand (including `reindex`) and
    calls its own load_settings() -- also patched, since both go through the
    same module-level `load_settings` name. `_startup` resolves its own
    `settings_path` default (config/settings.toml relative to cwd), which
    exists in this repo checkout regardless of the path this test passes to
    `reindex`'s own --settings-path, so it does not no-op and does reach
    settings.privacy.tmp_audio_ttl_seconds -- hence _FakePrivacy below, even
    though `reindex` itself never touches privacy."""

    def __init__(self, data_dir: str):
        self.paths = _FakePaths(data_dir)
        self.privacy = _FakePrivacy()


def test_reindex_success_prints_stats(tmp_path):
    settings_path = tmp_path / "settings.toml"
    fake_settings = _FakeSettings(str(tmp_path / "data"))

    with patch.object(main_module, "load_settings", return_value=fake_settings), \
         patch("cli.semantic_search.refresh_index", return_value={"chunks_added": 3, "files_removed": 0}) as mock_refresh:
        result = runner.invoke(main_module.app, ["reindex", "--settings-path", str(settings_path)])

    assert result.exit_code == 0, result.output
    assert "Semantic index updated" in result.output
    assert "chunks_added" in result.output
    mock_refresh.assert_called_once()
    called_args = mock_refresh.call_args[0]
    assert called_args[0] == Path(fake_settings.paths.data_dir) / "meetings"
    assert called_args[1] == Path(fake_settings.paths.data_dir) / "state"
    assert called_args[2] == Path(fake_settings.paths.data_dir) / "semantic_index.db"


def test_reindex_failure_prints_setup_hint_and_exits_nonzero(tmp_path):
    settings_path = tmp_path / "does-not-matter.toml"
    fake_settings = _FakeSettings("some/data/dir")

    with patch.object(main_module, "load_settings", return_value=fake_settings), \
         patch("cli.semantic_search.refresh_index", side_effect=RuntimeError("model not cached")):
        result = runner.invoke(main_module.app, ["reindex", "--settings-path", str(settings_path)])

    assert result.exit_code != 0
    assert "meeting-agent setup" in result.output
    assert "model not cached" in result.output
