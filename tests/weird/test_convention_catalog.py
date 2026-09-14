"""Convention tests for presets, toolsets, prompts, and name-part catalogs."""

from magebench.common.json5_utils import loads_json5
from tests.weird.repo_convention_helpers import PUPPETEER_DIR, REPO_ROOT, load_json


# A PRESET MAY DECLARE THAT ITS PROMPT IS WRITTEN AT LAUNCH, and the test honours the
# DECLARATION rather than inferring intent from an absent file. `p1-arm-s`'s prompt is the
# shared base plus a document, written by pipelines/eval/run_p1.py at launch into a per-block
# prompts/ directory -- it is not committed because its content derives from a document that
# can change, and committing a snapshot would make the two diverge silently.
#
# NOT AN ALLOWLIST IN THE TEST. A list of exempt names here would drift from the presets it
# exempts and would exempt by memory; the marker travels with the preset it describes. A
# preset with a missing prompt and NO marker still fails, which is the property that matters.
PROVISIONED = "system_prompt_provisioned_by"


def provisioned_at_launch(name: str, preset: dict) -> bool:
    """True if this preset DECLARES that its prompt is written at launch.

    A malformed declaration is an error, not an exemption: the value must name the thing
    that writes the prompt, so that the marker is followable. `{}` or `""` would otherwise
    exempt silently, which is the allowlist failure mode wearing a different hat.
    """
    if PROVISIONED not in preset:
        return False
    value = preset[PROVISIONED]
    if not isinstance(value, str) or not value.strip():
        raise AssertionError(
            f"{name!r}: {PROVISIONED}={value!r} must be a non-empty string naming what"
            " writes the prompt (e.g. \"pipelines/eval/run_p1.py\")"
        )
    return True


def unknown_prompt_refs(presets: dict, prompt_keys: set) -> list:
    """The rule, separable from the catalog on disk so it can be tested both ways."""
    missing = []
    for name, preset in presets.items():
        if provisioned_at_launch(name, preset):
            continue
        system_prompt = preset.get("system_prompt")
        if system_prompt and system_prompt not in prompt_keys:
            missing.append(f"{name!r} -> {system_prompt!r}")
    return missing

class TestPresetsReferenceValidModels:
    def test_all_preset_models_exist(self) -> None:
        models_data = load_json(PUPPETEER_DIR / "models.json")
        model_ids = {m["id"] for m in models_data["models"]}
        presets_data = load_json(PUPPETEER_DIR / "presets.json")

        missing = []
        for name, preset in presets_data["presets"].items():
            if preset["model"] not in model_ids:
                missing.append(f"{name!r} -> {preset['model']!r}")

        assert not missing, "Presets reference unknown models:\n  " + "\n  ".join(missing)

    def test_all_presets_have_valid_status(self) -> None:
        presets_data = load_json(PUPPETEER_DIR / "presets.json")
        valid_statuses = {"active", "retired", "buggy", "expensive"}

        bad = []
        for name, preset in presets_data["presets"].items():
            status = preset.get("status")
            if status is None:
                bad.append(f"{name!r}: missing 'status' field")
            elif status not in valid_statuses:
                bad.append(f"{name!r}: invalid status {status!r}")

        assert not bad, "Preset status issues:\n  " + "\n  ".join(bad)

    def test_preset_system_prompts_exist(self) -> None:
        presets_data = load_json(PUPPETEER_DIR / "presets.json")

        prompt_keys: set[str] = set()
        prompts_dir = PUPPETEER_DIR / "prompts"
        if prompts_dir.is_dir():
            for md in prompts_dir.glob("*.md"):
                prompt_keys.add(md.stem)
        prompts_json = PUPPETEER_DIR / "prompts.json"
        if prompts_json.exists():
            prompt_keys.update(load_json(prompts_json).keys())

        missing = unknown_prompt_refs(presets_data["presets"], prompt_keys)
        assert not missing, "Presets reference unknown system_prompts:\n  " + "\n  ".join(missing)

    def test_missing_prompt_without_the_marker_still_fails(self) -> None:
        """The property the marker must not cost us.

        Exempting the two arm presets is only acceptable if a preset that simply forgot its
        prompt is still caught. Asserted against the rule with synthetic presets, because
        asserting it against the catalog on disk would only pass while the catalog happens
        to be broken."""
        assert unknown_prompt_refs({"forgot": {"system_prompt": "nope"}}, set()) == [
            "'forgot' -> 'nope'"
        ]
        assert unknown_prompt_refs(
            {"declared": {"system_prompt": "nope", PROVISIONED: "pipelines/eval/run_p1.py"}},
            set(),
        ) == []

    def test_a_marker_must_name_its_provisioner(self) -> None:
        for bad in ("", "   ", True, {}, None):
            try:
                unknown_prompt_refs({"x": {"system_prompt": "nope", PROVISIONED: bad}}, set())
            except AssertionError as e:
                assert PROVISIONED in str(e)
            else:
                raise AssertionError(f"{bad!r} was accepted as a provisioning declaration")


