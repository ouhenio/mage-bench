"""An unoffered tool name costs one redraw, not the game.

Built on the schema eval's g14, guarded arm: a response with a VALID first call
(`get_action_choices`) and an INVENTED second one (`(http://127.0.0.1:5000/api/v2/popular)`).
The bridge answered the second with "Unknown tool", the pilot raised, and the game aborted with
no game_end. Root cause is upstream (vLLM's Qwen3 structural-tag END begins with the newline
BEGIN already consumed, so after a zero-argument call the tag never closes); this is the
pilot-side backstop.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace as NS

from magebench.pilot.cap_retry import (
    cap_hit_with_call_open,
    should_retry,
    should_retry_unoffered,
    unoffered_tool_calls,
)

OFFERED = {"get_game_log", "pass_priority", "get_game_state", "get_oracle_text",
           "get_action_choices", "choose_action"}
INVENTED = "(http://127.0.0.1:5000/api/v2/popular)"
LOG = logging.getLogger("test")


def _choice(*names, finish="tool_calls"):
    calls = [NS(function=NS(name=n, arguments="{}")) for n in names]
    return NS(finish_reason=finish, message=NS(tool_calls=calls, content=""))


def _state(seq=5):
    return NS(last_decision_seq=seq, cap_retry_used=False, cap_retry_decision_seq=None)


def test_g14_exactly_the_second_call_is_caught():
    assert unoffered_tool_calls(_choice("get_action_choices", INVENTED), offered=OFFERED) == [INVENTED]


def test_the_existing_cap_check_MISSES_g14__which_is_why_this_exists():
    """CONTROL ON THE CONTROL. If the cap-hit check already caught g14, the new code would be
    solving a problem that was not there. It does not: it inspects tool_calls[0] only, g14's
    first call is valid, and it was a complete 49-token call far below any cap."""
    resp = NS(usage=NS(completion_tokens=49))
    hit, _ = cap_hit_with_call_open(_choice("get_action_choices", INVENTED), resp,
                                    max_tokens=4096, offered=OFFERED)
    assert hit is False


def test_an_invented_FIRST_call_is_caught_too():
    assert unoffered_tool_calls(_choice(INVENTED), offered=OFFERED) == [INVENTED]


def test_only_offered_names_is_nothing():
    assert unoffered_tool_calls(_choice("get_action_choices", "choose_action"), offered=OFFERED) == []


def test_every_unoffered_name_is_reported_in_order():
    assert unoffered_tool_calls(_choice("choose_action", "get_api_state", INVENTED),
                                offered=OFFERED) == ["get_api_state", INVENTED]


def test_one_redraw_per_decision_then_the_existing_fatal_path():
    st = _state()
    assert should_retry_unoffered(st, [INVENTED], logger=LOG, temperature=0.7) is True
    assert should_retry_unoffered(st, [INVENTED], logger=LOG, temperature=0.7) is False


def test_a_new_decision_gets_its_own_redraw():
    st = _state(seq=5)
    assert should_retry_unoffered(st, [INVENTED], logger=LOG, temperature=0.7) is True
    st.last_decision_seq = 6
    assert should_retry_unoffered(st, [INVENTED], logger=LOG, temperature=0.7) is True


def test_the_budget_is_SHARED_with_cap_hits__a_second_cause_cannot_open_a_loop():
    st = _state()
    assert should_retry(st, {"tool": "choose_action"}, logger=LOG, temperature=0.7) is True
    assert should_retry_unoffered(st, [INVENTED], logger=LOG, temperature=0.7) is False
