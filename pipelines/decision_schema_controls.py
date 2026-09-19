#!/usr/bin/env python
"""The two controls the bound-choice schema has to pass before any eval is worth running.

CONTROL (a) -- DOES THE ENUM BIND ON A LIVE SERVER? The grammar proof compiles the tag offline
and shows xgrammar rejects an out-of-range id. That is necessary and not sufficient: it says
nothing about whether the SERVER honours a tag sent over HTTP in this shape. The tag-order probe
is the cautionary case -- four arms including an unconstrained control returned byte-identical
output in 396 of 396 cells, and nothing in the artifact could say whether the tag had ever been
applied. So (a) sends a real frame with the enum narrowed to a SINGLE id that the model did not
choose, and asks whether that id comes back. If it does not, every other number here is void.

CONTROL (b) -- DOES IT PREVENT THE DEFECT IT WAS BUILT FOR? Replays the positions where the
model named a real tool and chose an id that was not on the menu, guarded and unguarded, N draws
each, with both arms' raw responses persisted.

THE UNIT IS ONE FRAME PER POSITION (684), not one per failing call (976). Registered as 976 and
changed explicitly on 2026-09-19 once the candidates were measured and found to differ by 3x --
976 failing calls, 2,006 llm_call frames at those positions, 684 positions. Retries are
re-answers at the same `game_seq` with the rejection already in history, so counting them weights
a position by how badly the model floundered (13x at the worst one) and conflates "does the enum
prevent the first mistake" with "does it prevent the repeat". Retries stay tagged; `--attempt -1`
runs them.

THE STOPPING RULE IS PRE-REGISTERED AND IT IS NOT "GUARDED == 0". A guarded zero beside an
unguarded zero is a null instrument, and this repo has already produced exactly that once: the
7-case smoke reported "7 of 7 real tool calls, guard never turned an invention into silence",
and the honest reading afterwards was that the unguarded arm reproduced nothing, so the cases
tested nothing. Therefore:

    unguarded reproductions == 0  ->  control (b) is UNINFORMATIVE, not passed.

Exit codes: 0 both controls informative and passed; 1 a control failed or is uninformative;
2 the run could not be completed. The sbatch reads the code -- a gate is an exit code.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from openai import OpenAI, OpenAIError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from magebench.pilot.decision_schema import (  # noqa: E402
    CHOICE_FIELD,
    CHOICE_TOOL,
    bindable_enum,
    decision_structural_tag,
)


# THE PRODUCTION PATH, NOT A HAND-ROLLED POST. The first run of this control sent
# `{"extra_body": {"structured_outputs": ...}}` as raw JSON and the tag never reached vLLM:
# `structured_outputs` is a TOP-LEVEL field on ChatCompletionRequest, `extra_body` is not a
# field at all, and the model's `extra="allow"` swallows unknown keys silently. The pilot works
# because the OpenAI *client* flattens `extra_body` into the body before sending.
#
# So the control goes through the same client the pilot uses. A control that exercises a
# different transport proves a different program -- which is what 7866 established for the
# name-level guard and what this run re-learned the expensive way.
_WIRE: dict[int, bytes] = {}


def _record_wire(request: httpx.Request) -> None:
    """Capture what actually went over the wire, keyed by thread.

    Recorded because "was the tag applied" must be answerable from the ARTIFACT. The previous
    run could only answer it by reasoning about a comment in pilot.py, which is how a transport
    bug got reported as a finding about vLLM.
    """
    import threading

    _WIRE[threading.get_ident()] = request.content


def make_client(base_url: str, timeout: float) -> OpenAI:
    http = httpx.Client(timeout=timeout, event_hooks={"request": [_record_wire]})
    return OpenAI(base_url=base_url, api_key="EMPTY", http_client=http, max_retries=0)


def wire_tag_keys() -> dict:
    """The tag-bearing keys of the body this thread just sent, for the raw record."""
    import threading

    raw = _WIRE.get(threading.get_ident())
    if not raw:
        return {"wire": "not captured"}
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"wire": "unparsable"}
    so = body.get("structured_outputs")
    return {
        "top_level_keys": sorted(body),
        "structured_outputs_present": so is not None,
        "structural_tag_len": len((so or {}).get("structural_tag") or "") if isinstance(so, dict) else 0,
        "extra_body_present": "extra_body" in body,
    }


def called(response: dict) -> tuple[str | None, str | None]:
    """(tool name, choice argument) of the first tool call, or (None, None)."""
    try:
        message = (response.get("choices") or [{}])[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            return None, None
        fn = calls[0].get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        return fn.get("name"), (args or {}).get(CHOICE_FIELD)
    except (AttributeError, IndexError, TypeError):
        return None, None


def tool_names(request: dict) -> list[str]:
    return [t.get("function", {}).get("name") for t in (request.get("tools") or [])]


def build(frame: dict, arm: str, seed: int, max_tokens: int, enum_override: list[str] | None):
    """One request body. `arm` is 'guarded' or 'unguarded'; the ONLY difference is the tag."""
    request = frame["request"]
    body = {
        "model": request.get("model"),
        "messages": request.get("messages"),
        "tools": request.get("tools"),
        "tool_choice": "auto",
        "max_tokens": max_tokens,
        "seed": seed,
    }
    if arm == "unguarded":
        return body, None
    enum = enum_override
    if enum is None:
        enum, _why = bindable_enum(
            {"choices": frame.get("choices"), "respond_with": frame.get("respond_with")}
        )
    if enum is None:
        return body, None
    names = [n for n in tool_names(request) if n]
    # `extra_body` is the CLIENT's parameter, not a wire field: the client merges its contents
    # into the top level. This is byte-for-byte how pilot.py:1259 sends the name guard's tag.
    body["extra_body"] = {
        "structured_outputs": {"structural_tag": json.dumps(decision_structural_tag(names, enum))}
    }
    return body, enum


def run_arm(frames, arm, draws, client, timeout, max_tokens, conc, raw_fh, enum_of=None):
    """Returns per-frame results. Concurrent: the 16 calls per frame are independent."""
    jobs = []
    for i, frame in enumerate(frames):
        for d in range(draws):
            # Seed keyed on (frame, draw) and NOT on the arm: common random numbers, so a
            # difference between arms cannot be attributed to the arms drawing different
            # randomness. Keying on the arm is right for independent samples and wrong here.
            jobs.append((i, d, 20260919 * 1000 + i * 31 + d))
    started = time.time()
    done = 0
    out = collections.defaultdict(list)

    def one(job):
        i, d, seed = job
        frame = frames[i]
        override = enum_of(frame) if enum_of else None
        body, enum = build(frame, arm, seed, max_tokens, override)
        extra = body.pop("extra_body", None)
        try:
            resp = client.chat.completions.create(**body, **({"extra_body": extra} if extra else {}))
            response = resp.model_dump()
            err = None
        except (OpenAIError, httpx.HTTPError, TimeoutError) as exc:
            response, err = {}, f"{type(exc).__name__}: {exc}"
        name, choice = called(response)
        return i, d, enum, name, choice, err, response, wire_tag_keys()

    with ThreadPoolExecutor(max_workers=conc) as pool:
        for i, d, enum, name, choice, err, response, wire in pool.map(one, jobs):
            done += 1
            raw_fh.write(json.dumps({"arm": arm, "frame": i, "draw": d, "enum": enum,
                                     "tool": name, "choice": choice, "error": err,
                                     "wire": wire, "response": response}) + "\n")
            # `wire` MUST be here and not only in the raw file: the verdict reads these rows,
            # and without it every call looked as though it carried no tag. That produced an
            # INCONCLUSIVE on a run where the raw records show the tag present 8/8 -- a false
            # alarm in the safe direction, but a verdict that cannot see its own evidence.
            out[i].append({"tool": name, "choice": choice, "enum": enum, "error": err,
                           "wire": wire})
            # MEASURED RATE, printed early so the sbatch's --time can be set from it rather
            # than guessed. Prompts here run ~30k tokens, so this is prefill-bound and the
            # rate is not guessable from token counts alone.
            if done in (100, 250, 500) or done % 2000 == 0:
                rate = done / max(1e-9, time.time() - started)
                remain = (len(jobs) - done) / max(1e-9, rate)
                print(f"  [{arm}] {done}/{len(jobs)}  {rate:.2f} gen/s  "
                      f"eta {remain/60:.1f} min", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--draws", type=int, default=8)
    ap.add_argument("--conc", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--limit", type=int, default=0, help="first N frames; 0 = all")
    ap.add_argument("--attempt", type=int, default=0,
                    help="attempt index at a position; 0 = first attempt only (the registered "
                         "unit), -1 = every frame including retries")
    args = ap.parse_args()

    frames = [json.loads(l) for l in pathlib.Path(args.frames).open() if l.strip()]
    # THE REGISTERED UNIT IS ONE FRAME PER POSITION, changed from "976 calls" explicitly by
    # mtg-e3 on 2026-09-19 after the three candidate units turned out to differ by 3x:
    # 976 failing calls, 2,006 frames at those positions, 684 positions. The reason for the
    # change is FLOUNDER WEIGHTING -- retries are re-answers at the same `game_seq` with the
    # rejection already in history, so counting them weights a position by how badly the model
    # struggled there (up to 13x at one position) and mixes "does the enum prevent the first
    # mistake" with "does it prevent the repeat". `--attempt -1` runs the other population
    # without re-extracting.
    total_in = len(frames)
    if args.attempt >= 0:
        frames = [f for f in frames if f.get("attempt") == args.attempt]
    if args.limit:
        frames = frames[: args.limit]
    if not frames:
        print("FATAL: no frames", file=sys.stderr)
        return 2
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = make_client(args.base_url, args.timeout)

    # THE FRAMES' MODEL MUST BE THE MODEL THIS SERVER SERVES, asserted before a single call.
    # Frames carry the model that generated them -- 4452's carry `mtg-ckpt-265`/`mtg-ckpt-132`,
    # 4056's carry `Qwen/Qwen3.5-4B`. Two things go wrong without this check, and only one of
    # them is loud:
    #   loud   -- a served name the frames do not use gives HTTP 404 on every call, which the
    #             first smoke hit and which a content-reading verdict once misread as a
    #             scientific result.
    #   QUIET  -- a name that HAPPENS to match while the weights differ replays one model's
    #             mistakes through another model. deck-general-plan.md:1595 records that exact
    #             substitution costing a control: "the seven inventions came from the 4B
    #             students, so replaying them against the 35B is a control that cannot fail".
    #             An unguarded zero would then be about the substitution, not the guard.
    frame_models = sorted({f["request"].get("model") for f in frames})
    served = sorted(m.id for m in client.models.list().data)
    print(f"  frames' model(s): {frame_models}   server serves: {served}")
    if len(frame_models) != 1:
        print(f"FATAL: frames carry {len(frame_models)} different models {frame_models}. Split "
              f"them by model and serve each -- one serve cannot answer for both.", file=sys.stderr)
        return 2
    if frame_models[0] not in served:
        print(f"FATAL: frames were generated by {frame_models[0]!r}, which this server does not "
              f"serve ({served}). Serving a different model replays one model's mistakes through "
              f"another; see deck-general-plan.md:1595.", file=sys.stderr)
        return 2
    print(f"frames: {len(frames)} of {total_in} in file   "
          f"attempt filter: {'all' if args.attempt < 0 else args.attempt}   "
          f"draws: {args.draws}   conc: {args.conc}")

    # ---------------------------------------------------------------- control (a)
    # A single-id enum the model did NOT choose, on real frames. If the server honours the
    # tag, that id is the only thing generatable; if it ignores the tag, the model answers
    # whatever it would have answered.
    print("\n=== CONTROL (a): does the enum bind on a live server? ===", flush=True)
    a_frames = []
    for f in frames:
        ids = [c.get("id") for c in (f.get("choices") or []) if isinstance(c, dict)]
        ids = [i for i in ids if isinstance(i, str) and i and i != f.get("chosen")]
        if ids:
            a_frames.append({**f, "_forced": ids[0]})
        if len(a_frames) >= 25:
            break
    if not a_frames:
        print("FATAL: no frame offers an id other than the one the model chose", file=sys.stderr)
        return 2
    with (out / "raw_control_a.jsonl").open("w") as fh:
        res_a = run_arm(a_frames, "guarded", args.draws, client, args.timeout, args.max_tokens,
                        args.conc, fh, enum_of=lambda f: [f["_forced"]])
    # THE TAG CONSTRAINS ARGUMENTS, NOT TOOL CHOICE -- every offered name stays permitted, by
    # design and by the name guard's own docstring. So the question control (a) asks is: WHEN
    # `choose_action` was called, did the single permitted id come back? Counting a call that
    # chose a different tool as a binding failure understates the constraint and would have
    # reported 6/8 for a run in which every choose_action call obeyed the enum.
    answered = [(i, r) for i, rs in res_a.items() for r in rs if r["tool"] == CHOICE_TOOL]
    hits = sum(1 for i, r in answered if r["choice"] == a_frames[i]["_forced"])
    total_a = len(answered)
    all_calls = sum(len(rs) for rs in res_a.values())
    other_tool = all_calls - total_a
    # A GATE MUST CHECK THE CALLS SUCCEEDED BEFORE IT READS THEIR CONTENT.
    # The first run of this control reported `FAIL: the enum does not bind on the live server`
    # for a job in which EVERY request returned HTTP 404. `hits == 0` was true and meant
    # nothing: there were no responses to have hits in. A control exists to be believed, so a
    # control that can state a scientific conclusion from a transport failure is worse than no
    # control. Errors and "did the tag reach the wire" are now checked FIRST and produce
    # INCONCLUSIVE, which is a different word from FAIL on purpose.
    errs_a = sum(1 for rs in res_a.values() for r in rs if r["error"])
    no_tag = sum(1 for rs in res_a.values() for r in rs
                 if not (r.get("wire") or {}).get("structured_outputs_present"))
    # A run in which the model never called choose_action says nothing either way.
    if total_a == 0 and not errs_a:
        errs_a = 0
    print(f"  forced id emitted: {hits}/{total_a} choose_action calls   "
          f"(other tool: {other_tool} of {all_calls} -- permitted, the tag binds arguments only)")
    print(f"  request errors: {errs_a}/{total_a}   calls whose WIRE BODY carried no "
          f"structured_outputs: {no_tag}/{total_a}")
    a_pass = total_a > 0 and errs_a == 0 and no_tag == 0 and hits == total_a

    # ---------------------------------------------------------------- control (b)
    print("\n=== CONTROL (b): guarded vs unguarded on the out-of-range population ===", flush=True)
    with (out / "raw_control_b_unguarded.jsonl").open("w") as fh:
        res_u = run_arm(frames, "unguarded", args.draws, client, args.timeout,
                        args.max_tokens, args.conc, fh)
    with (out / "raw_control_b_guarded.jsonl").open("w") as fh:
        res_g = run_arm(frames, "guarded", args.draws, client, args.timeout,
                        args.max_tokens, args.conc, fh)

    def out_of_range(results, frames):
        n = bad = bound = 0
        for i, rs in results.items():
            ids = {c.get("id") for c in (frames[i].get("choices") or []) if isinstance(c, dict)}
            enum, _ = bindable_enum({"choices": frames[i].get("choices"),
                                     "respond_with": frames[i].get("respond_with")})
            for r in rs:
                if r["tool"] != CHOICE_TOOL or r["choice"] is None:
                    continue
                n += 1
                if enum is not None:
                    bound += 1
                    if r["choice"] not in enum:
                        bad += 1
                elif r["choice"] not in ids:
                    bad += 1
        return n, bad, bound

    nu, bu, _ = out_of_range(res_u, frames)
    ng, bg, boundable = out_of_range(res_g, frames)
    print(f"\n  unguarded: {bu} out-of-range of {nu} choose_action answers")
    print(f"  guarded:   {bg} out-of-range of {ng} answers   ({boundable} on bound decisions)")

    verdict = {"control_a_pass": a_pass, "control_a_hits": hits, "control_a_total": total_a,
               "unguarded_reproductions": bu, "unguarded_answers": nu,
               "guarded_out_of_range": bg, "guarded_answers": ng,
               "frames": len(frames), "draws": args.draws}
    verdict["control_a_errors"] = errs_a
    verdict["control_a_calls_without_tag_on_wire"] = no_tag
    if errs_a:
        verdict["status"] = (f"INCONCLUSIVE -- transport: {errs_a} of {total_a} control-(a) "
                             f"requests failed. Nothing is established about binding; fix the "
                             f"transport and re-run.")
        rc = 2
    elif no_tag:
        verdict["status"] = (f"INCONCLUSIVE -- the tag was not on the wire in {no_tag} of "
                             f"{total_a} calls. The client did not send `structured_outputs`, "
                             f"so the server was never asked to bind anything.")
        rc = 2
    elif not a_pass:
        verdict["status"] = (f"FAIL: the tag reached the server on every call and the forced id "
                             f"came back {hits}/{total_a} times. THIS is 'does not bind'.")
        rc = 1
    elif bu == 0:
        # The registered stopping rule. NOT a pass.
        verdict["status"] = ("UNINFORMATIVE: the unguarded arm reproduced the defect zero times, "
                             "so a guarded zero says nothing. Evidence falls back to the "
                             "compiled-grammar proof and the census.")
        rc = 1
    elif bg == 0:
        verdict["status"] = "PASS: unguarded reproduces, guarded is zero"
        rc = 0
    else:
        verdict["status"] = f"PARTIAL: guarded still produced {bg} out-of-range answers"
        rc = 1
    (out / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print(f"\n  {verdict['status']}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
