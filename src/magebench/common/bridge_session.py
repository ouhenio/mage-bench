"""Synchronous JSON-RPC client for a bridge's MCP HTTP server.

MOVED HERE FROM tests/golden_helpers.py, not copied. Production needs it: the
human-seat adapter drives a bridge from a thread that also serves HTTP, and the
MCP SDK's async session does not fit inside that without an event loop the
adapter would otherwise not need. The golden tests keep importing this exact
class, for the same reason the classpath helpers were moved to
orchestration.observer_session -- two copies of a transport drift, and the
drift shows up as "it works in tests".

The bridge's MCP server is plain JSON-RPC 2.0 over HTTP POST on /mcp
(McpServer.java), so this needs nothing but urllib.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from magebench.common.log import get_logger

logger = get_logger(__name__)


class BridgeSession:
    """Persistent MCP bridge JVM accessed via JSON-RPC over HTTP.

    Sends JSON-RPC requests to the bridge's MCP HTTP server and receives
    responses with natural HTTP timeouts. Avoids the MCP SDK's subprocess
    management so the JVM can outlive any one caller.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._id = 0

    @property
    def url(self) -> str:
        return self._url

    def _rpc(self, method: str, params: dict | None = None, timeout: int = 120) -> dict:
        self._id += 1
        req: dict = {"jsonrpc": "2.0", "method": method, "id": self._id}
        if params is not None:
            req["params"] = params
        body = json.dumps(req, separators=(",", ":")).encode("utf-8")
        # THE LABEL IS FOR A LOG LINE, and that is exactly why the old shape was
        # wrong rather than harmless: `(params or {}).get("name", "")` turned a
        # tools/call with no tool name into a label reading plain "tools/call",
        # so a malformed request logged as an ordinary one. Every caller in this
        # file passes a name (call_tool builds it), so an absent one is a
        # programming error, and the loud version costs nothing -- it fires
        # before the request is sent rather than as a bridge-side rejection
        # nobody can attribute.
        if method == "tools/call":
            assert params is not None and params.get("name"), (
                f"tools/call with no tool name: params={params!r}. The bridge "
                f"would reject this and the rejection would name the transport, "
                f"not the caller."
            )
            rpc_label = f"{method}({params['name']})"
        else:
            rpc_label = method
        t0 = time.monotonic()
        http_req = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(http_req, timeout=timeout) as http_resp:
                resp = json.loads(http_resp.read())
        except urllib.error.URLError as e:
            elapsed = time.monotonic() - t0
            msg = f"Bridge RPC error after {elapsed:.1f}s for {rpc_label}: {e}"
            logger.warning("[bridge-session] %s", msg)
            raise RuntimeError(msg) from e
        elapsed = time.monotonic() - t0
        if elapsed > 5:
            logger.debug("[bridge-session] %s OK (%.1fs)", rpc_label, elapsed)
        if "error" in resp and resp["error"] is not None:
            raise RuntimeError(f"MCP error: {resp['error']}")
        return resp["result"]

    def initialize(self) -> dict:
        return self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})

    def list_tool_defs(self) -> list[dict]:
        """Return raw MCP tool definitions."""
        result = self._rpc("tools/list", {})
        return result["tools"]

    def list_tools(self) -> list[str]:
        """Return names of available MCP tools."""
        return [t["name"] for t in self.list_tool_defs()]

    def call_tool(self, name: str, arguments: dict | None = None, timeout: int | None = None) -> str:
        """Call an MCP tool and return the result text (matches execute_tool()'s return format)."""
        # ABSENT MEANS EMPTY HERE, and it is written out rather than folded into
        # an `or` so it is a statement someone can disagree with: the signature's
        # default is None, meaning "this tool takes no arguments", and MCP wants
        # the key present with an empty object rather than missing. No behaviour
        # change -- `{}` and None were already the same call -- but `or {}` would
        # also silently swallow any other falsy value someone passes later.
        kwargs: dict = {"name": name, "arguments": {} if arguments is None else arguments}
        rpc_kwargs: dict = {}
        if timeout is not None:
            rpc_kwargs["timeout"] = timeout
        result = self._rpc("tools/call", kwargs, **rpc_kwargs)
        return result["content"][0]["text"]

    def call_tool_json(self, name: str, arguments: dict | None = None, timeout: int | None = None) -> dict:
        """Call an MCP tool and parse its result text as an object.

        Every bridge tool returns a JSON object; a body that does not parse is a
        bridge fault, not something to paper over with a default.
        """
        text = self.call_tool(name, arguments, timeout=timeout)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"MCP tool {name} returned non-JSON content: {text[:200]!r}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"MCP tool {name} returned {type(data).__name__}, expected an object")
        return data

    def close(self) -> None:
        pass

    def is_responsive(self, timeout: int = 5) -> bool:
        """Check if the bridge can respond to RPCs within the given timeout."""
        try:
            self._rpc("tools/list", {}, timeout=timeout)
            return True
        except (RuntimeError, json.JSONDecodeError):
            return False
