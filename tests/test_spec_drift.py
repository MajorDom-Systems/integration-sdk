from majordom_integration_sdk.spec_drift import diff_specs


def test_detects_each_tier():
    baseline = {(1, 1): "user", (1, 2): "setting", (1, 3): "system"}
    current = {(1, 1): "user", (1, 2): "user", (1, 4): "setting"}  # 1,2 reclassified; 1,3 removed; 1,4 added
    r = diff_specs(current, baseline)
    assert r.added == {(1, 4): "setting"}
    assert r.removed == {(1, 3): "system"}
    assert r.reclassified == {(1, 2): ("setting", "user")}


def test_reclassify_is_high_risk():
    r = diff_specs({(1, 1): "user"}, {(1, 1): "setting"})
    assert r.has_high_risk
    assert not r.is_empty


def test_add_only_is_not_high_risk():
    r = diff_specs({(1, 1): "user", (2, 2): "setting"}, {(1, 1): "user"})
    assert not r.has_high_risk
    assert r.added == {(2, 2): "setting"}


def test_identical_is_empty():
    spec = {(1, 1): "user"}
    r = diff_specs(spec, dict(spec))
    assert r.is_empty
    assert "no drift" in r.render(source="zha")


def test_render_lists_reclassify_first():
    r = diff_specs({(1, 1): "user", (9, 9): "user"}, {(1, 1): "setting"})
    out = r.render(source="zha")
    assert out.index("RECLASSIFY") < out.index("ADD")
