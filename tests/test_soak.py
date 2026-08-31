from wecom_ai_gateway.soak import should_monitor_instance, sustained_unhealthy, update_unhealthy_since


def test_soak_only_monitors_instances_expected_to_run():
    assert should_monitor_instance(True) is True
    assert should_monitor_instance(False) is False


def test_soak_state_tracks_only_continuous_unhealthy_periods():
    state = update_unhealthy_since({}, {"a": "online", "b": "error"}, now=10)
    assert state == {"b": 10}

    state = update_unhealthy_since(state, {"a": "reconnecting", "b": "error"}, now=20)
    assert state == {"a": 20, "b": 10}

    state = update_unhealthy_since(state, {"a": "online", "b": "online"}, now=30)
    assert state == {}


def test_soak_flags_only_after_grace_period():
    state = {"a": 10, "b": 25}

    assert sustained_unhealthy(state, now=30, grace_seconds=21) == []
    assert sustained_unhealthy(state, now=31, grace_seconds=21) == ["a"]
