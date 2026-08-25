"""Tests for the pilot module."""

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent
from openai import OpenAIError

from magebench.game.game_export_types import Decision, PilotContext
from magebench.pilot.pilot_recovery import is_context_overflow
from magebench.pilot.pilot import (
    MAX_CHAT_MESSAGES_PER_TURN,
    MAX_CONSECUTIVE_EMPTY_CHOICES,
    MAX_TOKENS,
    PermanentLLMError,
    _prefetch_first_action,
    main,
    run_pilot_loop,
)
from magebench.pilot.pilot_bridge import (
    build_pilot_decision,
    build_pilot_snapshot,
    execute_tool,
    mcp_tools_to_openai,
)
from magebench.pilot.pilot_game_state import extract_oracle_texts_from_board
from magebench.pilot.pilot_rendering import _fetch_state_summary, render_for_pilot
from magebench.pilot.tool_error import ToolExecutionError


def _make_session() -> MagicMock:
    """Create a mock MCP session."""
    session = MagicMock()
    result = CallToolResult(content=[TextContent(type="text", text='{"ok": true}')])
    session.call_tool = AsyncMock(return_value=result)
    return session


def _make_client(error: Exception) -> MagicMock:
    """Create a mock OpenAI client whose chat.completions.create raises *error*."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=error)
    return client


@pytest.mark.asyncio
async def test_execute_tool_raises_on_mcp_failure():
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=RuntimeError("bridge died"))

    with pytest.raises(ToolExecutionError, match="MCP tool pass_priority failed: bridge died"):
        await execute_tool(session, "pass_priority", {})


@pytest.mark.asyncio
async def test_fetch_state_summary_raises_on_error_payload():
    session = MagicMock()
    result = CallToolResult(content=[TextContent(type="text", text='{"error": "bridge died"}')])
    session.call_tool = AsyncMock(return_value=result)

    with pytest.raises(ToolExecutionError, match="get_game_state returned error: bridge died"):
        await _fetch_state_summary(session)


def test_main_accepts_explicit_api_key_for_non_default_provider():
    def fake_run_pilot(*_args, **_kwargs):
        return "run-pilot-sentinel"

    with (
        patch.object(
            sys,
            "argv",
            [
                "pilot",
                "--api-key",
                "sk-test",
                "--provider",
                "openai",
            ],
        ),
        patch("magebench.pilot.pilot.setup_logging"),
        patch("magebench.pilot.pilot.load_prices", return_value={}),
        patch("magebench.pilot.pilot._load_default_system_prompt", return_value="system"),
        patch("magebench.pilot.pilot.run_pilot", new=fake_run_pilot),
        patch("magebench.pilot.pilot.asyncio.run") as run_mock,
    ):
        assert main() == 0

    run_mock.assert_called_once()


def test_main_reports_provider_in_missing_key_log(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.ERROR)

    with (
        patch.object(
            sys,
            "argv",
            [
                "pilot",
                "--provider",
                "openai",
            ],
        ),
        patch("magebench.pilot.pilot.setup_logging"),
        patch.dict("os.environ", {}, clear=True),
    ):
        assert main() == 2

    assert "Missing API key for provider openai" in caplog.text
    assert "configured API key env var" in caplog.text


@pytest.fixture
def _no_prefetch():
    """Patch _prefetch_first_action so run_pilot_loop tests don't block."""
    with patch(
        "magebench.pilot.pilot._prefetch_first_action",
        new_callable=AsyncMock,
        return_value=("Game starting.", None, "{}"),
    ):
        yield


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_401_raises_permanent_failure():
    """A 401 error (user not found / bad API key) should raise PermanentLLMError."""
    session = _make_session()
    client = _make_client(OpenAIError("Error code: 401 - {'error': {'message': 'User not found.', 'code': 401}}"))

    with pytest.raises(PermanentLLMError, match="Credits exhausted"):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=[],
            prices={},
            username="test-player",
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_403_raises_permanent_failure():
    """A 403 error (key quota exceeded) should raise PermanentLLMError."""
    session = _make_session()
    client = _make_client(OpenAIError("Error code: 403 - Forbidden"))

    with pytest.raises(PermanentLLMError, match="Credits exhausted"):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=[],
            prices={},
            username="test-player",
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_402_raises_permanent_failure():
    """A 402 error (credits exhausted) should raise PermanentLLMError."""
    session = _make_session()
    client = _make_client(OpenAIError("Error code: 402 - Payment Required"))

    with pytest.raises(PermanentLLMError, match="Credits exhausted"):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=[],
            prices={},
            username="test-player",
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_404_raises_permanent_failure():
    """A 404 error (model not found) should raise PermanentLLMError."""
    session = _make_session()
    client = _make_client(OpenAIError("Error code: 404 - Not Found"))

    with pytest.raises(PermanentLLMError, match="Model not found"):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=[],
            prices={},
            username="test-player",
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_game_over_from_pass_priority_triggers_auto_pass():
    """When pass_priority returns game_over, pilot should switch to auto-pass."""
    session = _make_session()

    # Mock pass_priority to return game_over
    pass_result = CallToolResult(content=[TextContent(type="text", text='{"game_over": true, "timeout": true}')])
    session.call_tool = AsyncMock(return_value=pass_result)

    # Mock LLM to call pass_priority
    tool_call = MagicMock()
    tool_call.id = "call_1"
    tool_call.function.name = "pass_priority"
    tool_call.function.arguments = '{"timeout_ms": 10000}'

    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    choice.message.tool_calls = [tool_call]
    choice.message.content = None

    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock) as mock_auto_pass:
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "pass_priority", "parameters": {}},
                }
            ],
            prices={},
            username="test-player",
        )
        mock_auto_pass.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_game_over_from_get_action_choices_triggers_auto_pass():
    """When get_action_choices returns game_over, pilot should switch to auto-pass."""
    session = _make_session()

    # Mock get_action_choices to return game_over
    choices_text = '{"action_pending": false, "game_over": true}'
    choices_result = CallToolResult(content=[TextContent(type="text", text=choices_text)])
    session.call_tool = AsyncMock(return_value=choices_result)

    # Mock LLM to call get_action_choices
    tool_call = MagicMock()
    tool_call.id = "call_1"
    tool_call.function.name = "get_action_choices"
    tool_call.function.arguments = "{}"

    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    choice.message.tool_calls = [tool_call]
    choice.message.content = None

    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock) as mock_auto_pass:
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "get_action_choices", "parameters": {}},
                }
            ],
            prices={},
            username="test-player",
        )
        mock_auto_pass.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_game_over_from_choose_action_triggers_auto_pass():
    """When choose_action returns game_over, pilot should switch to auto-pass."""
    session = _make_session()

    # Mock choose_action to return error with game_over
    result_json = (
        '{"success": false, "error": "No pending action after 10s wait",'
        ' "error_code": "no_pending_action", "game_over": true}'
    )
    action_result = CallToolResult(content=[TextContent(type="text", text=result_json)])
    session.call_tool = AsyncMock(return_value=action_result)

    # Mock LLM to call choose_action
    tool_call = MagicMock()
    tool_call.id = "call_1"
    tool_call.function.name = "choose_action"
    tool_call.function.arguments = '{"index": 0}'

    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    choice.message.tool_calls = [tool_call]
    choice.message.content = None

    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock) as mock_auto_pass:
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "choose_action", "parameters": {}},
                }
            ],
            prices={},
            username="test-player",
        )
        mock_auto_pass.assert_called_once()


