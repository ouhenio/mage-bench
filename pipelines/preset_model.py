#!/usr/bin/env python
"""Print the model string a preset names, or exit non-zero saying why it cannot.

WHY THIS IS A FILE AND NOT AN INLINE EXPRESSION. Its only job is to make one string
available to a shell `[ "$a" = "$b" ]`, and the failure it guards against is a MISSING
preset -- `run_suite_slots.sh:148` defaults `MTG_PILOT_PRESET` to `q35-4b-local-strategy`
whenever the variable is unset, so a preset name that this tree does not define does not
raise: it silently becomes a different model. A helper that exits non-zero on the missing
name is the only version of this check that can fail.

It reads the tree's OWN presets.json, passed in, rather than resolving one itself: the
whole defect this guards was two trees disagreeing about which presets exist
(`q35-4b-ckpt265-strategy` is defined in wt-eval and was not in mb-wt-schema).
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <presets.json> <preset name>", file=sys.stderr)
        return 2
    path, name = sys.argv[1], sys.argv[2]
    try:
        doc = json.load(open(path))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2
    presets = doc.get("presets", doc)
    if name not in presets:
        near = [k for k in presets if name.split("-")[0] in k][:6]
        print(f"preset {name!r} is not defined in {path}. Similar: {near}", file=sys.stderr)
        return 1
    model = presets[name].get("model")
    if not model:
        print(f"preset {name!r} names no model", file=sys.stderr)
        return 1
    print(model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
