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
PORT=$(( 8400 + SLURM_JOB_ID % 200 ))
"$SERVE_VENV/bin/python" "$SRC/pipelines/rlvr/check_servable.py" "$MODEL" 2>&1 \
  | tee "$SERVE/check_servable.txt" || { log "FATAL: check_servable"; exit 3; }
MTG_SERVED_NAME=mtg-ckpt-265 OPD_LOG_DIR=$SERVE \
  bash "$SRC/pipelines/opd/serve_student.sh" slurm "$PORT" "$MODEL" \
  || { log "FATAL: serve"; exit 1; }
log "ckpt-265 up on $PORT"; cat "$SERVE"/served_*.id 2>/dev/null | sed 's/^/    served: /'

CONC=12
export MAGEBENCH_DISP_WIDTH=$((CONC * 4))    # 4x, not 2x -- 4056 lost 64 games to lapping
MTG_DATA_ROOT=$M \
  bash "$SRC/pipelines/opd/run_suite_slots.sh" "$SUITE" "$LABEL" "$PORT" "$CONC" "$MAGEBENCH_DISP_WIDTH"
rc=$?
log "runner returned $rc -- the wrapper's code is NOT the verdict; read the artifacts"

log "flag gate"; bash "$SRC/pipelines/synth/watch_autoresolve.sh" "$E" 2>&1 | sed 's/^/    /'
log "free at $M after the run: $(df -h --output=avail "$M" | tail -1 | tr -d ' ')"
log "schema eval end -- arm=$ARM evidence $E"
PGID=$(cat "$SERVE/serve_$PORT.pgid" 2>/dev/null)
[ -n "$PGID" ] && kill -- -"$PGID" 2>/dev/null
exit $rc