# --- mcp_tools_to_openai tests ---


def _make_mcp_tool(name: str) -> MagicMock:
    """Create a mock MCP tool definition."""
    tool = MagicMock()
    tool.name = name
    tool.description = f"Description for {name}"
    tool.inputSchema = {"type": "object", "properties": {}}
    return tool


def test_mcp_tools_to_openai_no_filter():
    """With no allowed_tools, should include all MCP tools."""
    mcp_tools = [_make_mcp_tool(name) for name in ["pass_priority", "choose_action", "wait_for_action"]]
    result = mcp_tools_to_openai(mcp_tools)
    names = {t["function"]["name"] for t in result}
    assert names == {"pass_priority", "choose_action", "wait_for_action"}


def test_mcp_tools_to_openai_custom_filter():
    """With custom allowed_tools, should filter to that set."""
    mcp_tools = [_make_mcp_tool(name) for name in ["pass_priority", "choose_action", "get_game_state"]]
    custom = {"pass_priority", "get_game_state"}
    result = mcp_tools_to_openai(mcp_tools, allowed_tools=custom)
    names = {t["function"]["name"] for t in result}
    assert names == {"pass_priority", "get_game_state"}
    assert "choose_action" not in names


# --- _prefetch_first_action tests ---


def _mock_tool_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


@pytest.mark.asyncio
async def test_prefetch_mulligan():
    """Pre-fetch should detect mulligan from pass_priority inline choices."""
    session = MagicMock()

    async def fake_call_tool(name, _args):
        if name == "pass_priority":
            # pass_priority returns choices inline (including message)
            return _mock_tool_result(
                '{"action_pending": true, "action_type": "GAME_ASK", '
                '"message": "Mulligan down to 6 cards?", "choices": []}'
            )
        raise AssertionError(f"Unexpected tool: {name}")

    session.call_tool = AsyncMock(side_effect=fake_call_tool)
    msg, _seq, _blob = await _prefetch_first_action(session)
    assert "Mulligan" in msg
    assert "choose_action" in msg


@pytest.mark.asyncio
async def test_prefetch_waits_for_action():
    """Pre-fetch calls pass_priority once (it blocks) and uses inline choices."""
    session = MagicMock()
    calls = []

    async def fake_call_tool(name, _args):
        calls.append(name)
        if name == "pass_priority":
            # pass_priority returns choices inline
            return _mock_tool_result(
                '{"action_pending": true, "action_type": "GAME_ASK", "message": "Choose play or draw"}'
            )
        raise AssertionError(f"Unexpected tool: {name}")

    session.call_tool = AsyncMock(side_effect=fake_call_tool)
    msg, _seq, _blob = await _prefetch_first_action(session)
    assert "GAME_ASK" in msg
    # Single blocking call, no separate get_action_choices
    assert calls == ["pass_priority"]


@pytest.mark.asyncio
async def test_prefetch_game_over():
    """If pass_priority returns game_over, return a game-over message."""
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_mock_tool_result('{"game_over": true}'))
    msg, _seq, _blob = await _prefetch_first_action(session)
    assert "over" in msg.lower()


# --- consecutive pass_priority error tests ---


def _make_llm_response(tool_name: str, args: str) -> MagicMock:
    """Create a mock LLM response that requests a single tool call."""
    tool_call = MagicMock()
    tool_call.id = f"call_{id(tool_call)}"
    tool_call.function.name = tool_name
    tool_call.function.arguments = args

    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    choice.message.tool_calls = [tool_call]
    choice.message.content = None

    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    response.usage.prompt_tokens_details = None
    response.usage.completion_tokens_details = None
    return response


def _make_truncated_response() -> MagicMock:
    """Create a mock LLM response truncated before it could emit tools."""
    choice = MagicMock()
    choice.finish_reason = "length"
    choice.message.tool_calls = None
    choice.message.content = None

    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=MAX_TOKENS)
    response.usage.prompt_tokens_details = None
    response.usage.completion_tokens_details = None
    return response


_BAD_PASS_ARGS = '{"until":"invalid_value"}'
_PASS_ERROR = '{"error": "Invalid until value: invalid_value"}'
_PASS_OK = '{"action_pending": false, "stop_reason": "passed"}'
_TOOLS = [{"type": "function", "function": {"name": "pass_priority", "parameters": {}}}]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_run_pilot_loop_raises_on_tool_failure():
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=RuntimeError("bridge died"))

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_make_llm_response("pass_priority", "{}"))

    with pytest.raises(ToolExecutionError, match="MCP tool pass_priority failed: bridge died"):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=_TOOLS,
            prices={},
            username="test-player",
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_run_pilot_loop_logs_failed_tool_call_before_reraising():
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=RuntimeError("bridge died"))

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_make_llm_response("choose_action", '{"choice":"no"}'))

    game_log = MagicMock()
    with (
        patch("magebench.pilot.pilot.log_error") as log_error_mock,
        pytest.raises(ToolExecutionError, match="MCP tool choose_action failed: bridge died"),
    ):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "choose_action", "parameters": {}},
                }
            ],
            prices={},
            username="test-player",
            game_dir=Path("/tmp/test-game"),
            game_log=game_log,
        )

    failed_tool_calls = [call for call in game_log.emit.call_args_list if call.args and call.args[0] == "tool_call"]
    assert len(failed_tool_calls) == 1
    failed_call = failed_tool_calls[0]
    assert failed_call.kwargs["tool"] == "choose_action"
    assert failed_call.kwargs["arguments"] == {"choice": "no"}
    failed_result = json.loads(failed_call.kwargs["result"])
    assert failed_result == {
        "success": False,
        "error": "MCP tool choose_action failed: bridge died",
        "error_code": "tool_execution_error",
        "retryable": False,
    }
    game_log.emit.assert_any_call(
        "llm_error",
        error_type="ToolExecutionError",
        error_message="MCP tool choose_action failed: bridge died",
    )
    log_error_mock.assert_called_once()
    assert log_error_mock.call_args.args[3] == "[pilot] Fatal tool error: MCP tool choose_action failed: bridge died"


