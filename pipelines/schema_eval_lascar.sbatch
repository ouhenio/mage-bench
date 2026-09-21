#!/usr/bin/env bash
#SBATCH --job-name=schema-eval
#SBATCH --partition=lascar
#SBATCH --account=latamgpt
#SBATCH --qos=lascar-unlimit
#SBATCH --nodelist=lascar
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=32
#SBATCH --mem-per-gpu=80G
#SBATCH --time=08:00:00
#SBATCH --no-requeue
#SBATCH --open-mode=append
#SBATCH --chdir=/workspace1/users/ouhenio/mtg
#SBATCH --output=/workspace1/users/ouhenio/mtg/slurm-logs/slurm-%x-%j.out
#
# ckpt-265 vs the engine on 4452's seeds, ONE ARM PER JOB.
#
#   sbatch ... pipelines/schema_eval_lascar.sbatch <guarded|unguarded>
#
# THE ONE VARIABLE IS THE PER-DECISION ENUM. Both arms carry the name guard
# (MAGEBENCH_TOOL_NAME_GUARD=structural_tag), the same preset, the same seeds, the same
# concurrency, the same engine opponent. Only MAGEBENCH_DECISION_SCHEMA differs.
#
# WHY TWO JOBS AND NOT TWO SEATS. The schema is read from the process environment, so a single
# game cannot host one guarded seat and one unguarded one. The arms are therefore two suites on
# the SAME seed set, paired at readout by seed -- which is also how 4056's arms were compared.
#
# THE ENGINE IS AT SKILL 1, as every eval in this project has been. karn-research established on
# 2026-09-19 that the search budget was never SET, not merely never recorded, so skill 1 is the
# engine's floor: maxNodes 5000, maxThinkTimeSecs 3, maxDepth 4. That is not a neutral choice and
# the readout says so -- the budget curve is a later decision and this eval does not pre-empt it.
set -uo pipefail

ARM="${1:?usage: $0 <guarded|unguarded>}"
case "$ARM" in guarded|unguarded) : ;; *) echo "FATAL: arm must be guarded|unguarded"; exit 2 ;; esac

MTG=/workspace1/users/ouhenio/mtg
SRC=$MTG
WT=/workspace1/users/ouhenio/mb-wt-schema
M=$(bash -c "source $MTG/env.sh >/dev/null 2>&1; echo \$MTG_DATA_ROOT")
PY=$MTG/.venv/bin/python
LABEL=schema-eval-$ARM-$SLURM_JOB_ID

# EVIDENCE UNDER THE DATA ROOT, which reopened on 2026-09-21 (445 GB free after a prune).
#
# AND THE PATH IS THE RUNNER'S, NOT MINE. Job 9396 exported MAGEBENCH_EVIDENCE_DIR expecting the
# suite to land on an overflow path; `run_suite_slots.sh` derives its own from the label and the
# data root and does not read that variable, so the redirection was INERT -- the run wrote to the
# data root anyway and the flag gate afterwards looked in the empty place and said "no such
# evidence dir". An env var the consumer does not read is indistinguishable from one that works
# until something downstream reads the other path. This now names the runner's own path.
E=$M/evidence/opd/$LABEL
SERVE=$M/evidence/eval/$LABEL-serve
[ -e "$E" ] && { echo "FATAL: $E exists"; exit 2; }
mkdir -p "$SERVE" || { echo "FATAL: cannot create $SERVE"; exit 1; }

log() { echo "[schema-eval $(date +%H:%M:%S)] $*"; }

