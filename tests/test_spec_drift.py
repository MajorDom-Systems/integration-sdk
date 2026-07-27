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


def test_render_key_label_annotates_each_change():
    r = diff_specs({(0x0556, 1): "user"}, {})
    out = r.render(source="matter-ha", key_label=lambda k: f"Chime.Attr{k[1]}")
    # the human name appears alongside the raw id on the change line
    assert "Chime.Attr1" in out
    assert "(1366, 1)" in out


def test_render_key_label_failure_falls_back_to_id():
    def boom(_key: object) -> str:
        raise KeyError("unknown")

    r = diff_specs({(0x0556, 1): "user"}, {})
    out = r.render(source="matter-ha", key_label=boom)  # must not raise
    assert "(1366, 1)" in out