@pytest.mark.asyncio
async def test_repeated_pass_error_forces_plain_pass():
    """After 3 identical pass_priority errors, pilot forces a plain pass."""
    session = MagicMock()
    tool_calls = []
    pass_call_count = 0

    async def fake_call_tool(name, _args):
        nonlocal pass_call_count
        tool_calls.append((name, dict(_args) if _args else {}))
        if name == "pass_priority":
            pass_call_count += 1
            if pass_call_count == 1:  # prefetch
                return _mock_tool_result('{"action_pending": true, "action_type": "GAME_SELECT"}')
            if pass_call_count in (2, 3, 4):  # LLM turns 1-3: error
                return _mock_tool_result(_PASS_ERROR)
            if pass_call_count == 5:  # forced plain pass
                return _mock_tool_result(_PASS_OK)
            return _mock_tool_result('{"game_over": true}')  # exit
        if name == "get_action_choices":
            return _mock_tool_result('{"action_type": "GAME_SELECT", "message": "Choose"}')
        return _mock_tool_result("{}")

    session.call_tool = AsyncMock(side_effect=fake_call_tool)

    bad_pass = _make_llm_response("pass_priority", _BAD_PASS_ARGS)
    clean_pass = _make_llm_response("pass_priority", "{}")

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=[bad_pass, bad_pass, bad_pass, clean_pass])

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=_TOOLS,
            prices={},
            username="test-player",
        )

    # Verify the forced plain pass was called (pass_priority with empty args)
    pass_calls = [(n, a) for n, a in tool_calls if n == "pass_priority"]
    # pass_calls[0] = prefetch ({}), [1-3] = LLM errors (bad args), [4] = forced ({})
    assert len(pass_calls) >= 5
    assert pass_calls[4] == ("pass_priority", {})


@pytest.mark.asyncio
async def test_different_pass_errors_dont_trigger_forced_pass():
    """Alternating between different errors should not trigger forced pass."""
    session = MagicMock()
    forced_pass_count = 0
    pass_call_count = 0
    error_a = '{"error": "error A"}'
    error_b = '{"error": "error B"}'

    async def fake_call_tool(name, args):
        nonlocal pass_call_count, forced_pass_count
        if name == "pass_priority":
            pass_call_count += 1
            # Detect forced passes: they come with empty args after an error sequence
            if (not args or args == {}) and pass_call_count > 1:  # not prefetch
                forced_pass_count += 1
            if pass_call_count == 1:  # prefetch
                return _mock_tool_result('{"action_pending": true, "action_type": "GAME_SELECT"}')
            if pass_call_count > 5:
                return _mock_tool_result('{"game_over": true}')
            # Alternate between two different errors
            if pass_call_count % 2 == 0:
                return _mock_tool_result(error_a)
            return _mock_tool_result(error_b)
        if name == "get_action_choices":
            return _mock_tool_result('{"action_type": "GAME_SELECT", "message": "Choose"}')
        return _mock_tool_result("{}")

    session.call_tool = AsyncMock(side_effect=fake_call_tool)

    bad_pass = _make_llm_response("pass_priority", '{"until":"bad"}')
    # Use until for the exit call so it's distinguishable from forced {}
    exit_pass = _make_llm_response("pass_priority", '{"until":"my_turn"}')

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=[bad_pass, bad_pass, bad_pass, bad_pass, exit_pass])

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=_TOOLS,
            prices={},
            username="test-player",
        )

    # No forced plain pass should have been made (errors alternate, never 3 identical)
    assert forced_pass_count == 0


