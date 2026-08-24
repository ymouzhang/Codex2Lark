from __future__ import annotations

import pytest

from codex2lark.runtime.rollout import RootAgentRollout


def test_rollout_is_sticky_and_respects_zero_and_full_percent() -> None:
    stable = RootAgentRollout(canary_version=2, canary_percent=0, salt="salt")
    canary = RootAgentRollout(canary_version=2, canary_percent=100, salt="salt")
    partial = RootAgentRollout(canary_version=2, canary_percent=17, salt="salt")

    assert stable.select("tenant", "app", "chat") == 1
    assert canary.select("tenant", "app", "chat") == 2
    assert partial.select("tenant", "app", "chat") == partial.select("tenant", "app", "chat")
    assert partial.select("tenant", "app", "chat") in {1, 2}


@pytest.mark.parametrize(
    "rollout",
    (
        RootAgentRollout(canary_version=2, canary_percent=0),
        RootAgentRollout(stable_version=3),
    ),
)
def test_disabled_rollout_does_not_require_salt(rollout: RootAgentRollout) -> None:
    assert rollout.select("tenant", "app", "chat") == rollout.stable_version


def test_enabled_rollout_rejects_unsafe_configuration() -> None:
    with pytest.raises(ValueError, match="positive definition"):
        RootAgentRollout(canary_percent=1)
    with pytest.raises(ValueError, match="differ"):
        RootAgentRollout(canary_version=1, canary_percent=1, salt="salt")
    with pytest.raises(ValueError, match="salt"):
        RootAgentRollout(canary_version=2, canary_percent=1)
    with pytest.raises(ValueError, match="between"):
        RootAgentRollout(canary_version=2, canary_percent=101, salt="salt")