class TestToolsetsReferenceValidTools:
    def test_all_toolset_tools_exist(self) -> None:
        toolsets = load_json(PUPPETEER_DIR / "toolsets.json")
        mcp_tools = loads_json5((REPO_ROOT / "website" / "src" / "data" / "mcp-tools.json5").read_text())
        real_tool_names = {t["name"] for t in mcp_tools}

        missing = []
        for toolset_name, tools in toolsets.items():
            missing.extend(f"{toolset_name!r} -> {tool!r}" for tool in tools if tool not in real_tool_names)

        assert not missing, "Toolsets reference nonexistent MCP tools:\n  " + "\n  ".join(missing)

    def test_preset_toolsets_exist(self) -> None:
        presets_data = load_json(PUPPETEER_DIR / "presets.json")
        toolsets = load_json(PUPPETEER_DIR / "toolsets.json")

        missing = []
        for name, preset in presets_data["presets"].items():
            toolset = preset.get("toolset")
            if toolset and toolset not in toolsets:
                missing.append(f"{name!r} -> {toolset!r}")

        assert not missing, "Presets reference unknown toolsets:\n  " + "\n  ".join(missing)


class TestModelNamePartsUnique:
    def test_no_duplicate_name_parts(self) -> None:
        models_data = load_json(PUPPETEER_DIR / "models.json")

        seen: dict[str, list[str]] = {}
        for model in models_data["models"]:
            seen.setdefault(model["name_part"], []).append(model["id"])

        dupes = {name_part: ids for name_part, ids in seen.items() if len(ids) > 1}
        assert not dupes, "Duplicate name_parts (would be ambiguous on leaderboard):\n  " + "\n  ".join(
            f"{name_part!r}: {ids}" for name_part, ids in dupes.items()
        )


class TestPersonalityNamePartsUnique:
    def test_no_duplicate_name_parts(self) -> None:
        personalities = load_json(PUPPETEER_DIR / "personalities.json")

        seen: dict[str, list[str]] = {}
        for key, value in personalities.items():
            seen.setdefault(value["name_part"], []).append(key)

        dupes = {name_part: keys for name_part, keys in seen.items() if len(keys) > 1}
        assert not dupes, "Duplicate personality name_parts:\n  " + "\n  ".join(
            f"{name_part!r}: {keys}" for name_part, keys in dupes.items()
        )


class TestNoOrphanedPrompts:
    def test_all_prompts_referenced_by_presets(self) -> None:
        """Every .md file in prompts/ should be referenced by at least one preset."""
        prompts_dir = PUPPETEER_DIR / "prompts"
        if not prompts_dir.is_dir():
            return
        prompt_files = {md.stem for md in prompts_dir.glob("*.md")}
        if not prompt_files:
            return

        presets_data = load_json(PUPPETEER_DIR / "presets.json")
        referenced = {
            preset.get("system_prompt") for preset in presets_data["presets"].values() if preset.get("system_prompt")
        }

        orphaned = prompt_files - referenced
        assert not orphaned, "Prompt files not referenced by any preset:\n  " + "\n  ".join(
            f"{name}.md" for name in sorted(orphaned)
        )


class TestPersonalityNamePartLength:
    # XMage player names have a length limit. Model name_part + " " +
    # personality name_part must fit. Max model name_part is currently 7
    # chars; keeping personality name_part <= 7 leaves room.
    _MAX_LENGTH = 7

    def test_name_parts_not_too_long(self) -> None:
        personalities = load_json(PUPPETEER_DIR / "personalities.json")

        too_long = [
            f"{key!r}: {value['name_part']!r} ({len(value['name_part'])} chars)"
            for key, value in personalities.items()
            if len(value["name_part"]) > self._MAX_LENGTH
        ]
        assert not too_long, f"Personality name_parts exceed {self._MAX_LENGTH} chars:\n  " + "\n  ".join(too_long)


class TestActivePresetsExist:
    def test_has_active_presets(self) -> None:
        presets_data = load_json(PUPPETEER_DIR / "presets.json")
        active = [name for name, preset in presets_data["presets"].items() if preset.get("status") == "active"]
        assert active, "No presets with status='active' — matchmaking needs at least one"