@pytest.mark.asyncio
async def test_successful_pass_resets_error_counter():
    """A successful pass between errors should reset the consecutive counter."""
    session = MagicMock()
    forced_pass_count = 0
    pass_call_count = 0

    async def fake_call_tool(name, args):
        nonlocal pass_call_count, forced_pass_count
        if name == "pass_priority":
            pass_call_count += 1
            if (not args or args == {}) and pass_call_count > 1:  # not prefetch
                forced_pass_count += 1
            if pass_call_count == 1:  # prefetch
                return _mock_tool_result('{"action_pending": true, "action_type": "GAME_SELECT"}')
            if pass_call_count in (2, 3):  # 2 errors
                return _mock_tool_result(_PASS_ERROR)
            if pass_call_count == 4:  # success resets counter
                return _mock_tool_result('{"action_pending": true, "stop_reason": "playable_cards"}')
            if pass_call_count in (5, 6):  # 2 more errors (not 3 consecutive)
                return _mock_tool_result(_PASS_ERROR)
            return _mock_tool_result('{"game_over": true}')
        if name == "get_action_choices":
            return _mock_tool_result('{"action_type": "GAME_SELECT", "message": "Choose"}')
        return _mock_tool_result("{}")

    session.call_tool = AsyncMock(side_effect=fake_call_tool)

    bad_pass = _make_llm_response("pass_priority", _BAD_PASS_ARGS)
    # Use until for the "ok" calls so they're distinguishable from forced {}
    ok_pass = _make_llm_response("pass_priority", '{"until":"my_turn"}')

    client = MagicMock()
    # 2 errors, 1 success, 2 errors, then game_over
    client.chat.completions.create = AsyncMock(side_effect=[bad_pass, bad_pass, ok_pass, bad_pass, bad_pass, ok_pass])

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=_TOOLS,
            prices={},
            username="test-player",
        )

    # No forced plain pass should have been made (never hit 3 consecutive)
    assert forced_pass_count == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_repeated_truncation_resets_board_context():
    """Repeated truncation should clear board context before the next pass."""
    session = MagicMock()
    tool_calls: list[tuple[str, dict]] = []

    async def fake_call_tool(name: str, args: dict) -> MagicMock:
        tool_calls.append((name, dict(args)))
        if name != "pass_priority":
            return _mock_tool_result("{}")
        if len([call for call in tool_calls if call[0] == "pass_priority"]) == 1:
            return _mock_tool_result(
                json.dumps(
                    {
                        "action_pending": True,
                        "action_type": "GAME_SELECT",
                        "board": [{"name": "You", "life": 20}],
                        "board_cursor": 7,
                    }
                )
            )
        return _mock_tool_result('{"game_over": true}')

    session.call_tool = AsyncMock(side_effect=fake_call_tool)
    client = MagicMock()

    async def fake_create(**_kwargs):
        if fake_create.calls == 0:
            fake_create.calls += 1
            return _make_llm_response("pass_priority", "{}")
        if fake_create.calls == 1:
            fake_create.calls += 1
            return _make_truncated_response()
        fake_create.calls += 1
        return _make_llm_response("pass_priority", "{}")

    fake_create.calls = 0
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    game_log = MagicMock()

    with (
        patch("magebench.pilot.pilot.MAX_CONSECUTIVE_TRUNCATIONS", 1),
        patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock),
    ):
        await asyncio.wait_for(
            run_pilot_loop(
                session=session,
                client=client,
                model="test-model",
                system_prompt="You are a test.",
                tools=_TOOLS,
                prices={},
                username="test-player",
                game_log=game_log,
            ),
            timeout=2,
        )

    pass_calls = [args for name, args in tool_calls if name == "pass_priority"]
    assert pass_calls == [{}, {}]
    game_log.emit.assert_any_call("context_reset", reason="repeated_truncations")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_repeated_timeout_resets_board_context():
    """Repeated timeout recovery should force a fresh board on the next pass."""
    session = MagicMock()
    tool_calls: list[tuple[str, dict]] = []
    pass_call_count = 0

    async def fake_call_tool(name: str, args: dict) -> MagicMock:
        nonlocal pass_call_count
        tool_calls.append((name, dict(args)))
        if name != "pass_priority":
            return _mock_tool_result("{}")
        pass_call_count += 1
        if pass_call_count == 1:
            return _mock_tool_result(
                json.dumps(
                    {
                        "action_pending": True,
                        "action_type": "GAME_SELECT",
                        "board": [{"name": "You", "life": 20}],
                        "board_cursor": 11,
                    }
                )
            )
        if pass_call_count == 2:
            return _mock_tool_result('{"action_pending": false}')
        return _mock_tool_result('{"game_over": true}')

    session.call_tool = AsyncMock(side_effect=fake_call_tool)
    client = MagicMock()

    async def fake_create(**_kwargs):
        if fake_create.calls == 0:
            fake_create.calls += 1
            return _make_llm_response("pass_priority", "{}")
        if fake_create.calls == 1:
            fake_create.calls += 1
            raise TimeoutError()
        fake_create.calls += 1
        return _make_llm_response("pass_priority", "{}")

    fake_create.calls = 0
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    game_log = MagicMock()

    with (
        patch("magebench.pilot.pilot.MAX_CONSECUTIVE_TIMEOUTS", 1),
        patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock),
    ):
        await asyncio.wait_for(
            run_pilot_loop(
                session=session,
                client=client,
                model="test-model",
                system_prompt="You are a test.",
                tools=_TOOLS,
                prices={},
                username="test-player",
                game_log=game_log,
            ),
            timeout=2,
        )

    pass_calls = [args for name, args in tool_calls if name == "pass_priority"]
    assert pass_calls == [{}, {}, {}]
    game_log.emit.assert_any_call("context_reset", reason="repeated_timeouts")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_transient_llm_error_aborts_the_game(tmp_path):
    """A transient LLM error must abort, not fabricate a move and carry on.

    The old behaviour blind-fired pass_priority and wiped the conversation for any
    error that was not a 401/402/403/404, then kept playing -- recording an action
    the policy never chose, with nothing to distinguish the game from a clean one.
    Observed in practice on context-overflow 400s and on APIConnectionError during a
    server restart, where one game reached GAME_OVER having made zero LLM calls.
    """
    session = MagicMock()
    tool_calls: list[tuple[str, dict]] = []
    pass_call_count = 0

    async def fake_call_tool(name: str, args: dict) -> MagicMock:
        nonlocal pass_call_count
        tool_calls.append((name, dict(args)))
        if name != "pass_priority":
            return _mock_tool_result("{}")
        pass_call_count += 1
        if pass_call_count == 1:
            return _mock_tool_result(
                json.dumps(
                    {
                        "action_pending": True,
                        "action_type": "GAME_SELECT",
                        "board": [{"name": "You", "life": 20}],
                        "board_cursor": 5,
                    }
                )
            )
        if pass_call_count == 2:
            return _mock_tool_result('{"action_pending": false}')
        return _mock_tool_result('{"game_over": true}')

    session.call_tool = AsyncMock(side_effect=fake_call_tool)
    client = MagicMock()

    async def fake_create(**_kwargs):
        if fake_create.calls == 0:
            fake_create.calls += 1
            return _make_llm_response("pass_priority", "{}")
        if fake_create.calls == 1:
            fake_create.calls += 1
            raise OpenAIError("temporary upstream failure")
        fake_create.calls += 1
        return _make_llm_response("pass_priority", "{}")

    fake_create.calls = 0
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    game_dir = tmp_path / "game"
    game_dir.mkdir()

    with (
        patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock),
        pytest.raises(PermanentLLMError),
    ):
        await asyncio.wait_for(
            run_pilot_loop(
                session=session,
                client=client,
                model="test-model",
                system_prompt="You are a test.",
                tools=_TOOLS,
                prices={},
                username="test-player",
                game_dir=game_dir,
            ),
            timeout=2,
        )

    # Exactly the one pass_priority the policy actually asked for -- no blind extra.
    pass_calls = [args for name, args in tool_calls if name == "pass_priority"]
    assert pass_calls == [{}]

    # The marker file is what a consumer scanning game dirs can see; the log and the
    # exit code are invisible to it.
    marker = json.loads((game_dir / "ABORTED.json").read_text())
    assert marker["aborted"] is True
    assert marker["error_type"] == "OpenAIError"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_stall_recovery_preserves_board_context():
    """Stall recovery should keep board cursor state for the next pilot turn."""
    session = MagicMock()
    tool_calls: list[tuple[str, dict]] = []
    pass_call_count = 0

    async def fake_call_tool(name: str, args: dict) -> MagicMock:
        nonlocal pass_call_count
        tool_calls.append((name, dict(args)))
        if name == "send_chat_message":
            return _mock_tool_result('{"success": true}')
        if name != "pass_priority":
            return _mock_tool_result("{}")
        pass_call_count += 1
        if pass_call_count == 1:
            return _mock_tool_result(
                json.dumps(
                    {
                        "action_pending": True,
                        "action_type": "GAME_SELECT",
                        "board": [{"name": "You", "life": 20}],
                        "board_cursor": 9,
                    }
                )
            )
        if pass_call_count == 2:
            return _mock_tool_result('{"action_pending": false}')
        return _mock_tool_result('{"game_over": true}')

    session.call_tool = AsyncMock(side_effect=fake_call_tool)
    client = MagicMock()

    async def fake_create(**_kwargs):
        if fake_create.calls == 0:
            fake_create.calls += 1
            return _make_llm_response("pass_priority", "{}")
        fake_create.calls += 1
        return _make_llm_response("pass_priority", "{}")

    fake_create.calls = 0
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    game_log = MagicMock()

    with (
        patch("magebench.pilot.pilot.MAX_TURNS_WITHOUT_PROGRESS", 1),
        patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock),
    ):
        await asyncio.wait_for(
            run_pilot_loop(
                session=session,
                client=client,
                model="test-model",
                system_prompt="You are a test.",
                tools=_TOOLS,
                prices={},
                username="test-player",
                game_log=game_log,
            ),
            timeout=2,
        )

    pass_calls = [args for name, args in tool_calls if name == "pass_priority"]
    assert pass_calls == [{}, {}, {"board_cursor": 9}]
    # harness_action marks the auto-pass as chosen by the harness rather than the
    # policy, so training data can exclude it.
    game_log.emit.assert_any_call(
        "stall",
        turns_without_progress=1,
        last_tools=["pass_priority"],
        harness_action=True,
    )