# --- SPACE, ASSERTED AT THE DESTINATION AND SIZED FROM A REAL RUN ----------------------------
# 4452 -- the same 200-deal mirror-paired suite, the same models -- produced 12 GB of evidence.
# So one arm needs ~12 GB and the pair needs ~24 GB. /workspace1 is xfs with PROJECT QUOTAS:
# `df /workspace1` reports terabytes while this destination has its own 200 GB allocation, so
# the check must run HERE. A job that dies at 80% of a 6-hour suite because the quota filled is
# the worst version of the failure we already had at 2 MB.
NEED_GB=30
AVAIL_GB=$(( $(df --output=avail -k "$M" | tail -1 | tr -d ' ') / 1024 / 1024 ))
log "free at $M: ${AVAIL_GB} GB (need >= ${NEED_GB}; 4452 measured 12 GB for this suite)"
[ "$AVAIL_GB" -ge "$NEED_GB" ] || { log "FATAL: not enough room at the data root"; exit 1; }

# --- provenance -------------------------------------------------------------------------------
for f in src/magebench/pilot/decision_schema.py src/magebench/pilot/tool_name_guard.py; do
  d=$(git -C "$WT" hash-object "$WT/$f"); h=$(git -C "$WT" rev-parse "HEAD:$f" 2>/dev/null || echo "?")
  printf "blob %-46s %s %s\n" "$f" "${d:0:12}" "$([ "$d" = "$h" ] && echo MATCHES-HEAD || echo DIFFERS)"
done
log "harness $WT @ $(git -C "$WT" rev-parse --short HEAD) ($(git -C "$WT" rev-parse --abbrev-ref HEAD))"

export HARNESS_REPO=$WT MAVEN_REPO_LOCAL=/workspace1/users/ouhenio/m2-schema
FLOOR=$("$HARNESS_REPO/.venv/bin/python" -c \
  "from magebench.pilot.context_segments import SERVE_MIN_MODEL_LEN as m; print(m)" 2>&1) \
  || { log "FATAL: HARNESS_REPO cannot read the harness floor: $FLOOR"; exit 1; }
log "harness floor $FLOOR"

# THE JARS THE GAME JVMs RESOLVE FROM, ASSERTED BEFORE A GPU IS SPENT ON THEM.
# Job 9494 ran twelve games at 240 s each and banked ZERO: every XMage server died on
# "Could not find artifact org.mage:mage:jar:1.4.58 ... in $MAVEN_REPO_LOCAL". The repo
# existed -- 51 MB of third-party deps -- and held NO org.mage module at all, because this
# HARNESS_REPO is a commit-only submodule worktree, which by this repo's own rule "has no
# JDK, Maven, Xvfb, venv or built jars". The controls never caught it because they replay
# recorded frames against a served model and start no engine; this is the first job on the
# branch that launches games.
#
# A DIRECTORY THAT EXISTS IS NOT A REPOSITORY THAT RESOLVES. The failure is also reported
# per game, 240 s at a time, so without this check the job spends its whole allocation
# rediscovering one missing build.
MAGE_ARTIFACTS=$(ls "$MAVEN_REPO_LOCAL/org/mage" 2>/dev/null | wc -l)
log "maven repo $MAVEN_REPO_LOCAL: $MAGE_ARTIFACTS org.mage artifacts"
[ "$MAGE_ARTIFACTS" -ge 38 ] || {
  log "FATAL: $MAVEN_REPO_LOCAL holds $MAGE_ARTIFACTS org.mage artifacts, expected >= 38."
  log "  Build the tree first:  sbatch $SRC/pipelines/eval/prepare_harness_lascar.sbatch \\"
  log "                             $WT $MAVEN_REPO_LOCAL <expect_fail> <required tests>"
  exit 1
}

# --- the one variable, and everything held constant around it ---------------------------------
export MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY=1
export MAGEBENCH_DECK_BLOCK=on
export MAGEBENCH_CONTEXT_LIMIT=143360
export MAGEBENCH_MULLIGAN=engine-rule
export MAGEBENCH_GAME_TIMEOUT=3600
unset MAGEBENCH_INJECT_CONTENT
# BOTH ARMS CARRY THE NAME GUARD. It is deployed, it is not what this eval tests, and holding it
# constant is what makes the enum the single variable.
export MAGEBENCH_TOOL_NAME_GUARD=structural_tag

