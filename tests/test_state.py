from friday_agent.core.state import Terminal, LoopState, Checkpoint
from friday_agent.messages.types import create_user_message


def test_terminal_reasons_constructible():
    for r in ("completed", "blocking_limit",
              "model_error", "image_error",
              "hook_stopped"):
        assert Terminal(reason=r).reason == r


def test_loopstate_defaults():
    s = LoopState(messages=[create_user_message("hi")])
    assert s.turn_count == 1
    assert len(s.messages) == 1


def test_loopstate_explicit_fields():
    s = LoopState(messages=[], turn_count=5)
    assert s.turn_count == 5
    assert len(s.messages) == 0


def test_checkpoint_wraps_state():
    s = LoopState(messages=[])
    cp = Checkpoint(state=s)
    assert cp.state is s