# --- Chat rate limiting tests ---


def _make_multi_tool_response(tool_calls_spec: list[tuple[str, str]]) -> MagicMock:
    """Create a mock LLM response with multiple tool calls.

    *tool_calls_spec* is a list of (tool_name, arguments_json) tuples.
    """
    tool_calls = []
    for i, (name, arguments) in enumerate(tool_calls_spec):
        tc = MagicMock()
        tc.id = f"call_{i}"
        tc.function.name = name
        tc.function.arguments = arguments
        tool_calls.append(tc)

    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    choice.message.tool_calls = tool_calls
    choice.message.content = None

    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    response.usage.prompt_tokens_details = None
    response.usage.completion_tokens_details = None
    return response


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_chat_messages_rate_limited_per_turn():
    """send_chat_message calls beyond MAX_CHAT_MESSAGES_PER_TURN should be blocked."""
    session = MagicMock()
    chat_calls_forwarded = 0

    async def fake_call_tool(name, _args):
        nonlocal chat_calls_forwarded
        if name == "send_chat_message":
            chat_calls_forwarded += 1
            return _mock_tool_result('{"success": true}')
        if name == "pass_priority":
            return _mock_tool_result('{"game_over": true}')
        return _mock_tool_result('{"ok": true}')

    session.call_tool = AsyncMock(side_effect=fake_call_tool)

    # LLM sends 4 chat messages + pass_priority in one turn
    chat_count = MAX_CHAT_MESSAGES_PER_TURN + 2
    tool_calls = [("send_chat_message", json.dumps({"message": f"msg {i}"})) for i in range(chat_count)]
    tool_calls.append(("pass_priority", '{"timeout_ms": 10000}'))
    llm_response = _make_multi_tool_response(tool_calls)

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=llm_response)

    tools = [
        {
            "type": "function",
            "function": {"name": "send_chat_message", "parameters": {}},
        },
        {"type": "function", "function": {"name": "pass_priority", "parameters": {}}},
    ]

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=tools,
            prices={},
            username="test-player",
        )

    # Only MAX_CHAT_MESSAGES_PER_TURN should have been forwarded to the bridge
    assert chat_calls_forwarded == MAX_CHAT_MESSAGES_PER_TURN


# --- Pilot rendering tests ---


def _sample_pass_priority_result() -> dict:
    """A realistic pass_priority result with board and choices."""
    return {
        "game_seq": 42,
        "action_type": "GAME_SELECT",
        "context": "T3 Precombat Main/Precombat Main (Alice) YOUR_MAIN",
        "stop_reason": "playable_cards",
        "response_type": "select",
        "respond_with": "choice=pN to play, or choice=no to pass",
        "message": "Play spells and abilities",
        "land_drops_used": 0,
        "action_pending": True,
        "board": [
            {
                "name": "Alice",
                "is_you": True,
                "is_active": True,
                "life": 20,
                "library_size": 50,
                "hand_size": 2,
                "hand": [
                    {
                        "name": "Lightning Bolt",
                        "mana_cost": "{R}",
                        "id": "p3",
                        "rules": ["Lightning Bolt deals 3 damage to any target."],
                    },
                    {
                        "name": "Mountain",
                        "is_land": True,
                        "id": "p5",
                        "rules": ["{T}: Add {R}."],
                    },
                ],
                "battlefield": [{"name": "Mountain", "is_land": True, "id": "p1", "tapped": False}],
            },
            {
                "name": "Bob",
                "is_you": False,
                "is_active": False,
                "life": 18,
                "library_size": 52,
                "hand_size": 2,
                "battlefield": [{"name": "Island", "is_land": True, "id": "p10"}],
            },
        ],
        "choices": [
            {
                "name": "Lightning Bolt",
                "index": 0,
                "action": "cast",
                "mana_cost": "{R}",
                "id": "p3",
            },
            {"name": "Mountain", "index": 1, "action": "land", "id": "p5"},
        ],
    }


class TestRenderForPilot:
    def test_basic_render(self) -> None:
        result = json.dumps(_sample_pass_priority_result())
        text, board = render_for_pilot(result, None)
        assert "Alice" in text
        assert "Lightning Bolt" in text
        assert "Mountain" in text
        assert "id=p3" in text
        assert board is not None

    def test_non_action_passthrough(self) -> None:
        result = json.dumps({"stop_reason": "game_over", "game_over": True})
        text, board = render_for_pilot(result, None)
        # Non-action_pending results pass through as-is
        assert "game_over" in text
        assert board is None

    def test_board_unchanged_uses_last_board(self) -> None:
        # First call: has board
        first_result = json.dumps(_sample_pass_priority_result())
        text1, board = render_for_pilot(first_result, None)
        assert board is not None
        assert "Alice" in text1

        # Second call: no board (board_unchanged)
        no_board = _sample_pass_priority_result()
        del no_board["board"]
        second_result = json.dumps(no_board)
        text2, board2 = render_for_pilot(second_result, board)
        assert "Alice" in text2  # Still renders board from last_board
        assert board2 is board  # Board reference preserved

    def test_respond_with_line(self) -> None:
        result = json.dumps(_sample_pass_priority_result())
        text, _ = render_for_pilot(result, None)
        assert "Respond:" in text

    def test_card_reference_included(self) -> None:
        result = json.dumps(_sample_pass_priority_result())
        text, _ = render_for_pilot(result, None)
        assert "## Card Reference" in text
        assert "3 damage" in text

    def test_invalid_json_passthrough(self) -> None:
        text, board = render_for_pilot("not json", None)
        assert text == "not json"
        assert board is None

    def test_seen_oracle_cards_filters_repeat(self) -> None:
        result = json.dumps(_sample_pass_priority_result())
        seen: set[str] = set()
        text1, board = render_for_pilot(result, None, seen)
        assert "3 damage" in text1
        assert "Lightning Bolt" in seen
        # Second render: oracle text should not repeat
        text2, _ = render_for_pilot(result, board, seen)
        assert "3 damage" not in text2

    def test_seen_oracle_cards_none_always_shows(self) -> None:
        result = json.dumps(_sample_pass_priority_result())
        text1, board = render_for_pilot(result, None, None)
        assert "3 damage" in text1
        text2, _ = render_for_pilot(result, board, None)
        assert "3 damage" in text2