# THE PRESET, SET EXPLICITLY, BECAUSE THE RUNNER HAS A SILENT DEFAULT AND THE SUITE NO
# LONGER OVERRIDES IT. `run_suite_slots.sh:148` reads
# `MTG_PILOT_PRESET="${MTG_PILOT_PRESET:-q35-4b-local-strategy}"`, and 4452 never set it
# because its `players` key named the preset per seat. Stripping `players` -- which this
# job must do, since 4452 was a head-to-head and this is one model against the engine --
# also removed the only thing that named `q35-4b-ckpt265-strategy`, so job 9494 fell back
# to the BASE 4B preset. The two presets differ in exactly one field, the model, so every
# line the log prints about them is identical; the fall-back is invisible by construction.
# karn-research hit the same fall-back on the budget curve, from the same `players` strip.
export MTG_PILOT_PRESET=q35-4b-ckpt265-strategy
if [ "$ARM" = guarded ]; then
  export MAGEBENCH_DECISION_SCHEMA=bound_choice
else
  unset MAGEBENCH_DECISION_SCHEMA
fi
log "arm=$ARM  MAGEBENCH_DECISION_SCHEMA=${MAGEBENCH_DECISION_SCHEMA:-<unset>}  guard=structural_tag"

# --- 4452's seeds, sliced from 4452's own suite ------------------------------------------------
SUITE_IN=$M/evidence/eval/v3-h2h-4452-serve/suite.json
[ -f "$SUITE_IN" ] || { log "FATAL: 4452 suite unreadable at $SUITE_IN"; exit 1; }
SUITE=$SERVE/suite.json
$PY - "$SUITE_IN" "$SUITE" <<'PYEOF'
import json, sys
s = json.load(open(sys.argv[1]))
assert s["n_games"] == 200, f"expected 4452's 200 games, got {s['n_games']}"
lo, hi = s["games"][0]["seed"], s["games"][-1]["seed"]
# STRIP THE SEATS. 4452 was a HEAD-TO-HEAD, so every game names two policy seats and the second
# is `provider: local_b`. run_suite_slots.sh uses a suite's `players` verbatim when present --
# deliberately, so a suite that names its seats owns them -- and this eval serves ONE model
# against the engine, so `local_b` resolves to nothing: job 9396 died in 2.5 minutes with
# `AssertionError: Unknown player provider 'local_b'` on all 36 games it launched.
#
# "4452's suite unmodified" was my phrase and it was wrong: what this eval reuses is 4452's
# SEEDS AND DECKS, not its seating. Removing `players` makes the runner fall back to
# pilot@local vs the XMage AI, which is the comparison registered.
stripped = 0
for g in s["games"]:
    if g.pop("players", None) is not None:
        stripped += 1
print(f"stripped h2h seats from {stripped} games -> pilot@local vs XMage AI")
assert stripped == s["n_games"], f"expected to strip all {s['n_games']}, stripped {stripped}"
s["derived_from"] = ("v3-h2h-4452 suite: seeds and decks reused, h2h seats REMOVED so the "
                     "runner seats ckpt-265 against the engine")
json.dump(s, open(sys.argv[2], "w"), indent=1)
print(f"suite: {s['n_games']} games, seeds {lo}-{hi}, mirror={s.get('mirror')}")
PYEOF
[ $? -eq 0 ] || { log "FATAL: suite slice"; exit 1; }

# --- serve ckpt-265 -----------------------------------------------------------------------------
. "$SRC/pipelines/resolve_venv.sh"
SERVE_VENV=$(resolve_vllm_venv) || exit 3
MODEL=$MTG/models/mtg-v3-sft-checkpoint-265
SERVED_NAME=mtg-ckpt-265
PORT=$(( 8400 + SLURM_JOB_ID % 200 ))

