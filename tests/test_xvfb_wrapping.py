"""Tests for Linux xvfb wrapping."""

import pytest

# wrap_with_xvfb moved into magebench when production needed it: the
# keepAlive observer is launched by the orchestrator now, not only by tests.
from magebench.orchestration import observer_session


def test_wrap_with_xvfb_prefixes_linux_commands(monkeypatch):
    monkeypatch.setattr(observer_session.sys, "platform", "linux")
    monkeypatch.setattr(
        observer_session.shutil,
        "which",
        lambda name: "/usr/bin/xvfb-run" if name == "xvfb-run" else None,
    )

    wrapped = observer_session.wrap_with_xvfb(["java", "-version"])

    assert wrapped[:3] == [
        "/usr/bin/xvfb-run",
        "--auto-servernum",
        "--server-args=-screen 0 1920x1080x24",
    ]
    assert wrapped[3:] == ["java", "-version"]


def test_wrap_with_xvfb_leaves_non_linux_commands_unchanged(monkeypatch):
    monkeypatch.setattr(observer_session.sys, "platform", "darwin")

    wrapped = observer_session.wrap_with_xvfb(["java", "-version"])

    assert wrapped == ["java", "-version"]


def test_wrap_with_xvfb_requires_xvfb_on_linux(monkeypatch):
    monkeypatch.setattr(observer_session.sys, "platform", "linux")
    monkeypatch.setattr(observer_session.shutil, "which", lambda _name: None)

    with pytest.raises(AssertionError, match="xvfb-run"):
        observer_session.wrap_with_xvfb(["java", "-version"])