class TestExtractOracleTexts:
    def test_extracts_rules(self) -> None:
        board = _sample_pass_priority_result()["board"]
        texts = extract_oracle_texts_from_board(board)
        assert "Lightning Bolt" in texts
        assert "3 damage" in texts["Lightning Bolt"]["oracle_text"]

    def test_skips_basic_lands(self) -> None:
        board = _sample_pass_priority_result()["board"]
        texts = extract_oracle_texts_from_board(board)
        assert "Mountain" not in texts
        assert "Island" not in texts


class TestBuildPilotDecision:
    def test_parses_context(self) -> None:
        data = _sample_pass_priority_result()
        decision = build_pilot_decision(data)
        assert isinstance(decision, Decision)
        assert decision.turn == 3
        assert "PRECOMBAT" in decision.phase
        assert decision.player == "Alice"

    def test_choices_preserved(self) -> None:
        data = _sample_pass_priority_result()
        decision = build_pilot_decision(data)
        assert isinstance(decision, Decision)
        assert len(decision.choices) == 2

    def test_pilot_context(self) -> None:
        data = _sample_pass_priority_result()
        decision = build_pilot_decision(data)
        assert isinstance(decision, Decision)
        assert isinstance(decision.pilot_context, PilotContext)
        assert decision.pilot_context.land_drops_used == 0

    def test_preserves_empty_pregame_phase_marker(self) -> None:
        data = _sample_pass_priority_result()
        data["context"] = "T1 ()"
        decision = build_pilot_decision(data)
        assert isinstance(decision, Decision)
        assert decision.turn == 1
        assert decision.phase == "()"


class TestBuildPilotSnapshot:
    def test_player_data(self) -> None:
        data = _sample_pass_priority_result()
        decision = build_pilot_decision(data)
        snapshot = build_pilot_snapshot(data, data["board"], decision)
        assert len(snapshot.players) == 2
        assert snapshot.players[0].name == "Alice"
        assert snapshot.players[0].life == 20
        assert len(snapshot.players[0].hand) == 2
        assert snapshot.players[0].hand_count == 2

    def test_no_board(self) -> None:
        data = _sample_pass_priority_result()
        decision = build_pilot_decision(data)
        snapshot = build_pilot_snapshot(data, None, decision)
        assert snapshot.players == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_consecutive_empty_choices_triggers_auto_pass():
    """After MAX_CONSECUTIVE_EMPTY_CHOICES empty responses, pilot should switch to auto-pass."""
    session = _make_session()

    # Mock LLM to always return empty choices
    response = MagicMock()
    response.choices = []
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=0)

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock) as mock_auto_pass:
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=[],
            prices={},
            username="test-player",
        )
        mock_auto_pass.assert_called_once()

    assert client.chat.completions.create.call_count == MAX_CONSECUTIVE_EMPTY_CHOICES


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_context_overflow_resets_and_retries_instead_of_aborting(tmp_path):
    """A context overflow is the prompt outgrowing the server, not the policy failing.

    It must reset and put the SAME decision back through the policy. Critically it must
    not fabricate a move: that is the distinction from the old blind-recovery path, which
    fired pass_priority and recorded an action nobody chose. Only safe now because the
    training layout segments on a reset and reproduces each segment's conditioning.
    """
    session = MagicMock()
    tool_calls: list[tuple[str, dict]] = []
    pass_call_count = 0

    async def fake_call_tool(name: str, args: dict) -> MagicMock:
        nonlocal pass_call_count
        tool_calls.append((name, dict(args)))
        if name != "pass_priority":
            return _mock_tool_result("{}")
        pass_call_count += 1
        if pass_call_count == 1:
            return _mock_tool_result(
                json.dumps({"action_pending": True, "action_type": "GAME_SELECT",
                            "board": [{"name": "You", "life": 20}], "board_cursor": 5})
            )
        return _mock_tool_result('{"game_over": true}')

    session.call_tool = AsyncMock(side_effect=fake_call_tool)
    client = MagicMock()

    async def fake_create(**_kwargs):
        fake_create.calls += 1
        if fake_create.calls == 2:
            raise OpenAIError(
                "Error code: 400 - This model's maximum context length is 32768 tokens. "
                "However, you requested 32769 tokens."
            )
        return _make_llm_response("pass_priority", "{}")

    fake_create.calls = 0
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    game_dir = tmp_path / "game"
    game_dir.mkdir()

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock):
        await asyncio.wait_for(
            run_pilot_loop(
                session=session, client=client, model="test-model",
                system_prompt="You are a test.", tools=_TOOLS, prices={},
                username="test-player", game_dir=game_dir,
            ),
            timeout=5,
        )

    # Survived: no abort marker, and the game reached its own end.
    assert not (game_dir / "ABORTED.json").exists()
    # No fabricated action. Two LLM responses asked for pass_priority (the third call
    # was the overflow, which returned nothing), so exactly two must have been issued.
    # Args are not compared: the harness adds board_cursor for its own routing, which
    # is not the policy choosing anything.
    assert len([n for n, _ in tool_calls if n == "pass_priority"]) == 2
    # It retried the decision rather than skipping it: 1 ok + 1 overflow + 1 retry.
    assert fake_create.calls >= 3


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_repeated_context_overflow_still_aborts(tmp_path):
    """If a freshly reset prompt still overflows, resetting cannot help.

    Without the bound the pilot would reset forever and the batch would hang to its
    deadline -- which is how one dead game cost 30 minutes on step 5.
    """
    session = MagicMock()

    async def fake_call_tool(name: str, args: dict) -> MagicMock:
        if name != "pass_priority":
            return _mock_tool_result("{}")
        return _mock_tool_result(
            json.dumps({"action_pending": True, "action_type": "GAME_SELECT",
                        "board": [{"name": "You", "life": 20}], "board_cursor": 5})
        )

    session.call_tool = AsyncMock(side_effect=fake_call_tool)
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        side_effect=OpenAIError("maximum context length is 32768 tokens")
    )
    game_dir = tmp_path / "game"
    game_dir.mkdir()

    with (
        patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock),
        pytest.raises(PermanentLLMError),
    ):
        await asyncio.wait_for(
            run_pilot_loop(
                session=session, client=client, model="test-model",
                system_prompt="You are a test.", tools=_TOOLS, prices={},
                username="test-player", game_dir=game_dir,
            ),
            timeout=5,
        )
    assert (game_dir / "ABORTED.json").exists()


