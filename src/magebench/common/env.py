"""Environment reads that distinguish ABSENT from EMPTY.

`os.environ.get(NAME, "")` collapses two different situations into one falsy
value: the variable was not set (a choice), and the variable was set to the
empty string (almost always a mistake -- an unquoted shell expansion, a
`VAR=$UNSET_THING`, a config that resolved to nothing). The second is how a run
gets configured to record nothing and reports no error, which is the failure
mode this module exists to prevent.

Both functions treat whitespace as empty, because `VAR=" "` from a shell is the
same accident with a space in it.
"""

from __future__ import annotations

import os


def env_or_none(name: str) -> str | None:
    """The variable's value, or None if it is not set.

    Raises if the variable is SET AND EMPTY. Absent means "not requested" and is
    a legitimate answer; present-and-empty means somebody meant to request it and
    the value did not survive, and continuing would silently disable whatever it
    controls.
    """
    if name not in os.environ:
        return None
    value = os.environ[name]
    if not value.strip():
        raise ValueError(
            f"{name} is set but empty. Absent would mean 'not requested', which is fine; "
            f"empty means it was requested and the value did not survive -- an unquoted "
            f"expansion or a variable that resolved to nothing. Unset it, or give it a value."
        )
    return value


def env_flag_dir(name: str, *, needed_by: str) -> str | None:
    """A directory-valued switch: absent disables the feature, empty is an error.

    `needed_by` names what silently stops working, so the message says what was
    lost rather than only which variable was wrong.
    """
    try:
        return env_or_none(name)
    except ValueError as exc:
        raise ValueError(f"{exc} Without it, {needed_by} writes nothing.") from exc
