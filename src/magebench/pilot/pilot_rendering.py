"""Prompt rendering and context-window helpers for the pilot loop."""

import json
import os

from mcp import ClientSession

from magebench.game.decision_renderer import render_decision
from magebench.pilot.pilot_bridge import (
    build_pilot_decision,
    build_pilot_snapshot,
    execute_tool,
)
from magebench.pilot.pilot_game_state import extract_oracle_texts_from_board
from magebench.pilot.tool_error import ToolExecutionError

# vLLM validates prompt_tokens + max_tokens <= max_model_len, so this value is
# subtracted from the usable prompt budget on every request. At 20_000 against a
# 32768 context the real prompt ceiling was 12,768 tokens, and games were walking
# into it: prompts grow ~50 tok/call, and the peak observed was 12,758 -- ten
# tokens of headroom. Seven 400s fired across four games of a live baseline.
#
# Those 400s used to be swallowed. A 400 is not in _classify_permanent_llm_failure's
# permanent set, so the pilot fired a blind pass_priority, wiped the conversation,
# and finished the game looking healthy. That recovery path is gone -- any LLM error
# now aborts, see the OpenAIError handler in run_pilot_loop -- so an overflow would
# fail loudly today rather than silently. 1024 keeps it from arising at all:
# measured completion length with thinking disabled is ~21 tokens mean, ~102 max,
# so this is ~10x headroom while returning ~19k tokens of prompt budget.
MAX_TOKENS = 1024


# Context window management.
# History is append-only; before each LLM call we render a bounded context
# window from history: recent messages at full fidelity, older messages
# with tool results summarised to save tokens.
CONTEXT_RECENT_COUNT = 40
CONTEXT_SUMMARY_COUNT = 20
TOOL_SUMMARY_TRIGGER_CHARS = 200
RENDER_INTERVAL = 5

_CACHE_BREAKPOINT_MARKER = "All cards listed are playable right now."


def render_for_pilot(
    result_text: str,
    last_board: list[dict] | None,
    seen_oracle_cards: set[str] | None = None,
) -> tuple[str, list[dict] | None]:
    """Render an action result for LLM consumption."""
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return result_text, last_board

    if not isinstance(data, dict) or not data.get("action_pending"):
        return result_text, last_board

    board = data.get("board")
    if isinstance(board, list):
        last_board = board
    elif board is None:
        board = last_board
    else:
        board = last_board

    # `board` is already resolved above (this result's, or the last one carried
    # forward). The decision must see the same board the snapshot does, or it
    # cannot identify which player is the pilot.
    decision = build_pilot_decision(data, board)
    snapshot = build_pilot_snapshot(data, board, decision)

    oracle_texts = extract_oracle_texts_from_board(board) if board else {}
    if seen_oracle_cards is not None:
        oracle_texts = {k: v for k, v in oracle_texts.items() if k not in seen_oracle_cards}
        seen_oracle_cards.update(oracle_texts)

    rendered = render_decision(
        decision,
        snapshot,
        oracle_texts=oracle_texts,
        deciding_player=decision.player,
        include_card_reference=True,
    )

    lines = [rendered]
    resp_type = data.get("response_type")
    respond_with = data.get("respond_with")
    if respond_with:
        total_min = data.get("total_min")
        total_max = data.get("total_max")
        if total_min is not None and total_max is not None and total_min == total_max:
            respond_with = respond_with.replace(
                "sum between total_min and total_max",
                f"sum must equal total ({total_min})",
            )
        lines.append(f"  Respond: {respond_with}")
    elif resp_type:
        lines.append(f"  Response type: {resp_type}")

    mana_pool = data.get("mana_pool")
    if mana_pool and any(v > 0 for v in mana_pool.values()):
        pool_str = ", ".join(f"{k}={v}" for k, v in mana_pool.items() if v > 0)
        lines.append(f"  Mana pool: {pool_str}")

    recent_chat = data.get("recent_chat")
    if recent_chat:
        lines.append("  Recent chat: " + " | ".join(recent_chat))

    return "\n".join(lines), last_board


