"""The golden suite must not take a display that corpus generation is using.

Generation derives its displays from each worker's port and lands in 90-160.
The golden suite used to take `--auto-servernum`, which picks a free number
WITHOUT holding it -- so the two could choose the same number in the gap
between the pick and the bind. This pins the golden band above it.
"""

import pytest

from tests import conftest


def _display(monkeypatch, worker: str | None) -> int:
    if worker is None:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    else:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
    return conftest._golden_display()


def test_the_display_is_outside_the_generation_band(monkeypatch):
    # The property that matters, stated over every worker the ceiling allows
    # rather than over the one case that happens to run today.
    for i in range(conftest.GOLDEN_DISPLAY_LIMIT - conftest.GOLDEN_DISPLAY_BASE):
        display = _display(monkeypatch, f"gw{i}")
        assert not 90 <= display <= 160, f"worker gw{i} would take display {display}"


def test_workers_never_share_a_display(monkeypatch):
    # The whole reason this is keyed on the worker and not on the test case:
    # the worker is the unit of concurrency, so distinctness here is what
    # actually prevents two Xvfb servers racing for one number.
    seen = {_display(monkeypatch, f"gw{i}") for i in range(20)}
    assert len(seen) == 20


def test_no_xdist_still_yields_a_pinned_display(monkeypatch):
    # `make regen-golden` runs without xdist, which is the path that regenerated
    # the goldens today. It must be pinned too, not fall back to auto.
    assert _display(monkeypatch, None) == conftest.GOLDEN_DISPLAY_BASE


def test_a_worker_past_the_ceiling_is_refused(monkeypatch):
    # NEGATIVE CONTROL. Without a ceiling the numbers run on unbounded, and at
    # -n 60 they would walk straight back into the generation band from above.
    over = conftest.GOLDEN_DISPLAY_LIMIT - conftest.GOLDEN_DISPLAY_BASE
    with pytest.raises(AssertionError, match="past the"):
        _display(monkeypatch, f"gw{over}")


def test_an_unrecognised_worker_name_is_refused(monkeypatch):
    # Refusing beats defaulting to 0: several workers silently sharing display
    # 200 is the same race, one band higher.
    with pytest.raises(AssertionError, match="gwN form"):
        _display(monkeypatch, "worker-3")


def test_the_spectator_is_actually_wrapped_with_it():
    # The band is worthless if the call site does not pass it. Asserted against
    # the source because the fixture needs a live XMage server to run.
    import inspect

    src = inspect.getsource(conftest.spectator_process)
    assert "wrap_with_xvfb(spectator_cmd, display=_golden_display())" in src
