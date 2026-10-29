"""What the SDK and integrations keep on disk themselves: JSON, written whole or not at all, for its owner alone."""

import importlib

import pytest

from majordom_integration_sdk.files import read_json, write_json


def test_json_is_written_whole_for_its_owner_alone(tmp_path):
    path = tmp_path / "esphome" / "keys.json"  # an integration's own: the keys it set on its nodes
    write_json(path, {"node": "secret"})
    assert read_json(path) == {"node": "secret"}
    assert path.stat().st_mode & 0o777 == 0o600 and not path.with_name("keys.json.tmp").exists()


def test_missing_or_unreadable_json_reads_as_nothing(tmp_path):
    (tmp_path / "broken.json").write_text("{")
    assert read_json(tmp_path / "missing.json") == read_json(tmp_path / "broken.json") == {}


def test_one_import_path_outside_the_runtime():
    with pytest.raises(ImportError):
        importlib.import_module("majordom_integration_sdk.runtime.files")