def _summarize_tool_result(tool_name: str, content: str) -> str:
    """Compress a tool result to a short summary for older context entries."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content

    if tool_name == "pass_priority":
        if data.get("player_dead"):
            return "player_dead"
        if data.get("action_pending"):
            stop = data.get("stop_reason")
            action_type = data.get("action_type", "?")
            parts = []
            if stop:
                parts.append(f"action_pending({action_type}, {stop})")
            else:
                parts.append(f"action_pending({action_type})")
            resp_type = data.get("response_type")
            if resp_type:
                parts.append(resp_type)
            choices = data.get("choices")
            if choices:
                names = [c.get("name", c.get("description", "?"))[:30] for c in choices[:3]]
                parts.append(f"{len(choices)} choices: {', '.join(names)}")
            msg = data.get("message")
            if msg and not choices:
                parts.append(msg[:60])
            return "; ".join(parts)
        stop = data.get("stop_reason")
        if isinstance(stop, str) and stop:
            return stop
        return "passed"

    if tool_name == "choose_action":
        if data.get("success"):
            summary = f"OK: {data.get('action_taken', '?')}"
            if data.get("mana_plan_set"):
                summary += f" (mana_plan: {data.get('mana_plan_size', '?')} entries)"
            return summary
        return f"FAIL: {data.get('error', '?')[:100]}"

    if tool_name == "get_action_choices":
        parts = [data.get("action_type", "?")]
        resp_type = data.get("response_type")
        if resp_type:
            parts.append(resp_type)
        choices = data.get("choices")
        if choices:
            names = [c.get("name", c.get("description", "?"))[:30] for c in choices[:3]]
            parts.append(f"{len(choices)} choices: {', '.join(names)}")
        msg = data.get("message")
        if msg and not choices:
            parts.append(msg[:60])
        return "; ".join(parts)

    if tool_name == "get_game_state":
        parts = []
        if "turn" in data:
            parts.append(f"T{data['turn']}")
        if "phase" in data:
            parts.append(data["phase"])
        players = data.get("players")
        if players is not None:
            for p in players:
                name = p.get("name", "?")
                life = p.get("life", "?")
                bf_zone = p.get("battlefield")
                bf = len(bf_zone) if bf_zone is not None else 0
                parts.append(f"{name}:{life}hp/{bf}perm")
        return "; ".join(parts) if parts else content

    if tool_name == "get_game_log":
        total = data.get("total_length", "?")
        truncated = data.get("truncated", False)
        since = data.get("since_turn")
        log_text = data.get("log")
        prefix = f"log({total} chars"
        if since is not None:
            prefix += f", since_turn={since}"
        if truncated:
            prefix += ", truncated"
        prefix += "): "
        if log_text:
            lines = [line.strip() for line in log_text.splitlines() if line.strip()]
            if lines:
                excerpt = " / ".join(lines[:4])
                if len(lines) > 4:
                    excerpt += " / ..."
                return prefix + excerpt
        return prefix.rstrip(": ")

    return content


def _find_tool_name(history: list[dict], tool_result_idx: int, tool_call_id: str) -> str:
    """Find the tool name for a tool result by searching backward for its assistant message."""
    for idx in range(tool_result_idx - 1, -1, -1):
        msg = history[idx]
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if tool_calls is None:
            continue
        for tool_call in tool_calls:
            if tool_call.get("id") != tool_call_id:
                continue
            function = tool_call.get("function")
            assert isinstance(function, dict), (
                f"assistant tool call {tool_call_id!r} missing function payload: {tool_call!r}"
            )
            name = function.get("name")
            assert isinstance(name, str), f"assistant tool call {tool_call_id!r} missing function name: {tool_call!r}"
            return name
        break
    return ""


def extract_last_reasoning(history: list[dict]) -> str:
    """Extract the last assistant reasoning text from history (for context resets)."""
    for msg in reversed(history):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content:
            return content[:300]
    return ""


def build_reset_message(base_text: str, last_reasoning: str) -> str:
    """Build the user message for a context reset."""
    parts = [base_text]
    if last_reasoning:
        parts.append(f"Before your context was reset, you were thinking: {last_reasoning}")
    return "\n\n".join(parts)


def _with_cache_control(msg: dict, cache_control: dict) -> dict:
    """Return a copy of msg with cache_control added to its content."""
    role = msg["role"]
    content = msg.get("content")

    if role in ("user", "tool"):
        if isinstance(content, str):
            return {
                **msg,
                "content": [{"type": "text", "text": content, "cache_control": cache_control}],
            }
        if isinstance(content, list):
            new_content = [dict(block) for block in content]
            for block in reversed(new_content):
                if isinstance(block, dict) and block.get("type") == "text":
                    block["cache_control"] = cache_control
                    break
            return {**msg, "content": new_content}
    elif role == "assistant" and isinstance(content, str) and content:
        return {
            **msg,
            "content": [{"type": "text", "text": content, "cache_control": cache_control}],
        }

    return msg


def _message_text(msg: dict) -> str:
    """Extract concatenated text content from a rendered message."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block["text"] for block in content if isinstance(block, dict) and block.get("type") == "text")
    return ""