def test_is_context_overflow_matches_the_server_and_nothing_else():
    """Matched on text because a 400 alone cannot separate an overflow from a bad
    request, and the two need opposite handling."""
    assert is_context_overflow(
        "Error code: 400 - This model's maximum context length is 32768 tokens. "
        "However, you requested 1024 output tokens..."
    )
    assert is_context_overflow("please reduce the length of the messages")
    assert not is_context_overflow("Error code: 400 - invalid tool_choice")
    assert not is_context_overflow("Error code: 404 - model not found")


# --- decision identity: the seq stamp and the gated passthrough ---

_DECISION_RESULTS = [
    '{"action_pending": true, "action_type": "GAME_SELECT", "message": "Play spells", '
    '"game_seq": 6, "choices": []}',
    '{"action_pending": true, "action_type": "GAME_SELECT", "message": "Play spells", '
    '"game_seq": 6, "choices": []}',
    '{"action_pending": true, "action_type": "GAME_SELECT", "message": "Play spells", '
    '"game_seq": 14, "choices": []}',
    '{"game_over": true}',
]


async def _run_scripted_decisions(*, results, decision_identity=False, trace_log=None, prefetch=("Go.", 6, "{}")):
    """Drive run_pilot_loop over a fixed list of tool results; return the request kwargs."""
    seen = {"i": 0}

    async def fake_call_tool(_name, _args):
        i = seen["i"]
        seen["i"] += 1
        return _mock_tool_result(results[min(i, len(results) - 1)])

    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=fake_call_tool)

    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        side_effect=lambda **_kw: _make_llm_response("pass_priority", "{}")
    )

    with (
        patch("magebench.pilot.pilot._prefetch_first_action", new_callable=AsyncMock, return_value=prefetch),
        patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock),
    ):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=_TOOLS,
            prices={},
            username="test-player",
            trace_log=trace_log,
            decision_identity=decision_identity,
        )
    return [c.kwargs for c in client.chat.completions.create.call_args_list]



@pytest.mark.asyncio
async def test_decision_identity_off_leaves_the_request_untouched():
    """Constraint: OFF must mean the request is UNCHANGED, not changed-but-empty.

    Mirrors test_blunder_experiment.py's `assert "extra_body" not in kwargs`. With no
    other extras set, `if extra_body:` leaves the key off create_kwargs entirely, so a
    provider that rejects unknown fields sees exactly the bytes it sees today.
    """
    kwargs_list = await _run_scripted_decisions(results=_DECISION_RESULTS, decision_identity=False)

    assert kwargs_list, "the scripted game produced no LLM calls, so this proves nothing"
    for kwargs in kwargs_list:
        assert "extra_body" not in kwargs


@pytest.mark.asyncio
async def test_decision_identity_on_adds_exactly_the_decision_seq():
    """Positive control for the test above: the same check must FAIL when the flag is on.

    Without this, `extra_body not in kwargs` would also pass if the passthrough were
    silently broken, and an absence is not a negative result.
    """
    kwargs_list = await _run_scripted_decisions(results=_DECISION_RESULTS, decision_identity=True)

    assert [kw["extra_body"] for kw in kwargs_list] == [
        {"magebench_decision": {"game_seq": 6}},
        {"magebench_decision": {"game_seq": 6}},
        {"magebench_decision": {"game_seq": 6}},
        {"magebench_decision": {"game_seq": 14}},
    ]


@pytest.mark.asyncio
async def test_trace_rows_carry_the_decision_seq_and_repeat_within_one_decision():
    """The stamp is per CALL, not a counter, and repeats while one decision is worked on.

    Two calls on seq 6 then one on 14: a decision can take several LLM turns (measured
    153 trace rows over 115 distinct seqs in one recorded game, seq=19 twenty times), so
    a stamp that advanced per row would be a different quantity with the same name.
    Row 1 is stamped from the prefetch, which is why there is no leading None.
    """
    trace = MagicMock()
    trace.emit = MagicMock()

    await _run_scripted_decisions(results=_DECISION_RESULTS, trace_log=trace)

    stamped = [c.kwargs["game_seq"] for c in trace.emit.call_args_list]
    assert stamped == [6, 6, 6, 14]
    assert None not in stamped


@pytest.mark.asyncio
async def test_trace_seq_is_absent_when_the_result_carries_none():
    """Negative control for the test above.

    If the stamp were coming from anywhere but the tool result -- a counter, a constant,
    the writer's own seq -- the assertion above would hold for the wrong reason. Strip
    game_seq from the results and the stamp must go to None.
    """
    trace = MagicMock()
    trace.emit = MagicMock()

    await _run_scripted_decisions(
        results=[
            '{"action_pending": true, "action_type": "GAME_SELECT", "message": "Play", "choices": []}',
            '{"game_over": true}',
        ],
        trace_log=trace,
        prefetch=("Go.", None, "{}"),
    )

    stamped = [c.kwargs["game_seq"] for c in trace.emit.call_args_list]
    assert stamped and set(stamped) == {None}, stamped


@pytest.mark.asyncio
async def test_decision_seq_follows_the_forced_pass_not_the_llm_call():
    """The stamp comes from the result that is RENDERED, after the forced-pass rebind.

    pilot.py's forced-pass path rebinds result_text after the earlier per-tool capture,
    and it is that new text which reaches render_for_pilot. Capturing at the earlier
    site would label the prompt with the decision it replaced. Never fired in the
    recorded corpus (0 forced_pass events across 688 game dirs) -- this test is the only
    thing exercising it.
    """
    trace = MagicMock()
    trace.emit = MagicMock()
    pass_calls = {"n": 0}

    async def fake_call_tool(name, _args):
        if name != "pass_priority":
            return _mock_tool_result('{"action_type": "GAME_SELECT", "message": "Choose"}')
        pass_calls["n"] += 1
        if pass_calls["n"] <= 3:
            return _mock_tool_result(_PASS_ERROR)
        if pass_calls["n"] == 4:  # the forced plain pass, a DIFFERENT decision
            return _mock_tool_result(
                '{"action_pending": true, "action_type": "GAME_SELECT", '
                '"message": "Play", "game_seq": 99, "choices": []}'
            )
        return _mock_tool_result('{"game_over": true}')

    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=fake_call_tool)
    bad = _make_llm_response("pass_priority", _BAD_PASS_ARGS)
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=[bad, bad, bad, bad])

    with (
        patch("magebench.pilot.pilot._prefetch_first_action", new_callable=AsyncMock, return_value=("Go.", 1, "{}")),
        patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock),
    ):
        await run_pilot_loop(
            session=session,
            client=client,
            model="test-model",
            system_prompt="You are a test.",
            tools=_TOOLS,
            prices={},
            username="test-player",
            trace_log=trace,
        )

    stamped = [c.kwargs["game_seq"] for c in trace.emit.call_args_list]
    assert stamped[-1] == 99, stamped


