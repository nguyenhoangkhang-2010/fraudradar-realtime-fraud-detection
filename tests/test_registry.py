import json

import pytest

from fraudradar.registry import Registry


def version(root, name):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "lgbm_paysim.txt").write_text("model", encoding="utf-8")
    (folder / "metadata.json").write_text("{}", encoding="utf-8")
    return folder


def test_nothing_is_current_until_a_version_is_promoted(tmp_path):
    reg = Registry(tmp_path)
    assert reg.current() is None and reg.stamp() is None and reg.history() == []


def test_promoting_points_at_the_version_and_moving_back_is_the_rollback(tmp_path):
    reg = Registry(tmp_path)
    v1, v2 = version(tmp_path, "v1"), version(tmp_path, "v2")
    reg.promote("v1", "first")
    stamp = reg.stamp()
    assert reg.current() == v1
    reg.promote("v2", "gate passed")
    assert reg.current() == v2 and reg.stamp() != stamp
    reg.promote("v1", "rollback")
    assert reg.current() == v1
    assert [h["name"] for h in reg.history()] == ["v1", "v2", "v1"] and reg.history()[1]["why"] == "gate passed"
    assert reg.versions() == ["v1", "v2"]


def test_an_incomplete_version_cannot_be_promoted(tmp_path):
    (tmp_path / "broken").mkdir()
    with pytest.raises(FileNotFoundError):
        Registry(tmp_path).promote("broken")


def test_a_pointer_to_a_missing_folder_means_no_current_model(tmp_path):
    (tmp_path / "current.json").write_text(json.dumps({"name": "gone"}), encoding="utf-8")
    assert Registry(tmp_path).current() is None


def test_a_torn_pointer_is_treated_as_absent(tmp_path):
    (tmp_path / "current.json").write_text("{\"na", encoding="utf-8")
    assert Registry(tmp_path).current() is None