def _find_cache_breakpoint_idx(messages: list[dict]) -> int:
    """Return the message index that ends the stable cacheable prefix."""
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user" and _CACHE_BREAKPOINT_MARKER in _message_text(messages[idx]):
            return idx
    return len(messages) - 1


def render_context(
    history: list[dict],
    system_prompt: str,
    state_summary: str,
    cache_control: dict | None = None,
) -> list[dict]:
    """Build the LLM messages list from append-only history."""
    messages: list[dict]
    if cache_control:
        messages = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": cache_control,
                    }
                ],
            }
        ]
    else:
        messages = [{"role": "system", "content": system_prompt}]

    # Append-only: the prompt only ever grows at the end, so decision k's prompt is
    # exactly decision k-1's prompt plus k-1's response plus the new observation.
    # That makes a whole game one training row instead of one row per decision.
    #
    # This is NOT a free optimisation. The model currently sees a truncated window
    # and here sees full history -- a different input, so different behaviour. The
    # count of training signals is unchanged; the signals are not. It has to be
    # A/B'd on paired seeds, not banked.
    #
    # Refuses rather than truncates: on overrun the chat template drops from the
    # HEAD, which is the system prompt and the tool grammar. A model told nothing
    # and asked to emit a tool call looks like a capability collapse, and the only
    # signal is a warning nobody reads.
    # Default ON as of 2026-08-17: ouhenio adopted append-only after the paired
    # A/B (8 identical deals, every behavioural interval crossing zero, ~39x less
    # compute). Opt OUT with MAGEBENCH_APPEND_ONLY=0 -- the windowed path is kept
    # deliberately, because it is the reference arm for re-running that A/B and
    # for re-measuring every baseline taken before today. Not dead code.
    if os.environ.get("MAGEBENCH_APPEND_ONLY", "1") != "0":
        messages.extend(history)
        # MAGEBENCH_CONTEXT_LIMIT is the SERVER's max_model_len, not a budget we
        # pick. It had a default of 40960 here, which was the server's setting the
        # day it was written; the server moved to 32768 and the guard did not, so it
        # sat 25% above the real ceiling and could never fire before vLLM rejected
        # the request. A live run lost a game to exactly that, over by ONE token
        # (31,745 prompt + 1,024 max_tokens = 32,769 against 32,768).
        #
        # So: no default. rollout_games.sh reads it from /v1/models, which is the
        # only source that cannot go stale. A number restated in a second place is a
        # number that will disagree with the first.
        limit = os.environ.get("MAGEBENCH_CONTEXT_LIMIT")
        assert limit, (
            "MAGEBENCH_CONTEXT_LIMIT is unset and append-only rendering needs it. Set it to "
            "the serving engine's max_model_len -- `curl -s $BASE_URL/models` reports it as "
            "max_model_len. Do not guess: a guard above the real ceiling never fires."
        )
        budget = int(limit) - MAX_TOKENS
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        # chars//3 is not a token count and is biased LOW, which is the wrong direction
        # for a guard. Measured against `usage.prompt_tokens` over 276 real calls from a
        # live run: chars//3 / actual is median 0.834, mean 0.818, max 0.870, and 0.867
        # on the >20k prompts that matter here. So actual can be up to ~1.22x the
        # estimate, and an unscaled guard trips ~4k tokens AFTER the server has already
        # refused the request -- which is what it is supposed to prevent.
        # Scaling by the worst observed ratio makes it fire slightly early rather than
        # slightly late. Early costs a game; late costs the guard its entire purpose.
        approx = int(chars / 3 / 0.818)
        assert approx < budget, (
            f"append-only prompt ~{approx} tokens (from {chars} chars) exceeds {budget} "
            f"(context limit {limit} minus {MAX_TOKENS} reserved for the completion, which "
            f"vLLM counts against the same ceiling): head truncation would silently drop the "
            f"system prompt and tool grammar. Shorten the game, or raise the limit AND the "
            f"training row capacity together -- raising only the server opens a band where a "
            f"game completes and its training row is dropped."
        )
        return messages

    if len(history) <= CONTEXT_RECENT_COUNT:
        messages.extend(history)
        return messages

    recent_start = len(history) - CONTEXT_RECENT_COUNT
    while recent_start > 0 and history[recent_start].get("role") == "tool":
        recent_start -= 1

    summary_start = max(0, recent_start - CONTEXT_SUMMARY_COUNT)
    while summary_start > 0 and history[summary_start].get("role") == "tool":
        summary_start -= 1

    for idx in range(summary_start, recent_start):
        msg = history[idx]
        if msg.get("role") == "tool" and len(msg["content"]) > TOOL_SUMMARY_TRIGGER_CHARS:
            tool_name = _find_tool_name(history, idx, msg["tool_call_id"])
            messages.append({**msg, "content": _summarize_tool_result(tool_name, msg["content"])})
        else:
            messages.append(msg)

    bridge_text = (
        f"{state_summary}"
        "Continue playing. Call pass_priority to get your next decision, "
        "then choose_action to respond. "
        "All cards listed are playable right now. "
        "Play cards with choice=pN, pass with choice=no."
    )
    if cache_control:
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": bridge_text,
                        "cache_control": cache_control,
                    }
                ],
            }
        )
    else:
        messages.append({"role": "user", "content": bridge_text})

    messages.extend(history[recent_start:])
    return messages


async def _fetch_state_summary(session: ClientSession) -> str:
    """Fetch a compact game state summary for context bridging."""
    state_result = await execute_tool(session, "get_game_state", {})
    try:
        state_data = json.loads(state_result)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ToolExecutionError(f"get_game_state returned invalid JSON: {state_result!r}") from exc
    if not isinstance(state_data, dict):
        raise ToolExecutionError(f"get_game_state returned non-object payload: {state_data!r}")
    if "error" in state_data:
        raise ToolExecutionError(f"get_game_state returned error: {state_data['error']}")
    parts: list[str] = []
    if "turn" in state_data:
        parts.append(f"Turn {state_data['turn']}")
    if "phase" in state_data:
        parts.append(state_data["phase"])
    for player in state_data["players"]:
        name = player.get("name", "?")
        life = player.get("life", "?")
        battlefield = player.get("battlefield")
        permanents = len(battlefield) if battlefield is not None else 0
        hand = player.get("hand_count", player.get("hand_size", "?"))
        parts.append(f"{name}: {life}hp, {permanents} permanents, {hand} cards")
    return "Current game state: " + "; ".join(parts) + ". "
