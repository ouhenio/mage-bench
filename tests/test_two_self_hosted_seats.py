"""Two pilot seats must reach TWO servers, and the proof is where the request lands.

vLLM cannot hold two weight sets in one server, so checkpoint-vs-checkpoint needs two
servers -- one per seat. The failure to design against is not an error: it is two seats
quietly hitting the SAME server, which produces a config, a log and a result identical to
the intended experiment while actually playing a checkpoint against itself. Nothing
downstream can detect that afterwards.

So these tests do not assert on configuration. They stand up two real HTTP listeners and
assert on WHICH ONE RECEIVED THE REQUEST.
"""
import http.server
import json
import socket
import threading
import urllib.request

import pytest

from magebench.common.llm_cost import (
    SUPPORTED_LLM_PROVIDERS,
    is_self_hosted,
    llm_base_url,
    required_api_key_env,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Recorder(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.server.hits.append(self.path)
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _Server:
    def __init__(self):
        self.port = _free_port()
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), _Recorder)
        self.httpd.hits = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def two_servers():
    a, b = _Server(), _Server()
    yield a, b
    a.close()
    b.close()


def _post(base_url: str) -> None:
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=5).read()


def test_local_b_exists_and_is_self_hosted():
    assert "local_b" in SUPPORTED_LLM_PROVIDERS
    assert is_self_hosted("local_b") and is_self_hosted("local")
    assert not is_self_hosted("openrouter")
    assert required_api_key_env("local_b") == "MAGEBENCH_LOCAL_B_API_KEY"


def test_the_two_providers_reach_DIFFERENT_servers(monkeypatch, two_servers):
    """The claim, proved where it can actually fail: at the listener."""
    a, b = two_servers
    monkeypatch.setenv("MAGEBENCH_LOCAL_BASE_URL", a.base_url)
    monkeypatch.setenv("MAGEBENCH_LOCAL_B_BASE_URL", b.base_url)

    _post(llm_base_url("local"))
    _post(llm_base_url("local_b"))

    assert len(a.httpd.hits) == 1, "seat A's request did not land on server A"
    assert len(b.httpd.hits) == 1, "seat B's request did not land on server B"


def test_THE_FAILURE_MODE_is_detectable(monkeypatch, two_servers):
    """The negative control, and the reason the test above is not enough on its own.

    Point both providers at ONE server -- the misconfiguration that looks like success --
    and show this harness sees two hits on one listener and none on the other. If that
    were indistinguishable here, it would be indistinguishable in a real run too, and
    these tests would prove nothing about the case we actually fear.
    """
    a, b = two_servers
    monkeypatch.setenv("MAGEBENCH_LOCAL_BASE_URL", a.base_url)
    monkeypatch.setenv("MAGEBENCH_LOCAL_B_BASE_URL", a.base_url)

    _post(llm_base_url("local"))
    _post(llm_base_url("local_b"))

    assert len(a.httpd.hits) == 2
    assert len(b.httpd.hits) == 0


def test_the_hosts_are_read_at_CALL_time(monkeypatch, two_servers):
    """They used to be captured into a module dict at import. With one self-hosted seat
    that meant a wrong host; with two it means BOTH fall back to their defaults, and if
    only one default is listening the two seats collapse onto one server silently."""
    a, b = two_servers
    monkeypatch.setenv("MAGEBENCH_LOCAL_BASE_URL", a.base_url)
    assert llm_base_url("local") == a.base_url
    monkeypatch.setenv("MAGEBENCH_LOCAL_BASE_URL", b.base_url)
    assert llm_base_url("local") == b.base_url, "resolved at import, not at call"


def test_the_defaults_are_different_ports():
    """Belt and braces for the collapse case: if both env vars are unset, the two
    providers must still not be the same URL."""
    import os
    for name in ("MAGEBENCH_LOCAL_BASE_URL", "MAGEBENCH_LOCAL_B_BASE_URL"):
        os.environ.pop(name, None)
    assert llm_base_url("local") != llm_base_url("local_b")


def test_an_unknown_provider_still_refuses():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        llm_base_url("local_c")
