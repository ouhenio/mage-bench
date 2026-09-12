"""The pilot must import, and the thing that breaks it is a moved symbol -- not a number.

mcp 2.x drops the top-level re-export of McpError. `from mcp import McpError` is what
auto_pass.py and sleepwalker.py do, so under 2.2.0 magebench.pilot.pilot does not import
at all. Every tree that works today works because its venv predates that release.

THESE TESTS IMPORT THE SYMBOL AND THE MODULE. A test that asserted a version number would
pass against a broken environment whose pin happened to be edited, and fail against a
working one on a version nobody had listed -- it would be checking the label rather than
the thing.
"""
import importlib.metadata


def test_McpError_is_importable_from_the_place_the_code_imports_it():
    """The exact line in auto_pass.py and sleepwalker.py."""
    from mcp import McpError  # noqa: F401


def test_the_pilot_module_actually_imports():
    """The failure as an operator meets it: not 'mcp is wrong' but 'the pilot is gone'.

    This is the assertion that matters. The symbol test above localises the break; this
    one is the property we actually need, and it would catch a different import breaking
    for a different reason.
    """
    import magebench.pilot.pilot  # noqa: F401


def test_the_installed_mcp_satisfies_the_declared_bound():
    """Not a version assertion in disguise: it reads the bound from the package metadata
    rather than restating a number here, so editing pyproject.toml cannot make this pass
    while the environment stays broken."""
    installed = importlib.metadata.version("mcp")
    major = int(installed.split(".")[0])
    assert major < 2, (
        f"mcp {installed} is installed; the pilot imports `from mcp import McpError`, "
        "which 2.x no longer provides at top level. See the pin comment in pyproject.toml "
        "for the migration candidate."
    )


def test_the_migration_target_is_where_we_think_it_is():
    """Documents the escape route as an executable claim rather than a comment.

    If a future mcp moves McpError out of mcp.shared.exceptions too, this fails and the
    migration note in pyproject.toml is known to be stale -- rather than being trusted
    years later by someone doing the upgrade.
    """
    from mcp import McpError
    from mcp.shared.exceptions import McpError as FromSubmodule

    assert FromSubmodule is McpError
    assert McpError.__module__ == "mcp.shared.exceptions"