def test_decision_identity_refuses_a_hosted_provider_at_startup(caplog: pytest.LogCaptureFixture):
    """Constraint: fail at startup, not mid-batch.

    OpenRouter/Anthropic/OpenAI reject unknown request fields with a 400, and a 400 found
    on the first LLM call means every game in the batch is played by the error handler.
    Return 2 before asyncio.run, the same shape as the --ignore-providers guard beside it.
    """
    with (
        patch("magebench.pilot.pilot.DECISION_IDENTITY", new=True),
        patch.object(sys, "argv", ["pilot", "--provider", "openrouter", "--api-key", "k"]),
        caplog.at_level(logging.ERROR),
    ):
        assert main() == 2
    assert "MAGEBENCH_DECISION_IDENTITY=1 requires --provider=local" in caplog.text


class TestHarnessPassAdvancesTheDecisionSeq:
    """The harness's OWN pass must move the decision stamp.

    The stamp used to be written only inside _process_tool_calls, which handles the
    POLICY's tool calls. _recover_from_stall and _handle_timeout call execute_tool
    directly, so their pass_priority -- which can answer several decisions at once --
    never moved it. Measured on game_20260818_025636 from its bridge log: the stall
    auto-pass answered server decisions 23, 26, 30, 36 and 39, and the next policy call
    was still stamped 23 while the engine was on 44.

    That is the failure the field exists to prevent, arriving through the path nobody
    reads: a join keyed on it looks exact and is silently wrong.
    """

    @staticmethod
    def _session(result_text: str):
        class _Stub:
            async def call_tool(self, name, args=None):
                class _R:
                    isError = False
                    content = [type("C", (), {"type": "text", "text": result_text})()]
                return _R()
        return _Stub()

    def _state(self):
        from magebench.pilot.pilot_state import PilotLoopState
        st = PilotLoopState(history=[])
        st.last_decision_seq = 23          # the pre-stall value, from the real trace
        return st

    @pytest.mark.asyncio
    async def test_stall_auto_pass_moves_the_stamp(self):
        import logging
        from magebench.pilot.pilot_recovery import _recover_from_stall
        st = self._state()
        await _recover_from_stall(
            self._session('{"game_seq": 44, "action_pending": true}'),
            st, None, set(), logger=logging.getLogger("t"),
        )
        assert st.last_decision_seq == 44, (
            "the harness answered decisions the policy never saw and the stamp stayed "
            f"at {st.last_decision_seq}; every later row would name a consumed decision"
        )

    @pytest.mark.asyncio
    async def test_timeout_auto_pass_moves_the_stamp(self):
        import logging
        from magebench.pilot.pilot_recovery import _handle_timeout
        st = self._state()
        await _handle_timeout(
            self._session('{"game_seq": 44, "action_pending": true}'),
            st, None, logger=logging.getLogger("t"),
            llm_request_timeout_secs=1, max_consecutive_timeouts=99,
        )
        assert st.last_decision_seq == 44

    @pytest.mark.asyncio
    async def test_a_result_without_a_seq_leaves_the_stamp_alone(self):
        """Absent stays absent. 0 is a legal seq, so a sentinel would be indistinguishable
        from decision zero -- the third instance of that encoding error on this project."""
        import logging
        from magebench.pilot.pilot_recovery import _recover_from_stall
        st = self._state()
        await _recover_from_stall(
            self._session('{"action_pending": true}'),
            st, None, set(), logger=logging.getLogger("t"),
        )
        assert st.last_decision_seq == 23, "a seq-less result must not overwrite or zero the stamp"


class TestTheDecisionHeaderCountsDecisions:
    """`[Decision N]` must move. It was a literal 0 on every decision of every game.

    Measured before the fix: 29,906 of 29,906 decisions across 400 rollouts rendered
    as "[Decision 0, snapshot=0]". The header exists to tell the model where it is in
    the game, and any analysis asking "how far back was this card revealed" was
    reading a position field with no position in it.
    """

    def test_the_index_reaches_the_header(self):
        from magebench.pilot.pilot_rendering import render_for_pilot

        result = json.dumps({
            "action_pending": True,
            "context": "T4 Combat Damage (Alice)",
            "choices": [{"id": "p1", "text": "Mountain"}],
            "message": "Choose",
        })

        first, _ = render_for_pilot(result, None, set(), 0)
        fortieth, _ = render_for_pilot(result, None, set(), 39)

        assert "[Decision 0," in first
        assert "[Decision 39," in fortieth

    def test_a_non_decision_result_is_returned_untouched(self):
        from magebench.pilot.pilot_rendering import render_for_pilot

        # No action_pending: a state query between two decisions. It must not render
        # a header, which is also why it must not consume an index.
        result = json.dumps({"board": []})

        text, _ = render_for_pilot(result, None, set(), 7)

        assert text == result

    def test_only_decisions_advance_the_counter(self):
        from magebench.pilot.pilot import _carries_a_decision

        assert _carries_a_decision(json.dumps({"action_pending": True})) is True
        assert _carries_a_decision(json.dumps({"board": []})) is False
        assert _carries_a_decision(json.dumps({"action_pending": False})) is False
        assert _carries_a_decision("not json at all") is False


class TestPromptsLoadFromTheModuleNotTheWorkingDirectory:
    """load_prompts must not depend on where the process happens to be running.

    `Path("puppeteer") / "prompts"` is relative to the CWD, so the defaults were
    found only when something ran from inside the harness checkout. From the mtg
    trunk, a pipeline, or a notebook the directory silently did not exist,
    load_prompts returned {} for the defaults, and the caller died later on a bare
    KeyError: 'default' with nothing in the traceback naming a path.
    """

    def test_defaults_load_from_an_unrelated_working_directory(self, tmp_path, monkeypatch):
        from magebench.pilot.prompts import load_prompts

        monkeypatch.chdir(tmp_path)

        prompts = load_prompts(None)

        assert "default" in prompts, (
            "the repo's own puppeteer/prompts must be found regardless of CWD"
        )