# PRESET.MODEL == SERVED NAME, ASSERTED BEFORE A GPU IS SPENT.
# The preset names the model string the pilot puts in every request; the serve names what
# the server will answer to. NOTHING JOINS THEM. When they disagree the job still starts,
# still serves, still launches games, and the disagreement surfaces only as a 404 per LLM
# call (job 9396) -- or, if the wrong name happens to resolve, as 200 games of a model
# nobody registered. A subject is not the subject because the serve log names it.
PRESET_MODEL=$("$WT/.venv/bin/python" "$WT/pipelines/preset_model.py" \
                     "$WT" "$MTG_PILOT_PRESET" 2>&1) \
  || { log "FATAL: $PRESET_MODEL"; exit 1; }
log "preset $MTG_PILOT_PRESET -> model '$PRESET_MODEL'; this job serves '$SERVED_NAME'"
[ "$PRESET_MODEL" = "$SERVED_NAME" ] || {
  log "FATAL: the preset asks for '$PRESET_MODEL' and this job serves '$SERVED_NAME'."
  log "  Those must be the same string or every LLM call 404s. The registered subject is"
  log "  ckpt-265; job 9494 ran ten minutes with the base-4B preset on exactly this gap."
  exit 1
}
"$SERVE_VENV/bin/python" "$SRC/pipelines/rlvr/check_servable.py" "$MODEL" 2>&1 \
  | tee "$SERVE/check_servable.txt" || { log "FATAL: check_servable"; exit 3; }
MTG_SERVED_NAME=$SERVED_NAME OPD_LOG_DIR=$SERVE \
  bash "$SRC/pipelines/opd/serve_student.sh" slurm "$PORT" "$MODEL" \
  || { log "FATAL: serve"; exit 1; }
log "ckpt-265 up on $PORT"; cat "$SERVE"/served_*.id 2>/dev/null | sed 's/^/    served: /'

CONC=12
export MAGEBENCH_DISP_WIDTH=$((CONC * 4))    # 4x, not 2x -- 4056 lost 64 games to lapping

# THE FIFTH ARGUMENT IS THE DISPLAY BASE, NOT THE WIDTH. `run_suite_slots.sh` reads
# `DISP0="${5:-90}"` and takes the width from MAGEBENCH_DISP_WIDTH in the environment.
# Job 9495 passed the WIDTH (48) in the base's position, so both arms allocated :48-:95
# identically; 9494 started a second earlier and took :48-:59, and 9495 died in 3 m 48 s
# printing eleven "FATAL: display :NN is live".
#
# The port was already derived per job and the display was not -- and a shared resource
# derived on one axis only is not derived. Both are now functions of the job id, which is
# also what makes a running arm killable by identity rather than by name.
DISP0=$(( 1000 + (SLURM_JOB_ID % 20) * 60 ))   # stride 60 > width 48, so adjacent job ids
                                               # cannot overlap; >= 1000 clears :100-:275
                                               # (the budget curve's five arms), :200-:455
                                               # and :500-:671 (corpus blocks).
log "displays :$DISP0-:$((DISP0 + MAGEBENCH_DISP_WIDTH - 1))  port $PORT"
MTG_DATA_ROOT=$M \
  bash "$SRC/pipelines/opd/run_suite_slots.sh" "$SUITE" "$LABEL" "$PORT" "$CONC" "$DISP0"
rc=$?
log "runner returned $rc -- the wrapper's code is NOT the verdict; read the artifacts"

log "flag gate"; bash "$SRC/pipelines/synth/watch_autoresolve.sh" "$E" 2>&1 | sed 's/^/    /'
log "free at $M after the run: $(df -h --output=avail "$M" | tail -1 | tr -d ' ')"
log "schema eval end -- arm=$ARM evidence $E"
PGID=$(cat "$SERVE/serve_$PORT.pgid" 2>/dev/null)
[ -n "$PGID" ] && kill -- -"$PGID" 2>/dev/null
exit $rc
